"""
Catalog router — /api/catalog

On-demand catalog export trigger.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..catalog import export_catalog

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


@router.get("/refresh")
async def catalog_refresh(request: Request) -> JSONResponse:
    """Trigger an immediate catalog export to JSON + CSV."""
    config = request.app.state.config
    db = request.app.state.db
    counts = await export_catalog(config, db)
    return JSONResponse({"status": "ok", **counts})
