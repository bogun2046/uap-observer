"""Build knowledge-attempt-metrics.v1 payloads for resolve_claims."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .contracts import MappingReport


def failure_metrics(reason_code: str) -> dict[str, Any]:
    """Terminal metrics that stay arithmetically consistent with no materialization."""

    return {
        "schema_version": "knowledge-attempt-metrics.v1",
        "input_candidates": 1,
        "materialized_candidates": 0,
        "input_locators": 1,
        "materialized_locators": 0,
        "rejected_candidates": 1,
        "rejected_locators": 1,
        "empty_valid_result": False,
        "rejected_by_code": {reason_code: 1},
        "samples": [
            {
                "candidate_ordinal": 0,
                "locator_ordinal": 0,
                "reason_code": reason_code,
            }
        ],
    }


def build_attempt_metrics(
    report: MappingReport,
    *,
    materialized_candidates: int,
    materialized_locators: int,
    empty_valid_result: bool,
) -> dict[str, Any]:
    """Strict metrics object: frozen keys only, no free text."""

    input_candidates = len(report.accepted_candidates) + len(report.rejected_candidates)
    rejected_candidates = len(report.rejected_candidates)
    input_locators = 0
    for accepted_cand in report.accepted_candidates:
        input_locators += len(accepted_cand.accepted_locators) + len(
            accepted_cand.rejected_locators
        )
    for rejected_cand in report.rejected_candidates:
        input_locators += len(rejected_cand.rejected_locators)

    rejected_locators = report.rejected_locator_count
    code_counter: Counter[str] = Counter()
    samples: list[dict[str, object]] = []

    for accepted in report.accepted_candidates:
        for loc in accepted.rejected_locators:
            code_counter[loc.reason_code] += 1
            if len(samples) < 50:
                samples.append(
                    {
                        "candidate_ordinal": accepted.ordinal,
                        "locator_ordinal": loc.locator_ordinal,
                        "reason_code": loc.reason_code,
                    }
                )
    for rejected in report.rejected_candidates:
        for loc in rejected.rejected_locators:
            code_counter[loc.reason_code] += 1
            if len(samples) < 50:
                samples.append(
                    {
                        "candidate_ordinal": rejected.ordinal,
                        "locator_ordinal": loc.locator_ordinal,
                        "reason_code": loc.reason_code,
                    }
                )

    if empty_valid_result:
        return {
            "schema_version": "knowledge-attempt-metrics.v1",
            "input_candidates": 0,
            "materialized_candidates": 0,
            "input_locators": 0,
            "materialized_locators": 0,
            "rejected_candidates": 0,
            "rejected_locators": 0,
            "empty_valid_result": True,
            "rejected_by_code": {},
            "samples": [],
        }

    if report.terminal_reason is not None and rejected_locators == 0:
        return failure_metrics(report.terminal_reason)

    return {
        "schema_version": "knowledge-attempt-metrics.v1",
        "input_candidates": input_candidates,
        "materialized_candidates": materialized_candidates,
        "input_locators": input_locators,
        "materialized_locators": materialized_locators,
        "rejected_candidates": rejected_candidates,
        "rejected_locators": rejected_locators,
        "empty_valid_result": False,
        "rejected_by_code": dict(code_counter),
        "samples": samples,
    }
