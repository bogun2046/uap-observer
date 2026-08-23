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
    """Platform-wide G8-16A activation set. Not a Claims worker's p_job_types.

    A general worker that owns fetch/extract/translate/analyze handlers uses this
    set; it never includes resolve_claims until that handler is deployed. A
    Claims-specific consumer must pass only types it can dispatch to claim_job.
    """

    if claims_handler_active:
        return CLAIMABLE_JOB_TYPES
    return PRE_CLAIM_HANDLER_JOB_TYPES
