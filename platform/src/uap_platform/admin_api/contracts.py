"""Strict Admin DTOs frozen by the WP10 API contract."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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


class ReviewCaseType(StrEnum):
    DOCUMENT = "document"
    CLAIM = "claim"
    ENTITY = "entity"


class ReviewStatus(StrEnum):
    OPEN = "open"
    ASSIGNED = "assigned"
    APPROVED = "approved"
    REJECTED = "rejected"
    DISPUTED = "disputed"
    WITHDRAWN = "withdrawn"
    CLOSED = "closed"


class ReviewDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    DISPUTE = "dispute"
    WITHDRAW = "withdraw"
    REVISE = "revise"


class GrantStatus(StrEnum):
    ACTIVE = "active"
    WITHDRAWN = "withdrawn"
    SUPERSEDED = "superseded"


class ProjectionState(StrEnum):
    PENDING = "pending"
    VISIBLE = "visible"
    BLOCKED = "blocked"
    WITHDRAWN = "withdrawn"


class PublicationEventState(StrEnum):
    RETRY_WAIT = "retry_wait"
    TERMINAL = "terminal"


class EntityStatus(StrEnum):
    ACTIVE = "active"
    MERGED = "merged"
    RETIRED = "retired"
    DISPUTED = "disputed"


class CandidateStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    RESOLVED = "resolved"
    WITHDRAWN = "withdrawn"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Problem(StrictModel):
    type: str
    title: str
    status: int
    code: str
    request_id: UUID
    detail: str | None


class ReasonRequest(StrictModel):
    reason: str = Field(min_length=10, max_length=5000)


class OpenCaseRequest(StrictModel):
    case_type: ReviewCaseType
    subject_id: UUID
    priority: int = Field(default=0, ge=-32768, le=32767)
    reason: str = Field(min_length=10, max_length=5000)


class AssignmentRequest(StrictModel):
    assignee_id: UUID


class DecisionRequest(StrictModel):
    decision: ReviewDecision
    reason: str = Field(min_length=10, max_length=5000)
    structured_changes: dict[str, Any] = Field(default_factory=dict)


class BindCandidateRequest(StrictModel):
    entity_id: UUID
    reason: str = Field(min_length=10, max_length=5000)


class MergeRequest(StrictModel):
    source_entity_id: UUID
    target_entity_id: UUID
    reason: str = Field(min_length=10, max_length=5000)


class ManualClaimRequest(StrictModel):
    document_version_id: UUID
    claim_text: str = Field(min_length=1, max_length=10000)
    claim_type: ClaimType
    assertion_status: AssertionStatus
    attribution: str | None
    span_ids: list[UUID] = Field(min_length=1, max_length=20)


class PublicationState(StrictModel):
    grant_id: UUID
    revision: int = Field(ge=1)
    grant_status: GrantStatus
    outbox_event_id: UUID | None
    projection_state: ProjectionState


class WriteResult(StrictModel):
    operation: str
    resource_id: UUID
    request_id: UUID
    publication: PublicationState | None


class ReviewCaseSummary(StrictModel):
    id: UUID
    case_type: ReviewCaseType
    status: ReviewStatus
    priority: int
    subject_id: UUID
    assigned_to: UUID | None
    opened_by: UUID
    opened_at: datetime
    closed_at: datetime | None


class ReviewDecisionSummary(StrictModel):
    id: UUID
    sequence_no: int = Field(ge=1)
    decision: ReviewDecision
    reason: str
    decided_by: UUID
    decided_at: datetime


class ReviewCaseDetail(ReviewCaseSummary):
    decisions: list[ReviewDecisionSummary]
    publication: PublicationState | None


class ReviewCasePage(StrictModel):
    items: list[ReviewCaseSummary]
    next_cursor: str | None


class AnalysisResultSummary(StrictModel):
    id: UUID
    document_version_id: UUID
    result_type: str
    schema_version: str
    validation_status: str
    created_at: datetime


class AnalysisResultPage(StrictModel):
    items: list[AnalysisResultSummary]
    next_cursor: str | None


class EntityCandidateSummary(StrictModel):
    id: UUID
    analysis_result_id: UUID
    document_version_id: UUID
    ordinal: int = Field(ge=0)
    proposed_entity_type: EntityType
    proposed_name: str
    status: str
    created_at: datetime


class EntityCandidatePage(StrictModel):
    items: list[EntityCandidateSummary]
    next_cursor: str | None


class EvidenceSpanSummary(StrictModel):
    id: UUID
    document_version_id: UUID
    locator_type: str
    char_start: int | None
    char_end: int | None
    page_start: int | None
    page_end: int | None
    time_start_ms: int | None
    time_end_ms: int | None
    locator: dict[str, object]
    created_at: datetime


class EvidenceSpanPage(StrictModel):
    items: list[EvidenceSpanSummary]
    next_cursor: str | None


class AdminEntitySummary(StrictModel):
    id: UUID
    type: EntityType
    name: str
    description: str | None
    country_code: str | None
    status: str
    created_at: datetime


class AdminEntityPage(StrictModel):
    items: list[AdminEntitySummary]
    next_cursor: str | None


class PublicationEventSummary(StrictModel):
    id: UUID
    event_type: str
    aggregate_type: str
    aggregate_id: UUID
    occurred_at: datetime
    publish_attempts: int
    state: PublicationEventState
    error_code: str | None
    error_summary: str | None


class PublicationEventPage(StrictModel):
    items: list[PublicationEventSummary]
    next_cursor: str | None
