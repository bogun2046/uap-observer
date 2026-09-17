"""Dedicated standard-library process for the OIDC Admin API."""

from __future__ import annotations

import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import load_admin_api_settings
from .cursor import CursorCodec
from .handler import AdminApiApplication
from .oidc import OidcValidator, TokenError
from .pool import AdminApiPool
from .service import AdminQueryService

LOGGER = logging.getLogger(__name__)
_MAX_BODY_LENGTH = 1_000_000


def make_handler(application: AdminApiApplication) -> type[BaseHTTPRequestHandler]:
    class AdminRequestHandler(BaseHTTPRequestHandler):
        server_version = "UAPAdminAPI"
        sys_version = ""

        def _read_body(self) -> bytes:
            length_header = self.headers.get("Content-Length", "0")
            try:
                length = int(length_header)
            except ValueError:
                length = 0
            if length < 0 or length > _MAX_BODY_LENGTH:
                return b""
            if length == 0:
                return b""
            return self.rfile.read(length)

        def _respond(self, method: str) -> None:
            target = getattr(self, "path", "/")
            request_headers = dict(getattr(self, "headers", {}).items())
            body = self._read_body() if method in {"POST", "PUT", "PATCH"} else None
            response = application.handle(method, target, request_headers, body)
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
            LOGGER.info("admin API request completed")

    return AdminRequestHandler


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = load_admin_api_settings()
    try:
        oidc = OidcValidator.from_settings(
            settings.oidc_issuer, settings.oidc_audience, settings.jwks_document
        )
    except TokenError as error:
        raise SystemExit("admin API OIDC configuration is invalid") from error
    pool = AdminApiPool(
        settings.psycopg_database_url,
        min_size=settings.pool_min_size,
        max_size=settings.pool_max_size,
    )
    codec = CursorCodec(settings.cursor_key, settings.cursor_max_length)
    application = AdminApiApplication(AdminQueryService(pool, codec), oidc, settings.oidc_issuer)
    server = ThreadingHTTPServer((settings.host, settings.port), make_handler(application))
    LOGGER.info("admin API started settings=%s", settings.safe_summary())
    try:
        server.serve_forever()
    finally:
        server.server_close()
        pool.close()


if __name__ == "__main__":
    main()
