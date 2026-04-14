"""
Stats router — /api/stats

Returns a single JSON document with all summary counters.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..web_state import get_health_state

router = APIRouter(prefix="/api", tags=["stats"])


@router.get("/stats")
async def stats(request: Request) -> JSONResponse:
    db = request.app.state.db
    config = request.app.state.config

    item_counts = await db.count_items_by_status()
    track_counts = await db.count_tracks()
    last_sync = await db.get_last_sync()

    return JSONResponse(
        {
            "items": item_counts,
            "tracks": track_counts,
            "last_sync": last_sync,
            "collections": config.collections,
            "health": get_health_state(),
        }
    )
