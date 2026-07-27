"""Typed domain models shared by collectors, AI processing, and persistence."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class StringEnum(str, Enum):
    """Python 3.9-compatible string enum."""

    def __str__(self) -> str:
        return self.value


class NewsCategory(StringEnum):
    OFFICIAL_REPORT = "official_report"
    GOVERNMENT_DOCUMENT = "government_document"
    MILITARY = "military"
    SCIENTIFIC_RESEARCH = "scientific_research"
    HISTORICAL_EVENT = "historical_event"
    SIGHTING = "sighting"
    DISPUTED_EVENT = "disputed_event"
    OTHER = "other"


class FactStatus(StringEnum):
    OFFICIAL_RECORD = "official_record"
    CORROBORATED = "corroborated"
    SOURCE_REPORTED = "source_reported"
    UNVERIFIED = "unverified"
    DISPUTED = "disputed"
    OPINION = "opinion"


class ProcessingStatus(StringEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class EventStatus(StringEnum):
    OFFICIAL_RECORD = "official_record"
    CORROBORATED = "corroborated"
    UNVERIFIED = "unverified"
    DISPUTED = "disputed"


class EntityType(StringEnum):
    NEWS = "news"
    EVENT = "event"
    PERSON = "person"
    ORGANIZATION = "organization"


@dataclass
class News:
    title: str
    original_title: str
    source: str
    source_url: str
    category: NewsCategory
    credibility: int
    fact_status: FactStatus
    id: int | None = None
    canonical_url: str | None = None
    publish_date: str | None = None
    country: str | None = None
    summary: str | None = None
    key_facts: list[str] = field(default_factory=list)
    viewpoints: list[str] = field(default_factory=list)
    raw_content: str | None = None
    content_hash: str | None = None
    ai_model: str | None = None
    ai_processed_at: str | None = None
    processing_status: ProcessingStatus = ProcessingStatus.PENDING
    created_time: str | None = None
    updated_time: str | None = None


@dataclass
class Event:
    event_name: str
    credibility: int
    id: int | None = None
    date_start: str | None = None
    date_end: str | None = None
    location: str | None = None
    country: str | None = None
    description: str | None = None
    status: EventStatus = EventStatus.UNVERIFIED
    created_time: str | None = None
    updated_time: str | None = None


@dataclass
class Person:
    name: str
    id: int | None = None
    country: str | None = None
    organization: str | None = None
    description: str | None = None
    created_time: str | None = None
    updated_time: str | None = None


@dataclass
class Relationship:
    source_type: EntityType
    source_id: int
    target_type: EntityType
    target_id: int
    relationship_type: str
    id: int | None = None
    evidence_news_id: int | None = None
    confidence: float | None = None
    created_time: str | None = None
