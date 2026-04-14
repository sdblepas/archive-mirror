"""
Periodic sync scheduler.

If SYNC_INTERVAL == 0, runs exactly once and exits.
Otherwise runs immediately, then sleeps SYNC_INTERVAL seconds between runs.
After each sync, triggers a catalog export.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from .catalog import export_catalog
from .config import Config
from .database import Database
from .logger import get_logger
from .sync import SyncManager
from .web import set_health

log = get_logger(__name__)


async def run_forever(config: Config, db: Database) -> None:
    manager = SyncManager(config, db)

    while True:
        started = datetime.now(timezone.utc)
        log.info("scheduler.tick", at=started.isoformat())
        set_health("ok", sync_status="running")

        try:
            stats = await manager.run_sync()
            log.info("scheduler.sync_done", **stats)

            # Export catalog after every successful sync
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
