from __future__ import annotations

import http.client
import json
import uuid
from http import HTTPStatus
from typing import Any, cast

import pytest

from uap_platform.model_governance import ModelTaskType
from uap_platform.v1 import server
from uap_platform.v1.server import Application

TOKEN = "t" * 32
DOCUMENT_ID = uuid.UUID("00000000-0000-7000-8000-000000009201")


class FakeLibrary:
    def __init__(self) -> None:
        self.list_args: dict[str, object] | None = None
        self.reanalysis: tuple[object, ...] | None = None
        self.document: dict[str, object] | None = {"id": str(DOCUMENT_ID)}
        self.error: Exception | None = None

    def list_documents(self, **kwargs: object) -> dict[str, object]:
        self.list_args = kwargs
        if self.error:
            raise self.error
        return {"items": [str(DOCUMENT_ID)]}

    def get_document(self, _document_id: uuid.UUID) -> dict[str, object] | None:
        if self.error:
            raise self.error
        return self.document

    def usage(self) -> dict[str, object]:
        if self.error:
            raise self.error
        return {"blocked": False}

    def request_reanalysis(self, *args: object) -> dict[str, object]:
        if self.error:
            raise self.error
        self.reanalysis = args
        return {"status": "queued"}


def _app(library: FakeLibrary, admin_url: str | None = None) -> Application:
    return Application(cast(Any, library), TOKEN, admin_url)


def _body(result: tuple[int, str, bytes]) -> dict[str, object]:
    return cast(dict[str, object], json.loads(result[2]))


def test_public_shell_health_and_internal_list_routes() -> None:
    library = FakeLibrary()
    app = _app(library)

    shell = app.handle("GET", "/", None)
    assert shell[0] == HTTPStatus.OK
    assert shell[1] == "text/html; charset=utf-8"
    assert b"<!doctype html" in shell[2].lower()
    assert app.handle("GET", "/healthz", None) == (
        HTTPStatus.OK,
        "application/json",
        b'{"status":"ok"}',
    )

    listed = app.handle(
        "GET", "/internal/v1/documents?q=report&limit=17", f"Bearer {TOKEN}"
    )
    assert listed[0] == HTTPStatus.OK
    assert library.list_args == {"query": "report", "limit": 17}
    assert _body(app.handle("GET", "/internal/v1/usage", f"Bearer {TOKEN}")) == {
        "blocked": False
    }


@pytest.mark.parametrize(
    ("method", "target", "body", "expected_status", "expected_code"),
    [
        (
            "GET",
            "/internal/v1/documents/not-a-uuid",
            b"",
            HTTPStatus.BAD_REQUEST,
            "invalid_request",
        ),
        ("GET", "/internal/v1/documents?limit=no", b"", HTTPStatus.BAD_REQUEST, "invalid_request"),
        (
            "POST",
            f"/internal/v1/documents/{DOCUMENT_ID}/reanalyze",
            b"{",
            HTTPStatus.BAD_REQUEST,
            "invalid_request",
        ),
        (
            "POST",
            f"/internal/v1/documents/{DOCUMENT_ID}/reanalyze",
            b'{"task_type":"unknown"}',
            HTTPStatus.BAD_REQUEST,
            "invalid_request",
        ),
    ],
)
def test_malformed_internal_requests_fail_as_bad_request(
    method: str, target: str, body: bytes, expected_status: int, expected_code: str
) -> None:
    result = _app(FakeLibrary()).handle(method, target, f"Bearer {TOKEN}", body)
    assert result[0] == expected_status
    assert _body(result) == {"error": expected_code}


def test_missing_document_unknown_route_and_reanalysis_arguments() -> None:
    library = FakeLibrary()
    library.document = None
    app = _app(library)
    assert app.handle("GET", f"/internal/v1/documents/{DOCUMENT_ID}", f"Bearer {TOKEN}")[0] == 404
    assert app.handle("DELETE", "/internal/v1/nope", f"Bearer {TOKEN}")[0] == 404

    library.document = {"id": str(DOCUMENT_ID)}
    accepted = app.handle(
        "POST",
        f"/internal/v1/documents/{DOCUMENT_ID}/reanalyze",
        f"Bearer {TOKEN}",
        b'{"task_type":"summary","reason":"manual retry"}',
    )
    assert accepted[0] == HTTPStatus.ACCEPTED
    assert library.reanalysis is not None
    assert library.reanalysis[:3] == (
        DOCUMENT_ID,
        ModelTaskType.SUMMARY,
        "manual retry",
    )
    assert isinstance(library.reanalysis[3], uuid.UUID)


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (LookupError("document unavailable"), HTTPStatus.CONFLICT, "document unavailable"),
        (RuntimeError("budget blocked"), HTTPStatus.PAYMENT_REQUIRED, "budget blocked"),
        (OSError("database secret"), HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error"),
    ],
)
def test_internal_library_errors_are_mapped(
    error: Exception, expected_status: int, expected_code: str
) -> None:
    library = FakeLibrary()
    library.error = error
    result = _app(library).handle("GET", "/internal/v1/usage", f"Bearer {TOKEN}")
    assert result[0] == expected_status
    assert _body(result) == {"error": expected_code}


