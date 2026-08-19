"""Versioned, persistence-independent contracts for document extraction."""

from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


EXTRACTION_PAYLOAD_SCHEMA_VERSION = "extract.v1"
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ExtractionOutcome(StrEnum):
    """Terminal classification persisted in ``core.extractions``."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


def normalize_text(value: str) -> str:
    """Normalize extracted text without changing its semantic line order."""

    normalized = unicodedata.normalize("NFKC", value).replace("\u00a0", " ")
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in normalized.splitlines()]
    compact: list[str] = []
    for line in lines:
        if line:
            compact.append(line)
        elif compact and compact[-1] != "":
            compact.append("")
    while compact and compact[-1] == "":
        compact.pop()
    return "\n".join(compact)


def text_sha256(text: str) -> str:
    """Hash the UTF-8 normalized text that is written to derived storage."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ExtractionInput:
    """The immutable identity and version inputs for one extraction attempt."""

    document_version_id: uuid.UUID
    source_object_id: uuid.UUID
    media_type: str
    extractor_name: str
    extractor_version: str
    payload_schema_version: str = EXTRACTION_PAYLOAD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field_name in ("media_type", "extractor_name", "extractor_version"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} is required")
        if self.payload_schema_version != EXTRACTION_PAYLOAD_SCHEMA_VERSION:
            raise ValueError("unsupported extraction payload schema version")


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """A result that can be persisted without exposing the input bytes."""

    request: ExtractionInput
    outcome: ExtractionOutcome
    text: str = ""
    output_sha256: str | None = None
    title: str | None = None
    author: str | None = None
    language_code: str | None = None
    source_date: str | None = None
    location_map: Sequence[Mapping[str, object]] = field(default_factory=tuple)
    error_code: str | None = None
    error_summary: str | None = None

    def __post_init__(self) -> None:
        if self.outcome is ExtractionOutcome.SUCCEEDED:
            if not self.text.strip():
                raise ValueError("successful extraction requires non-empty text")
            expected = text_sha256(self.text)
            if self.output_sha256 != expected:
                raise ValueError("successful extraction hash does not match text")
            if self.error_code is not None:
                raise ValueError("successful extraction cannot have an error code")
        else:
            if self.text or self.output_sha256 is not None:
                raise ValueError("failed extraction cannot contain output text")
            if not self.error_code or not self.error_summary:
                raise ValueError("failed extraction requires an error code and summary")

        if self.output_sha256 is not None and not _HEX_SHA256.fullmatch(self.output_sha256):
            raise ValueError("output_sha256 must be a lowercase SHA-256 value")

    @property
    def payload_schema_version(self) -> str:
        return self.request.payload_schema_version

    def as_record(self) -> dict[str, object]:
        """Return a JSON-safe record for a job result or audit payload."""

        return {
            "document_version_id": str(self.request.document_version_id),
            "source_object_id": str(self.request.source_object_id),
            "media_type": self.request.media_type,
            "extractor_name": self.request.extractor_name,
            "extractor_version": self.request.extractor_version,
            "payload_schema_version": self.payload_schema_version,
            "outcome": self.outcome.value,
            "output_sha256": self.output_sha256,
            "title": self.title,
            "author": self.author,
            "language_code": self.language_code,
            "source_date": self.source_date,
            "location_map": [dict(span) for span in self.location_map],
            "error_code": self.error_code,
            "error_summary": self.error_summary,
        }


class Extractor(Protocol):
    """Common adapter surface used by the future extraction Worker handler."""

    name: str
    version: str

    def extract(self, request: ExtractionInput, payload: bytes) -> ExtractionResult: ...

