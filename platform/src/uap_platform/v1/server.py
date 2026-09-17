"""V1 internal library browser exposed only through a loopback host port."""

from __future__ import annotations

import hmac
import json
import logging
import os
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from uap_platform.model_governance import ModelTaskType
from uap_platform.object_registry import ObjectClient

from .library import InternalLibrary
from .worker import build_client_from_environment

LOGGER = logging.getLogger(__name__)

_HTML = files("uap_platform.v1").joinpath("library.html").read_text("utf-8")


class Application:
    def __init__(self, library: InternalLibrary, token: str) -> None:
        if len(token) < 32:
            raise ValueError("UAP_V1_LOCAL_ADMIN_TOKEN must contain at least 32 characters")
        self.library = library
        self.token = token

    def handle(
        self, method: str, target: str, authorization: str | None, body: bytes = b""
    ) -> tuple[int, str, bytes]:
        parsed = urlsplit(target)
        if method == "GET" and parsed.path == "/":
            return HTTPStatus.OK, "text/html; charset=utf-8", _HTML.encode()
        if method == "GET" and parsed.path == "/healthz":
            return HTTPStatus.OK, "application/json", b'{"status":"ok"}'
        if not self._authorized(authorization):
            return self._problem(HTTPStatus.UNAUTHORIZED, "authorization_required")
        try:
            if method == "GET" and parsed.path == "/internal/v1/documents":
                query = parse_qs(parsed.query)
                value = self.library.list_documents(
                    query=query.get("q", [None])[0],
                    limit=int(query.get("limit", ["50"])[0]),
                )
                return self._json(value)
            if method == "GET" and parsed.path == "/internal/v1/usage":
                return self._json(self.library.usage())
            parts = parsed.path.strip("/").split("/")
            if len(parts) >= 4 and parts[:3] == ["internal", "v1", "documents"]:
                document_id = uuid.UUID(parts[3])
                if method == "GET" and len(parts) == 4:
                    document = self.library.get_document(document_id)
                    if document is None:
                        return self._problem(HTTPStatus.NOT_FOUND, "document_not_found")
                    return self._json(document)
                if method == "POST" and len(parts) == 5 and parts[4] == "reanalyze":
                    payload = json.loads(body)
                    value = self.library.request_reanalysis(
                        document_id,
                        ModelTaskType(str(payload.get("task_type"))),
                        str(payload.get("reason", "")),
                        uuid.uuid4(),
                    )
                    return self._json(value, HTTPStatus.ACCEPTED)
            return self._problem(HTTPStatus.NOT_FOUND, "route_not_found")
        except (ValueError, json.JSONDecodeError):
            return self._problem(HTTPStatus.BAD_REQUEST, "invalid_request")
        except LookupError as error:
            return self._problem(HTTPStatus.CONFLICT, str(error))
        except RuntimeError as error:
            return self._problem(HTTPStatus.PAYMENT_REQUIRED, str(error))
        except Exception:
            LOGGER.exception("V1 internal request failed")
            return self._problem(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error")

    def _authorized(self, value: str | None) -> bool:
        if value is None or not value.startswith("Bearer "):
            return False
        return hmac.compare_digest(value[7:], self.token)

    @staticmethod
    def _json(value: object, status: int = HTTPStatus.OK) -> tuple[int, str, bytes]:
        return status, "application/json", json.dumps(value, default=str).encode()

    @staticmethod
    def _problem(status: int, code: str) -> tuple[int, str, bytes]:
        return status, "application/json", json.dumps({"error": code}).encode()


def make_handler(application: Application) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _respond(self, method: str) -> None:
            length = min(int(self.headers.get("Content-Length", "0")), 1_000_000)
            body = self.rfile.read(length) if length else b""
            status, content_type, payload = application.handle(
                method, self.path, self.headers.get("Authorization"), body
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            self._respond("GET")

        def do_POST(self) -> None:
            self._respond("POST")

        def log_message(self, _format: str, *_args: object) -> None:
            LOGGER.info("V1 internal request completed")

    return Handler


def main() -> None:
    logging.basicConfig(level=os.environ.get("UAP_LOG_LEVEL", "INFO"))
    read_url = os.environ.get("UAP_V1_READ_DATABASE_URL")
    worker_url = os.environ.get("UAP_V1_WORKER_DATABASE_URL")
    model_url = os.environ.get("UAP_V1_MODEL_DATABASE_URL")
    token = os.environ.get("UAP_V1_LOCAL_ADMIN_TOKEN")
    if not read_url or not worker_url or not model_url or not token:
        raise SystemExit(
            "UAP_V1_READ_DATABASE_URL, UAP_V1_WORKER_DATABASE_URL, "
            "UAP_V1_MODEL_DATABASE_URL and UAP_V1_LOCAL_ADMIN_TOKEN are required"
        )
    host = os.environ.get("UAP_V1_LIBRARY_HOST", "127.0.0.1")
    # 0.0.0.0 is required inside the container; Compose publishes it only on
    # 127.0.0.1. Direct local runs remain loopback-only by default.
    if host not in {"127.0.0.1", "::1", "localhost", "0.0.0.0"}:  # noqa: S104
        raise SystemExit("unsupported V1 internal library bind address")
    port = int(os.environ.get("UAP_V1_LIBRARY_PORT", "8091"))
    client = cast_object_client(build_client_from_environment(worker_url))
    application = Application(
        InternalLibrary(read_url, worker_url, model_url, client), token
    )
    server = ThreadingHTTPServer((host, port), make_handler(application))
    LOGGER.info("V1 internal library started host=%s port=%s", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def cast_object_client(value: Any) -> ObjectClient:
    return cast(ObjectClient, value)
