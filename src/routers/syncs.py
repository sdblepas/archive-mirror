"""
Syncs router — /api/syncs

Returns recent sync run history.
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api", tags=["syncs"])


@router.get("/syncs")
async def recent_syncs(
    request: Request,
    limit: int = Query(default=10, ge=1, le=100),
) -> JSONResponse:
    db = request.app.state.db
    syncs = await db.get_recent_syncs(limit=limit)
    return JSONResponse(syncs)
