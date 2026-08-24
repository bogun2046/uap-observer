"""Worker claimable job-type sets for knowledge resolve activation (G8-16A/G8-16B)."""

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

PRE_ENTITY_HANDLER_JOB_TYPES: tuple[str, ...] = CLAIMABLE_JOB_TYPES

ENTITY_CLAIMABLE_JOB_TYPES: tuple[str, ...] = (
    *CLAIMABLE_JOB_TYPES,
    "resolve_entities",
)

FORBIDDEN_CLAIMABLE_JOB_TYPES: tuple[str, ...] = (
    "resolve_entities",
    "resolve_relations",
)


def claimable_job_types(
    *,
    claims_handler_active: bool,
    entities_handler_active: bool = False,
) -> tuple[str, ...]:
    """Platform-wide G8-16A/G8-16B activation set. Not a typed worker's p_job_types.

    A general worker that owns fetch/extract/translate/analyze handlers uses this
    set; it never includes resolve_claims until that handler is deployed, and never
    includes resolve_entities until the entity handler is deployed. Claims- and
    entities-specific consumers must pass only types they can dispatch to claim_job.
    resolve_relations is never added.
    """

    types: list[str] = list(PRE_CLAIM_HANDLER_JOB_TYPES)
    if claims_handler_active:
        types.append("resolve_claims")
    if entities_handler_active:
        types.append("resolve_entities")
    return tuple(types)
