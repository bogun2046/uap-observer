"""WP8 knowledge package: locator mapping, claim and entity materialization."""

from .anchors import resolve_extraction_anchor
from .bundle import build_knowledge_bundle
from .contracts import (
    AcceptedCandidate,
    AcceptedLocator,
    AnchorStatus,
    ExtractionAnchor,
    ExtractionRecord,
    MappingClass,
    MappingReport,
    RejectedCandidate,
    RejectedLocator,
    SourceCandidate,
    SourceLocator,
    TypedAxes,
)
from .handler import ResolveClaimsHandler, ResolveEntitiesHandler, payload_from_claim
from .job_types import (
    CLAIMABLE_JOB_TYPES,
    ENTITY_CLAIMABLE_JOB_TYPES,
    FORBIDDEN_CLAIMABLE_JOB_TYPES,
    PRE_CLAIM_HANDLER_JOB_TYPES,
    PRE_ENTITY_HANDLER_JOB_TYPES,
    claimable_job_types,
)
from .locators import build_envelope, map_locator
from .mapping import map_knowledge_result
from .metrics import build_attempt_metrics, failure_metrics
from .payload import FrozenKnowledgePayload, KnowledgePayloadError, parse_knowledge_payload
from .reasons import FROZEN_REASON_CODES, LOCATOR_SCHEMA_VERSION, MAX_EVIDENCE_UTF8_BYTES
from .worker import ResolveClaimsWorker, ResolveEntitiesWorker

__all__ = [
    "CLAIMABLE_JOB_TYPES",
    "ENTITY_CLAIMABLE_JOB_TYPES",
    "FORBIDDEN_CLAIMABLE_JOB_TYPES",
    "FROZEN_REASON_CODES",
    "LOCATOR_SCHEMA_VERSION",
    "MAX_EVIDENCE_UTF8_BYTES",
    "PRE_CLAIM_HANDLER_JOB_TYPES",
    "PRE_ENTITY_HANDLER_JOB_TYPES",
    "AcceptedCandidate",
    "AcceptedLocator",
    "AnchorStatus",
    "ExtractionAnchor",
    "ExtractionRecord",
    "FrozenKnowledgePayload",
    "KnowledgePayloadError",
    "MappingClass",
    "MappingReport",
    "RejectedCandidate",
    "RejectedLocator",
    "ResolveClaimsHandler",
    "ResolveClaimsWorker",
    "ResolveEntitiesHandler",
    "ResolveEntitiesWorker",
    "SourceCandidate",
    "SourceLocator",
    "TypedAxes",
    "build_attempt_metrics",
    "build_envelope",
    "build_knowledge_bundle",
    "claimable_job_types",
    "failure_metrics",
    "map_knowledge_result",
    "map_locator",
    "parse_knowledge_payload",
    "payload_from_claim",
    "resolve_extraction_anchor",
]
