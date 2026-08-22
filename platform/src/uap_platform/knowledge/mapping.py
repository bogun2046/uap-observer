"""Classify candidates and locators without writing knowledge tables."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from .anchors import lookup_extraction, specified_extraction_usable
from .contracts import (
    AcceptedCandidate,
    AnchorStatus,
    DuplicatePolicy,
    ExtractionAnchor,
    ExtractionRecord,
    LocationMap,
    MappingClass,
    MappingReport,
    RejectedCandidate,
    RejectedLocator,
    SourceCandidate,
)
from .locators import LocatorRejected, map_locator
from .reasons import (
    KNOWLEDGE_EXTRACTION_AMBIGUOUS,
    KNOWLEDGE_EXTRACTION_MISMATCH,
    KNOWLEDGE_EXTRACTION_MISSING,
    KNOWLEDGE_LOCATOR_UNMAPPABLE,
    KNOWLEDGE_PAYLOAD_MISMATCH,
    LOCATOR_DUPLICATE,
)


def _reject_all(
    candidates: Sequence[SourceCandidate],
    reason_code: str,
    classification: MappingClass,
    anchor: ExtractionAnchor,
) -> MappingReport:
    rejected = tuple(
        RejectedCandidate(
            ordinal=candidate.ordinal,
            reason_code=reason_code,
            rejected_locators=tuple(
                RejectedLocator(locator_ordinal=index, reason_code=reason_code)
                for index, _locator in enumerate(candidate.locators)
            ),
        )
        for candidate in candidates
    )
    return MappingReport(
        classification=classification,
        anchor=anchor,
        accepted_candidates=(),
        rejected_candidates=rejected,
        terminal_reason=reason_code,
    )


def _terminal_class(reason_code: str) -> MappingClass:
    if reason_code == KNOWLEDGE_EXTRACTION_MISSING:
        return MappingClass.TERMINAL_EXTRACTION_MISSING
    if reason_code == KNOWLEDGE_EXTRACTION_AMBIGUOUS:
        return MappingClass.TERMINAL_EXTRACTION_AMBIGUOUS
    return MappingClass.TERMINAL_EXTRACTION_MISMATCH


def payload_shape_reason(payload_anchor: ExtractionAnchor) -> str | None:
    """Illegal knowledge.v2 status/id pairing is fail-closed even for empty arrays."""

    matched = payload_anchor.status is AnchorStatus.MATCHED
    if matched != (payload_anchor.extraction_id is not None):
        return KNOWLEDGE_PAYLOAD_MISMATCH
    return None


def frozen_payload_reason(
    payload_anchor: ExtractionAnchor,
    records: Sequence[ExtractionRecord],
    *,
    document_version_id: uuid.UUID,
    input_sha256: str,
) -> str | None:
    """Validate the immutable job payload. Never recount current hash matches."""

    shape = payload_shape_reason(payload_anchor)
    if shape is not None:
        return shape
    matched = payload_anchor.status is AnchorStatus.MATCHED
    if matched != (payload_anchor.extraction_id is not None):
        return KNOWLEDGE_PAYLOAD_MISMATCH
    if payload_anchor.status is AnchorStatus.MISSING:
        return KNOWLEDGE_EXTRACTION_MISSING
    if payload_anchor.status is AnchorStatus.AMBIGUOUS:
        return KNOWLEDGE_EXTRACTION_AMBIGUOUS
    extraction_id = payload_anchor.extraction_id
    if extraction_id is None:
        return KNOWLEDGE_PAYLOAD_MISMATCH
    row = lookup_extraction(records, extraction_id)
    if row is None or not specified_extraction_usable(
        row, document_version_id=document_version_id, input_sha256=input_sha256
    ):
        return KNOWLEDGE_EXTRACTION_MISMATCH
    return None


def _map_candidate(
    candidate: SourceCandidate,
    *,
    extracted_text: str,
    location_map: LocationMap,
    document_version_id: uuid.UUID,
    extraction_id: uuid.UUID,
    input_sha256: str,
    duplicate_policy: DuplicatePolicy,
) -> AcceptedCandidate | RejectedCandidate:
    accepted = []
    rejected: list[RejectedLocator] = []
    seen: set[tuple[object, ...]] = set()
    for index, locator in enumerate(candidate.locators):
        identity = locator.identity()
        if duplicate_policy == "claim" and identity in seen:
            rejected.append(RejectedLocator(locator_ordinal=index, reason_code=LOCATOR_DUPLICATE))
            continue
        try:
            mapped = map_locator(
                locator,
                extracted_text=extracted_text,
                location_map=location_map,
                document_version_id=document_version_id,
                extraction_id=extraction_id,
                input_sha256=input_sha256,
                locator_ordinal=index,
            )
        except LocatorRejected as error:
            rejected.append(RejectedLocator(locator_ordinal=index, reason_code=error.reason_code))
            continue
        if duplicate_policy == "claim":
            seen.add(identity)
        accepted.append(mapped)
    if accepted:
        return AcceptedCandidate(
            ordinal=candidate.ordinal,
            accepted_locators=tuple(accepted),
            rejected_locators=tuple(rejected),
        )
    if not rejected:
        rejected = [RejectedLocator(locator_ordinal=0, reason_code=KNOWLEDGE_LOCATOR_UNMAPPABLE)]
    return RejectedCandidate(
        ordinal=candidate.ordinal,
        reason_code=KNOWLEDGE_LOCATOR_UNMAPPABLE,
        rejected_locators=tuple(rejected),
    )


def map_knowledge_result(
    *,
    candidates: Sequence[SourceCandidate],
    payload_anchor: ExtractionAnchor,
    records: Sequence[ExtractionRecord],
    document_version_id: uuid.UUID,
    input_sha256: str,
    extracted_text: str,
    location_map: LocationMap,
    duplicate_policy: DuplicatePolicy,
) -> MappingReport:
    """Map using the frozen knowledge.v2 payload. Does not recount extractions."""

    ordered = tuple(sorted(candidates, key=lambda item: item.ordinal))
    shape = payload_shape_reason(payload_anchor)
    if shape is not None:
        return _reject_all(ordered, shape, _terminal_class(shape), payload_anchor)
    if not ordered:
        return MappingReport(
            classification=MappingClass.EMPTY_VALID,
            anchor=payload_anchor,
            accepted_candidates=(),
            rejected_candidates=(),
        )
    terminal = frozen_payload_reason(
        payload_anchor,
        records,
        document_version_id=document_version_id,
        input_sha256=input_sha256,
    )
    if terminal is not None:
        return _reject_all(ordered, terminal, _terminal_class(terminal), payload_anchor)
    extraction_id = payload_anchor.extraction_id
    if extraction_id is None:
        return _reject_all(
            ordered,
            KNOWLEDGE_PAYLOAD_MISMATCH,
            MappingClass.TERMINAL_EXTRACTION_MISMATCH,
            payload_anchor,
        )
    accepted: list[AcceptedCandidate] = []
    rejected: list[RejectedCandidate] = []
    for candidate in ordered:
        mapped = _map_candidate(
            candidate,
            extracted_text=extracted_text,
            location_map=location_map,
            document_version_id=document_version_id,
            extraction_id=extraction_id,
            input_sha256=input_sha256,
            duplicate_policy=duplicate_policy,
        )
        if isinstance(mapped, AcceptedCandidate):
            accepted.append(mapped)
        else:
            rejected.append(mapped)
    if accepted:
        classification = MappingClass.MATERIALIZABLE
    else:
        classification = MappingClass.TERMINAL_UNMAPPABLE
    return MappingReport(
        classification=classification,
        anchor=payload_anchor,
        accepted_candidates=tuple(accepted),
        rejected_candidates=tuple(rejected),
    )
