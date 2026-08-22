"""Frozen WP8.2 reason codes. Logs and metrics may only use these tokens."""

from __future__ import annotations

from typing import Final

LOCATOR_END_NOT_AFTER_START: Final = "locator_end_not_after_start"
LOCATOR_OUT_OF_RANGE: Final = "locator_out_of_range"
LOCATOR_AXIS_CONFLICT: Final = "locator_axis_conflict"
LOCATOR_PDF_PAGE_MISSING: Final = "locator_pdf_page_missing"
LOCATOR_TIME_MISSING: Final = "locator_time_missing"
LOCATOR_PAGE_RANGE_INVALID: Final = "locator_page_range_invalid"
LOCATOR_TIME_RANGE_INVALID: Final = "locator_time_range_invalid"
LOCATOR_LOCATION_MAP_INVALID: Final = "locator_location_map_invalid"
LOCATOR_CROSS_AXIS_MISMATCH: Final = "locator_cross_axis_mismatch"
LOCATOR_EXCERPT_TOO_LARGE: Final = "locator_excerpt_too_large"
LOCATOR_DUPLICATE: Final = "locator_duplicate"
KNOWLEDGE_EXTRACTION_MISSING: Final = "knowledge_extraction_missing"
KNOWLEDGE_EXTRACTION_AMBIGUOUS: Final = "knowledge_extraction_ambiguous"
KNOWLEDGE_EXTRACTION_MISMATCH: Final = "knowledge_extraction_mismatch"
KNOWLEDGE_LOCATOR_UNMAPPABLE: Final = "knowledge_locator_unmappable"
KNOWLEDGE_INVALID_ORIGIN: Final = "knowledge_invalid_origin"
KNOWLEDGE_SCHEMA_UNSUPPORTED: Final = "knowledge_schema_unsupported"
KNOWLEDGE_PAYLOAD_MISMATCH: Final = "knowledge_payload_mismatch"
KNOWLEDGE_BUNDLE_MISMATCH: Final = "knowledge_bundle_mismatch"

FROZEN_REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        LOCATOR_END_NOT_AFTER_START,
        LOCATOR_OUT_OF_RANGE,
        LOCATOR_AXIS_CONFLICT,
        LOCATOR_PDF_PAGE_MISSING,
        LOCATOR_TIME_MISSING,
        LOCATOR_PAGE_RANGE_INVALID,
        LOCATOR_TIME_RANGE_INVALID,
        LOCATOR_LOCATION_MAP_INVALID,
        LOCATOR_CROSS_AXIS_MISMATCH,
        LOCATOR_EXCERPT_TOO_LARGE,
        LOCATOR_DUPLICATE,
        KNOWLEDGE_EXTRACTION_MISSING,
        KNOWLEDGE_EXTRACTION_AMBIGUOUS,
        KNOWLEDGE_EXTRACTION_MISMATCH,
        KNOWLEDGE_LOCATOR_UNMAPPABLE,
        KNOWLEDGE_INVALID_ORIGIN,
        KNOWLEDGE_SCHEMA_UNSUPPORTED,
        KNOWLEDGE_PAYLOAD_MISMATCH,
        KNOWLEDGE_BUNDLE_MISMATCH,
    }
)

MAX_EVIDENCE_UTF8_BYTES: Final = 8192
LOCATOR_SCHEMA_VERSION: Final = "evidence-locator.v2"
