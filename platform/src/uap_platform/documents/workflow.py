"""Validate extract-document payloads and dispatch the versioned adapters."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from .contracts import ExtractionInput, ExtractionResult, Extractor
from .html import HtmlExtractor
from .pdf import PdfExtractor
from .persistence import PostgresExtractionStore
from .subtitles import SrtExtractor, WebVttExtractor

_ALLOWED_FIELDS = frozenset(
    {
        "document_version_id",
        "source_object_id",
        "media_type",
        "extractor_name",
        "extractor_version",
        "payload_schema_version",
    }
)


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"extract payload field {key} is required")
    return value.strip()


def _required_uuid(payload: Mapping[str, object], key: str) -> uuid.UUID:
    value = _required_string(payload, key)
    try:
        return uuid.UUID(value)
    except ValueError as error:
        raise ValueError(f"extract payload field {key} is not a UUID") from error


def _extractor_for(name: str, media_type: str) -> Extractor:
    base_media_type = media_type.split(";", 1)[0].strip().casefold()
    if name == HtmlExtractor.name:
        return HtmlExtractor()
    if name == PdfExtractor.name:
        return PdfExtractor()
    if name == WebVttExtractor.name and base_media_type in {"text/vtt", "text/webvtt"}:
        return WebVttExtractor()
    if name == SrtExtractor.name and base_media_type in {
        "application/x-subrip",
        "application/srt",
        "text/srt",
    }:
        return SrtExtractor()
    raise ValueError("extractor does not support the requested media type")


def build_extraction_request(payload: Mapping[str, object]) -> tuple[ExtractionInput, Extractor]:
    """Validate a durable-job payload and choose its immutable adapter version."""

    unknown = set(payload) - _ALLOWED_FIELDS
    if unknown:
        raise ValueError(f"extract payload contains unknown fields: {sorted(unknown)}")
    schema_version = _required_string(payload, "payload_schema_version")
    if schema_version != "extract.v1":
        raise ValueError("unsupported extraction payload schema version")
    media_type = _required_string(payload, "media_type")
    extractor_name = _required_string(payload, "extractor_name")
    extractor_version = _required_string(payload, "extractor_version")
    extractor = _extractor_for(extractor_name, media_type)
    if extractor.version != extractor_version:
        raise ValueError("extractor version is not supported")
    request = ExtractionInput(
        document_version_id=_required_uuid(payload, "document_version_id"),
        source_object_id=_required_uuid(payload, "source_object_id"),
        media_type=media_type,
        extractor_name=extractor_name,
        extractor_version=extractor_version,
        payload_schema_version=schema_version,
    )
    return request, extractor


class ExtractionJobHandler:
    """Run one claimed ``extract_document`` job through the atomic store boundary."""

    def __init__(self, store: PostgresExtractionStore) -> None:
        self._store = store

    def handle(
        self,
        job_id: uuid.UUID,
        job_attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
        payload: Mapping[str, object],
    ) -> tuple[uuid.UUID, ExtractionResult]:
        request, extractor = build_extraction_request(payload)
        return self._store.run_and_finish_job(
            job_id,
            job_attempt_id,
            lease_token,
            request,
            extractor,
        )


def payload_from_claim(claim: tuple[Any, ...]) -> Mapping[str, object]:
    """Return the JSON payload column from the WP4 claim tuple."""

    if len(claim) < 5 or not isinstance(claim[3], Mapping):
        raise ValueError("extract job claim does not contain a JSON object payload")
    return claim[3]
