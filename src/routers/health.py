"""
Health router — /health and /healthz

Used by Docker HEALTHCHECK and any uptime monitor.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..web_state import get_health_state

router = APIRouter(tags=["health"])


@router.get("/health", include_in_schema=False)
@router.get("/healthz", include_in_schema=False)
async def health() -> JSONResponse:
    state = get_health_state()
    ok = state.get("status") == "ok"
    return JSONResponse(state, status_code=200 if ok else 503)
