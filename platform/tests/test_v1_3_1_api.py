"""V1-3.1 publication review API/UI contract tests without provider calls."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from uap_platform.admin_api.contracts import (
    PublicationReviewRequest,
    PublicationStatus,
    WriteResult,
)
from uap_platform.admin_api.handler import AdminApiApplication

DOCUMENT_ID = UUID("00000000-0000-7300-8000-000000000001")
VERSION_ID = UUID("00000000-0000-7300-8000-000000000002")
REVISION_ID = UUID("00000000-0000-7300-8000-000000000003")
PRINCIPAL_ID = UUID("00000000-0000-7300-8000-000000000004")


class FakeOidc:
    def validate(self, token: str) -> str:
        if token != "editorial-token":
            raise ValueError("invalid token")
        return "editorial-admin"


class FakePublicationService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def resolve_principal(self, issuer: str, subject: str) -> UUID:
        assert issuer == "https://issuer.test"
        assert subject == "editorial-admin"
        return PRINCIPAL_ID

    def get_publication_status(self, **kwargs: Any) -> PublicationStatus:
        self.calls.append(("publication-status", kwargs))
        return PublicationStatus(
            document_id=DOCUMENT_ID,
            document_version_id=VERSION_ID,
            current_editorial_revision_id=REVISION_ID,
            current_editorial_revision_no=4,
            selected_editorial_revision_id=None,
            selected_editorial_revision_no=None,
            eligibility="eligible",
            status="NOT_SUBMITTED",
            review_case_id=None,
            review_case_status=None,
            decision_status=None,
            grant_id=None,
            grant_status=None,
            publication_sequence=None,
            manifest_id=None,
            manifest_hash=None,
            outbox_event_id=None,
            outbox_status=None,
            public_visible=False,
        )

    def submit_publication_review(self, **kwargs: Any) -> WriteResult:
        self.calls.append(("publication-submit", kwargs))
        return WriteResult(
            operation="publication.review.submit",
            resource_id=uuid4(),
            request_id=kwargs["request_id"],
            publication=None,
        )


def _app(service: FakePublicationService) -> AdminApiApplication:
    return AdminApiApplication(service, FakeOidc(), "https://issuer.test")  # type: ignore[arg-type]


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer editorial-token", "Idempotency-Key": str(uuid4())}


def test_publication_request_binds_exact_revision_and_rejects_unknown_fields() -> None:
    request = PublicationReviewRequest.model_validate(
        {
            "document_version_id": VERSION_ID,
            "editorial_revision_id": REVISION_ID,
            "editorial_revision_no": 4,
            "expected_revision": 4,
            "reason": "submit the selected revision for publication review",
        }
    )
    assert request.editorial_revision_no == request.expected_revision == 4
    with pytest.raises(ValidationError):
        PublicationReviewRequest.model_validate(
            {
                "document_version_id": VERSION_ID,
                "editorial_revision_id": REVISION_ID,
                "editorial_revision_no": 4,
                "expected_revision": 4,
                "reason": "submit the selected revision for publication review",
                "grant": True,
            }
        )


def test_status_read_and_review_submission_routes_are_wired() -> None:
    service = FakePublicationService()
    app = _app(service)
    status = app.handle(
        "GET",
        f"/admin/v1/documents/{DOCUMENT_ID}/publication",
        _headers(),
    )
    assert status.status == 200
    assert json.loads(status.body)["eligibility"] == "eligible"
    submitted = app.handle(
        "POST",
        f"/admin/v1/documents/{DOCUMENT_ID}/publication-review",
        _headers(),
        json.dumps(
            {
                "document_version_id": str(VERSION_ID),
                "editorial_revision_id": str(REVISION_ID),
                "editorial_revision_no": 4,
                "expected_revision": 4,
                "reason": "submit the selected revision for publication review",
            }
        ).encode(),
    )
    assert submitted.status == 200
    assert service.calls[-1][0] == "publication-submit"
    assert service.calls[-1][1]["document_id"] == DOCUMENT_ID


def test_submission_does_not_expose_direct_publication_route() -> None:
    html = (Path(__file__).parents[1] / "src/uap_platform/v1/library.html").read_text(
        encoding="utf-8"
    )
    assert "PUBLICATION STATUS" in html
    assert "Submit revision" in html
    assert "publication-review" in html
    assert "publish-now" not in html
    assert "prompt(" not in html
    assert "confirm(" not in html
