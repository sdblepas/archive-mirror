"""
Entry point for the archive-mirror service.

Lifecycle:
  1. Load Config from environment variables.
  2. Configure structured logging.
  3. Start health-check HTTP server.
  4. Open SQLite database (creates schema on first run).
  5. Run the sync scheduler (blocks until done or interrupted).
  6. Clean shutdown.
"""
from __future__ import annotations

import asyncio
import signal
import sys

from .config import Config
from .database import Database
from .health import HealthServer
from .logger import configure_logging, get_logger
from .scheduler import run_forever

log = get_logger(__name__)


async def _main() -> None:
    config = Config()
    configure_logging(config.log_level)

    log.info(
        "service.start",
        collection=config.collection,
        output_dir=str(config.output_dir),
        state_dir=str(config.state_dir),
        sync_interval=config.sync_interval,
        concurrency=config.max_workers,
        dry_run=config.dry_run,
    )

    # Health server runs in a background daemon thread
    health = HealthServer(config.health_port)
    health.start()

    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _handle_signal(sig: signal.Signals) -> None:
        log.info("service.signal_received", signal=sig.name)
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig)

    async with Database(config.db_path) as db:
        # Run the scheduler in a task so we can cancel it on shutdown
        scheduler_task = asyncio.create_task(
            run_forever(config, db, health),
            name="scheduler",
        )
        shutdown_task = asyncio.create_task(
            shutdown_event.wait(),
            name="shutdown-watcher",
        )

        done, pending = await asyncio.wait(
            {scheduler_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Re-raise any exception from the scheduler
        for task in done:
            if task.get_name() == "scheduler" and not task.cancelled():
                exc = task.exception()
                if exc:
                    raise exc

    health.stop()
    log.info("service.stopped")


def main() -> None:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        log.exception("service.fatal", error=str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
