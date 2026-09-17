"""Build knowledge-bundle.v2 mapping decisions from a WP8.2 MappingReport."""

from __future__ import annotations

import uuid
from typing import Any

from .contracts import MappingClass, MappingReport


def build_knowledge_bundle(
    report: MappingReport,
    *,
    analysis_result_id: uuid.UUID,
    analysis_result_sha256: str,
) -> dict[str, Any]:
    """Serialize mapping decisions only; claim text stays authoritative in analysis_results."""

    accepted: list[dict[str, Any]] = []
    for candidate in report.accepted_candidates:
        accepted.append(
            {
                "ordinal": candidate.ordinal,
                "accepted_locators": [
                    {
                        "locator_ordinal": loc.locator_ordinal,
                        "evidence_text": loc.evidence_text,
                        "char_start": loc.axes.char_start,
                        "char_end": loc.axes.char_end,
                        "page_start": loc.axes.page_start,
                        "page_end": loc.axes.page_end,
                        "time_start_ms": loc.axes.time_start_ms,
                        "time_end_ms": loc.axes.time_end_ms,
                    }
                    for loc in candidate.accepted_locators
                ],
                "rejected_locators": [
                    {
                        "locator_ordinal": loc.locator_ordinal,
                        "reason_code": loc.reason_code,
                    }
                    for loc in candidate.rejected_locators
                ],
            }
        )

    rejected: list[dict[str, Any]] = []
    for rejected_candidate in report.rejected_candidates:
        rejected.append(
            {
                "ordinal": rejected_candidate.ordinal,
                "reason_code": rejected_candidate.reason_code,
                "rejected_locators": [
                    {
                        "locator_ordinal": loc.locator_ordinal,
                        "reason_code": loc.reason_code,
                    }
                    for loc in rejected_candidate.rejected_locators
                ],
            }
        )

    return {
        "bundle_schema_version": "knowledge-bundle.v2",
        "analysis_result_id": str(analysis_result_id),
        "analysis_result_sha256": analysis_result_sha256,
        "accepted_candidates": accepted,
        "rejected_candidates": rejected,
    }


def is_empty_valid(report: MappingReport) -> bool:
    return report.classification is MappingClass.EMPTY_VALID


def is_terminal_mapping(report: MappingReport) -> bool:
    return report.classification in (
        MappingClass.TERMINAL_UNMAPPABLE,
        MappingClass.TERMINAL_EXTRACTION_MISSING,
        MappingClass.TERMINAL_EXTRACTION_AMBIGUOUS,
        MappingClass.TERMINAL_EXTRACTION_MISMATCH,
    )


def terminal_error_code(report: MappingReport) -> str:
    if report.terminal_reason is not None:
        return report.terminal_reason
    if report.classification is MappingClass.TERMINAL_EXTRACTION_MISSING:
        return "knowledge_extraction_missing"
    if report.classification is MappingClass.TERMINAL_EXTRACTION_AMBIGUOUS:
        return "knowledge_extraction_ambiguous"
    if report.classification is MappingClass.TERMINAL_EXTRACTION_MISMATCH:
        return "knowledge_extraction_mismatch"
    return "knowledge_locator_unmappable"
