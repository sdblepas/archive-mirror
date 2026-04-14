"""
Shared in-process state written by the scheduler and scan tasks,
read by the API routers.

Keeping this in its own module avoids circular imports between
scheduler → web → routers → scheduler.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

# ── Health state (set by scheduler) ──────────────────────────────────────────

_health: dict[str, Any] = {
    "status": "starting",
    "started_at": datetime.now(timezone.utc).isoformat(),
}


def set_health(status: str, **extra: Any) -> None:
    _health.update({"status": status, **extra})


def get_health_state() -> dict[str, Any]:
    return dict(_health)


# ── Scan state (set by scan background task) ──────────────────────────────────

_scan: dict[str, Any] = {
    "running": False,
    "last_scan_at": None,
    "last_scan_stats": None,
}


def set_scan_started() -> None:
    _scan["running"] = True


def set_scan_done(stats: Optional[dict]) -> None:
    _scan["running"] = False
    _scan["last_scan_at"] = datetime.now(timezone.utc).isoformat()
    _scan["last_scan_stats"] = stats


def is_scan_running() -> bool:
    return bool(_scan["running"])


def get_scan_state() -> dict[str, Any]:
    return dict(_scan)
