"""
SQLite persistence layer via aiosqlite.

Schema
------
items       – one row per Internet Archive item (concert)
tracks      – one row per FLAC file within an item
sync_runs   – history of sync operations with summary statistics

All timestamps are stored as ISO-8601 strings in UTC.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from .logger import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Allowed column names (prevent column-name injection in dynamic UPDATE)
# ---------------------------------------------------------------------------
_ALLOWED_ITEM_COLS = frozenset({
    "title", "date", "artist", "venue", "description",
    "discovered_at", "processed_at", "status", "has_flac",
    "folder_name", "retry_count", "last_error", "raw_metadata", "collection",
})

_ALLOWED_TRACK_COLS = frozenset({
    "local_filename", "local_path", "title", "track_number", "format",
    "size", "md5", "sha1", "status", "downloaded_at", "retry_count",
    "last_error",
})

_ALLOWED_SYNCRUN_COLS = frozenset({
    "completed_at", "items_discovered", "items_new", "items_with_flac",
    "items_skipped", "items_completed", "tracks_downloaded",
    "tracks_failed", "status", "error_msg",
})

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------
_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS items (
    identifier      TEXT PRIMARY KEY,
    collection      TEXT NOT NULL DEFAULT '',
    title           TEXT,
    date            TEXT,
    artist          TEXT,
    venue           TEXT,
    description     TEXT,
    discovered_at   TEXT NOT NULL,
    processed_at    TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    has_flac        INTEGER NOT NULL DEFAULT 0,
    folder_name     TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    raw_metadata    TEXT
);

CREATE TABLE IF NOT EXISTS tracks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_identifier TEXT NOT NULL REFERENCES items(identifier),
    ia_filename     TEXT NOT NULL,
    local_filename  TEXT,
    local_path      TEXT,
    title           TEXT,
    track_number    INTEGER,
    format          TEXT,
    size            INTEGER,
    md5             TEXT,
    sha1            TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    downloaded_at   TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    UNIQUE(item_identifier, ia_filename)
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL UNIQUE,
    started_at          TEXT NOT NULL,
    completed_at        TEXT,
    items_discovered    INTEGER DEFAULT 0,
    items_new           INTEGER DEFAULT 0,
    items_with_flac     INTEGER DEFAULT 0,
    items_skipped       INTEGER DEFAULT 0,
    items_completed     INTEGER DEFAULT 0,
    tracks_downloaded   INTEGER DEFAULT 0,
    tracks_failed       INTEGER DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'running',
    error_msg           TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_status         ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_collection     ON items(collection);
CREATE INDEX IF NOT EXISTS idx_tracks_item          ON tracks(item_identifier);
CREATE INDEX IF NOT EXISTS idx_tracks_status        ON tracks(status);
CREATE INDEX IF NOT EXISTS idx_tracks_item_status   ON tracks(item_identifier, status);
"""

