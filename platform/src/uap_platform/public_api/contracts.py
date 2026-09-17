"""Strict public DTOs frozen by the WP10 API contract."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DocumentCategory(StrEnum):
    OFFICIAL_REPORT = "official_report"
    GOVERNMENT_DOCUMENT = "government_document"
    MILITARY = "military"
    SCIENTIFIC_RESEARCH = "scientific_research"
    HISTORICAL_EVENT = "historical_event"
    SIGHTING = "sighting"
    DISPUTED_EVENT = "disputed_event"
    OTHER = "other"


class FactStatus(StrEnum):
    OFFICIAL_RECORD = "official_record"
    CORROBORATED = "corroborated"
    SOURCE_REPORTED = "source_reported"
    UNVERIFIED = "unverified"
    DISPUTED = "disputed"
    OPINION = "opinion"


class EntityType(StrEnum):
    PERSON = "person"
    ORGANIZATION = "organization"
    LOCATION = "location"
    EVENT = "event"
    OBJECT = "object"
    CONCEPT = "concept"


class LocatorType(StrEnum):
    TEXT = "text"
    HTML = "html"
    PDF = "pdf"
    VIDEO = "video"
    AUDIO = "audio"


class ClaimType(StrEnum):
    OBSERVATION = "observation"
    ATTRIBUTION = "attribution"
    EVENT = "event"
    ASSESSMENT = "assessment"
    OTHER = "other"


class AssertionStatus(StrEnum):
    REPORTED = "reported"
    CORROBORATED = "corroborated"
    DISPUTED = "disputed"
    UNVERIFIED = "unverified"
    FALSE = "false"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def validate_public_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("public URL must be an absolute safe HTTP(S) URL")
    return value


class PublicSource(StrictModel):
    name: str = Field(min_length=1)
    url: str

    _url = field_validator("url")(validate_public_url)


class DocumentSummary(StrictModel):
    id: UUID
    slug: str = Field(min_length=1)
    title: str = Field(min_length=1)
    summary: str | None
    category: DocumentCategory
    fact_status: FactStatus
    source: PublicSource
    source_published_at: datetime | None
    published_at: datetime
    revised_at: datetime | None
    revision: int = Field(ge=1)


class PublicEvidence(StrictModel):
    id: UUID
    excerpt: str = Field(min_length=1, max_length=2000)
    locator_type: LocatorType
    page_start: int | None
    page_end: int | None
    time_start_ms: int | None
    time_end_ms: int | None
    locator: dict[str, object]
    source_url: str

    _source_url = field_validator("source_url")(validate_public_url)


class ClaimDetail(StrictModel):
    id: UUID
    document_id: UUID
    ordinal: int = Field(ge=0)
    text: str = Field(min_length=1)
    type: ClaimType
    assertion_status: AssertionStatus
    attribution: str | None
    revision: int = Field(ge=1)
    evidence: list[PublicEvidence] = Field(min_length=1)


class EntitySummary(StrictModel):
    id: UUID
    slug: str = Field(min_length=1)
    type: EntityType
    name: str = Field(min_length=1)
    description: str | None
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    revision: int = Field(ge=1)


class DocumentDetail(DocumentSummary):
    claims: list[ClaimDetail]
    related_entities: list[EntitySummary]


class EntityDetail(EntitySummary):
    related_documents: list[DocumentSummary]


class DocumentPage(StrictModel):
    items: list[DocumentSummary]
    next_cursor: str | None


class EntityPage(StrictModel):
    items: list[EntitySummary]
    next_cursor: str | None


class SearchHit(StrictModel):
    document: DocumentSummary
    highlights: list[str]


class SearchPage(StrictModel):
    items: list[SearchHit]
    next_cursor: str | None


class Problem(StrictModel):
    type: str
    title: str
    status: int
    code: str
    request_id: UUID
    detail: str | None
