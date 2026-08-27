from __future__ import annotations

import uuid
from typing import cast

import pytest

from uap_platform.documents import (
    ExtractionInput,
    ExtractionJobHandler,
    PdfExtractor,
    SrtExtractor,
    WebVttExtractor,
    build_extraction_request,
    payload_from_claim,
)
from uap_platform.documents.persistence import PostgresExtractionStore

DOCUMENT_VERSION_ID = uuid.UUID("00000000-0000-7000-8000-000000000501")
SOURCE_OBJECT_ID = uuid.UUID("00000000-0000-7000-8000-000000000502")


def payload(
    media_type: str = "text/vtt",
    extractor_name: str = WebVttExtractor.name,
) -> dict[str, object]:
    return {
        "document_version_id": str(DOCUMENT_VERSION_ID),
        "source_object_id": str(SOURCE_OBJECT_ID),
        "media_type": media_type,
        "extractor_name": extractor_name,
        "extractor_version": "1.0.0",
        "payload_schema_version": "extract.v1",
    }


def test_build_request_dispatches_versioned_adapters() -> None:
    request, extractor = build_extraction_request(payload())
    assert isinstance(request, ExtractionInput)
    assert isinstance(extractor, WebVttExtractor)

    _, pdf_extractor = build_extraction_request(
        payload("application/pdf", PdfExtractor.name)
    )
    _, srt_extractor = build_extraction_request(
        payload("text/srt", SrtExtractor.name)
    )
    assert isinstance(pdf_extractor, PdfExtractor)
    assert isinstance(srt_extractor, SrtExtractor)


def test_build_request_rejects_bad_schema_version_and_media_pair() -> None:
    bad_schema = payload()
    bad_schema["payload_schema_version"] = "extract.v0"
    with pytest.raises(ValueError, match="unsupported extraction payload schema"):
        build_extraction_request(bad_schema)

    with pytest.raises(ValueError, match="does not support"):
        build_extraction_request(payload("application/pdf"))

    extra = payload()
    extra["unexpected"] = "reject"
    with pytest.raises(ValueError, match="unknown fields"):
        build_extraction_request(extra)


def test_payload_from_claim_requires_json_object() -> None:
    claim = (uuid.uuid4(), uuid.uuid4(), "extract_document", payload(), "extract.v1")
    assert payload_from_claim(claim) == payload()
    with pytest.raises(ValueError, match="JSON object"):
        payload_from_claim((uuid.uuid4(), uuid.uuid4(), "extract_document", [], "extract.v1"))


def test_job_handler_delegates_atomic_run_and_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[object] = []

    class FakeStore:
        def run_and_finish_job(self, *args: object) -> tuple[uuid.UUID, object]:
            captured.extend(args)
            return uuid.UUID("00000000-0000-7000-8000-000000000503"), object()

    result = ExtractionJobHandler(cast(PostgresExtractionStore, FakeStore())).handle(
        uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), payload()
    )
    assert result[0] == uuid.UUID("00000000-0000-7000-8000-000000000503")
    assert len(captured) == 5
