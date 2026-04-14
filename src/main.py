"""
Entry point.

Runs the FastAPI web UI (via uvicorn) and the sync scheduler concurrently
inside a single asyncio event loop — they share the same DB connection.
"""
from __future__ import annotations

import asyncio
import signal
import sys

import uvicorn

from .config import Config
from .database import Database
from .logger import configure_logging, get_logger
from .scheduler import run_forever
from .web import create_app, set_health

log = get_logger(__name__)


async def _main() -> None:
    config = Config()
    configure_logging(config.log_level)

    log.info(
        "service.start",
        collections=config.collections,
        output_dir=str(config.output_dir),
        state_dir=str(config.state_dir),
        sync_interval=config.sync_interval,
        concurrency=config.max_workers,
        dry_run=config.dry_run,
        web_port=config.web_port,
    )

    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _handle_signal(sig: signal.Signals) -> None:
        log.info("service.signal_received", signal=sig.name)
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig)

    async with Database(config.db_path) as db:
        app = create_app(config, db)

        uv_config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=config.web_port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(uv_config)

        set_health("ok", sync_status="starting")

        scheduler_task = asyncio.create_task(
            run_forever(config, db), name="scheduler"
        )
        web_task = asyncio.create_task(
            server.serve(), name="web"
        )
        shutdown_task = asyncio.create_task(
            shutdown_event.wait(), name="shutdown-watcher"
        )

        done, pending = await asyncio.wait(
            {scheduler_task, web_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Graceful shutdown
        server.should_exit = True
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        # Surface any unexpected exception from scheduler or web
        for task in done:
            if not task.cancelled() and task.get_name() in ("scheduler", "web"):
                exc = task.exception()
                if exc:
                    log.error(
                        "service.task_failed",
                        task=task.get_name(),
                        error=str(exc),
                    )

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
