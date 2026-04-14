"""
SyncManager – the main orchestrator.

One full sync cycle:
  1. Discover all items in the IA collection (cursor-paginated scrape API).
  2. Upsert each discovered item into the DB (new → pending).
  3. Build a work list: pending + failed-with-retries-remaining.
  4. For each work item (concurrency limited):
       a. Fetch item metadata from IA.
       b. If no FLAC files → mark no_flac, skip.
       c. Create destination folder.
       d. Download each FLAC track (with resume & checksum).
       e. Write FLAC tags.
       f. Write checksum manifest (optional).
       g. Mark item complete in DB.
  5. Emit summary statistics.
  6. Optionally POST a webhook notification.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiofiles
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
from .health import HealthServer
from .logger import get_logger
from .metadata import ConcertInfo, MetadataFetcher

log = get_logger(__name__)


class SyncManager:
    def __init__(
        self,
        config: Config,
        db: Database,
        health: HealthServer,
    ) -> None:
        self._cfg = config
        self._db = db
        self._health = health

    # ── Public entry point ───────────────────────────────────────────────────

    async def run_sync(self) -> dict:
        """Run a full sync cycle.  Returns summary statistics."""
        run_id = str(uuid.uuid4())
        await self._db.start_sync_run(run_id)

        stats = {
            "run_id": run_id,
            "items_discovered": 0,
            "items_new": 0,
            "items_with_flac": 0,
            "items_skipped": 0,
            "items_completed": 0,
            "tracks_downloaded": 0,
            "tracks_failed": 0,
        }

        log.info("sync.start", run_id=run_id, dry_run=self._cfg.dry_run)
        self._health.set_healthy(sync_status="running", run_id=run_id)

        limits = httpx.Limits(
            max_keepalive_connections=10,
            max_connections=20,
            keepalive_expiry=30,
        )
        headers = {
            "User-Agent": (
                "archive-mirror/1.0 "
                "(https://github.com/your-org/archive-mirror; respectful bot)"
            )
        }

        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=True,
            limits=limits,
        ) as client:
            discoverer = Discoverer(self._cfg, client)
            metadata_fetcher = MetadataFetcher(self._cfg, client)
            downloader = Downloader(self._cfg, client)

            # ── Phase 1: Discovery ───────────────────────────────────────
            log.info("sync.discovery_start", collection=self._cfg.collection)
            async for item_stub in discoverer.iter_items():
                identifier = item_stub.get("identifier", "")
                if not identifier:
                    continue
                stats["items_discovered"] += 1

                is_new = await self._db.upsert_item(
                    identifier=identifier,
                    title=item_stub.get("title"),
                    date=item_stub.get("date"),
                    artist=item_stub.get("creator"),
                )
                if is_new:
                    stats["items_new"] += 1

            log.info(
                "sync.discovery_complete",
                discovered=stats["items_discovered"],
                new=stats["items_new"],
            )
            await self._db.update_sync_run(
                run_id,
                items_discovered=stats["items_discovered"],
                items_new=stats["items_new"],
            )

            # ── Phase 2: Build work list ─────────────────────────────────
            pending = await self._db.get_items_by_status("pending")
            retryable = await self._db.get_items_for_retry(self._cfg.retry_count)
            # Deduplicate (an item shouldn't be in both, but be safe)
            seen: set[str] = set()
            work_list: list[dict] = []
            for item in pending + retryable:
                if item["identifier"] not in seen:
                    seen.add(item["identifier"])
                    work_list.append(item)

            # Items already complete or no_flac are intentionally excluded
            skipped_count = (
                stats["items_discovered"]
                - len(work_list)
                - stats["items_new"]  # new ones ARE in work_list
                + len(pending)        # correct for new items counted above
            )
            # Simpler: just count complete + no_flac items
            status_counts = await self._db.count_items_by_status()
            stats["items_skipped"] = (
                status_counts.get("complete", 0) + status_counts.get("no_flac", 0)
            )

            log.info(
                "sync.work_list_ready",
                to_process=len(work_list),
                skipped=stats["items_skipped"],
            )

            if not work_list:
                log.info("sync.nothing_to_do")
                await self._db.finish_sync_run(run_id, status="complete", **stats)
                self._health.set_healthy(sync_status="idle")
                return stats

            if self._cfg.dry_run:
                log.info("sync.dry_run", would_process=len(work_list))
                for item in work_list:
                    log.info(
                        "dry_run.would_process",
                        identifier=item["identifier"],
                        title=item.get("title"),
                    )
                await self._db.finish_sync_run(run_id, status="complete", **stats)
                self._health.set_healthy(sync_status="idle")
                return stats

            # ── Phase 3: Process items concurrently ──────────────────────
            sem = asyncio.Semaphore(self._cfg.max_workers)

            async def process_one(item: dict) -> None:
                async with sem:
                    s = await self._process_item(
                        item, metadata_fetcher, downloader
                    )
                    stats["items_with_flac"] += s.get("has_flac", 0)
                    stats["items_completed"] += s.get("completed", 0)
                    stats["tracks_downloaded"] += s.get("tracks_downloaded", 0)
                    stats["tracks_failed"] += s.get("tracks_failed", 0)

            await asyncio.gather(*(process_one(item) for item in work_list))

        # ── Phase 4: Finalize ────────────────────────────────────────────
        log.info("sync.complete", **stats)
        await self._db.finish_sync_run(run_id, status="complete", **stats)
        self._health.set_healthy(
            sync_status="idle",
            last_sync=datetime.now(timezone.utc).isoformat(),
        )
        self._health.update_metrics(last_sync=stats)

        if self._cfg.webhook_url:
            await self._post_webhook(stats)

        return stats

    # ── Per-item processing ──────────────────────────────────────────────────

    async def _process_item(
        self,
        item: dict,
        metadata_fetcher: MetadataFetcher,
        downloader: Downloader,
    ) -> dict:
        identifier = item["identifier"]
        result = {"has_flac": 0, "completed": 0, "tracks_downloaded": 0, "tracks_failed": 0}

        # Mark as downloading so a crashed run shows the right state
        await self._db.mark_item_status(identifier, "downloading")

        # ── Fetch metadata ───────────────────────────────────────────────
        try:
            concert = await metadata_fetcher.fetch(identifier)
        except Exception as exc:
            log.error(
                "sync.metadata_error",
                identifier=identifier,
                error=str(exc),
            )
            await self._db.mark_item_status(
                identifier, "failed", error=f"metadata fetch failed: {exc}"
            )
            result["tracks_failed"] += 1
            return result

        if concert is None:
            await self._db.mark_item_status(
                identifier, "failed", error="item not found (404)"
            )
            return result

        # ── Update DB with rich metadata ─────────────────────────────────
        await self._db.update_item(
            identifier,
            title=concert.title,
            artist=concert.artist,
            date=concert.date,
            venue=concert.venue,
            description=concert.description,
            raw_metadata=json.dumps(concert.raw),
        )

        # ── Check for FLAC ───────────────────────────────────────────────
        if not concert.flac_tracks:
            log.info(
                "sync.no_flac",
                identifier=identifier,
                title=concert.title,
            )
            await self._db.mark_item_status(
                identifier, "no_flac", has_flac=False
            )
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

        # ── Upsert tracks into DB ────────────────────────────────────────
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

        # ── Build local filenames (deduplicated) ─────────────────────────
        folder_name = make_folder_name(concert.artist, concert.date)
        dest_dir = self._cfg.output_dir / folder_name
        dest_dir.mkdir(parents=True, exist_ok=True)

        total = len(concert.flac_tracks)
        raw_names = [
            make_track_filename(
                t.track_number,  # type: ignore[arg-type]  # filled by _fill_track_numbers
                t.title or "untitled",
                t.artist or concert.artist,
                total_tracks=total,
            )
            for t in concert.flac_tracks
        ]
        dedup_names = deduplicate_filenames(raw_names)

        album_tag = build_album_tag(concert.artist, concert.date, concert.venue)

        # ── Download each track ──────────────────────────────────────────
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

                # Tag the file (skip on SKIPPED_EXISTING only if tags are already OK)
                final_path = dest_dir / local_filename
                if final_path.exists():
                    _tag_safe(
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

        # ── Write checksum manifest ──────────────────────────────────────
        if self._cfg.write_checksum_manifest and checksum_entries:
            await write_checksum_manifest(dest_dir, checksum_entries)

        # ── Mark item status ─────────────────────────────────────────────
        if tracks_fail == 0:
            await self._db.mark_item_status(
                identifier,
                "complete",
                has_flac=True,
                folder_name=folder_name,
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

    # ── Webhook ──────────────────────────────────────────────────────────────

    async def _post_webhook(self, stats: dict) -> None:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                await client.post(self._cfg.webhook_url, json=stats)
            log.info("webhook.sent", url=self._cfg.webhook_url)
        except Exception as exc:
            log.warning("webhook.failed", error=str(exc))


# ---------------------------------------------------------------------------
# Tagging helper (non-raising)
# ---------------------------------------------------------------------------

def _tag_safe(path: Path, **kwargs) -> None:  # type: ignore[type-arg]
    from .tagger import tag_flac
    try:
        tag_flac(path, **kwargs)
    except Exception as exc:
        log.warning("sync.tag_error", path=str(path), error=str(exc))
