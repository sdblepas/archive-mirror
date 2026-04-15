"""
POST /api/scan        – Trigger an IA discovery run in the background.
GET  /api/scan/status – Current scan state (running, last_scan_at, stats).

Authentication
--------------
If the ``API_KEY`` environment variable is set, every POST request must
include an ``X-Api-Key: <value>`` header.  Requests with a missing or wrong
key are rejected with 401.  When ``API_KEY`` is empty the endpoint is
unauthenticated — only expose it on a trusted network in that case.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from ..sync import SyncManager
from ..web_state import get_scan_state, is_scan_running, set_scan_done, set_scan_started

router = APIRouter()
_log = logging.getLogger(__name__)


async def _run_scan(config, db) -> None:  # type: ignore[type-arg]
    """Background coroutine: run discovery and update scan state."""
    set_scan_started()
    try:
        manager = SyncManager(config, db)
        stats = await manager.run_discovery()
        set_scan_done(stats)
    except Exception as exc:
        _log.error("scan.background_failed: %s", exc, exc_info=True)
        set_scan_done(None)


def _scan_done_callback(task: asyncio.Task) -> None:  # type: ignore[type-arg]
    """Log any unhandled exception that escapes _run_scan."""
    if not task.cancelled() and (exc := task.exception()):
        _log.error("scan.task_unhandled_exception: %s", exc, exc_info=exc)


@router.post("/api/scan", summary="Scan Internet Archive for new concerts")
async def trigger_scan(
    request: Request,
    x_api_key: str = Header(default=""),
) -> JSONResponse:
    """
    Start an IA discovery pass in the background.

    - **401** if ``API_KEY`` is configured and the header is wrong/missing.
    - **409** if a scan is already running.
    - **202** otherwise — poll ``GET /api/scan/status`` for progress.
    """
    cfg = request.app.state.config

    # Enforce API key when one is configured
    if cfg.api_key and x_api_key != cfg.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Api-Key header")

    if is_scan_running():
        return JSONResponse(
            {"error": "A scan is already running — check /api/scan/status"},
            status_code=409,
        )

    task = asyncio.create_task(
        _run_scan(cfg, request.app.state.db),
        name="scan-discovery",
    )
    # Catch any exception that leaks past _run_scan's own try/except
    task.add_done_callback(_scan_done_callback)

    return JSONResponse({"status": "started"}, status_code=202)


@router.get("/api/scan/status", summary="Current scan state")
async def scan_status() -> dict:
    """
    Returns:
    - ``running`` — bool
    - ``last_scan_at`` — ISO timestamp of last completed scan (or null)
    - ``last_scan_stats`` — ``{items_discovered, items_new}`` from the last scan
    """
    return get_scan_state()
