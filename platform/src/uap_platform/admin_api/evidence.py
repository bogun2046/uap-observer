"""Small bridge from frozen AI locators to canonical evidence spans.

This module intentionally reuses the WP8 locator mapper.  It does not define a
second locator normalisation or hashing algorithm.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast
from uuid import UUID

from psycopg import Connection
from psycopg.types.json import Jsonb

from uap_platform.knowledge.contracts import AnchorStatus, ExtractionAnchor, ExtractionRecord
from uap_platform.knowledge.handler import _source_candidates_from_items
from uap_platform.knowledge.mapping import map_knowledge_result
from uap_platform.object_registry import ObjectClient, read_verified_object

from .errors import AdminError


def materialize_ai_evidence(
    connection: Connection[dict[str, object]],
    object_client: ObjectClient | None,
    *,
    analysis_result_id: UUID,
    document_version_id: UUID,
    result_type: str,
    item_ordinal: int | None = None,
) -> dict[int, list[UUID]]:
    """Resolve AI evidence and ask the SECURITY DEFINER helper to persist spans."""

    if object_client is None:
        raise AdminError("api_dependency_unavailable")
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT result.result, result.validation_status,
                   run.input_sha256
              FROM core.analysis_results AS result
              JOIN ops.model_runs AS run
                ON run.id = result.model_run_id
               AND run.document_version_id = result.document_version_id
             WHERE result.id = %s
               AND result.document_version_id = %s
               AND result.result_type = %s::ops.model_task_type
            """,
            (analysis_result_id, document_version_id, result_type),
        )
        result_row = cursor.fetchone()
        cursor.execute(
            """
            SELECT e.id, e.output_sha256, e.location_map,
                   stored.bucket_name, stored.object_key,
                   stored.content_sha256, stored.byte_length
              FROM core.extractions AS e
              JOIN core.stored_objects AS stored ON stored.id = e.text_object_id
             WHERE e.document_version_id = %s
               AND e.outcome = 'succeeded'
             ORDER BY e.created_at DESC, e.id DESC
             LIMIT 1
            """,
            (document_version_id,),
        )
        extraction_row = cursor.fetchone()

    def value(row: object, key: str, index: int) -> Any:
        return row[key] if isinstance(row, Mapping) else row[index]  # type: ignore[index]

    if result_row is None:
        raise AdminError("editorial_ai_result_invalid")
    if str(value(result_row, "validation_status", 1)) != "valid":
        raise AdminError("editorial_ai_result_invalid")
    result = cast(Mapping[str, Any], value(result_row, "result", 0) or {})
    items = result.get("claims" if result_type == "claim_extraction" else "entities")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        raise AdminError("editorial_ai_result_invalid")
    selected: list[Mapping[str, Any]] = []
    for ordinal, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise AdminError("editorial_ai_result_invalid")
        if item_ordinal is None or item_ordinal == ordinal:
            selected.append(item)
    if item_ordinal is not None and not selected:
        raise AdminError("editorial_ai_item_not_found")
    if extraction_row is None:
        raise AdminError("editorial_evidence_extraction_mismatch")
    extraction_id = UUID(str(value(extraction_row, "id", 0)))
    input_sha256 = str(value(result_row, "input_sha256", 2))
    if str(value(extraction_row, "output_sha256", 1)) != input_sha256:
        raise AdminError("editorial_evidence_extraction_mismatch")
    try:
        text = read_verified_object(
            object_client,
            str(value(extraction_row, "bucket_name", 3)),
            str(value(extraction_row, "object_key", 4)),
            str(value(extraction_row, "content_sha256", 5)),
            int(value(extraction_row, "byte_length", 6)),
        ).decode("utf-8")
    except (RuntimeError, UnicodeDecodeError, LookupError) as error:
        raise AdminError("editorial_evidence_extraction_mismatch") from error
    raw_location_map = value(extraction_row, "location_map", 2)
    location_map = raw_location_map if isinstance(raw_location_map, list) else []
    record = ExtractionRecord(
        extraction_id=extraction_id,
        document_version_id=document_version_id,
        outcome="succeeded",
        output_sha256=input_sha256,
        stored_domain="derived",
        stored_sha256=str(value(extraction_row, "content_sha256", 5)),
    )
    candidates = _source_candidates_from_items(selected)
    report = map_knowledge_result(
        candidates=candidates,
        payload_anchor=ExtractionAnchor(status=AnchorStatus.MATCHED, extraction_id=extraction_id),
        records=[record],
        document_version_id=document_version_id,
        input_sha256=input_sha256,
        extracted_text=text,
        location_map=cast(Sequence[Mapping[str, object]], location_map),
        duplicate_policy="claim" if result_type == "claim_extraction" else "entity",
    )
    if not report.accepted_candidates or report.rejected_candidates:
        raise AdminError("editorial_evidence_provenance_mismatch")
    payload: list[dict[str, object]] = []
    by_ordinal: dict[int, list[int]] = {}
    for candidate in report.accepted_candidates:
        ids_for_item: list[int] = []
        for accepted in candidate.accepted_locators:
            axes = accepted.axes
            payload.append(
                {
                    "evidence_text": accepted.evidence_text,
                    "locator_type": cast(Mapping[str, object], accepted.envelope["source_locator"])[
                        "locator_type"
                    ],
                    "char_start": axes.char_start,
                    "char_end": axes.char_end,
                    "page_start": axes.page_start,
                    "page_end": axes.page_end,
                    "time_start_ms": axes.time_start_ms,
                    "time_end_ms": axes.time_end_ms,
                    "locator": accepted.envelope,
                }
            )
            ids_for_item.append(len(payload) - 1)
        by_ordinal[candidate.ordinal] = ids_for_item
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT audit.materialize_editorial_evidence(%s, %s, %s, %s::jsonb)",
            (document_version_id, extraction_id, input_sha256, Jsonb(payload)),
        )
        row = cursor.fetchone()
    if row is None:
        raise AdminError("api_internal_error")
    raw_span_ids = value(row, "materialize_editorial_evidence", 0) or []
    span_ids = [UUID(str(item)) for item in cast(Sequence[object], raw_span_ids)]
    result_map: dict[int, list[UUID]] = {}
    offset = 0
    for ordinal, indexes in by_ordinal.items():
        result_map[ordinal] = span_ids[offset : offset + len(indexes)]
        offset += len(indexes)
    return result_map
