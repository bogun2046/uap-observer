"""Map one WP7 evidence locator onto G3 typed columns and an evidence-locator.v2 envelope."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Literal

from .contracts import AcceptedLocator, LocationMap, SourceLocator, TypedAxes
from .reasons import (
    LOCATOR_AXIS_CONFLICT,
    LOCATOR_CROSS_AXIS_MISMATCH,
    LOCATOR_END_NOT_AFTER_START,
    LOCATOR_EXCERPT_TOO_LARGE,
    LOCATOR_LOCATION_MAP_INVALID,
    LOCATOR_OUT_OF_RANGE,
    LOCATOR_PAGE_RANGE_INVALID,
    LOCATOR_PDF_PAGE_MISSING,
    LOCATOR_SCHEMA_VERSION,
    LOCATOR_TIME_MISSING,
    LOCATOR_TIME_RANGE_INVALID,
    MAX_EVIDENCE_UTF8_BYTES,
)

_PDF_KIND: Literal["pdf_page"] = "pdf_page"
_CUE_KIND: Literal["subtitle_cue"] = "subtitle_cue"


class LocatorRejected(Exception):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def canonical_envelope_text(envelope: Mapping[str, object]) -> str:
    """PostgreSQL-16-shaped jsonb text: sorted keys, compact separators, UTF-8 JSON."""

    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def canonical_locator_digest(envelope: Mapping[str, object]) -> str:
    """Python digest of the frozen envelope algorithm. Production hash stays in PostgreSQL."""

    return hashlib.sha256(canonical_envelope_text(envelope).encode("utf-8")).hexdigest()


def build_envelope(
    locator: SourceLocator,
    *,
    document_version_id: uuid.UUID,
    extraction_id: uuid.UUID,
    input_sha256: str,
) -> dict[str, object]:
    return {
        "locator_schema_version": LOCATOR_SCHEMA_VERSION,
        "document_version_id": str(document_version_id),
        "extraction_id": str(extraction_id),
        "input_sha256": input_sha256,
        "source_locator": locator.source_locator_fields(),
    }


def typed_axes(locator: SourceLocator) -> TypedAxes:
    if locator.locator_type in ("text", "html"):
        return TypedAxes(
            char_start=locator.start,
            char_end=locator.end,
            page_start=None,
            page_end=None,
            time_start_ms=None,
            time_end_ms=None,
        )
    if locator.locator_type == "pdf":
        return TypedAxes(
            char_start=None,
            char_end=None,
            page_start=locator.page_start,
            page_end=locator.page_end,
            time_start_ms=None,
            time_end_ms=None,
        )
    return TypedAxes(
        char_start=None,
        char_end=None,
        page_start=None,
        page_end=None,
        time_start_ms=locator.time_start_ms,
        time_end_ms=locator.time_end_ms,
    )


def _require_int(row: Mapping[str, object], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise LocatorRejected(LOCATOR_LOCATION_MAP_INVALID)
    return value


def _half_open_intersects(left_start: int, left_end: int, right_start: int, right_end: int) -> bool:
    return left_start < right_end and right_start < left_end


def _inclusive_intersects(left_start: int, left_end: int, right_start: int, right_end: int) -> bool:
    return left_start <= right_end and right_start <= left_end


def _parse_map_rows(
    location_map: LocationMap,
    kind: Literal["pdf_page", "subtitle_cue"],
) -> tuple[tuple[int, Mapping[str, object], int, int], ...]:
    if isinstance(location_map, (str, bytes)) or not isinstance(location_map, Sequence):
        raise LocatorRejected(LOCATOR_LOCATION_MAP_INVALID)
    parsed: list[tuple[int, Mapping[str, object], int, int]] = []
    for ordinal, raw in enumerate(location_map):
        if not isinstance(raw, Mapping):
            raise LocatorRejected(LOCATOR_LOCATION_MAP_INVALID)
        row_kind = raw.get("kind")
        if row_kind is not None and row_kind != kind:
            continue
        if row_kind is None:
            if kind == _PDF_KIND and "page_start" not in raw:
                continue
            if kind == _CUE_KIND and "time_start_ms" not in raw:
                continue
        char_start = _require_int(raw, "char_start")
        char_end = _require_int(raw, "char_end")
        if char_start < 0 or char_end < 0 or char_start >= char_end:
            raise LocatorRejected(LOCATOR_LOCATION_MAP_INVALID)
        if kind == _PDF_KIND:
            page_start = _require_int(raw, "page_start")
            page_end = _require_int(raw, "page_end")
            if page_start < 1 or page_end < page_start:
                raise LocatorRejected(LOCATOR_LOCATION_MAP_INVALID)
        else:
            time_start = _require_int(raw, "time_start_ms")
            time_end = _require_int(raw, "time_end_ms")
            if time_start < 0 or time_end <= time_start:
                raise LocatorRejected(LOCATOR_LOCATION_MAP_INVALID)
        parsed.append((ordinal, raw, char_start, char_end))
    if not parsed:
        raise LocatorRejected(LOCATOR_LOCATION_MAP_INVALID)
    for index, (_ordinal, _row, start, end) in enumerate(parsed):
        for _other_ordinal, _other_row, other_start, other_end in parsed[index + 1 :]:
            if _half_open_intersects(start, end, other_start, other_end):
                raise LocatorRejected(LOCATOR_LOCATION_MAP_INVALID)
    return tuple(parsed)


def _cross_axis_pdf(locator: SourceLocator, location_map: LocationMap) -> None:
    rows = _parse_map_rows(location_map, _PDF_KIND)
    if locator.page_start is None or locator.page_end is None:
        raise LocatorRejected(LOCATOR_PDF_PAGE_MISSING)
    char_hits: set[int] = set()
    page_hits: set[int] = set()
    for ordinal, row, char_start, char_end in rows:
        if _half_open_intersects(locator.start, locator.end, char_start, char_end):
            char_hits.add(ordinal)
        page_start = _require_int(row, "page_start")
        page_end = _require_int(row, "page_end")
        if _inclusive_intersects(locator.page_start, locator.page_end, page_start, page_end):
            page_hits.add(ordinal)
    if not char_hits or char_hits != page_hits:
        raise LocatorRejected(LOCATOR_CROSS_AXIS_MISMATCH)


def _cross_axis_media(locator: SourceLocator, location_map: LocationMap) -> None:
    rows = _parse_map_rows(location_map, _CUE_KIND)
    if locator.time_start_ms is None or locator.time_end_ms is None:
        raise LocatorRejected(LOCATOR_TIME_MISSING)
    char_hits: set[int] = set()
    time_hits: set[int] = set()
    for ordinal, row, char_start, char_end in rows:
        if _half_open_intersects(locator.start, locator.end, char_start, char_end):
            char_hits.add(ordinal)
        time_start = _require_int(row, "time_start_ms")
        time_end = _require_int(row, "time_end_ms")
        if _half_open_intersects(locator.time_start_ms, locator.time_end_ms, time_start, time_end):
            time_hits.add(ordinal)
    if not char_hits or char_hits != time_hits:
        raise LocatorRejected(LOCATOR_CROSS_AXIS_MISMATCH)


def _validate_axes(locator: SourceLocator) -> None:
    has_page = locator.page_start is not None or locator.page_end is not None
    has_time = locator.time_start_ms is not None or locator.time_end_ms is not None
    if locator.locator_type in ("text", "html"):
        if has_page or has_time:
            raise LocatorRejected(LOCATOR_AXIS_CONFLICT)
        return
    if locator.locator_type == "pdf":
        if has_time:
            raise LocatorRejected(LOCATOR_AXIS_CONFLICT)
        if locator.page_start is None or locator.page_end is None:
            raise LocatorRejected(LOCATOR_PDF_PAGE_MISSING)
        if locator.page_start < 1 or locator.page_end < locator.page_start:
            raise LocatorRejected(LOCATOR_PAGE_RANGE_INVALID)
        return
    if has_page:
        raise LocatorRejected(LOCATOR_AXIS_CONFLICT)
    if locator.time_start_ms is None or locator.time_end_ms is None:
        raise LocatorRejected(LOCATOR_TIME_MISSING)
    if locator.time_start_ms < 0 or locator.time_end_ms <= locator.time_start_ms:
        raise LocatorRejected(LOCATOR_TIME_RANGE_INVALID)


def map_locator(
    locator: SourceLocator,
    *,
    extracted_text: str,
    location_map: LocationMap,
    document_version_id: uuid.UUID,
    extraction_id: uuid.UUID,
    input_sha256: str,
    locator_ordinal: int,
) -> AcceptedLocator:
    if locator.start >= locator.end:
        raise LocatorRejected(LOCATOR_END_NOT_AFTER_START)
    text_len = len(extracted_text)
    if not 0 <= locator.start < locator.end <= text_len:
        raise LocatorRejected(LOCATOR_OUT_OF_RANGE)
    _validate_axes(locator)
    if locator.locator_type == "pdf":
        _cross_axis_pdf(locator, location_map)
    elif locator.locator_type in ("video", "audio"):
        _cross_axis_media(locator, location_map)
    excerpt = extracted_text[locator.start : locator.end]
    if len(excerpt.encode("utf-8")) > MAX_EVIDENCE_UTF8_BYTES:
        raise LocatorRejected(LOCATOR_EXCERPT_TOO_LARGE)
    envelope = build_envelope(
        locator,
        document_version_id=document_version_id,
        extraction_id=extraction_id,
        input_sha256=input_sha256,
    )
    return AcceptedLocator(
        locator_ordinal=locator_ordinal,
        evidence_text=excerpt,
        envelope=envelope,
        digest=canonical_locator_digest(envelope),
        axes=typed_axes(locator),
    )
