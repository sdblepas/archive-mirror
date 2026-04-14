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
# Schema DDL
# ---------------------------------------------------------------------------
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS items (
    identifier      TEXT PRIMARY KEY,
    title           TEXT,
    date            TEXT,           -- e.g. "1990-11-09"
    artist          TEXT,
    venue           TEXT,
    description     TEXT,
    discovered_at   TEXT NOT NULL,
    processed_at    TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    -- pending | no_flac | downloading | complete | failed
    has_flac        INTEGER NOT NULL DEFAULT 0,
    folder_name     TEXT,           -- actual folder on disk
    retry_count     INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    raw_metadata    TEXT            -- full JSON blob from IA
);

CREATE TABLE IF NOT EXISTS tracks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_identifier TEXT NOT NULL REFERENCES items(identifier),
    ia_filename     TEXT NOT NULL,  -- remote filename on IA
    local_filename  TEXT,           -- sanitised filename on disk
    local_path      TEXT,           -- path relative to output_dir
    title           TEXT,
    track_number    INTEGER,
    format          TEXT,           -- "Flac", "VBR MP3", …
    size            INTEGER,        -- bytes (from IA metadata)
    md5             TEXT,
    sha1            TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    -- pending | complete | failed | skipped
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
    -- running | complete | failed | interrupted
    error_msg           TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_status         ON items(status);
CREATE INDEX IF NOT EXISTS idx_tracks_item          ON tracks(item_identifier);
CREATE INDEX IF NOT EXISTS idx_tracks_status        ON tracks(status);
CREATE INDEX IF NOT EXISTS idx_tracks_item_status   ON tracks(item_identifier, status);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Database class
# ---------------------------------------------------------------------------
class Database:
    """Async wrapper around a single aiosqlite connection.

    Open with ``async with Database(path) as db:``.
    All write methods are serialised through an asyncio.Lock so concurrent
    asyncio tasks can safely call them without WAL contention.
    """

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(str(self._path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        log.info("database.connected", path=str(self._path))

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "Database":
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected")
        return self._conn

    # ── items ────────────────────────────────────────────────────────────────

    async def upsert_item(
        self,
        *,
        identifier: str,
        title: Optional[str] = None,
        date: Optional[str] = None,
        artist: Optional[str] = None,
        venue: Optional[str] = None,
        description: Optional[str] = None,
        raw_metadata: Optional[dict] = None,
    ) -> bool:
        """Insert item if not present; returns True if it was new."""
        conn = self._require_conn()
        async with self._write_lock:
            cursor = await conn.execute(
                "SELECT 1 FROM items WHERE identifier = ?", (identifier,)
            )
            exists = await cursor.fetchone() is not None
            if not exists:
                await conn.execute(
                    """
                    INSERT INTO items
                        (identifier, title, date, artist, venue, description,
                         discovered_at, raw_metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        title,
                        date,
                        artist,
                        venue,
                        description,
                        _now(),
                        json.dumps(raw_metadata) if raw_metadata else None,
                    ),
                )
                await conn.commit()
            return not exists

    async def get_item(self, identifier: str) -> Optional[dict]:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT * FROM items WHERE identifier = ?", (identifier,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_item(self, identifier: str, **kwargs: Any) -> None:
        conn = self._require_conn()
        if not kwargs:
            return
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [identifier]
        async with self._write_lock:
            await conn.execute(
                f"UPDATE items SET {sets} WHERE identifier = ?", values
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
            kwargs["last_error"] = error[:2000]  # cap error length
        if folder_name is not None:
            kwargs["folder_name"] = folder_name
        if has_flac is not None:
            kwargs["has_flac"] = int(has_flac)
        if status in ("complete", "no_flac", "failed"):
            kwargs["processed_at"] = _now()
        if status == "failed":
            # Increment retry_count atomically
            conn = self._require_conn()
            async with self._write_lock:
                sets = ", ".join(f"{k} = ?" for k in kwargs)
                await conn.execute(
                    f"UPDATE items SET {sets}, retry_count = retry_count + 1 "
                    f"WHERE identifier = ?",
                    list(kwargs.values()) + [identifier],
                )
                await conn.commit()
            return
        await self.update_item(identifier, **kwargs)

    async def get_items_by_status(self, *statuses: str) -> list[dict]:
        conn = self._require_conn()
        placeholders = ",".join("?" * len(statuses))
        cursor = await conn.execute(
            f"SELECT * FROM items WHERE status IN ({placeholders})", list(statuses)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_items_for_retry(self, max_retries: int) -> list[dict]:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT * FROM items WHERE status = 'failed' AND retry_count < ?",
            (max_retries,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def count_items_by_status(self) -> dict[str, int]:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT status, COUNT(*) as n FROM items GROUP BY status"
        )
        rows = await cursor.fetchall()
        return {r["status"]: r["n"] for r in rows}

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
        conn = self._require_conn()
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
                (
                    item_identifier,
                    ia_filename,
                    title,
                    track_number,
                    format,
                    size,
                    md5,
                    sha1,
                ),
            )
            await conn.commit()

    async def update_track(
        self, item_identifier: str, ia_filename: str, **kwargs: Any
    ) -> None:
        conn = self._require_conn()
        if not kwargs:
            return
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [item_identifier, ia_filename]
        async with self._write_lock:
            await conn.execute(
                f"UPDATE tracks SET {sets} "
                f"WHERE item_identifier = ? AND ia_filename = ?",
                values,
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
            item_identifier,
            ia_filename,
            status="complete",
            local_filename=local_filename,
            local_path=local_path,
            downloaded_at=_now(),
        )

    async def mark_track_failed(
        self, item_identifier: str, ia_filename: str, error: str
    ) -> None:
        conn = self._require_conn()
        async with self._write_lock:
            await conn.execute(
                """
                UPDATE tracks
                SET status = 'failed',
                    last_error = ?,
                    retry_count = retry_count + 1
                WHERE item_identifier = ? AND ia_filename = ?
                """,
                (error[:2000], item_identifier, ia_filename),
            )
            await conn.commit()

    async def get_tracks_for_item(self, item_identifier: str) -> list[dict]:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT * FROM tracks WHERE item_identifier = ? ORDER BY track_number, ia_filename",
            (item_identifier,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def count_tracks(self) -> dict[str, int]:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT status, COUNT(*) as n FROM tracks GROUP BY status"
        )
        rows = await cursor.fetchall()
        return {r["status"]: r["n"] for r in rows}

    # ── sync_runs ────────────────────────────────────────────────────────────

    async def start_sync_run(self, run_id: str) -> None:
        conn = self._require_conn()
        async with self._write_lock:
            await conn.execute(
                "INSERT INTO sync_runs (run_id, started_at) VALUES (?, ?)",
                (run_id, _now()),
            )
            await conn.commit()

    async def update_sync_run(self, run_id: str, **kwargs: Any) -> None:
        conn = self._require_conn()
        if not kwargs:
            return
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [run_id]
        async with self._write_lock:
            await conn.execute(
                f"UPDATE sync_runs SET {sets} WHERE run_id = ?", values
            )
            await conn.commit()

    async def finish_sync_run(self, run_id: str, *, status: str, **stats: Any) -> None:
        await self.update_sync_run(
            run_id, status=status, completed_at=_now(), **stats
        )

    async def get_last_sync(self) -> Optional[dict]:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
