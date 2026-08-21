"""WP8.2 evidence locator mapping. No knowledge-table writes and no handlers."""

from .anchors import resolve_extraction_anchor
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
from .locators import build_envelope, map_locator
from .mapping import map_knowledge_result
from .reasons import FROZEN_REASON_CODES, LOCATOR_SCHEMA_VERSION, MAX_EVIDENCE_UTF8_BYTES

__all__ = [
    "FROZEN_REASON_CODES",
    "LOCATOR_SCHEMA_VERSION",
    "MAX_EVIDENCE_UTF8_BYTES",
    "AcceptedCandidate",
    "AcceptedLocator",
    "AnchorStatus",
    "ExtractionAnchor",
    "ExtractionRecord",
    "MappingClass",
    "MappingReport",
    "RejectedCandidate",
    "RejectedLocator",
    "SourceCandidate",
    "SourceLocator",
    "TypedAxes",
    "build_envelope",
    "map_knowledge_result",
    "map_locator",
    "resolve_extraction_anchor",
]
