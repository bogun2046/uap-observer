"""Minimal health server used only to validate the WP2 environment."""

from __future__ import annotations

import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

from uap_platform.config import Settings, load_settings
from uap_platform.readiness import collect_readiness

LOGGER = logging.getLogger("uap_platform.devserver")


class HealthHandler(BaseHTTPRequestHandler):
    """Serve a dependency-aware health endpoint without business APIs."""

    settings: ClassVar[Settings]

    def do_GET(self) -> None:
        if self.path != "/healthz":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        status_code, payload = collect_readiness(self.settings)
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, message_format: str, *args: object) -> None:
        LOGGER.info(message_format, *args)


def main() -> None:
    """Start the WP2-only readiness HTTP server."""

    settings = load_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    LOGGER.info("starting WP2 health service: %s", settings.safe_summary())
    HealthHandler.settings = settings
    # The health endpoint must accept the container port mapping.
    bind_host = "0.0.0.0"  # noqa: S104  # nosec B104
    server = ThreadingHTTPServer((bind_host, settings.health_port), HealthHandler)
    server.serve_forever()
