"""Unit tests for WP8.3 bundle/metrics without database."""

from __future__ import annotations

import uuid

from uap_platform.knowledge.bundle import (
    build_knowledge_bundle,
    is_empty_valid,
    is_terminal_mapping,
    terminal_error_code,
)
from uap_platform.knowledge.contracts import (
    AcceptedCandidate,
    AcceptedLocator,
    AnchorStatus,
    ExtractionAnchor,
    MappingClass,
    MappingReport,
    RejectedCandidate,
    RejectedLocator,
    TypedAxes,
)
from uap_platform.knowledge.metrics import build_attempt_metrics, failure_metrics


def _axes_text(start: int = 0, end: int = 5) -> TypedAxes:
    return TypedAxes(
        char_start=start,
        char_end=end,
        page_start=None,
        page_end=None,
        time_start_ms=None,
        time_end_ms=None,
    )


def test_build_knowledge_bundle_empty() -> None:
    report = MappingReport(
        classification=MappingClass.EMPTY_VALID,
        anchor=ExtractionAnchor(status=AnchorStatus.MISSING, extraction_id=None),
        accepted_candidates=(),
        rejected_candidates=(),
    )
    bundle = build_knowledge_bundle(
        report, analysis_result_id=uuid.uuid4(), analysis_result_sha256="a" * 64
    )
    assert bundle["bundle_schema_version"] == "knowledge-bundle.v2"
    assert bundle["accepted_candidates"] == []
    assert bundle["rejected_candidates"] == []
    assert set(bundle) == {
        "accepted_candidates",
        "analysis_result_id",
        "analysis_result_sha256",
        "bundle_schema_version",
        "rejected_candidates",
    }
    assert is_empty_valid(report)
    assert not is_terminal_mapping(report)
    assert "claim" not in bundle


def test_build_knowledge_bundle_partial() -> None:
    accepted = AcceptedCandidate(
        ordinal=0,
        accepted_locators=(
            AcceptedLocator(
                locator_ordinal=0,
                evidence_text="hello",
                envelope={"locator_schema_version": "evidence-locator.v2"},
                axes=_axes_text(),
            ),
        ),
        rejected_locators=(
            RejectedLocator(locator_ordinal=1, reason_code="locator_out_of_range"),
        ),
    )
    rejected = RejectedCandidate(
        ordinal=1,
        reason_code="knowledge_locator_unmappable",
        rejected_locators=(
            RejectedLocator(locator_ordinal=0, reason_code="locator_axis_conflict"),
        ),
    )
    report = MappingReport(
        classification=MappingClass.MATERIALIZABLE,
        anchor=ExtractionAnchor(status=AnchorStatus.MATCHED, extraction_id=uuid.uuid4()),
        accepted_candidates=(accepted,),
        rejected_candidates=(rejected,),
    )
    bundle = build_knowledge_bundle(
        report, analysis_result_id=uuid.uuid4(), analysis_result_sha256="b" * 64
    )
    assert len(bundle["accepted_candidates"]) == 1
    assert bundle["rejected_candidates"][0]["reason_code"] == "knowledge_locator_unmappable"


def test_terminal_helpers() -> None:
    report = MappingReport(
        classification=MappingClass.TERMINAL_UNMAPPABLE,
        anchor=ExtractionAnchor(status=AnchorStatus.MATCHED, extraction_id=uuid.uuid4()),
        accepted_candidates=(),
        rejected_candidates=(),
        terminal_reason="knowledge_locator_unmappable",
    )
    assert is_terminal_mapping(report)
    assert terminal_error_code(report) == "knowledge_locator_unmappable"
    missing = MappingReport(
        classification=MappingClass.TERMINAL_EXTRACTION_MISSING,
        anchor=ExtractionAnchor(status=AnchorStatus.MISSING, extraction_id=None),
        accepted_candidates=(),
        rejected_candidates=(),
    )
    assert terminal_error_code(missing) == "knowledge_extraction_missing"
    ambiguous = MappingReport(
        classification=MappingClass.TERMINAL_EXTRACTION_AMBIGUOUS,
        anchor=ExtractionAnchor(status=AnchorStatus.AMBIGUOUS, extraction_id=None),
        accepted_candidates=(),
        rejected_candidates=(),
    )
    assert terminal_error_code(ambiguous) == "knowledge_extraction_ambiguous"


def test_build_attempt_metrics_counts() -> None:
    accepted = AcceptedCandidate(
        ordinal=0,
        accepted_locators=(
            AcceptedLocator(
                locator_ordinal=0,
                evidence_text="x",
                envelope={},
                axes=_axes_text(),
            ),
        ),
        rejected_locators=(
            RejectedLocator(locator_ordinal=1, reason_code="locator_duplicate"),
        ),
    )
    rejected = RejectedCandidate(
        ordinal=1,
        reason_code="knowledge_locator_unmappable",
        rejected_locators=(
            RejectedLocator(locator_ordinal=0, reason_code="locator_out_of_range"),
        ),
    )
    report = MappingReport(
        classification=MappingClass.MATERIALIZABLE,
        anchor=ExtractionAnchor(status=AnchorStatus.MATCHED, extraction_id=uuid.uuid4()),
        accepted_candidates=(accepted,),
        rejected_candidates=(rejected,),
    )
    metrics = build_attempt_metrics(
        report,
        materialized_candidates=1,
        materialized_locators=1,
        empty_valid_result=False,
    )
    assert metrics["input_candidates"] == 2
    assert metrics["rejected_candidates"] == 1
    assert metrics["input_locators"] == 3
    assert metrics["rejected_locators"] == 2
    assert sum(metrics["rejected_by_code"].values()) == metrics["rejected_locators"]


def test_failure_metrics_arithmetic() -> None:
    metrics = failure_metrics("knowledge_payload_mismatch")
    assert (
        metrics["materialized_candidates"] + metrics["rejected_candidates"]
        == metrics["input_candidates"]
    )
    assert (
        metrics["materialized_locators"] + metrics["rejected_locators"]
        == metrics["input_locators"]
    )
    assert metrics["rejected_by_code"]["knowledge_payload_mismatch"] == 1
    assert metrics["empty_valid_result"] is False
