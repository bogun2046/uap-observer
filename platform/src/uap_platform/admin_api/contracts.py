"""Strict Admin DTOs frozen by the WP10 API contract."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
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
    editorial_revision_id: UUID | None = None
    editorial_revision_no: int | None = Field(default=None, ge=1)
    manifest_id: UUID | None = None
    manifest_hash: str | None = None
    outbox_status: str | None = None
    public_visible: bool = False


class PublicationStatus(StrictModel):
    document_id: UUID
    document_version_id: UUID
    current_editorial_revision_id: UUID | None
    current_editorial_revision_no: int | None = Field(default=None, ge=1)
    selected_editorial_revision_id: UUID | None
    selected_editorial_revision_no: int | None = Field(default=None, ge=1)
    eligibility: str
    status: str
    review_case_id: UUID | None
    review_case_status: ReviewStatus | None
    decision_status: ReviewDecision | None
    grant_id: UUID | None
    grant_status: GrantStatus | None
    publication_sequence: int | None = Field(default=None, ge=1)
    manifest_id: UUID | None
    manifest_hash: str | None
    outbox_event_id: UUID | None
    outbox_status: str | None
    public_visible: bool


class PublicationReviewRequest(StrictModel):
    document_version_id: UUID
    editorial_revision_id: UUID
    editorial_revision_no: int = Field(ge=1)
    expected_revision: int = Field(ge=1)
    priority: int = Field(default=0, ge=-32768, le=32767)
    reason: str = Field(min_length=10, max_length=5000)


class WriteResult(StrictModel):
    operation: str
    resource_id: UUID
    request_id: UUID
    publication: PublicationState | None


class EditorialClaim(StrictModel):
    claim_id: UUID | None = None
    claim: str = Field(min_length=1, max_length=10_000)
    source_statement: str = Field(min_length=1, max_length=20_000)
    speaker: str | None = Field(default=None, max_length=500)
    claim_type: ClaimType
    assertion_status: AssertionStatus
    evidence_span_ids: list[UUID] = Field(default_factory=list, max_length=20)
    state: Literal["active", "removed"] = "active"


class EditorialEntity(StrictModel):
    entity_id: UUID | None = None
    name: str = Field(min_length=1, max_length=500)
    entity_type: EntityType
    aliases: list[str] = Field(default_factory=list, max_length=20)
    evidence_span_ids: list[UUID] = Field(default_factory=list, max_length=20)
    state: Literal["active", "removed"] = "active"


class EditorialClaimMutationRequest(StrictModel):
    document_version_id: UUID
    expected_revision: int = Field(ge=0)
    claim: EditorialClaim | None = None
    claim_id: UUID | None = None
    item_ordinal: int | None = Field(default=None, ge=0)
    evidence_span_ids: list[UUID] | None = Field(default=None, max_length=20)
    reason: str | None = Field(default=None, max_length=2_000)


class EditorialEntityMutationRequest(StrictModel):
    document_version_id: UUID
    expected_revision: int = Field(ge=0)
    entity: EditorialEntity | None = None
    entity_id: UUID | None = None
    item_ordinal: int | None = Field(default=None, ge=0)
    evidence_span_ids: list[UUID] | None = Field(default=None, max_length=20)
    reason: str | None = Field(default=None, max_length=2_000)


class EditorialContent(StrictModel):
    title: str = Field(min_length=1, max_length=500)
    summary: str | None = Field(default=None, max_length=20_000)
    bullets: list[str] = Field(default_factory=list, max_length=20)
    category: DocumentCategory
    labels: list[str] = Field(default_factory=list, max_length=50)
    claims: list[EditorialClaim] = Field(default_factory=list, max_length=200)
    entities: list[EditorialEntity] = Field(default_factory=list, max_length=200)


class EditorialPatchRequest(StrictModel):
    document_version_id: UUID
    expected_revision: int = Field(ge=0)
    title: str | None = Field(default=None, max_length=500)
    summary: str | None = Field(default=None, max_length=20_000)
    bullets: list[str] | None = Field(default=None, max_length=20)
    category: DocumentCategory | None = None
    labels: list[str] | None = Field(default=None, max_length=50)
    claims: list[EditorialClaim] | None = Field(default=None, max_length=200)
    entities: list[EditorialEntity] | None = Field(default=None, max_length=200)

    def changes(self) -> dict[str, object]:
        return self.model_dump(
            mode="json",
            exclude_unset=True,
            exclude={"document_version_id", "expected_revision"},
        )


class AdoptEditorialRequest(StrictModel):
    document_version_id: UUID
    expected_revision: int = Field(ge=0)
    source_analysis_result_id: UUID
    fields: list[
        Literal[
            "title",
            "summary",
            "bullets",
            "category",
            "labels",
            "claims",
            "entities",
        ]
    ] = Field(min_length=1, max_length=7)
    values: dict[str, Any] = Field(default_factory=dict)
    item_ordinal: int | None = Field(default=None, ge=0)


class ReanalysisRequest(StrictModel):
    document_version_id: UUID
    task_type: Literal["classification", "summary", "claim_extraction", "entity_extraction"]
    reason: str = Field(min_length=1, max_length=2_000)


class LifecycleRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    reason: str | None = Field(default=None, max_length=5_000)


class EditorialRevisionRestoreRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    reason: str | None = Field(default=None, max_length=5_000)


class EditorialRevisionSummary(StrictModel):
    id: UUID
    document_version_id: UUID
    revision_no: int = Field(ge=1)
    operation: Literal["save", "adopt", "trash", "restore", "restore_revision"]
    base_revision_no: int = Field(ge=0)
    created_by: UUID
    created_at: datetime
    source_map: dict[str, Any]
    adopted_from: dict[str, Any]
    is_current: bool = False
    content_digest: str | None = None
    reason: str | None = None
    source_revision_id: UUID | None = None
    source_revision_no: int | None = None


class EditorialRevisionPage(StrictModel):
    items: list[EditorialRevisionSummary]
    next_cursor: str | None


class EditorialRevisionDetail(EditorialRevisionSummary):
    content: EditorialContent | None


class DocumentDetail(StrictModel):
    document_id: UUID
    document_version_id: UUID
    source: dict[str, Any]
    canonical_url: str | None
    internal_state: str
    lifecycle: dict[str, Any]
    raw: dict[str, Any]
    ai_results: dict[str, Any]
    editorial: dict[str, Any] | None
    indicators: dict[str, bool]
    publication: PublicationStatus | None = None


class DocumentListSummary(StrictModel):
    document_id: UUID
    document_version_id: UUID
    title: str | None
    source: dict[str, Any]
    canonical_url: str | None
    source_published_at: datetime | None
    internal_state: str
    lifecycle: dict[str, Any]
    indicators: dict[str, bool]


class DocumentListPage(StrictModel):
    items: list[DocumentListSummary]
    next_cursor: str | None


class TrashDocumentSummary(StrictModel):
    document_id: UUID
    document_version_id: UUID
    title: str | None
    source: dict[str, Any]
    trashed_at: datetime
    trashed_by: UUID
    reason: str | None
    revision_no: int = Field(ge=0)


class TrashDocumentPage(StrictModel):
    items: list[TrashDocumentSummary]
    next_cursor: str | None


class AuditHistoryEvent(StrictModel):
    id: UUID
    event_key: str
    action: str
    actor_id: UUID
    occurred_at: datetime
    request_id: UUID | None
    target_id: UUID
    metadata: dict[str, Any]


class AuditHistoryPage(StrictModel):
    items: list[AuditHistoryEvent]
    next_cursor: str | None


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
    editorial_revision_id: UUID | None = None
    editorial_revision_no: int | None = Field(default=None, ge=1)


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
