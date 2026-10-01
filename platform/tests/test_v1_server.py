from __future__ import annotations

import http.client
import json
import uuid
from io import BytesIO
from typing import Any, cast

import pytest

from uap_platform.model_governance import ModelTaskType
from uap_platform.v1 import server
from uap_platform.v1.server import Application

TOKEN = "x" * 32
DOCUMENT_ID = uuid.UUID("00000000-0000-7000-8000-000000009101")


class Library:
    def __init__(self) -> None:
        self.reanalysis: tuple[object, ...] | None = None

    def list_documents(self, **_kwargs: object) -> dict[str, object]:
        return {"items": [{"document_id": str(DOCUMENT_ID)}], "count": 1}

    def get_document(self, document_id: uuid.UUID) -> dict[str, object] | None:
        return {"document_id": str(document_id), "public_authorized": False}

    def usage(self) -> dict[str, object]:
        return {"monthly_budget_microunits": 20_000_000, "blocked": False}

    def request_reanalysis(self, *args: object) -> dict[str, object]:
        self.reanalysis = args
        return {"job_id": "job-1", "status": "queued"}


def application(library: Library) -> Application:
    return Application(library, TOKEN)  # type: ignore[arg-type]


def test_internal_api_requires_local_admin_token() -> None:
    status, _, _ = application(Library()).handle("GET", "/internal/v1/documents", None)
    assert status == 401


def test_internal_detail_is_explicitly_not_public() -> None:
    status, _, body = application(Library()).handle(
        "GET",
        f"/internal/v1/documents/{DOCUMENT_ID}",
        f"Bearer {TOKEN}",
    )
    assert status == 200
    assert json.loads(body)["public_authorized"] is False


def test_reanalysis_requires_reason_and_uses_task_contract() -> None:
    library = Library()
    status, _, body = application(library).handle(
        "POST",
        f"/internal/v1/documents/{DOCUMENT_ID}/reanalyze",
        f"Bearer {TOKEN}",
        json.dumps({"task_type": "summary", "reason": "manual quality retry"}).encode(),
    )
    assert status == 202
    assert json.loads(body)["status"] == "queued"
    assert library.reanalysis is not None
    assert library.reanalysis[1] is ModelTaskType.SUMMARY


def test_short_token_is_rejected() -> None:
    try:
        Application(Library(), "short")  # type: ignore[arg-type]
    except ValueError as error:
        assert "32" in str(error)
    else:
        raise AssertionError("short token accepted")


def test_library_handler_accepts_patch_and_preserves_request_contract() -> None:
    calls: list[tuple[str, str, dict[str, str], bytes]] = []

    class FakeApplication:
        def handle(
            self,
            method: str,
            target: str,
            authorization: str | None,
            body: bytes,
            request_headers: dict[str, str],
        ) -> tuple[int, str, bytes]:
            calls.append(
                (
                    method,
                    target,
                    {**request_headers, "Authorization": authorization or ""},
                    body,
                )
            )
            return 200, "application/json", b'{"status":"ok"}'

    handler_cls = server.make_handler(FakeApplication())  # type: ignore[arg-type]
    handler: Any = object.__new__(handler_cls)
    handler.path = "/admin/v1/documents/example/editorial?view=full"
    handler.headers = {
        "Content-Length": "15",
        "Content-Type": "application/json",
        "Authorization": "Bearer oidc-token",
        "Idempotency-Key": "request-1",
        "X-Request-ID": "correlation-1",
    }
    handler.rfile = BytesIO(b'{"expected": 1}')
    handler.wfile = BytesIO()
    sent: list[tuple[str, object]] = []
    handler.send_response = lambda code: sent.append(("status", code))
    handler.send_header = lambda name, value: sent.append((name, value))
    handler.end_headers = lambda: sent.append(("end", None))

    handler.do_PATCH()

    assert calls == [
        (
            "PATCH",
            "/admin/v1/documents/example/editorial?view=full",
            {
                "Content-Length": "15",
                "Content-Type": "application/json",
                "Authorization": "Bearer oidc-token",
                "Idempotency-Key": "request-1",
                "X-Request-ID": "correlation-1",
            },
            b'{"expected": 1}',
        )
    ]
    assert ("status", 200) in sent
    assert b'{"status":"ok"}' in handler.wfile.getvalue()


@pytest.mark.parametrize("url", ["http://admin.test", "https://admin.test"])
def test_admin_api_base_url_accepts_http_and_https(url: str) -> None:
    app = Application(cast(Any, Library()), TOKEN, url)
    assert app.admin_api_base_url == url


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/admin",
        "ftp://admin.test",
        "custom://admin.test",
        "http:///missing-host",
        "http://user:password@admin.test",
        "https://admin.test/#fragment",
        "https://admin.test/?tenant=one",
    ],
)
def test_admin_api_base_url_rejects_unsupported_or_ambiguous_urls(url: str) -> None:
    with pytest.raises(ValueError, match="UAP_ADMIN_API_BASE_URL"):
        Application(cast(Any, Library()), TOKEN, url)


