"""V1 internal library browser exposed only through a loopback host port."""

from __future__ import annotations

import hmac
import http.client
import ipaddress
import json
import logging
import os
import uuid
from collections.abc import Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any, cast
from urllib.parse import SplitResult, parse_qs, urlsplit

from uap_platform.model_governance import ModelTaskType
from uap_platform.object_registry import ObjectClient

from .library import InternalLibrary
from .worker import build_client_from_environment

LOGGER = logging.getLogger(__name__)

_HTML = files("uap_platform.v1").joinpath("library.html").read_text("utf-8")


class Application:
    def __init__(
        self, library: InternalLibrary, token: str, admin_api_base_url: str | None = None
    ) -> None:
        if len(token) < 32:
            raise ValueError("UAP_V1_LOCAL_ADMIN_TOKEN must contain at least 32 characters")
        self.library = library
        self.token = token
        self.admin_api_base_url = admin_api_base_url.rstrip("/") if admin_api_base_url else None
        self._admin_api_url = (
            _validated_admin_api_url(self.admin_api_base_url)
            if self.admin_api_base_url is not None
            else None
        )

    def handle(
        self,
        method: str,
        target: str,
        authorization: str | None,
        body: bytes = b"",
        request_headers: Mapping[str, str] | None = None,
    ) -> tuple[int, str, bytes]:
        parsed = urlsplit(target)
        if method == "GET" and parsed.path == "/":
            return HTTPStatus.OK, "text/html; charset=utf-8", _HTML.encode()
        if method == "GET" and parsed.path == "/healthz":
            return HTTPStatus.OK, "application/json", b'{"status":"ok"}'
        if parsed.path.startswith("/admin/v1/"):
            return self._proxy_admin(method, target, authorization, body, request_headers or {})
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

    def _proxy_admin(
        self,
        method: str,
        target: str,
        authorization: str | None,
        body: bytes,
        request_headers: Mapping[str, str],
    ) -> tuple[int, str, bytes]:
        """Forward OIDC requests to the dedicated Admin API as a presentation bridge."""

        if self.admin_api_base_url is None:
            return self._problem(HTTPStatus.SERVICE_UNAVAILABLE, "admin_api_unconfigured")
        parsed_target = urlsplit(target)
        if (
            not target.startswith("/")
            or parsed_target.scheme
            or parsed_target.netloc
            or parsed_target.fragment
            or "#" in target
        ):
            return self._problem(HTTPStatus.BAD_REQUEST, "invalid_request")

        base_url = self._admin_api_url
        if base_url is None:
            return self._problem(HTTPStatus.SERVICE_UNAVAILABLE, "admin_api_unconfigured")
        path = f"{base_url.path.rstrip('/')}{parsed_target.path}"
        if "?" in target:
            path = f"{path}?{parsed_target.query}"
        connection_type = (
            http.client.HTTPSConnection
            if base_url.scheme == "https"
            else http.client.HTTPConnection
        )
        connection: http.client.HTTPConnection | http.client.HTTPSConnection | None = None
        headers: dict[str, str] = {}
        for header in (
            "Authorization",
            "Content-Type",
            "Accept",
            "Idempotency-Key",
            "X-Request-ID",
        ):
            value = authorization if header == "Authorization" else request_headers.get(header)
            if value:
                headers[header] = value
        try:
            connection = connection_type(cast(str, base_url.hostname), base_url.port, timeout=15)
            connection.request(
                method,
                path,
                body=body if method in {"POST", "PUT", "PATCH"} else None,
                headers=headers,
            )
            response = connection.getresponse()
            payload = response.read()
            content_type = response.getheader("Content-Type", "application/json")
            return response.status, content_type, payload
        except (http.client.HTTPException, OSError, TimeoutError):
            return self._problem(HTTPStatus.SERVICE_UNAVAILABLE, "admin_api_unavailable")
        finally:
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    LOGGER.debug("Admin API proxy connection close failed")

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
                method,
                self.path,
                self.headers.get("Authorization"),
                body,
                dict(self.headers.items()),
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

        def do_PATCH(self) -> None:
            self._respond("PATCH")

        def log_message(self, _format: str, *_args: object) -> None:
            LOGGER.info("V1 internal request completed")

    return Handler


def _validated_admin_api_url(value: str) -> SplitResult:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ValueError("UAP_ADMIN_API_BASE_URL is invalid") from error
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("UAP_ADMIN_API_BASE_URL must use HTTP or HTTPS")
    if not hostname:
        raise ValueError("UAP_ADMIN_API_BASE_URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("UAP_ADMIN_API_BASE_URL must not include credentials")
    if parsed.fragment or "#" in value:
        raise ValueError("UAP_ADMIN_API_BASE_URL must not include a fragment")
    if parsed.query or "?" in value:
        raise ValueError("UAP_ADMIN_API_BASE_URL must not include a query")
    if port == 0:
        raise ValueError("UAP_ADMIN_API_BASE_URL must use a valid port")
    return parsed


def _is_allowed_bind_host(host: str) -> bool:
    if host.lower() == "localhost" or host in {"127.0.0.1", "::1"}:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.version == 4 and address.is_unspecified


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
    # Unspecified IPv4 is required inside the container; Compose publishes it
    # only on loopback. Direct local runs remain loopback-only by default.
    if not _is_allowed_bind_host(host):
        raise SystemExit("unsupported V1 internal library bind address")
    port = int(os.environ.get("UAP_V1_LIBRARY_PORT", "8091"))
    client = cast_object_client(build_client_from_environment(worker_url))
    application = Application(
        InternalLibrary(read_url, worker_url, model_url, client),
        token,
        os.environ.get("UAP_ADMIN_API_BASE_URL"),
    )
    server = ThreadingHTTPServer((host, port), make_handler(application))
    LOGGER.info("V1 internal library started host=%s port=%s", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def cast_object_client(value: Any) -> ObjectClient:
    return cast(ObjectClient, value)
