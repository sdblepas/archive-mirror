"""
Central configuration loaded entirely from environment variables.
All fields have sane defaults so the service works out of the box.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool_env(key: str, default: str = "false") -> bool:
    return os.getenv(key, default).strip().lower() in ("1", "true", "yes")


@dataclass
class Config:
    # ── Paths ──────────────────────────────────────────────────────────────
    output_dir: Path = field(
        default_factory=lambda: Path(os.getenv("OUTPUT_DIR", "/data/music"))
    )
    state_dir: Path = field(
        default_factory=lambda: Path(os.getenv("STATE_DIR", "/data/state"))
    )

    # ── Collection ─────────────────────────────────────────────────────────
    collection: str = field(
        default_factory=lambda: os.getenv("COLLECTION", "aadamjacobs")
    )

    # ── Scheduling ─────────────────────────────────────────────────────────
    # Seconds between full syncs. 0 = run once then exit.
    sync_interval: int = field(
        default_factory=lambda: int(os.getenv("SYNC_INTERVAL", "3600"))
    )

    # ── Concurrency / throttling ───────────────────────────────────────────
    max_workers: int = field(
        default_factory=lambda: int(os.getenv("CONCURRENCY", "3"))
    )
    # Minimum seconds to wait between HTTP requests (per worker)
    rate_limit_delay: float = field(
        default_factory=lambda: float(os.getenv("RATE_LIMIT_DELAY", "1.0"))
    )

    # ── HTTP behaviour ──────────────────────────────────────────────────────
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

    # ── Optional notifications ─────────────────────────────────────────────
    webhook_url: str = field(default_factory=lambda: os.getenv("WEBHOOK_URL", ""))

    # ── Health endpoint ────────────────────────────────────────────────────
    health_port: int = field(
        default_factory=lambda: int(os.getenv("HEALTH_PORT", "8080"))
    )

    def __post_init__(self) -> None:
        self.output_dir = Path(self.output_dir)
        self.state_dir = Path(self.state_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    # ── Derived paths ──────────────────────────────────────────────────────
    @property
    def db_path(self) -> Path:
        return self.state_dir / "mirror.db"

    @property
    def health_file(self) -> Path:
        return self.state_dir / ".health"
