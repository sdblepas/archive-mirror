"""
Central configuration loaded entirely from environment variables.

COLLECTION supports comma-separated values for multi-collection mirroring:
  COLLECTION=aadamjacobs
  COLLECTION=aadamjacobs,gratefuldead,phish

API_KEY (optional): if set, POST /api/scan requires an X-Api-Key header
  matching this value.  Leave unset to allow unauthenticated scan triggers
  (only appropriate when the port is not exposed publicly).

WEBHOOK_URL: must be an http(s) URL.  Loopback addresses (localhost,
  127.x, ::1) and the cloud-metadata service (169.254.x) are rejected
  at startup to prevent SSRF.  Private-range IPs (10.x, 192.168.x,
  172.16-31.x) are allowed so webhooks can reach other services on the
  same Docker network.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


def _bool_env(key: str, default: str = "false") -> bool:
    return os.getenv(key, default).strip().lower() in ("1", "true", "yes")


def _parse_collections() -> list[str]:
    raw = os.getenv("COLLECTION", "aadamjacobs")
    return [c.strip() for c in raw.split(",") if c.strip()]


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1", "ip6-localhost"})
_LOOPBACK_PREFIXES = ("127.", "169.254.")   # loopback range + cloud metadata


def _validate_webhook_url(url: str) -> None:
    """Raise ValueError if *url* looks dangerous or malformed."""
    if not url:
        return
    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise ValueError(f"Malformed WEBHOOK_URL: {exc}") from exc

    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"WEBHOOK_URL must use http or https scheme, got: {parsed.scheme!r}"
        )

    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("WEBHOOK_URL has no host")

    if host in _LOOPBACK_HOSTS:
        raise ValueError(
            f"WEBHOOK_URL points to a loopback address ({host!r}), which is not allowed"
        )
    for prefix in _LOOPBACK_PREFIXES:
        if host.startswith(prefix):
            raise ValueError(
                f"WEBHOOK_URL points to a reserved address ({host!r}), which is not allowed"
            )


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

    # ── Security ───────────────────────────────────────────────────────────
    # If set, POST /api/scan requires X-Api-Key: <value>
    api_key: str = field(default_factory=lambda: os.getenv("API_KEY", ""))

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

        # Validate webhook URL at startup so bad configs fail fast.
        try:
            _validate_webhook_url(self.webhook_url)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

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
