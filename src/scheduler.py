"""
Periodic sync scheduler.

If SYNC_INTERVAL == 0, runs exactly once and exits.
Otherwise runs immediately, then sleeps SYNC_INTERVAL seconds between runs.

Uses asyncio.sleep so the event loop remains responsive to signals and
the health server thread continues ticking.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from .config import Config
from .database import Database
from .health import HealthServer
from .logger import get_logger
from .sync import SyncManager

log = get_logger(__name__)


async def run_forever(
    config: Config,
    db: Database,
    health: HealthServer,
) -> None:
    manager = SyncManager(config, db, health)

    while True:
        started = datetime.now(timezone.utc)
        log.info("scheduler.tick", at=started.isoformat())

        try:
            stats = await manager.run_sync()
            log.info("scheduler.sync_done", **stats)
        except Exception as exc:
            log.exception("scheduler.sync_error", error=str(exc))
            health.set_unhealthy(f"sync error: {exc}")

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
        health.set_healthy(
            sync_status="sleeping",
            next_sync_in_seconds=round(sleep_for),
        )
        await asyncio.sleep(sleep_for)
