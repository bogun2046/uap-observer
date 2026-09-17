from __future__ import annotations

import json
import uuid

from uap_platform.model_governance import ModelTaskType
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