def _install_fake_http_connections(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: int = 202,
    content_type: str = "application/problem+json; charset=utf-8",
    payload: bytes = b'{"error":"editorial_revision_conflict"}',
    request_error: Exception | None = None,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []

    class FakeResponse:
        def read(self) -> bytes:
            return payload

        @property
        def status(self) -> int:
            return status

        def getheader(self, name: str, default: str | None = None) -> str | None:
            return content_type if name == "Content-Type" else default

    class FakeConnection:
        def __init__(self, transport: str, host: str, port: int | None, timeout: float) -> None:
            self.transport = transport
            self.host = host
            self.port = port
            self.timeout = timeout
            self.request_record: dict[str, object] = {
                "transport": transport,
                "host": host,
                "port": port,
                "timeout": timeout,
            }
            records.append(self.request_record)

        def request(
            self,
            method: str,
            url: str,
            body: bytes | None = None,
            headers: dict[str, str] | None = None,
        ) -> None:
            self.request_record.update(
                {"method": method, "url": url, "body": body, "headers": headers or {}}
            )
            if request_error is not None:
                raise request_error

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            self.request_record["closed"] = True

    def fake_http_connection(
        host: str, port: int | None = None, timeout: float = 15
    ) -> FakeConnection:
        return FakeConnection("http", host, port, timeout)

    def fake_https_connection(
        host: str, port: int | None = None, timeout: float = 15
    ) -> FakeConnection:
        return FakeConnection("https", host, port, timeout)

    monkeypatch.setattr(http.client, "HTTPConnection", cast(Any, fake_http_connection))
    monkeypatch.setattr(http.client, "HTTPSConnection", cast(Any, fake_https_connection))
    return records


@pytest.mark.parametrize(
    ("scheme", "transport", "port"),
    [("http", "http", 8080), ("https", "https", 8443)],
)
@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH"])
def test_admin_proxy_preserves_request_contract(
    monkeypatch: pytest.MonkeyPatch, scheme: str, transport: str, port: int, method: str
) -> None:
    records = _install_fake_http_connections(monkeypatch)
    app = Application(cast(Any, Library()), TOKEN, f"{scheme}://admin.test:{port}/gateway/")
    body = b'{"expected_revision":1}'
    result = app.handle(
        method,
        "/admin/v1/documents/doc/editorial?view=full&cursor=a%2Fb",
        "Bearer oidc-token",
        body,
        {
            "Content-Type": "application/json",
            "Accept": "application/problem+json",
            "Idempotency-Key": "request-1",
            "X-Request-ID": "correlation-1",
        },
    )

    assert result == (
        202,
        "application/problem+json; charset=utf-8",
        b'{"error":"editorial_revision_conflict"}',
    )
    assert records == [
        {
            "transport": transport,
            "host": "admin.test",
            "port": port,
            "timeout": 15,
            "method": method,
            "url": "/gateway/admin/v1/documents/doc/editorial?view=full&cursor=a%2Fb",
            "body": body if method in {"POST", "PUT", "PATCH"} else None,
            "headers": {
                "Authorization": "Bearer oidc-token",
                "Content-Type": "application/json",
                "Accept": "application/problem+json",
                "Idempotency-Key": "request-1",
                "X-Request-ID": "correlation-1",
            },
            "closed": True,
        }
    ]


@pytest.mark.parametrize(
    "target",
    [
        "http://attacker.test/admin/v1/documents/doc",
        "//attacker.test/admin/v1/documents/doc",
    ],
)
def test_admin_proxy_rejects_absolute_or_authority_form_target(
    monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    records = _install_fake_http_connections(monkeypatch)
    app = Application(cast(Any, Library()), TOKEN, "https://admin.test")

    status, _, body = app.handle("GET", target, None)

    assert status == 400
    assert json.loads(body) == {"error": "invalid_request"}
    assert records == []


def test_admin_proxy_maps_connection_errors_to_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_http_connections(monkeypatch, request_error=ConnectionRefusedError())
    app = Application(cast(Any, Library()), TOKEN, "http://admin.test")

    status, _, body = app.handle("GET", "/admin/v1/documents", None)

    assert status == 503
    assert json.loads(body) == {"error": "admin_api_unavailable"}


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "0.0.0.0"])
def test_v1_bind_host_allows_loopback_and_ipv4_unspecified(host: str) -> None:
    assert server._is_allowed_bind_host(host)


@pytest.mark.parametrize("host", ["8.8.8.8", "192.168.1.20", "::"])
def test_v1_bind_host_rejects_other_addresses(host: str) -> None:
    assert not server._is_allowed_bind_host(host)
