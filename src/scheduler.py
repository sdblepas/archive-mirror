"""
Periodic download scheduler.

Lifecycle
---------
1. On the very first tick, check if the DB has any items at all.
   • Empty DB  → run full sync (discovery + downloads) so the service
                  bootstraps itself without requiring a manual Scan.
   • Items exist → skip discovery; just run downloads and pick up from
                  where the last run left off (fast restarts).

2. After the first tick, subsequent ticks always run downloads only.
   New concerts are picked up via the "Scan" button in the UI
   (POST /api/scan), which runs discovery in the background.

3. If SYNC_INTERVAL == 0, runs exactly once then exits.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from .catalog import export_catalog
from .config import Config
from .database import Database
from .logger import get_logger
from .sync import SyncManager
from .web_state import set_health, set_scan_done, set_scan_started

log = get_logger(__name__)


async def run_forever(config: Config, db: Database) -> None:
    manager = SyncManager(config, db)
    first_tick = True

    while True:
        started = datetime.now(timezone.utc)
        log.info("scheduler.tick", at=started.isoformat())

        try:
            if first_tick:
                first_tick = False
                counts = await db.count_items_by_status()
                total_known = sum(counts.values())

                if total_known == 0:
                    # Brand-new install: discover everything first, then download.
                    log.info("scheduler.first_run_empty_db_discovery")
                    set_health("ok", sync_status="discovering")
                    set_scan_started()
                    try:
                        disc_stats = await manager.run_discovery()
                        set_scan_done(disc_stats)
                    except Exception:
                        set_scan_done(None)
                        log.exception("scheduler.discovery_error")

                    set_health("ok", sync_status="running")
                    stats = await manager.run_downloads()
                else:
                    # DB already has items: skip discovery, just download.
                    log.info(
                        "scheduler.resume_from_db",
                        known_items=total_known,
                        pending=counts.get("pending", 0),
                        failed=counts.get("failed", 0),
                    )
                    set_health("ok", sync_status="running")
                    stats = await manager.run_downloads()
            else:
                # Subsequent ticks: downloads only.
                set_health("ok", sync_status="running")
                stats = await manager.run_downloads()

            log.info("scheduler.tick_done", **stats)

            # Export catalog after every download run.
            try:
                await export_catalog(config, db)
            except Exception:
                log.exception("scheduler.catalog_error")

            set_health("ok", sync_status="idle", last_sync=started.isoformat())

        except Exception:
            log.exception("scheduler.sync_error")
            set_health("degraded", sync_status="error")

        if config.sync_interval <= 0:
            log.info("scheduler.one_shot_done")
            return

        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        sleep_for = max(0.0, config.sync_interval - elapsed)
        log.info(
            "scheduler.sleeping",
            interval=config.sync_interval,
            sleep_seconds=round(sleep_for, 1),
        )
        set_health("ok", sync_status="sleeping", next_sync_in_seconds=round(sleep_for))
        await asyncio.sleep(sleep_for)
