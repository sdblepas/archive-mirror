"""
POST /api/scan        – Trigger an IA discovery run in the background.
GET  /api/scan/status – Current scan state (running, last_scan_at, stats).
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..sync import SyncManager
from ..web_state import get_scan_state, is_scan_running, set_scan_done, set_scan_started

router = APIRouter()


async def _run_scan(config, db) -> None:  # type: ignore[type-arg]
    """Background task: discover items and update scan state."""
    set_scan_started()
    try:
        manager = SyncManager(config, db)
        stats = await manager.run_discovery()
        set_scan_done(stats)
    except Exception:
        set_scan_done(None)


@router.post("/api/scan", summary="Scan Internet Archive for new concerts")
async def trigger_scan(request: Request) -> JSONResponse:
    """
    Start an IA discovery pass in the background.

    - **409** if a scan is already running.
    - **202** otherwise (accepted, running in background).

    Poll `GET /api/scan/status` for progress.
    """
    if is_scan_running():
        return JSONResponse(
            {"error": "A scan is already running — check /api/scan/status"},
            status_code=409,
        )

    asyncio.create_task(
        _run_scan(request.app.state.config, request.app.state.db),
        name="scan-discovery",
    )
    return JSONResponse({"status": "started"}, status_code=202)


@router.get("/api/scan/status", summary="Current scan state")
async def scan_status() -> dict:
    """
    Returns:
    - `running` — bool
    - `last_scan_at` — ISO timestamp of last completed scan (or null)
    - `last_scan_stats` — `{items_discovered, items_new}` from the last scan
    """
    return get_scan_state()
