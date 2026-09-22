from __future__ import annotations

import json
import uuid
from io import BytesIO
from typing import Any, cast
from urllib.error import HTTPError
from urllib.request import Request

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


@pytest.mark.parametrize("status", [200, 401, 403, 409])
def test_patch_proxy_preserves_admin_error_status(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    class FakeResponse:
        def __init__(self, response_status: int) -> None:
            self.status = response_status
            self.headers = {"Content-Type": "application/problem+json"}

        def read(self) -> bytes:
            return b'{"error":"editorial_revision_conflict"}'

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def fake_urlopen(request: Request, timeout: int) -> FakeResponse:
        assert request.get_method() == "PATCH"
        assert request.full_url == "http://admin.test/admin/v1/documents/doc/editorial"
        assert request.data == b'{"expected_revision":1}'
        assert request.get_header("Authorization") == "Bearer oidc-token"
        assert request.get_header("Idempotency-key") == "request-1"
        assert timeout == 15
        if status == 200:
            return FakeResponse(status)
        raise HTTPError(
            request.full_url,
            status,
            "admin error",
            cast(Any, {"Content-Type": "application/problem+json"}),
            BytesIO(b'{"error":"editorial_revision_conflict"}'),
        )

    monkeypatch.setattr(server, "urlopen", fake_urlopen)
    app = Application(cast(Any, Library()), TOKEN, "http://admin.test")
    result = app.handle(
        "PATCH",
        "/admin/v1/documents/doc/editorial",
        "Bearer oidc-token",
        b'{"expected_revision":1}',
        {
            "Content-Type": "application/json",
            "Idempotency-Key": "request-1",
        },
    )
    assert result[0] == status
    assert result[1] == "application/problem+json"
    assert json.loads(result[2]) == {"error": "editorial_revision_conflict"}
