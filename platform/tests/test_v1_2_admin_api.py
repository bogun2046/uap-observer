"""V1-2.1B Admin API contract and route tests without a live model provider."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from uap_platform.admin_api.contracts import (
    AuditHistoryPage,
    DocumentDetail,
    DocumentListPage,
    EditorialPatchRequest,
    EditorialRevisionDetail,
    EditorialRevisionPage,
    TrashDocumentPage,
    WriteResult,
)
from uap_platform.admin_api.handler import AdminApiApplication

DOCUMENT_ID = UUID("00000000-0000-7000-8000-000000000001")
VERSION_ID = UUID("00000000-0000-7000-8000-000000000002")
PRINCIPAL_ID = UUID("00000000-0000-7000-8000-000000000003")


class FakeOidc:
    def validate(self, token: str) -> str:
        if token == "editorial-token":
            return "editorial-admin"
        raise ValueError("invalid token")


class FakeEditorialService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def resolve_principal(self, issuer: str, subject: str) -> UUID:
        assert issuer == "https://issuer.test"
        assert subject == "editorial-admin"
        return PRINCIPAL_ID

    def get_document_detail(self, principal_id: UUID, document_id: UUID) -> DocumentDetail | None:
        self.calls.append(("detail", {"principal_id": principal_id, "document_id": document_id}))
        return None

    def list_trash_documents(self, **kwargs: Any) -> TrashDocumentPage:
        self.calls.append(("trash-list", kwargs))
        return TrashDocumentPage(items=[], next_cursor=None)

    def list_documents(self, **kwargs: Any) -> DocumentListPage:
        self.calls.append(("document-list", kwargs))
        return DocumentListPage(items=[], next_cursor=None)

    def list_document_audit(self, **kwargs: Any) -> AuditHistoryPage:
        self.calls.append(("audit", kwargs))
        return AuditHistoryPage(items=[], next_cursor=None)

    def list_editorial_revisions(self, **kwargs: Any) -> EditorialRevisionPage:
        self.calls.append(("revision-list", kwargs))
        return EditorialRevisionPage(items=[], next_cursor=None)

    def get_editorial_revision(self, **kwargs: Any) -> EditorialRevisionDetail | None:
        self.calls.append(("revision-detail", kwargs))
        return None

    def restore_editorial_revision(self, **kwargs: Any) -> WriteResult:
        self.calls.append(("revision-restore", kwargs))
        return WriteResult(
            operation="editorial.revision_restore",
            resource_id=uuid4(),
            request_id=kwargs["request_id"],
            publication=None,
        )

    def save_editorial(self, **kwargs: Any) -> WriteResult:
        self.calls.append(("save", kwargs))
        return WriteResult(
            operation="editorial.save",
            resource_id=uuid4(),
            request_id=kwargs["request_id"],
            publication=None,
        )

    def adopt_editorial(self, **kwargs: Any) -> WriteResult:
        self.calls.append(("adopt", kwargs))
        return WriteResult(
            operation="editorial.adopt",
            resource_id=uuid4(),
            request_id=kwargs["request_id"],
            publication=None,
        )

    def request_editorial_reanalysis(self, **kwargs: Any) -> WriteResult:
        self.calls.append(("reanalyze", kwargs))
        return WriteResult(
            operation="reanalysis.request",
            resource_id=uuid4(),
            request_id=kwargs["request_id"],
            publication=None,
        )

    def trash_document(self, **kwargs: Any) -> WriteResult:
        self.calls.append(("trash", kwargs))
        return WriteResult(
            operation="document.trash",
            resource_id=uuid4(),
            request_id=kwargs["request_id"],
            publication=None,
        )

    def restore_document(self, **kwargs: Any) -> WriteResult:
        self.calls.append(("restore", kwargs))
        return WriteResult(
            operation="document.restore",
            resource_id=uuid4(),
            request_id=kwargs["request_id"],
            publication=None,
        )


def _app(service: FakeEditorialService) -> AdminApiApplication:
    return AdminApiApplication(service, FakeOidc(), "https://issuer.test")  # type: ignore[arg-type]


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer editorial-token", "Idempotency-Key": str(uuid4())}


def test_patch_preserves_omitted_fields_and_accepts_explicit_clear() -> None:
    omitted = EditorialPatchRequest.model_validate(
        {"document_version_id": VERSION_ID, "expected_revision": 0, "title": "Edited"}
    )
    assert omitted.changes() == {"title": "Edited"}
    explicit_clear = EditorialPatchRequest.model_validate(
        {"document_version_id": VERSION_ID, "expected_revision": 0, "summary": None}
    )
    assert explicit_clear.changes() == {"summary": None}
    with pytest.raises(ValidationError):
        EditorialPatchRequest.model_validate(
            {"document_version_id": VERSION_ID, "expected_revision": 0, "unknown": "x"}
        )


def test_editorial_routes_require_idempotency_and_preserve_document_identity() -> None:
    service = FakeEditorialService()
    app = _app(service)
    body = {
        "document_version_id": str(VERSION_ID),
        "expected_revision": 0,
        "title": "新标题",
    }
    response = app.handle(
        "PATCH",
        f"/admin/v1/documents/{DOCUMENT_ID}/editorial",
        _headers(),
        json.dumps(body).encode(),
    )
    assert response.status == 200
    assert service.calls[-1][0] == "save"
    assert service.calls[-1][1]["document_id"] == DOCUMENT_ID
    assert service.calls[-1][1]["request"].changes() == {"title": "新标题"}
    missing_key = app.handle(
        "POST",
        f"/admin/v1/documents/{DOCUMENT_ID}/trash",
        {"Authorization": "Bearer editorial-token"},
        b'{"expected_revision":0}',
    )
    assert missing_key.status == 400


def test_all_editorial_write_routes_are_wired_without_publication_payload() -> None:
    service = FakeEditorialService()
    app = _app(service)
    requests = [
        (
            "POST",
            f"/admin/v1/documents/{DOCUMENT_ID}/editorial/adopt",
            {
                "document_version_id": str(VERSION_ID),
                "expected_revision": 0,
                "source_analysis_result_id": str(uuid4()),
                "fields": ["summary"],
                "values": {"summary": "x"},
            },
            "adopt",
        ),
        (
            "POST",
            f"/admin/v1/documents/{DOCUMENT_ID}/reanalyze",
            {"document_version_id": str(VERSION_ID), "task_type": "summary", "reason": "verify"},
            "reanalyze",
        ),
        (
            "POST",
            f"/admin/v1/documents/{DOCUMENT_ID}/trash",
            {"expected_revision": 0, "reason": "remove from active list"},
            "trash",
        ),
        (
            "POST",
            f"/admin/v1/documents/{DOCUMENT_ID}/restore",
            {"expected_revision": 1, "reason": "return to internal list"},
            "restore",
        ),
    ]
    for method, path, payload, operation in requests:
        response = app.handle(method, path, _headers(), json.dumps(payload).encode())
        assert response.status == 200
        assert service.calls[-1][0] == operation
        assert json.loads(response.body)["publication"] is None


def test_document_detail_and_trash_are_editorial_routes() -> None:
    service = FakeEditorialService()
    app = _app(service)
    detail = app.handle("GET", f"/admin/v1/documents/{DOCUMENT_ID}", _headers())
    trash = app.handle("GET", "/admin/v1/documents/trash?limit=10", _headers())
    audit = app.handle("GET", f"/admin/v1/documents/{DOCUMENT_ID}/audit?limit=10", _headers())
    assert detail.status == 404
    assert trash.status == 200
    assert audit.status == 200


def test_editorial_revision_history_routes_preserve_document_identity() -> None:
    service = FakeEditorialService()
    app = _app(service)
    history = app.handle(
        "GET", f"/admin/v1/documents/{DOCUMENT_ID}/editorial/revisions?limit=10", _headers()
    )
    assert history.status == 200
    assert service.calls[-1][0] == "revision-list"
    assert service.calls[-1][1]["document_id"] == DOCUMENT_ID
    detail = app.handle(
        "GET",
        f"/admin/v1/documents/{DOCUMENT_ID}/editorial/revisions/3",
        _headers(),
    )
    assert detail.status == 404
    restore = app.handle(
        "POST",
        f"/admin/v1/documents/{DOCUMENT_ID}/editorial/revisions/1/restore",
        _headers(),
        json.dumps({"expected_revision": 3, "reason": "restore prior editorial"}).encode(),
    )
    assert restore.status == 200
    assert service.calls[-1][0] == "revision-restore"
    assert service.calls[-1][1]["document_id"] == DOCUMENT_ID
    assert service.calls[-1][1]["revision_ref"] == "1"


def test_document_list_is_oidc_read_projection_and_excludes_unknown_query() -> None:
    service = FakeEditorialService()
    app = _app(service)
    listed = app.handle("GET", "/admin/v1/documents?q=reddit&limit=10", _headers())
    assert listed.status == 200
    assert service.calls[-1][0] == "document-list"
    assert service.calls[-1][1]["query"] == "reddit"
    invalid = app.handle("GET", "/admin/v1/documents?state=trash", _headers())
    assert invalid.status == 422


def test_unauthenticated_editorial_request_is_rejected() -> None:
    service = FakeEditorialService()
    app = _app(service)
    response = app.handle("GET", f"/admin/v1/documents/{DOCUMENT_ID}")
    assert response.status == 401