# ---------------------------------------------------------------------------
# Migrations applied to existing databases on startup
# ---------------------------------------------------------------------------
_MIGRATIONS = [
    # v0.1.x → v0.2.x: add collection column to items
    "ALTER TABLE items ADD COLUMN collection TEXT NOT NULL DEFAULT ''",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_columns(cols: set[str], allowed: frozenset[str], context: str) -> None:
    bad = cols - allowed
    if bad:
        raise ValueError(f"Disallowed column(s) in {context}: {bad}")


# ---------------------------------------------------------------------------
# Database class
# ---------------------------------------------------------------------------
class Database:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        conn = await aiosqlite.connect(str(self._path))
        try:
            conn.row_factory = aiosqlite.Row
            await conn.executescript(_SCHEMA)
            await conn.commit()
            await self._run_migrations(conn)
        except Exception:
            await conn.close()
            raise
        self._conn = conn
        log.info("database.connected", path=str(self._path))

    async def _run_migrations(self, conn: aiosqlite.Connection) -> None:
        for sql in _MIGRATIONS:
            try:
                await conn.execute(sql)
                await conn.commit()
                log.info("database.migration_applied", sql=sql[:60])
            except Exception:
                # Column already exists — expected on all runs after the first
                pass

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "Database":
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    def _conn_or_raise(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected — call connect() first")
        return self._conn

    # ── items ────────────────────────────────────────────────────────────────

    async def upsert_item(
        self,
        *,
        identifier: str,
        collection: str = "",
        title: Optional[str] = None,
        date: Optional[str] = None,
        artist: Optional[str] = None,
        venue: Optional[str] = None,
        description: Optional[str] = None,
        raw_metadata: Optional[dict] = None,
    ) -> bool:
        """Insert item if not present. Returns True if it was new."""
        conn = self._conn_or_raise()
        async with self._write_lock:
            cur = await conn.execute(
                "SELECT 1 FROM items WHERE identifier = ?", (identifier,)
            )
            exists = await cur.fetchone() is not None
            if not exists:
                await conn.execute(
                    """
                    INSERT INTO items
                        (identifier, collection, title, date, artist, venue,
                         description, discovered_at, raw_metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier, collection, title, date, artist, venue,
                        description, _now(),
                        json.dumps(raw_metadata) if raw_metadata else None,
                    ),
                )
                await conn.commit()
            return not exists

    async def get_item(self, identifier: str) -> Optional[dict]:
        conn = self._conn_or_raise()
        cur = await conn.execute(
            "SELECT * FROM items WHERE identifier = ?", (identifier,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def update_item(self, identifier: str, **kwargs: Any) -> None:
        if not kwargs:
            return
        _validate_columns(set(kwargs.keys()), _ALLOWED_ITEM_COLS, "items")
        conn = self._conn_or_raise()
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        async with self._write_lock:
            await conn.execute(
                f"UPDATE items SET {sets} WHERE identifier = ?",
                [*kwargs.values(), identifier],
            )
            await conn.commit()

    async def mark_item_status(
        self,
        identifier: str,
        status: str,
        *,
        error: Optional[str] = None,
        folder_name: Optional[str] = None,
        has_flac: Optional[bool] = None,
    ) -> None:
        kwargs: dict[str, Any] = {"status": status}
        if error is not None:
            kwargs["last_error"] = error[:2000]
        if folder_name is not None:
            kwargs["folder_name"] = folder_name
        if has_flac is not None:
            kwargs["has_flac"] = int(has_flac)
        if status in ("complete", "no_flac", "failed"):
            kwargs["processed_at"] = _now()
        if status == "failed":
            conn = self._conn_or_raise()
            _validate_columns(set(kwargs.keys()), _ALLOWED_ITEM_COLS, "items")
            sets = ", ".join(f"{k} = ?" for k in kwargs)
            async with self._write_lock:
                await conn.execute(
                    f"UPDATE items SET {sets}, retry_count = retry_count + 1 "
                    f"WHERE identifier = ?",
                    [*kwargs.values(), identifier],
                )
                await conn.commit()
            return
        await self.update_item(identifier, **kwargs)

    async def get_items_by_status(self, *statuses: str) -> list[dict]:
        conn = self._conn_or_raise()
        placeholders = ",".join("?" * len(statuses))
        cur = await conn.execute(
            f"SELECT * FROM items WHERE status IN ({placeholders})",
            list(statuses),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def get_items_for_retry(self, max_retries: int) -> list[dict]:
        conn = self._conn_or_raise()
        cur = await conn.execute(
            "SELECT * FROM items WHERE status = 'failed' AND retry_count < ?",
            (max_retries,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def count_items_by_status(self) -> dict[str, int]:
        conn = self._conn_or_raise()
        cur = await conn.execute(
            "SELECT status, COUNT(*) as n FROM items GROUP BY status"
        )
        return {r["status"]: r["n"] for r in await cur.fetchall()}

    async def get_items_paginated(
        self,
        *,
        status: Optional[str] = None,
        collection: Optional[str] = None,
        search: Optional[str] = None,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[dict], int]:
        """Return (items, total_count) with optional filters."""
        conn = self._conn_or_raise()
        conditions: list[str] = []
        params: list[Any] = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if collection:
            conditions.append("collection = ?")
            params.append(collection)
        if search:
            conditions.append(
                "(title LIKE ? OR artist LIKE ? OR venue LIKE ? OR identifier LIKE ?)"
            )
            term = f"%{search}%"
            params.extend([term, term, term, term])
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cur = await conn.execute(
            f"SELECT COUNT(*) as n FROM items {where}", params
        )
        total = (await cur.fetchone())["n"]
        offset = (page - 1) * per_page
        cur = await conn.execute(
            f"SELECT * FROM items {where} ORDER BY date DESC, identifier "
            f"LIMIT ? OFFSET ?",
            params + [per_page, offset],
        )
        return [dict(r) for r in await cur.fetchall()], total

    # ── tracks ───────────────────────────────────────────────────────────────

    async def upsert_track(
        self,
        *,
        item_identifier: str,
        ia_filename: str,
        title: Optional[str] = None,
        track_number: Optional[int] = None,
        format: Optional[str] = None,
        size: Optional[int] = None,
        md5: Optional[str] = None,
        sha1: Optional[str] = None,
    ) -> None:
        conn = self._conn_or_raise()
        async with self._write_lock:
            await conn.execute(
                """
                INSERT INTO tracks
                    (item_identifier, ia_filename, title, track_number,
                     format, size, md5, sha1)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_identifier, ia_filename) DO UPDATE SET
                    title        = excluded.title,
                    track_number = excluded.track_number,
                    format       = excluded.format,
                    size         = excluded.size,
                    md5          = excluded.md5,
                    sha1         = excluded.sha1
                """,
                (item_identifier, ia_filename, title, track_number,
                 format, size, md5, sha1),
            )
            await conn.commit()

    async def update_track(
        self, item_identifier: str, ia_filename: str, **kwargs: Any
    ) -> None:
        if not kwargs:
            return
        _validate_columns(set(kwargs.keys()), _ALLOWED_TRACK_COLS, "tracks")
        conn = self._conn_or_raise()
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        async with self._write_lock:
            await conn.execute(
                f"UPDATE tracks SET {sets} "
                f"WHERE item_identifier = ? AND ia_filename = ?",
                [*kwargs.values(), item_identifier, ia_filename],
            )
            await conn.commit()

    async def mark_track_complete(
        self,
        item_identifier: str,
        ia_filename: str,
        *,
        local_filename: str,
        local_path: str,
    ) -> None:
        await self.update_track(
            item_identifier, ia_filename,
            status="complete",
            local_filename=local_filename,
            local_path=local_path,
            downloaded_at=_now(),
        )

    async def mark_track_failed(
        self, item_identifier: str, ia_filename: str, error: str
    ) -> None:
        conn = self._conn_or_raise()
        async with self._write_lock:
            await conn.execute(
                """
                UPDATE tracks
                SET status = 'failed', last_error = ?, retry_count = retry_count + 1
                WHERE item_identifier = ? AND ia_filename = ?
                """,
                (error[:2000], item_identifier, ia_filename),
            )
            await conn.commit()

    async def get_tracks_for_item(self, item_identifier: str) -> list[dict]:
        conn = self._conn_or_raise()
        cur = await conn.execute(
            "SELECT * FROM tracks WHERE item_identifier = ? "
            "ORDER BY track_number, ia_filename",
            (item_identifier,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def count_tracks(self) -> dict[str, int]:
        conn = self._conn_or_raise()
        cur = await conn.execute(
            "SELECT status, COUNT(*) as n FROM tracks GROUP BY status"
        )
        return {r["status"]: r["n"] for r in await cur.fetchall()}

    # ── sync_runs ────────────────────────────────────────────────────────────

    async def start_sync_run(self, run_id: str) -> None:
        conn = self._conn_or_raise()
        async with self._write_lock:
            await conn.execute(
                "INSERT INTO sync_runs (run_id, started_at) VALUES (?, ?)",
                (run_id, _now()),
            )
            await conn.commit()

    async def update_sync_run(self, run_id: str, **kwargs: Any) -> None:
        if not kwargs:
            return
        _validate_columns(set(kwargs.keys()), _ALLOWED_SYNCRUN_COLS, "sync_runs")
        conn = self._conn_or_raise()
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        async with self._write_lock:
            await conn.execute(
                f"UPDATE sync_runs SET {sets} WHERE run_id = ?",
                [*kwargs.values(), run_id],
            )
            await conn.commit()

    async def finish_sync_run(self, run_id: str, *, status: str, **stats: Any) -> None:
        await self.update_sync_run(
            run_id, status=status, completed_at=_now(), **stats
        )

    async def get_recent_syncs(self, limit: int = 10) -> list[dict]:
        conn = self._conn_or_raise()
        cur = await conn.execute(
            "SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in await cur.fetchall()]

    async def get_last_sync(self) -> Optional[dict]:
        syncs = await self.get_recent_syncs(limit=1)
        return syncs[0] if syncs else None
