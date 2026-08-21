"""Frozen dataclasses for WP8.2 extraction anchors and locator mapping."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

LocatorType = Literal["text", "html", "pdf", "video", "audio"]
DuplicatePolicy = Literal["claim", "entity"]


class AnchorStatus(StrEnum):
    MATCHED = "matched"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    MISMATCH = "mismatch"


class MappingClass(StrEnum):
    EMPTY_VALID = "empty_valid_result"
    MATERIALIZABLE = "materializable"
    TERMINAL_UNMAPPABLE = "terminal_unmappable"
    TERMINAL_EXTRACTION_MISSING = "knowledge_extraction_missing"
    TERMINAL_EXTRACTION_AMBIGUOUS = "knowledge_extraction_ambiguous"
    TERMINAL_EXTRACTION_MISMATCH = "knowledge_extraction_mismatch"


@dataclass(frozen=True, slots=True)
class ExtractionRecord:
    """One extraction row plus the derived stored-object identity used to count matches."""

    extraction_id: uuid.UUID
    document_version_id: uuid.UUID
    outcome: str
    output_sha256: str
    stored_domain: str
    stored_sha256: str
    extractor_name: str = ""
    extractor_version: str = ""


@dataclass(frozen=True, slots=True)
class ExtractionAnchor:
    status: AnchorStatus
    extraction_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class SourceLocator:
    locator_type: LocatorType
    start: int
    end: int
    page_start: int | None = None
    page_end: int | None = None
    time_start_ms: int | None = None
    time_end_ms: int | None = None

    def identity(self) -> tuple[object, ...]:
        """WP7 source-locator identity used for claim-level duplicate detection."""

        return (
            self.locator_type,
            self.start,
            self.end,
            self.page_start,
            self.page_end,
            self.time_start_ms,
            self.time_end_ms,
        )

    def source_locator_fields(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "locator_type": self.locator_type,
            "start": self.start,
            "end": self.end,
        }
        if self.page_start is not None:
            payload["page_start"] = self.page_start
        if self.page_end is not None:
            payload["page_end"] = self.page_end
        if self.time_start_ms is not None:
            payload["time_start_ms"] = self.time_start_ms
        if self.time_end_ms is not None:
            payload["time_end_ms"] = self.time_end_ms
        return payload


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    ordinal: int
    locators: tuple[SourceLocator, ...]


@dataclass(frozen=True, slots=True)
class TypedAxes:
    char_start: int | None
    char_end: int | None
    page_start: int | None
    page_end: int | None
    time_start_ms: int | None
    time_end_ms: int | None


@dataclass(frozen=True, slots=True)
class AcceptedLocator:
    locator_ordinal: int
    evidence_text: str
    envelope: dict[str, object]
    digest: str
    axes: TypedAxes


@dataclass(frozen=True, slots=True)
class RejectedLocator:
    locator_ordinal: int
    reason_code: str


@dataclass(frozen=True, slots=True)
class AcceptedCandidate:
    ordinal: int
    accepted_locators: tuple[AcceptedLocator, ...]
    rejected_locators: tuple[RejectedLocator, ...]


@dataclass(frozen=True, slots=True)
class RejectedCandidate:
    ordinal: int
    reason_code: str
    rejected_locators: tuple[RejectedLocator, ...]


@dataclass(frozen=True, slots=True)
class MappingReport:
    classification: MappingClass
    anchor: ExtractionAnchor
    accepted_candidates: tuple[AcceptedCandidate, ...]
    rejected_candidates: tuple[RejectedCandidate, ...]

    @property
    def rejected_locator_count(self) -> int:
        accepted = sum(len(item.rejected_locators) for item in self.accepted_candidates)
        rejected = sum(len(item.rejected_locators) for item in self.rejected_candidates)
        return accepted + rejected

    def reason_codes(self) -> tuple[str, ...]:
        codes: list[str] = []
        for accepted in self.accepted_candidates:
            codes.extend(item.reason_code for item in accepted.rejected_locators)
        for rejected in self.rejected_candidates:
            codes.extend(item.reason_code for item in rejected.rejected_locators)
            codes.append(rejected.reason_code)
        return tuple(codes)


LocationMap = Sequence[Mapping[str, object]]
