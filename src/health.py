"""
Lightweight HTTP health-check server running in a daemon thread.

GET /health  → 200 JSON  {"status": "ok", ...}
GET /metrics → 200 JSON  {"items": {...}, "tracks": {...}, "last_sync": {...}}

The shared state dict is updated by the main sync loop.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from .logger import get_logger

log = get_logger(__name__)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in ("/health", "/healthz"):
            self._respond(self.server.health_state)  # type: ignore[attr-defined]
        elif self.path in ("/metrics", "/status"):
            self._respond(self.server.metrics_state)  # type: ignore[attr-defined]
        else:
            self.send_response(404)
            self.end_headers()

    def _respond(self, state: dict) -> None:
        body = json.dumps(state, default=str).encode()
        healthy = state.get("status") == "ok"
        self.send_response(200 if healthy else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: Any) -> None:
        pass  # Suppress default HTTP access logs


class HealthServer:
    def __init__(self, port: int) -> None:
        self._port = port
        self._server: HTTPServer | None = None
        self.health_state: dict[str, Any] = {
            "status": "starting",
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        self.metrics_state: dict[str, Any] = {}

    def start(self) -> None:
        self._server = HTTPServer(("0.0.0.0", self._port), _Handler)
        self._server.health_state = self.health_state  # type: ignore[attr-defined]
        self._server.metrics_state = self.metrics_state  # type: ignore[attr-defined]
        thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="health-server"
        )
        thread.start()
        log.info("health.server_started", port=self._port)

    def set_healthy(self, **extra: Any) -> None:
        self.health_state.update({"status": "ok", **extra})

    def set_unhealthy(self, reason: str) -> None:
        self.health_state.update({"status": "degraded", "reason": reason})

    def update_metrics(self, **metrics: Any) -> None:
        self.metrics_state.update(metrics)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
