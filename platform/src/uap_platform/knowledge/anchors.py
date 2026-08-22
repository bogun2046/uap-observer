"""Resolve extraction identity without recency, id order, or extractor version."""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence

from .contracts import AnchorStatus, ExtractionAnchor, ExtractionRecord

_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SUCCEEDED = "succeeded"
_DERIVED = "derived"


def _is_sha256(value: str) -> bool:
    return bool(_HEX_SHA256.fullmatch(value))


def hash_matched_rows(
    records: Sequence[ExtractionRecord],
    *,
    document_version_id: uuid.UUID,
    input_sha256: str,
) -> tuple[ExtractionRecord, ...]:
    """Rows that share the analysis document version and model-run input hash."""

    if not _is_sha256(input_sha256):
        return ()
    matched: list[ExtractionRecord] = []
    for record in records:
        if record.document_version_id != document_version_id:
            continue
        if record.outcome != _SUCCEEDED:
            continue
        if record.output_sha256 != input_sha256:
            continue
        matched.append(record)
    return tuple(matched)


def object_consistent(record: ExtractionRecord) -> bool:
    return (
        record.stored_domain == _DERIVED
        and record.stored_sha256 == record.output_sha256
        and _is_sha256(record.stored_sha256)
    )


def lookup_extraction(
    records: Sequence[ExtractionRecord], extraction_id: uuid.UUID
) -> ExtractionRecord | None:
    for record in records:
        if record.extraction_id == extraction_id:
            return record
    return None


def specified_extraction_usable(
    record: ExtractionRecord,
    *,
    document_version_id: uuid.UUID,
    input_sha256: str,
) -> bool:
    """True when the payload-named extraction still matches the frozen snapshot rules."""

    if record.document_version_id != document_version_id:
        return False
    if record.outcome != _SUCCEEDED:
        return False
    if record.output_sha256 != input_sha256:
        return False
    return object_consistent(record) and _is_sha256(input_sha256)


def resolve_extraction_anchor(
    records: Sequence[ExtractionRecord],
    *,
    document_version_id: uuid.UUID,
    input_sha256: str,
) -> ExtractionAnchor:
    """Enqueue-time 0/1/>1 count. Never break ties. Inconsistent objects do not count."""

    hashed = hash_matched_rows(
        records, document_version_id=document_version_id, input_sha256=input_sha256
    )
    consistent = tuple(record for record in hashed if object_consistent(record))
    if len(consistent) == 0:
        return ExtractionAnchor(status=AnchorStatus.MISSING, extraction_id=None)
    if len(consistent) == 1:
        return ExtractionAnchor(
            status=AnchorStatus.MATCHED, extraction_id=consistent[0].extraction_id
        )
    return ExtractionAnchor(status=AnchorStatus.AMBIGUOUS, extraction_id=None)
