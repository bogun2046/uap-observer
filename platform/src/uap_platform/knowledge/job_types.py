"""Worker claimable job-type sets for knowledge resolve activation (G8-16A)."""

from __future__ import annotations

PRE_CLAIM_HANDLER_JOB_TYPES: tuple[str, ...] = (
    "fetch_source",
    "extract_document",
    "translate_document",
    "analyze_document",
)

CLAIMABLE_JOB_TYPES: tuple[str, ...] = (
    *PRE_CLAIM_HANDLER_JOB_TYPES,
    "resolve_claims",
)

FORBIDDEN_CLAIMABLE_JOB_TYPES: tuple[str, ...] = (
    "resolve_entities",
    "resolve_relations",
)


def claimable_job_types(*, claims_handler_active: bool) -> tuple[str, ...]:
    """Return the job-type array passed to ops.claim_job."""

    if claims_handler_active:
        return CLAIMABLE_JOB_TYPES
    return PRE_CLAIM_HANDLER_JOB_TYPES
