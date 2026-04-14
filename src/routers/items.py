"""
Items router — /api/items

Paginated, searchable concert browser + single-item detail.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/items", tags=["items"])


@router.get("")
async def items_list(
    request: Request,
    q: Optional[str] = Query(default=None, description="Search artist, title, venue or identifier"),
    status: Optional[str] = Query(default=None, description="Filter by status"),
    collection: Optional[str] = Query(default=None, description="Filter by collection"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
) -> JSONResponse:
    db = request.app.state.db
    rows, total = await db.get_items_paginated(
        status=status,
        collection=collection,
        search=q,
        page=page,
        per_page=per_page,
    )
    return JSONResponse(
        {
            "items": rows,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": max(1, (total + per_page - 1) // per_page),
        }
    )


@router.get("/{identifier:path}")
async def item_detail(request: Request, identifier: str) -> JSONResponse:
    db = request.app.state.db
    item = await db.get_item(identifier)
    if item is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    tracks = await db.get_tracks_for_item(identifier)
    item.pop("raw_metadata", None)   # don't expose the full JSON blob
    return JSONResponse({"item": item, "tracks": tracks})
