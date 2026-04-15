"""
SyncManager – the main orchestrator.

Two separate phases, each independently callable:

  run_discovery()  – Crawl IA (slow, network-bound).  Upserts every item
                     found into the DB with status=pending.  Safe to re-run:
                     existing items are untouched.

  run_downloads()  – Work off the DB.  Downloads every pending/retryable item.
                     No IA crawling.  Safe to call after a container restart:
                     picks up exactly where the last run left off.

  run_sync()       – Convenience wrapper: discovery followed by downloads.
                     Used for the initial one-shot run when the DB is empty.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from .config import Config
from .database import Database
from .discovery import Discoverer
from .downloader import DownloadResult, Downloader, write_checksum_manifest
from .file_naming import (
    build_album_tag,
    deduplicate_filenames,
    make_folder_name,
    make_track_filename,
)
from .logger import get_logger
from .metadata import ConcertInfo, MetadataFetcher
from .tagger import tag_flac

log = get_logger(__name__)

_UA = (
    "archive-mirror/1.0 "
    "(https://github.com/sdblepas/archive-mirror; respectful bot)"
)


def _make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": _UA},
        follow_redirects=True,
        limits=httpx.Limits(
            max_keepalive_connections=10,
            max_connections=20,
            keepalive_expiry=30,
        ),
    )


class SyncManager:
    def __init__(self, config: Config, db: Database) -> None:
        self._cfg = config
        self._db = db

    # ── Discovery ─────────────────────────────────────────────────────────────

    async def run_discovery(self) -> dict:
        """Crawl all configured IA collections → upsert into DB.

        Only inserts new items (status=pending).  Items already in the DB are
        left completely untouched so in-progress downloads are not disrupted.

        Returns {"items_discovered": N, "items_new": N}.
        """
        stats: dict = {"items_discovered": 0, "items_new": 0}

        log.info("discovery.start", collections=self._cfg.collections)

        async with _make_client() as client:
            discoverer = Discoverer(self._cfg, client)

            for collection in self._cfg.collections:
                log.info("discovery.collection_start", collection=collection)
                async for item_stub in discoverer.iter_items(collection):
                    identifier = item_stub.get("identifier", "")
                    if not identifier:
                        continue
                    stats["items_discovered"] += 1
                    is_new = await self._db.upsert_item(
                        identifier=identifier,
                        collection=collection,
                        title=item_stub.get("title"),
                        date=item_stub.get("date"),
                        artist=item_stub.get("creator"),
                    )
                    if is_new:
                        stats["items_new"] += 1

        log.info(
            "discovery.complete",
            discovered=stats["items_discovered"],
            new=stats["items_new"],
        )
        return stats

    # ── Downloads ─────────────────────────────────────────────────────────────

    async def run_downloads(self) -> dict:
        """Download all pending/retryable items from the DB.

        Does NOT touch IA discovery.  Creates a sync_run record in the DB so
        the history table stays accurate.

        Returns full stats dict.
        """
        run_id = str(uuid.uuid4())
        await self._db.start_sync_run(run_id)

        stats: dict = {
            "run_id": run_id,
            "items_discovered": 0,
            "items_new": 0,
            "items_with_flac": 0,
            "items_skipped": 0,
            "items_completed": 0,
            "tracks_downloaded": 0,
            "tracks_failed": 0,
        }

        log.info("downloads.start", run_id=run_id, dry_run=self._cfg.dry_run)

        # ── Build work list ───────────────────────────────────────────────
        pending = await self._db.get_items_by_status("pending")
        retryable = await self._db.get_items_for_retry(self._cfg.retry_count)

        seen: set[str] = set()
        work_list: list[dict] = []
        for item in pending + retryable:
            if item["identifier"] not in seen:
                seen.add(item["identifier"])
                work_list.append(item)

        status_counts = await self._db.count_items_by_status()
        stats["items_skipped"] = (
            status_counts.get("complete", 0) + status_counts.get("no_flac", 0)
        )

        log.info(
            "downloads.work_list_ready",
            to_process=len(work_list),
            already_done=stats["items_skipped"],
        )

        if not work_list:
            log.info("downloads.nothing_to_do")
            await self._db.finish_sync_run(run_id, status="complete", **stats)
            return stats

        if self._cfg.dry_run:
            log.info(
                "downloads.dry_run_mode",
                would_process=len(work_list),
                already_complete=stats["items_skipped"],
            )
            for item in work_list:
                log.info(
                    "dry_run.would_process",
                    identifier=item["identifier"],
                    title=item.get("title"),
                    status=item.get("status"),
                )
            await self._db.finish_sync_run(run_id, status="complete", **stats)
            return stats

        # ── Process items concurrently ────────────────────────────────────
        sem = asyncio.Semaphore(self._cfg.max_workers)
        lock = asyncio.Lock()

        async with _make_client() as client:
            metadata_fetcher = MetadataFetcher(self._cfg, client)
            downloader = Downloader(self._cfg, client)

            async def process_one(item: dict) -> None:
                async with sem:
                    s = await self._process_item(item, metadata_fetcher, downloader)
                async with lock:
                    stats["items_with_flac"] += s.get("has_flac", 0)
                    stats["items_completed"] += s.get("completed", 0)
                    stats["tracks_downloaded"] += s.get("tracks_downloaded", 0)
                    stats["tracks_failed"] += s.get("tracks_failed", 0)

            await asyncio.gather(*(process_one(item) for item in work_list))

            if self._cfg.webhook_url:
                await self._post_webhook(client, stats)

        log.info("downloads.complete", **stats)
        await self._db.finish_sync_run(run_id, status="complete", **stats)
        return stats

    # ── Full cycle (discovery + downloads) ────────────────────────────────────

    async def run_sync(self) -> dict:
        """Convenience: run_discovery() then run_downloads().

        Use this for first-time setup (empty DB) or one-shot mode
        (SYNC_INTERVAL=0).
        """
        disc = await self.run_discovery()
        dl = await self.run_downloads()
        # Merge: preserve run_id from downloads; surface discovery counts.
        return {**dl, "items_discovered": disc["items_discovered"], "items_new": disc["items_new"]}

    # ── Per-item processing ───────────────────────────────────────────────────

    async def _process_item(
        self,
        item: dict,
        metadata_fetcher: MetadataFetcher,
        downloader: Downloader,
    ) -> dict:
        identifier = item["identifier"]
        result = {
            "has_flac": 0,
            "completed": 0,
            "tracks_downloaded": 0,
            "tracks_failed": 0,
        }

        await self._db.mark_item_status(identifier, "downloading")

        # ── Fetch IA metadata ─────────────────────────────────────────────
        try:
            concert = await metadata_fetcher.fetch(identifier)
        except Exception:
            log.exception("sync.metadata_error", identifier=identifier)
            await self._db.mark_item_status(
                identifier, "failed", error="metadata fetch failed"
            )
            result["tracks_failed"] += 1
            return result

        if concert is None:
            await self._db.mark_item_status(
                identifier, "failed", error="item not found (404)"
            )
            return result

        raw_json = json.dumps(concert.raw)
        if len(raw_json) > 500_000:  # 500 KB safety cap — discard rather than truncate
            log.warning(
                "sync.raw_metadata_too_large",
                identifier=identifier,
                size=len(raw_json),
            )
            raw_json = "{}"
        await self._db.update_item(
            identifier,
            title=concert.title,
            artist=concert.artist,
            date=concert.date,
            venue=concert.venue,
            description=concert.description,
            raw_metadata=raw_json,
        )

        if not concert.flac_tracks:
            log.info("sync.no_flac", identifier=identifier, title=concert.title)
            await self._db.mark_item_status(identifier, "no_flac", has_flac=False)
            return result

        result["has_flac"] = 1
        log.info(
            "sync.processing",
            identifier=identifier,
            title=concert.title,
            date=concert.date,
            artist=concert.artist,
            venue=concert.venue,
            tracks=len(concert.flac_tracks),
        )

        for track in concert.flac_tracks:
            await self._db.upsert_track(
                item_identifier=identifier,
                ia_filename=track.ia_filename,
                title=track.title,
                track_number=track.track_number,
                format=track.format,
                size=track.size,
                md5=track.md5,
                sha1=track.sha1,
            )

        folder_name = make_folder_name(concert.artist, concert.date)
        dest_dir = self._cfg.output_dir / folder_name
        dest_dir.mkdir(parents=True, exist_ok=True)

        total = len(concert.flac_tracks)
        raw_names = [
            make_track_filename(
                t.track_number,  # type: ignore[arg-type]
                t.title or "untitled",
                t.artist or concert.artist,
                total_tracks=total,
            )
            for t in concert.flac_tracks
        ]
        dedup_names = deduplicate_filenames(raw_names)
        album_tag = build_album_tag(concert.artist, concert.date, concert.venue)

        checksum_entries: list[tuple[str, str]] = []
        tracks_ok = 0
        tracks_fail = 0

        for track, local_filename in zip(concert.flac_tracks, dedup_names):
            outcome = await downloader.download_track(
                identifier=identifier,
                ia_filename=track.ia_filename,
                dest_dir=dest_dir,
                local_filename=local_filename,
                expected_size=track.size,
                expected_md5=track.md5,
                expected_sha1=track.sha1,
            )

            if outcome.result in (
                DownloadResult.DOWNLOADED,
                DownloadResult.SKIPPED_EXISTING,
            ):
                tracks_ok += 1
                relative = str(
                    (dest_dir / local_filename).relative_to(self._cfg.output_dir)
                )
                await self._db.mark_track_complete(
                    identifier,
                    track.ia_filename,
                    local_filename=local_filename,
                    local_path=relative,
                )

                final_path = dest_dir / local_filename
                tagged = _tag_safe(
                    final_path,
                    title=track.title or "untitled",
                    artist=track.artist or concert.artist,
                    album=album_tag,
                    date=concert.date,
                    venue=concert.venue,
                    track_number=track.track_number or 1,
                    total_tracks=total,
                    identifier=identifier,
                )
                if not tagged:
                    log.warning(
                        "sync.tag_skipped",
                        identifier=identifier,
                        filename=local_filename,
                    )

                if self._cfg.write_checksum_manifest and track.md5:
                    checksum_entries.append((local_filename, track.md5))

            else:
                tracks_fail += 1
                await self._db.mark_track_failed(
                    identifier,
                    track.ia_filename,
                    outcome.error or "unknown error",
                )
                log.warning(
                    "sync.track_failed",
                    identifier=identifier,
                    filename=local_filename,
                    error=outcome.error,
                )

        if self._cfg.write_checksum_manifest and checksum_entries:
            await write_checksum_manifest(dest_dir, checksum_entries)

        if tracks_fail == 0:
            await self._db.mark_item_status(
                identifier, "complete", has_flac=True, folder_name=folder_name
            )
            result["completed"] = 1
            log.info(
                "sync.item_complete",
                identifier=identifier,
                folder=folder_name,
                tracks=tracks_ok,
            )
        else:
            await self._db.mark_item_status(
                identifier,
                "failed",
                has_flac=True,
                folder_name=folder_name,
                error=f"{tracks_fail} track(s) failed",
            )
            log.warning(
                "sync.item_partial",
                identifier=identifier,
                ok=tracks_ok,
                failed=tracks_fail,
            )

        result["tracks_downloaded"] = tracks_ok
        result["tracks_failed"] = tracks_fail
        return result

    # ── Webhook ───────────────────────────────────────────────────────────────

    async def _post_webhook(self, client: httpx.AsyncClient, stats: dict) -> None:
        """POST stats to webhook with up to 3 attempts and exponential backoff."""
        for attempt in range(1, 4):
            try:
                resp = await client.post(
                    self._cfg.webhook_url, json=stats, timeout=15
                )
                resp.raise_for_status()
                log.info("webhook.sent", url=self._cfg.webhook_url, attempt=attempt)
                return
            except Exception as exc:
                if attempt < 3:
                    wait = 2 ** attempt  # 2 s, 4 s
                    log.warning(
                        "webhook.retry",
                        attempt=attempt,
                        wait_seconds=wait,
                        error=str(exc),
                    )
                    await asyncio.sleep(wait)
        log.error("webhook.failed_all_attempts", url=self._cfg.webhook_url)


# ---------------------------------------------------------------------------
# Tag helper (non-raising, returns bool)
# ---------------------------------------------------------------------------

def _tag_safe(
    path: Path,
    *,
    title: str,
    artist: str,
    album: str,
    date: str,
    venue: Optional[str],
    track_number: int,
    total_tracks: int,
    identifier: str,
) -> bool:
    try:
        return tag_flac(
            path,
            title=title,
            artist=artist,
            album=album,
            date=date,
            venue=venue,
            track_number=track_number,
            total_tracks=total_tracks,
            identifier=identifier,
        )
    except Exception:
        log.exception("sync.tag_error", path=str(path))
        return False
