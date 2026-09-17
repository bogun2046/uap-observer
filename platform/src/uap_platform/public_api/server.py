"""Dedicated standard-library process for the anonymous Public API."""

from __future__ import annotations

import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import load_public_api_settings
from .cursor import CursorCodec
from .handler import PublicApiApplication
from .pool import PublicReaderPool
from .service import PublicQueryService

LOGGER = logging.getLogger(__name__)


def make_handler(application: PublicApiApplication) -> type[BaseHTTPRequestHandler]:
    class PublicRequestHandler(BaseHTTPRequestHandler):
        server_version = "UAPPublicAPI"
        sys_version = ""

        def _respond(self, method: str) -> None:
            target = getattr(self, "path", "/")
            request_headers = dict(getattr(self, "headers", {}).items())
            response = application.handle(method, target, request_headers)
            self.send_response(response.status)
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            self.wfile.write(response.body)

        def do_GET(self) -> None:
            self._respond("GET")

        def do_POST(self) -> None:
            self._respond("POST")

        def do_PUT(self) -> None:
            self._respond("PUT")

        def do_PATCH(self) -> None:
            self._respond("PATCH")

        def do_DELETE(self) -> None:
            self._respond("DELETE")

        def do_OPTIONS(self) -> None:
            self._respond("OPTIONS")

        def do_HEAD(self) -> None:
            self._respond("HEAD")

        def send_error(
            self,
            code: int,
            message: str | None = None,
            explain: str | None = None,
        ) -> None:
            del code, message, explain
            self._respond("INVALID")

        def log_message(self, format: str, *args: object) -> None:
            # Never log the raw route/query because search input may be sensitive.
            LOGGER.info("public API request completed")

    return PublicRequestHandler


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = load_public_api_settings()
    pool = PublicReaderPool(
        settings.psycopg_database_url,
        min_size=settings.pool_min_size,
        max_size=settings.pool_max_size,
    )
    codec = CursorCodec(settings.cursor_key, settings.cursor_max_length)
    application = PublicApiApplication(PublicQueryService(pool, codec))
    server = ThreadingHTTPServer((settings.host, settings.port), make_handler(application))
    LOGGER.info("public API started settings=%s", settings.safe_summary())
    try:
        server.serve_forever()
    finally:
        server.server_close()
        pool.close()


if __name__ == "__main__":
    main()
