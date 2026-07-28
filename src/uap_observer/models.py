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


class SourceType(StringEnum):
    RSS = "rss"
    API = "api"
    WEB_PAGE = "web_page"


class AnalysisRiskFlag(StringEnum):
    INSUFFICIENT_SOURCE = "insufficient_source"
    SINGLE_SOURCE_CLAIM = "single_source_claim"
    ANONYMOUS_CLAIM = "anonymous_claim"
    SENSATIONAL_LANGUAGE = "sensational_language"
    MILITARY_SENSITIVITY = "military_sensitivity"


@dataclass
class Source:
    slug: str
    name: str
    source_type: SourceType
    homepage_url: str
    default_category: NewsCategory
    default_credibility: int
    default_fact_status: FactStatus
    id: int | None = None
    feed_url: str | None = None
    country: str | None = None
    language: str | None = None
    include_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    enabled: bool = True
    etag: str | None = None
    last_modified: str | None = None
    last_fetched_at: str | None = None
    last_success_at: str | None = None
    last_error: str | None = None


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
    source_id: int | None = None
    feed_entry_id: str | None = None
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
    extraction_status: ProcessingStatus = ProcessingStatus.PENDING
    extracted_content: str | None = None
    extracted_title: str | None = None
    extracted_author: str | None = None
    extracted_publish_date: str | None = None
    extracted_language: str | None = None
    extracted_by: str | None = None
    extraction_attempts: int = 0
    extraction_started_at: str | None = None
    content_extracted_at: str | None = None
    extraction_error: str | None = None
    created_time: str | None = None
    updated_time: str | None = None


@dataclass(frozen=True)
class ArticleTask:
    news_id: int
    url: str
    original_title: str
    extraction_attempts: int


@dataclass(frozen=True)
class AnalysisTask:
    news_id: int
    original_title: str
    source: str
    source_url: str
    publish_date: str | None
    extracted_content: str
    analysis_attempts: int


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
class Organization:
    name: str
    id: int | None = None
    country: str | None = None
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
