"""
Shared in-process state written by the scheduler, read by the API routers.

Keeping this in its own module avoids circular imports between
scheduler → web → routers → scheduler.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

_state: dict[str, Any] = {
    "status": "starting",
    "started_at": datetime.now(timezone.utc).isoformat(),
}


def set_health(status: str, **extra: Any) -> None:
    _state.update({"status": status, **extra})


def get_health_state() -> dict[str, Any]:
    return dict(_state)