def test_admin_proxy_is_configured_before_local_auth_and_maps_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unconfigured = _app(FakeLibrary()).handle("GET", "/admin/v1/cases", None)
    assert unconfigured[0] == HTTPStatus.SERVICE_UNAVAILABLE
    assert _body(unconfigured) == {"error": "admin_api_unconfigured"}

    class FailConnection:
        def __init__(
            self, _host: str, _port: int | None = None, *, timeout: float | None = None
        ) -> None:
            assert timeout == 15

        def request(self, *_args: object, **_kwargs: object) -> None:
            raise ConnectionRefusedError("offline")

        def close(self) -> None:
            return None

    monkeypatch.setattr(http.client, "HTTPConnection", cast(Any, FailConnection))
    unavailable = _app(FakeLibrary(), "http://admin.test/").handle(
        "GET", "/admin/v1/cases", None
    )
    assert unavailable[0] == HTTPStatus.SERVICE_UNAVAILABLE
    assert _body(unavailable) == {"error": "admin_api_unavailable"}


def test_main_requires_configuration_and_rejects_non_loopback_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "UAP_V1_READ_DATABASE_URL",
        "UAP_V1_WORKER_DATABASE_URL",
        "UAP_V1_MODEL_DATABASE_URL",
        "UAP_V1_LOCAL_ADMIN_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit, match="are required"):
        server.main()

    monkeypatch.setenv("UAP_V1_READ_DATABASE_URL", "postgresql://reader@db/read")
    monkeypatch.setenv("UAP_V1_WORKER_DATABASE_URL", "postgresql://worker@db/work")
    monkeypatch.setenv("UAP_V1_MODEL_DATABASE_URL", "postgresql://model@db/model")
    monkeypatch.setenv("UAP_V1_LOCAL_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("UAP_V1_LIBRARY_HOST", "192.0.2.10")
    with pytest.raises(SystemExit, match="unsupported V1 internal library bind address"):
        server.main()


def test_main_builds_server_and_closes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "UAP_V1_READ_DATABASE_URL": "postgresql://reader@db/read",
        "UAP_V1_WORKER_DATABASE_URL": "postgresql://worker@db/work",
        "UAP_V1_MODEL_DATABASE_URL": "postgresql://model@db/model",
        "UAP_V1_LOCAL_ADMIN_TOKEN": TOKEN,
        "UAP_V1_LIBRARY_HOST": "0.0.0.0",
        "UAP_V1_LIBRARY_PORT": "8099",
        "UAP_ADMIN_API_BASE_URL": "https://admin.test/",
    }.items():
        monkeypatch.setenv(name, value)

    class FakeServer:
        address: tuple[str, int] | None = None
        handler: type[object] | None = None
        closed = False

        def __init__(self, address: tuple[str, int], handler: type[object]) -> None:
            self.address = address
            self.handler = handler

        def serve_forever(self) -> None:
            return None

        def server_close(self) -> None:
            self.closed = True

    instances: list[FakeServer] = []

    def make_server(address: tuple[str, int], handler: type[object]) -> FakeServer:
        instance = FakeServer(address, handler)
        instances.append(instance)
        return instance

    monkeypatch.setattr(server, "ThreadingHTTPServer", make_server)
    monkeypatch.setattr(server, "build_client_from_environment", lambda _url: object())
    monkeypatch.setattr(server, "InternalLibrary", lambda *args: FakeLibrary())

    server.main()

    assert len(instances) == 1
    assert instances[0].address == ("0.0.0.0", 8099)
    assert instances[0].handler is not None
    assert instances[0].closed is True
