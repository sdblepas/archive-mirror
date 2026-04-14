"""
Central configuration loaded entirely from environment variables.

COLLECTION supports comma-separated values for multi-collection mirroring:
  COLLECTION=aadamjacobs
  COLLECTION=aadamjacobs,gratefuldead,phish
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool_env(key: str, default: str = "false") -> bool:
    return os.getenv(key, default).strip().lower() in ("1", "true", "yes")


def _parse_collections() -> list[str]:
    raw = os.getenv("COLLECTION", "aadamjacobs")
    return [c.strip() for c in raw.split(",") if c.strip()]


@dataclass
class Config:
    # ── Paths ──────────────────────────────────────────────────────────────
    output_dir: Path = field(
        default_factory=lambda: Path(os.getenv("OUTPUT_DIR", "/data/music"))
    )
    state_dir: Path = field(
        default_factory=lambda: Path(os.getenv("STATE_DIR", "/data/state"))
    )

    # ── Collections (comma-separated) ─────────────────────────────────────
    collections: list[str] = field(default_factory=_parse_collections)

    # ── Scheduling ─────────────────────────────────────────────────────────
    sync_interval: int = field(
        default_factory=lambda: int(os.getenv("SYNC_INTERVAL", "3600"))
    )

    # ── Concurrency / throttling ───────────────────────────────────────────
    max_workers: int = field(
        default_factory=lambda: int(os.getenv("CONCURRENCY", "3"))
    )
    rate_limit_delay: float = field(
        default_factory=lambda: float(os.getenv("RATE_LIMIT_DELAY", "1.0"))
    )

    # ── HTTP ───────────────────────────────────────────────────────────────
    request_timeout: float = field(
        default_factory=lambda: float(os.getenv("REQUEST_TIMEOUT", "120"))
    )
    retry_count: int = field(
        default_factory=lambda: int(os.getenv("RETRY_COUNT", "5"))
    )

    # ── Logging ────────────────────────────────────────────────────────────
    log_level: str = field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper()
    )

    # ── Behaviour flags ────────────────────────────────────────────────────
    dry_run: bool = field(default_factory=lambda: _bool_env("DRY_RUN"))
    write_checksum_manifest: bool = field(
        default_factory=lambda: _bool_env("CHECKSUM_MANIFEST", "true")
    )

    # ── Notifications ──────────────────────────────────────────────────────
    webhook_url: str = field(default_factory=lambda: os.getenv("WEBHOOK_URL", ""))

    # ── Web UI / Health port ───────────────────────────────────────────────
    web_port: int = field(
        default_factory=lambda: int(
            os.getenv("WEB_PORT", os.getenv("HEALTH_PORT", "6547"))
        )
    )

    def __post_init__(self) -> None:
        self.output_dir = Path(self.output_dir)
        self.state_dir = Path(self.state_dir)
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self.state_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f"Cannot create required directories: {exc}"
            ) from exc

    # ── Convenience ───────────────────────────────────────────────────────
    @property
    def collection(self) -> str:
        """Primary collection (first in list). Used in legacy single-collection contexts."""
        return self.collections[0] if self.collections else "aadamjacobs"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "mirror.db"

    @property
    def catalog_json_path(self) -> Path:
        return self.state_dir / "catalog.json"

    @property
    def catalog_csv_path(self) -> Path:
        return self.state_dir / "catalog.csv"
