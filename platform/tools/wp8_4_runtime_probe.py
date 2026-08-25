"""Exercise WP8.4 entity materialization as uap_worker: G8-14, G8-15, G8-16B."""

from __future__ import annotations

import io
import json
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

_PLATFORM_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLATFORM_ROOT / "src"
for _path in (str(_PLATFORM_ROOT), str(_SRC)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import psycopg  # noqa: E402
from psycopg.errors import Error as PsycopgError  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

from tools.wp8_1_runtime_probe import (  # noqa: E402
    connect,
    execute,
    grant_running_lease,
    insert_analysis,
    insert_extraction,
    insert_model_run,
    job_for,
    one,
    require,
    scalar,
    seed_document,
    sha256_text,
    sqlstate,
)
from uap_platform.config import load_settings  # noqa: E402
from uap_platform.knowledge.handler import ResolveEntitiesHandler  # noqa: E402
from uap_platform.knowledge.job_types import (  # noqa: E402
    CLAIMABLE_JOB_TYPES,
    PRE_ENTITY_HANDLER_JOB_TYPES,
)
from uap_platform.knowledge.metrics import failure_metrics  # noqa: E402
from uap_platform.knowledge.payload import parse_knowledge_payload  # noqa: E402
from uap_platform.knowledge.worker import ResolveEntitiesWorker  # noqa: E402
from uap_platform.object_registry import (  # noqa: E402
    ObjectClient,
    StorageDomain,
    put_verified,
    read_verified_object,
)
from uap_platform.object_store_init import build_client  # noqa: E402

CURRENT_HEAD = "0014_review_session_authority"
FIXTURE_TEXT = "The craft hovered over the hangar at dawn."
_FROZEN_G8_15_CODES = {
    "knowledge_payload_mismatch",
    "knowledge_schema_unsupported",
    "knowledge_extraction_mismatch",
    "knowledge_bundle_mismatch",
}
OBJECTS: ObjectClient | None = None


def _client() -> ObjectClient:
    if OBJECTS is None:
        raise RuntimeError("object client is not initialized")
    return OBJECTS


def _entities_result(*entities: dict[str, object]) -> dict[str, object]:
    return {"entities": list(entities)}


def _text_locator(start: int, end: int) -> dict[str, object]:
    return {"locator_type": "text", "start": start, "end": end}


def _bind_derived_object(
    admin: psycopg.Connection[Any], extraction_id: uuid.UUID, text: str
) -> None:
    payload = text.encode("utf-8")
    digest = sha256_text(text)
    physical = put_verified(
        _client(),
        StorageDomain.DERIVED,
        payload,
        "text/plain",
        expected_sha256=digest,
    )
    require("derived object hash", physical.content_sha256, digest)
    execute(
        admin,
        """
        UPDATE core.stored_objects AS stored
           SET bucket_name = %s,
               object_key = %s,
               byte_length = %s
          FROM core.extractions AS extraction
         WHERE extraction.id = %s
           AND stored.id = extraction.text_object_id
           AND stored.content_sha256 = %s
        """,
        physical.bucket_name,
        physical.object_key,
        physical.byte_length,
        extraction_id,
        digest,
    )
    verified = read_verified_object(
        _client(),
        physical.bucket_name,
        physical.object_key,
        physical.content_sha256,
        physical.byte_length,
    )
    require("verified derived bytes", verified, payload)


def _slice_of(body: str, token: str) -> tuple[int, int]:
    start = body.find(token)
    if start < 0:
        raise RuntimeError(f"fixture token {token!r} missing from {body!r}")
    return start, start + len(token)


def _default_entities(body: str) -> list[dict[str, object]]:
    craft = _slice_of(body, "craft")
    hovered = _slice_of(body, "hovered")
    hangar = _slice_of(body, "hangar")
    dawn = _slice_of(body, "dawn")
    return [
        {
            "name": "craft",
            "entity_type": "object",
            "evidence": [
                _text_locator(*craft),
                _text_locator(*hovered),
                _text_locator(*hangar),
            ],
        },
        {
            "name": "craft",
            "entity_type": "object",
            "evidence": [_text_locator(*dawn)],
        },
    ]


def _seed_entity_world(
    admin: psycopg.Connection[Any],
    tag: str,
    *,
    entities: dict[str, object] | None = None,
    extractor: str = "text",
) -> dict[str, Any]:
    _principal, document_version_id, _source = seed_document(admin, tag)
    body = f"{FIXTURE_TEXT} [{tag}]"
    input_sha = sha256_text(body)
    extraction_id = insert_extraction(
        admin, document_version_id, tag, input_sha, extractor, text=body
    )
    _bind_derived_object(admin, extraction_id, body)
    model_run_id = insert_model_run(
        admin,
        document_version_id=document_version_id,
        task_type="entity_extraction",
        input_sha256=input_sha,
        tag=tag,
    )
    result = entities or _entities_result(*_default_entities(body))
    analysis_id = insert_analysis(
        admin,
        model_run_id=model_run_id,
        document_version_id=document_version_id,
        result_type="entity_extraction",
        result=result,
    )
    job = job_for(admin, analysis_id, "entity_extraction")
    return {
        "document_version_id": document_version_id,
        "extraction_id": extraction_id,
        "model_run_id": model_run_id,
        "analysis_id": analysis_id,
        "job_id": job[0],
        "payload": job[3] if len(job) > 3 else None,
        "input_sha": input_sha,
        "frozen_text": body,
        "entities": result["entities"],
    }


def _payload_for(admin: psycopg.Connection[Any], job_id: uuid.UUID) -> dict[str, object]:
    raw = scalar(admin, "SELECT payload FROM ops.jobs WHERE id=%s", job_id)
    return dict(raw)


def _rows(
    connection: psycopg.Connection[Any], statement: str, *params: object
) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        return [tuple(row) for row in cursor.fetchall()]


def _as_int(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        return int(value)
    raise TypeError(f"expected int, got {type(value)!r}")


def _locator_bundle_item(body: str, locator: dict[str, object], ordinal: int) -> dict[str, object]:
    start = _as_int(locator["start"])
    end = _as_int(locator["end"])
    return {
        "locator_ordinal": ordinal,
        "evidence_text": body[start:end],
        "char_start": start,
        "char_end": end,
        "page_start": None,
        "page_end": None,
        "time_start_ms": None,
        "time_end_ms": None,
    }


def _valid_bundle(payload: dict[str, object], world: dict[str, Any]) -> dict[str, object]:
    accepted: list[dict[str, object]] = []
    frozen_text = str(world["frozen_text"])
    for ordinal, entity in enumerate(cast(list[dict[str, object]], world["entities"])):
        evidence = cast(list[dict[str, object]], entity["evidence"])
        accepted.append(
            {
                "ordinal": ordinal,
                "accepted_locators": [
                    _locator_bundle_item(frozen_text, loc, index)
                    for index, loc in enumerate(evidence)
                ],
                "rejected_locators": [],
            }
        )
    return {
        "bundle_schema_version": "knowledge-bundle.v2",
        "analysis_result_id": payload["analysis_result_id"],
        "analysis_result_sha256": payload["analysis_result_sha256"],
        "accepted_candidates": accepted,
        "rejected_candidates": [],
    }


def _run_handler(
    worker: psycopg.Connection[Any],
    job_id: uuid.UUID,
    attempt_id: uuid.UUID,
    token: uuid.UUID,
    payload: dict[str, object],
) -> str:
    handler = ResolveEntitiesHandler(worker, _client())
    return handler.handle(job_id, attempt_id, token, payload)


def _knowledge_counts(admin: psycopg.Connection[Any], analysis_id: uuid.UUID) -> tuple[int, int]:
    candidates = scalar(
        admin,
        "SELECT count(*) FROM core.entity_candidates WHERE analysis_result_id=%s",
        analysis_id,
    )
    evidence = scalar(
        admin,
        """
        SELECT count(*) FROM core.entity_candidate_evidence AS evidence
          JOIN core.entity_candidates AS candidate
            ON candidate.id = evidence.entity_candidate_id
         WHERE candidate.analysis_result_id = %s
        """,
        analysis_id,
    )
    return int(candidates), int(evidence)


def _extraction_object(admin: psycopg.Connection[Any], extraction_id: uuid.UUID) -> tuple[Any, ...]:
    return one(
        admin,
        """
        SELECT stored.bucket_name, stored.object_key, stored.content_sha256,
               stored.byte_length, stored.id
          FROM core.stored_objects AS stored
          JOIN core.extractions AS extraction
            ON extraction.text_object_id = stored.id
         WHERE extraction.id = %s
        """,
        extraction_id,
    )


def _claim_one_resolve(conn: psycopg.Connection[Any], worker_id: str) -> tuple[Any, ...] | None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT job_id, attempt_id, job_type, payload, lease_token
              FROM ops.claim_job('worker', %s, ARRAY['resolve_entities'], 60)
            """,
            (worker_id,),
        )
        row = cast(tuple[Any, ...] | None, cursor.fetchone())
    conn.commit()
    return None if row is None else tuple(row)


def _close_claimed_resolve_job(
    conn: psycopg.Connection[Any],
    claimed: tuple[Any, ...],
    handler: ResolveEntitiesHandler | None = None,
) -> str:
    """Close a claimed resolve_entities job through the production handler.

    There is no pre-handler succeeded bypass. Queued leftovers go to
    ResolveEntitiesHandler. Handler failures never fall back to succeeded.
    """

    job_id = uuid.UUID(str(claimed[0]))
    attempt_id = uuid.UUID(str(claimed[1]))
    payload = claimed[3]
    token = uuid.UUID(str(claimed[4]))
    active = handler if handler is not None else ResolveEntitiesHandler(conn, _client())
    try:
        return active.handle(job_id, attempt_id, token, payload)
    except PsycopgError as error:
        conn.rollback()
        if error.sqlstate == "40001":
            raise
        raise


def _claim_target_job(
    conn: psycopg.Connection[Any],
    *,
    worker_id: str,
    job_id: uuid.UUID,
    limit: int = 64,
) -> tuple[Any, ...]:
    for index in range(limit):
        row = _claim_one_resolve(conn, f"{worker_id}-{index}")
        if row is None:
            raise RuntimeError(f"g8-15 did not claim {job_id}")
        if row[0] == job_id:
            return row
        _close_claimed_resolve_job(conn, row)
    raise RuntimeError(f"g8-15 claim loop missed {job_id}")


def _drain_resolve_entities(
    conn: psycopg.Connection[Any], worker_id: str, limit: int = 256
) -> None:
    for index in range(limit):
        row = _claim_one_resolve(conn, f"{worker_id}-drain-{index}")
        if row is None:
            return
        _close_claimed_resolve_job(conn, row)


def _require_handler_fail_closed(
    admin: psycopg.Connection[Any],
    worker: psycopg.Connection[Any],
    world: dict[str, Any],
    tag: str,
    name: str,
    payload: dict[str, object] | None = None,
) -> str:
    if payload is None:
        payload = _payload_for(admin, world["job_id"])
    before_candidates, before_evidence = _knowledge_counts(admin, world["analysis_id"])
    attempt_id, token = grant_running_lease(admin, world["job_id"], f"wp8-4-15-{name}-{tag}")
    status = _run_handler(worker, world["job_id"], attempt_id, token, payload)
    after_candidates, after_evidence = _knowledge_counts(admin, world["analysis_id"])
    outcome = scalar(admin, "SELECT outcome::text FROM ops.job_attempts WHERE id=%s", attempt_id)
    error_code = scalar(admin, "SELECT error_code FROM ops.job_attempts WHERE id=%s", attempt_id)
    require(f"g8-15 {name} no new candidates", after_candidates, before_candidates)
    require(f"g8-15 {name} no new evidence", after_evidence, before_evidence)
    require(
        f"g8-15 {name} closed",
        outcome in {"terminal_failure", "retryable_failure"},
        True,
    )
    require(f"g8-15 {name} not succeeded", status != "succeeded", True)
    require(f"g8-15 {name} frozen code", error_code in _FROZEN_G8_15_CODES, True)
    return str(error_code)


def _snapshot_side_effects(admin: psycopg.Connection[Any]) -> tuple[int, int, int]:
    return (
        int(scalar(admin, "SELECT count(*) FROM core.entities")),
        int(scalar(admin, "SELECT count(*) FROM core.relations")),
        int(scalar(admin, "SELECT count(*) FROM core.claims WHERE subject_entity_id IS NOT NULL")),
    )


def g8_14(
    admin: psycopg.Connection[Any],
    worker: psycopg.Connection[Any],
    tag: str,
) -> dict[str, Any]:
    before = _snapshot_side_effects(admin)
    world = _seed_entity_world(admin, f"{tag}-14")
    job_id = world["job_id"]
    payload = _payload_for(admin, job_id)
    parse_knowledge_payload(payload, expected_result_type="entity_extraction")
    attempt_id, token = grant_running_lease(admin, job_id, f"wp8-4-g814-{tag}")
    status = _run_handler(worker, job_id, attempt_id, token, payload)
    rows = _rows(
        admin,
        """
        SELECT ordinal, proposed_name, proposed_entity_type::text, proposed_aliases,
               candidate_payload, status::text, evidence_span_id, resolved_entity_id
          FROM core.entity_candidates
         WHERE analysis_result_id = %s
         ORDER BY ordinal
        """,
        world["analysis_id"],
    )
    require("g8-14 handler status", status in {"succeeded", "success"}, True)
    require("g8-14 two candidates", len(rows), 2)
    require("g8-14 pending 0", rows[0][5], "pending")
    require("g8-14 pending 1", rows[1][5], "pending")
    require("g8-14 name 0", rows[0][1], "craft")
    require("g8-14 name 1 same as 0", rows[1][1], "craft")
    require("g8-14 type 0", rows[0][2], "object")
    require("g8-14 type 1", rows[1][2], "object")
    require("g8-14 aliases 0", list(rows[0][3]), [])
    require("g8-14 aliases 1", list(rows[1][3]), [])
    require("g8-14 span null 0", rows[0][6], None)
    require("g8-14 span null 1", rows[1][6], None)
    require("g8-14 unresolved 0", rows[0][7], None)
    require("g8-14 unresolved 1", rows[1][7], None)
    require("g8-14 distinct rows", rows[0][0] != rows[1][0], True)
    payload0 = rows[0][4]
    payload1 = rows[1][4]
    require("g8-14 payload name 0", payload0["name"], "craft")
    require("g8-14 payload type 0", payload0["entity_type"], "object")
    require("g8-14 payload no identifier", "identifier" not in payload0, True)
    require("g8-14 payload no description", "description" not in payload0, True)
    require("g8-14 payload1 no identifier", "identifier" not in payload1, True)

    evidence_rows = _rows(
        admin,
        """
        SELECT candidate.ordinal, evidence.evidence_ordinal
          FROM core.entity_candidate_evidence AS evidence
          JOIN core.entity_candidates AS candidate
            ON candidate.id = evidence.entity_candidate_id
         WHERE candidate.analysis_result_id = %s
         ORDER BY candidate.ordinal, evidence.evidence_ordinal
        """,
        world["analysis_id"],
    )
    require("g8-14 evidence 3+1", len(evidence_rows), 4)
    require(
        "g8-14 evidence ordinals",
        [(int(row[0]), int(row[1])) for row in evidence_rows],
        [(0, 0), (0, 1), (0, 2), (1, 0)],
    )
    slices = _rows(
        admin,
        """
        SELECT span.evidence_text
          FROM core.evidence_spans AS span
          JOIN core.entity_candidate_evidence AS link ON link.evidence_span_id = span.id
          JOIN core.entity_candidates AS candidate ON candidate.id = link.entity_candidate_id
         WHERE candidate.analysis_result_id = %s
           AND candidate.ordinal = 0
           AND link.evidence_ordinal = 0
        """,
        world["analysis_id"],
    )
    require("g8-14 evidence slice", slices[0][0], "craft")

    after = _snapshot_side_effects(admin)
    require("g8-14 no canonical entity", after[0], before[0])
    require("g8-14 no relations", after[1], before[1])
    require("g8-14 no claim subject backfill", after[2], before[2])
    job_status = scalar(admin, "SELECT status::text FROM ops.jobs WHERE id=%s", job_id)
    outcome = scalar(admin, "SELECT outcome::text FROM ops.job_attempts WHERE id=%s", attempt_id)
    metrics = scalar(admin, "SELECT metrics FROM ops.job_attempts WHERE id=%s", attempt_id)
    require("g8-14 job succeeded", job_status, "succeeded")
    require("g8-14 attempt succeeded", outcome, "succeeded")
    require("g8-14 materialized candidates", int(metrics["materialized_candidates"]), 2)
    require("g8-14 materialized locators", int(metrics["materialized_locators"]), 4)
    return {"passed": True, "job_status": job_status}


def g8_live_definitions(admin: psycopg.Connection[Any]) -> dict[str, Any]:
    """Assert the live 0012 function body, not just the migration source."""

    materialize = scalar(
        admin,
        """
        SELECT pg_get_functiondef(
            'core.materialize_entity_bundle(uuid, uuid, uuid, jsonb)'::regprocedure
        )
        """,
    )
    require("live entity materialize uses v_candidate_id", "v_candidate_id" in materialize, True)
    require("live entity materialize exact keys", "_jsonb_keys_exact" in materialize, True)
    require(
        "live entity reuses source locator helper",
        "_claim_source_locator_json" in materialize,
        True,
    )
    require("live entity no canonical insert", "INSERT INTO core.entities" not in materialize, True)
    require("live entity no claim update", "UPDATE core.claims" not in materialize, True)
    require("live entity pending only", "pending" in materialize, True)
    require("live entity aliases empty", "'[]'::jsonb" in materialize, True)
    return {"passed": True}


def g8_15(
    admin: psycopg.Connection[Any],
    worker: psycopg.Connection[Any],
    tag: str,
) -> dict[str, Any]:
    results: dict[str, object] = {}

    empty_world = _seed_entity_world(admin, f"{tag}-15-empty", entities=_entities_result())
    empty_payload = _payload_for(admin, empty_world["job_id"])
    empty_attempt, empty_token = grant_running_lease(
        admin, empty_world["job_id"], f"wp8-4-empty-{tag}"
    )
    empty_status = _run_handler(
        worker, empty_world["job_id"], empty_attempt, empty_token, empty_payload
    )
    empty_count, empty_evidence = _knowledge_counts(admin, empty_world["analysis_id"])
    empty_metrics = scalar(admin, "SELECT metrics FROM ops.job_attempts WHERE id=%s", empty_attempt)
    require("g8-15 empty status", empty_status in {"succeeded", "success"}, True)
    require("g8-15 empty candidates", empty_count, 0)
    require("g8-15 empty evidence", empty_evidence, 0)
    require("g8-15 empty_valid_result", empty_metrics["empty_valid_result"], True)
    results["empty"] = True

    empty_missing_world = _seed_entity_world(
        admin, f"{tag}-15-empty-keys", entities=_entities_result()
    )
    empty_missing_payload = _payload_for(admin, empty_missing_world["job_id"])
    empty_missing_attempt, empty_missing_token = grant_running_lease(
        admin, empty_missing_world["job_id"], f"wp8-4-empty-keys-{tag}"
    )
    missing_keys_bundle = {
        "bundle_schema_version": "knowledge-bundle.v2",
        "analysis_result_id": empty_missing_payload["analysis_result_id"],
        "analysis_result_sha256": empty_missing_payload["analysis_result_sha256"],
    }
    missing_keys_state = "ok"
    with worker.cursor() as cursor:
        cursor.execute("SAVEPOINT knowledge_materialize")
        try:
            cursor.execute(
                "SELECT core.materialize_entity_bundle(%s, %s, %s, %s::jsonb)",
                (
                    empty_missing_world["job_id"],
                    empty_missing_attempt,
                    empty_missing_token,
                    json.dumps(missing_keys_bundle),
                ),
            )
        except psycopg.Error as error:
            missing_keys_state = error.sqlstate or "none"
            cursor.execute("ROLLBACK TO SAVEPOINT knowledge_materialize")
        cursor.execute(
            """
            SELECT ops.finish_knowledge_job(
                %s, %s, %s, 'terminal_failure'::ops.attempt_outcome,
                NULL, %s, %s, NULL, %s
            )
            """,
            (
                empty_missing_world["job_id"],
                empty_missing_attempt,
                empty_missing_token,
                "knowledge_bundle_mismatch",
                "knowledge_bundle_mismatch",
                Jsonb(failure_metrics("knowledge_bundle_mismatch")),
            ),
        )
    worker.commit()
    require("g8-15 empty missing keys sqlstate", missing_keys_state, "22023")
    empty_missing_outcome = scalar(
        admin,
        "SELECT outcome::text FROM ops.job_attempts WHERE id=%s",
        empty_missing_attempt,
    )
    require("g8-15 empty missing keys closed", empty_missing_outcome, "terminal_failure")
    results["empty_missing_keys"] = True

    body_mix = f"{FIXTURE_TEXT} [{tag}-15-mix]"
    craft = _slice_of(body_mix, "craft")
    mixed = _entities_result(
        {
            "name": "craft",
            "entity_type": "object",
            "evidence": [_text_locator(*craft)],
        },
        {
            "name": "bad-range",
            "entity_type": "object",
            "evidence": [_text_locator(0, 9999)],
        },
        {
            "name": "inverted",
            "entity_type": "object",
            "evidence": [_text_locator(10, 4)],
        },
    )
    mixed_world = _seed_entity_world(admin, f"{tag}-15-mix", entities=mixed)
    mixed_payload = _payload_for(admin, mixed_world["job_id"])
    mixed_attempt, mixed_token = grant_running_lease(
        admin, mixed_world["job_id"], f"wp8-4-mix-{tag}"
    )
    mixed_status = _run_handler(
        worker, mixed_world["job_id"], mixed_attempt, mixed_token, mixed_payload
    )
    mixed_count, mixed_evidence = _knowledge_counts(admin, mixed_world["analysis_id"])
    require("g8-15 partial succeeded", mixed_status in {"succeeded", "success"}, True)
    require("g8-15 partial one candidate", mixed_count, 1)
    require("g8-15 partial one evidence", mixed_evidence, 1)
    results["partial"] = True

    unmap = _entities_result(
        {
            "name": "Nope",
            "entity_type": "object",
            "evidence": [_text_locator(0, 9999)],
        },
        {
            "name": "Also no",
            "entity_type": "object",
            "evidence": [_text_locator(8, 3)],
        },
    )
    unmap_world = _seed_entity_world(admin, f"{tag}-15-unmap", entities=unmap)
    unmap_payload = _payload_for(admin, unmap_world["job_id"])
    unmap_attempt, unmap_token = grant_running_lease(
        admin, unmap_world["job_id"], f"wp8-4-unmap-{tag}"
    )
    unmap_status = _run_handler(
        worker, unmap_world["job_id"], unmap_attempt, unmap_token, unmap_payload
    )
    unmap_count, unmap_evidence = _knowledge_counts(admin, unmap_world["analysis_id"])
    unmap_outcome = scalar(
        admin, "SELECT outcome::text FROM ops.job_attempts WHERE id=%s", unmap_attempt
    )
    require("g8-15 unmappable not succeeded", unmap_status in {"failed", "dead"}, True)
    require("g8-15 unmappable zero candidates", unmap_count, 0)
    require("g8-15 unmappable zero evidence", unmap_evidence, 0)
    require("g8-15 unmappable terminal", unmap_outcome, "terminal_failure")
    results["unmappable"] = True

    raise_world = _seed_entity_world(admin, f"{tag}-15-raise")
    raise_payload = _payload_for(admin, raise_world["job_id"])
    raise_attempt, raise_token = grant_running_lease(
        admin, raise_world["job_id"], f"wp8-4-raise-{tag}"
    )
    bad_bundle = {
        "bundle_schema_version": "knowledge-bundle.v2",
        "analysis_result_id": raise_payload["analysis_result_id"],
        "analysis_result_sha256": raise_payload["analysis_result_sha256"],
        "accepted_candidates": [{"ordinal": 99, "accepted_locators": [], "rejected_locators": []}],
        "rejected_candidates": [],
    }
    with worker.cursor() as cursor:
        cursor.execute("SAVEPOINT knowledge_materialize")
        try:
            cursor.execute(
                "SELECT core.materialize_entity_bundle(%s, %s, %s, %s::jsonb)",
                (
                    raise_world["job_id"],
                    raise_attempt,
                    raise_token,
                    json.dumps(bad_bundle),
                ),
            )
            materialize_state = "ok"
        except psycopg.Error as error:
            materialize_state = error.sqlstate or "none"
            cursor.execute("ROLLBACK TO SAVEPOINT knowledge_materialize")
    worker.commit()
    require("g8-15 materialize raise", materialize_state, "22023")
    still_running = scalar(
        admin, "SELECT outcome::text FROM ops.job_attempts WHERE id=%s", raise_attempt
    )
    require("g8-15 raise attempt still running before finish", still_running, "running")
    results["sql_raise"] = True

    expire_world = _seed_entity_world(admin, f"{tag}-15-exp")
    expire_payload = _payload_for(admin, expire_world["job_id"])
    expire_attempt, expire_token = grant_running_lease(
        admin, expire_world["job_id"], f"wp8-4-exp-{tag}"
    )
    execute(
        admin,
        """
        UPDATE ops.jobs
           SET lease_expires_at = clock_timestamp() - interval '1 second'
         WHERE id=%s
        """,
        expire_world["job_id"],
    )
    state_40001 = "ok"
    try:
        _run_handler(worker, expire_world["job_id"], expire_attempt, expire_token, expire_payload)
    except psycopg.Error as error:
        state_40001 = error.sqlstate or "none"
    expire_outcome = scalar(
        admin, "SELECT outcome::text FROM ops.job_attempts WHERE id=%s", expire_attempt
    )
    require("g8-15 expired lease 40001", state_40001, "40001")
    require("g8-15 expired not closed", expire_outcome, "running")
    results["lease_40001"] = True

    tampers = _g8_15_payload_and_bundle(admin, worker, tag)
    object_outcomes = _g8_15_object_and_slice(admin, worker, tag)
    replay = _g8_15_replay(admin, worker, tag)
    once = _g8_15_at_least_once(admin, worker, tag)
    return {
        "passed": True,
        **results,
        "tamper": tampers,
        "object": object_outcomes,
        "replay": replay,
        "at_least_once": once,
    }


def _g8_15_payload_and_bundle(
    admin: psycopg.Connection[Any],
    worker: psycopg.Connection[Any],
    tag: str,
) -> dict[str, str]:
    outcomes: dict[str, str] = {}
    cases = {
        "model_run_id": str(uuid.uuid4()),
        "payload_schema_version": "knowledge.v1",
        "analysis_result_sha256": "c" * 64,
        "input_sha256": "d" * 64,
        "extraction_id": str(uuid.uuid4()),
        "analysis_result_id": str(uuid.uuid4()),
        "missing_key": None,
        "illegal_uuid": "not-a-uuid",
    }
    seed_tags = [f"{tag}-15-{name}" for name in cases]
    require("g8-15 payload seed tags unique", len(seed_tags) == len(set(seed_tags)), True)
    for name, value in cases.items():
        world = _seed_entity_world(admin, f"{tag}-15-{name}")
        payload = _payload_for(admin, world["job_id"])
        if name == "missing_key":
            del payload["model_run_id"]
        elif name == "illegal_uuid":
            payload["document_version_id"] = value
        else:
            payload[name] = value
        outcomes[name] = _require_handler_fail_closed(admin, worker, world, tag, name, payload)

    tampers = {
        "hash": "bundle hash",
        "ordinal": "accepted ordinal",
        "missing_candidate": "omitted candidate",
        "missing_keys": "missing accepted/rejected keys",
        "locator_ordinal": "locator ordinal",
        "locator_axes": "locator content axes",
        "missing_locator": "omitted source evidence ordinal",
    }
    bundle_tags = [f"{tag}-15b-{name}" for name in tampers]
    require("g8-15 bundle seed tags unique", len(bundle_tags) == len(set(bundle_tags)), True)
    for name in tampers:
        world = _seed_entity_world(admin, f"{tag}-15b-{name}")
        payload = _payload_for(admin, world["job_id"])
        attempt_id, token = grant_running_lease(admin, world["job_id"], f"wp8-4-15b-{name}-{tag}")
        bundle = _valid_bundle(payload, world)
        if name == "hash":
            bundle["analysis_result_sha256"] = "e" * 64
        elif name == "ordinal":
            accepted_raw = bundle["accepted_candidates"]
            first = dict(cast(list[dict[str, object]], accepted_raw)[0])
            first["ordinal"] = 99
            bundle["accepted_candidates"] = [first]
        elif name == "missing_candidate":
            bundle["accepted_candidates"] = []
            bundle["rejected_candidates"] = []
        elif name == "locator_ordinal":
            accepted_raw = cast(list[dict[str, object]], bundle["accepted_candidates"])
            candidate = dict(accepted_raw[0])
            locators_raw = cast(list[dict[str, object]], candidate["accepted_locators"])
            locator = dict(locators_raw[0])
            locator["locator_ordinal"] = 99
            candidate["accepted_locators"] = [locator]
            bundle["accepted_candidates"] = [candidate]
        elif name == "locator_axes":
            accepted_raw = cast(list[dict[str, object]], bundle["accepted_candidates"])
            candidate = dict(accepted_raw[0])
            locators_raw = cast(list[dict[str, object]], candidate["accepted_locators"])
            locator = dict(locators_raw[0])
            locator["char_start"] = _as_int(locator["char_start"]) + 1
            candidate["accepted_locators"] = [locator]
            bundle["accepted_candidates"] = [candidate]
        elif name == "missing_locator":
            accepted_raw = cast(list[dict[str, object]], bundle["accepted_candidates"])
            candidate = dict(accepted_raw[0])
            locators_raw = cast(list[dict[str, object]], candidate["accepted_locators"])
            candidate["accepted_locators"] = [locators_raw[0]]
            candidate["rejected_locators"] = []
            bundle["accepted_candidates"] = [candidate, *accepted_raw[1:]]
        else:
            del bundle["accepted_candidates"]
            del bundle["rejected_candidates"]
        before_c, before_e = _knowledge_counts(admin, world["analysis_id"])
        with worker.cursor() as cursor:
            cursor.execute("SAVEPOINT knowledge_materialize")
            try:
                cursor.execute(
                    "SELECT core.materialize_entity_bundle(%s, %s, %s, %s::jsonb)",
                    (world["job_id"], attempt_id, token, json.dumps(bundle)),
                )
                sqlstate_seen = "ok"
            except psycopg.Error as error:
                sqlstate_seen = error.sqlstate or "none"
                cursor.execute("ROLLBACK TO SAVEPOINT knowledge_materialize")
            cursor.execute(
                """
                SELECT ops.finish_knowledge_job(
                    %s, %s, %s, 'terminal_failure'::ops.attempt_outcome,
                    NULL, %s, %s, NULL, %s
                )
                """,
                (
                    world["job_id"],
                    attempt_id,
                    token,
                    "knowledge_bundle_mismatch",
                    "knowledge_bundle_mismatch",
                    Jsonb(failure_metrics("knowledge_bundle_mismatch")),
                ),
            )
        worker.commit()
        after_c, after_e = _knowledge_counts(admin, world["analysis_id"])
        outcome = scalar(
            admin, "SELECT outcome::text FROM ops.job_attempts WHERE id=%s", attempt_id
        )
        require(f"g8-15 bundle {name} sqlstate", sqlstate_seen, "22023")
        require(f"g8-15 bundle {name} no rows", after_c, before_c)
        require(f"g8-15 bundle {name} no evidence", after_e, before_e)
        require(f"g8-15 bundle {name} closed", outcome, "terminal_failure")
        outcomes[f"bundle-{name}"] = "knowledge_bundle_mismatch"
    return outcomes


def _g8_15_object_and_slice(
    admin: psycopg.Connection[Any],
    worker: psycopg.Connection[Any],
    tag: str,
) -> dict[str, str]:
    outcomes: dict[str, str] = {}
    happy = _seed_entity_world(admin, f"{tag}-15-slice")
    payload = _payload_for(admin, happy["job_id"])
    attempt_id, token = grant_running_lease(admin, happy["job_id"], f"wp8-4-15-slice-{tag}")
    status = _run_handler(worker, happy["job_id"], attempt_id, token, payload)
    require("g8-15 handler slice succeeded", status in {"succeeded", "success"}, True)
    stored = scalar(
        admin,
        """
        SELECT span.evidence_text
          FROM core.evidence_spans AS span
          JOIN core.entity_candidate_evidence AS link ON link.evidence_span_id = span.id
          JOIN core.entity_candidates AS candidate ON candidate.id = link.entity_candidate_id
         WHERE candidate.analysis_result_id = %s
           AND candidate.ordinal = 0
           AND link.evidence_ordinal = 0
        """,
        happy["analysis_id"],
    )
    require("g8-15 evidence slice", stored, "craft")
    outcomes["slice"] = "ok"

    content = _seed_entity_world(admin, f"{tag}-15-object-content")
    bucket, key, _digest, _length, _stored_id = _extraction_object(admin, content["extraction_id"])
    tampered = b"TAMPERED CONTENT that is not the frozen derived slice"
    _client().put_object(
        str(bucket),
        str(key),
        io.BytesIO(tampered),
        len(tampered),
        "text/plain",
        {"sha256": "tampered"},
    )
    outcomes["object_content"] = _require_handler_fail_closed(
        admin, worker, content, tag, "object_content"
    )

    length = _seed_entity_world(admin, f"{tag}-15-object-length")
    _bucket, _key, _digest, _length, stored_id = _extraction_object(admin, length["extraction_id"])
    execute(
        admin,
        "UPDATE core.stored_objects SET byte_length = byte_length + 1 WHERE id=%s",
        stored_id,
    )
    outcomes["object_length"] = _require_handler_fail_closed(
        admin, worker, length, tag, "object_length"
    )
    return outcomes


def _g8_15_replay(
    admin: psycopg.Connection[Any],
    worker: psycopg.Connection[Any],
    tag: str,
) -> dict[str, Any]:
    replay = _seed_entity_world(admin, f"{tag}-15-replay")
    payload = _payload_for(admin, replay["job_id"])
    attempt_id, token = grant_running_lease(admin, replay["job_id"], f"wp8-4-replay-{tag}")
    bundle = _valid_bundle(payload, replay)
    with worker.cursor() as cursor:
        cursor.execute(
            "SELECT core.materialize_entity_bundle(%s, %s, %s, %s::jsonb)",
            (replay["job_id"], attempt_id, token, json.dumps(bundle)),
        )
        cursor.execute(
            "SELECT core.materialize_entity_bundle(%s, %s, %s, %s::jsonb)",
            (replay["job_id"], attempt_id, token, json.dumps(bundle)),
        )
        metrics = {
            "schema_version": "knowledge-attempt-metrics.v1",
            "input_candidates": 2,
            "materialized_candidates": 2,
            "input_locators": 4,
            "materialized_locators": 4,
            "rejected_candidates": 0,
            "rejected_locators": 0,
            "empty_valid_result": False,
            "rejected_by_code": {},
            "samples": [],
        }
        cursor.execute(
            """
            SELECT ops.finish_knowledge_job(
                %s, %s, %s, 'succeeded'::ops.attempt_outcome,
                NULL, NULL, NULL, NULL, %s
            )
            """,
            (replay["job_id"], attempt_id, token, Jsonb(metrics)),
        )
    worker.commit()
    replay_count, replay_evidence = _knowledge_counts(admin, replay["analysis_id"])
    require("g8-15 replay two candidates", replay_count, 2)
    require("g8-15 replay four evidence", replay_evidence, 4)
    return {"candidates": replay_count, "evidence": replay_evidence}


def _g8_15_at_least_once(
    admin: psycopg.Connection[Any],
    worker: psycopg.Connection[Any],
    tag: str,
) -> dict[str, Any]:
    _drain_resolve_entities(worker, f"wp8-4-drain-{tag}")
    race = _seed_entity_world(admin, f"{tag}-15-race")
    job_id = race["job_id"]
    worker_b = connect("uap_worker")
    worker_b.autocommit = False
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(_claim_one_resolve, worker, f"wp8-4-race-a-{tag}")
            future_b = pool.submit(_claim_one_resolve, worker_b, f"wp8-4-race-b-{tag}")
            claimed_a = future_a.result()
            claimed_b = future_b.result()
        hits = [row for row in (claimed_a, claimed_b) if row is not None and row[0] == job_id]
        require("g8-15 race at most one claim", len(hits) <= 1, True)
        require("g8-15 race claimed our job", len(hits), 1)
        winner = hits[0]
        winner_conn = worker if claimed_a is not None and claimed_a[0] == job_id else worker_b
        payload = _payload_for(admin, job_id)
        status = ResolveEntitiesHandler(winner_conn, _client()).handle(
            winner[0], winner[1], winner[4], payload
        )
        require("g8-15 race winner succeeded", status in {"succeeded", "success"}, True)

        reclaim = _seed_entity_world(admin, f"{tag}-15-reclaim")
        first = _claim_target_job(
            worker, worker_id=f"wp8-4-reclaim-a-{tag}", job_id=reclaim["job_id"]
        )
        attempt_a = first[1]
        token_a = first[4]
        execute(
            admin,
            """
            UPDATE ops.jobs
               SET lease_expires_at = clock_timestamp() - interval '1 second'
             WHERE id=%s
            """,
            reclaim["job_id"],
        )
        second = _claim_target_job(
            worker_b, worker_id=f"wp8-4-reclaim-b-{tag}", job_id=reclaim["job_id"]
        )
        require("g8-15 expiry new attempt", second[1] != attempt_a, True)
        expired_outcome = scalar(
            admin, "SELECT outcome::text FROM ops.job_attempts WHERE id=%s", attempt_a
        )
        expired_code = scalar(
            admin, "SELECT error_code FROM ops.job_attempts WHERE id=%s", attempt_a
        )
        require("g8-15 expired attempt closed", expired_outcome, "retryable_failure")
        require("g8-15 expired error_code", expired_code, "lease_expired")

        state_40001 = "ok"
        try:
            _run_handler(
                worker,
                reclaim["job_id"],
                attempt_a,
                token_a,
                _payload_for(admin, reclaim["job_id"]),
            )
        except psycopg.Error as error:
            state_40001 = error.sqlstate or "none"
        require("g8-15 expired first 40001", state_40001, "40001")

        second_payload = _payload_for(admin, reclaim["job_id"])
        reclaim_status = ResolveEntitiesHandler(worker_b, _client()).handle(
            second[0], second[1], second[4], second_payload
        )
        require("g8-15 reclaim succeeded", reclaim_status in {"succeeded", "success"}, True)

        succeeded = int(
            scalar(
                admin,
                """
                SELECT count(*) FROM ops.job_attempts
                 WHERE job_id=%s AND outcome='succeeded'
                """,
                reclaim["job_id"],
            )
        )
        running = int(
            scalar(
                admin,
                """
                SELECT count(*) FROM ops.job_attempts
                 WHERE job_id=%s AND outcome='running'
                """,
                reclaim["job_id"],
            )
        )
        open_attempts = int(
            scalar(
                admin,
                """
                SELECT count(*) FROM ops.job_attempts
                 WHERE job_id=%s AND finished_at IS NULL
                """,
                reclaim["job_id"],
            )
        )
        require("g8-15 at most one succeeded", succeeded <= 1, True)
        require("g8-15 exactly one succeeded", succeeded, 1)
        require("g8-15 no running attempts", running, 0)
        require("g8-15 all attempts closed", open_attempts, 0)
        return {
            "race_claimed": 1,
            "reclaim_succeeded": 1,
            "expired_closed": expired_outcome,
        }
    finally:
        worker_b.close()


def g8_16b(
    admin: psycopg.Connection[Any],
    worker: psycopg.Connection[Any],
    tag: str,
) -> dict[str, Any]:
    world = _seed_entity_world(admin, f"{tag}-16b")
    job_id = world["job_id"]
    queued = scalar(
        admin,
        "SELECT status::text FROM ops.jobs WHERE id=%s AND job_type='resolve_entities'",
        job_id,
    )
    require("g8-16b queued", queued, "queued")

    inactive = ResolveEntitiesWorker.from_settings(
        worker,
        worker_id=f"wp8-4-pre-{tag}",
        entities_handler_active=False,
        lease_seconds=30,
    )
    require(
        "g8-16b inactive types omit entities",
        "resolve_entities" not in inactive.job_types,
        True,
    )
    require(
        "g8-16b inactive platform set is pre-entity",
        inactive.job_types == PRE_ENTITY_HANDLER_JOB_TYPES,
        True,
    )
    require(
        "g8-16b inactive platform set is claimable",
        inactive.job_types == CLAIMABLE_JOB_TYPES,
        True,
    )
    require(
        "g8-16b inactive entities consumer requests no types",
        inactive.claim_job_types == (),
        True,
    )
    pre_claim = inactive.claim_one()
    require("g8-16b not claimed before activation", pre_claim is None, True)
    still_queued = scalar(admin, "SELECT status::text FROM ops.jobs WHERE id=%s", job_id)
    require("g8-16b still queued after inactive claim_one", still_queued, "queued")

    with worker.cursor() as cursor:
        cursor.execute("SAVEPOINT g8_16b_preentity_control")
        try:
            cursor.execute(
                """
                SELECT job_id, job_type
                  FROM ops.claim_job('worker', %s, %s::text[], 30)
                """,
                (
                    f"wp8-4-preentity-control-{tag}",
                    list(PRE_ENTITY_HANDLER_JOB_TYPES),
                ),
            )
            control = cursor.fetchone()
            if control is not None:
                require(
                    "g8-16b pre-entity control type",
                    str(control[1]) != "resolve_entities",
                    True,
                )
                require(
                    "g8-16b pre-entity control missed our job",
                    control[0] != job_id,
                    True,
                )
        finally:
            cursor.execute("ROLLBACK TO SAVEPOINT g8_16b_preentity_control")
    worker.commit()
    queued_after_control = scalar(admin, "SELECT status::text FROM ops.jobs WHERE id=%s", job_id)
    require("g8-16b still queued after pre-entity control", queued_after_control, "queued")

    active = ResolveEntitiesWorker.from_settings(
        worker,
        worker_id=f"wp8-4-post-{tag}",
        lease_seconds=60,
    )
    require(
        "g8-16b active contains resolve_entities",
        "resolve_entities" in active.job_types,
        True,
    )
    require(
        "g8-16b production default claim_job_types",
        active.claim_job_types == ("resolve_entities",),
        True,
    )
    require(
        "g8-16b active excludes relations",
        "resolve_relations" not in active.job_types,
        True,
    )
    require(
        "g8-16b active claim_job_types only entities",
        active.claim_job_types == ("resolve_entities",),
        True,
    )

    consumed: tuple[Any, ...] | None = None
    status = None
    for index in range(64):
        claimed = ResolveEntitiesWorker.from_settings(
            worker,
            worker_id=f"wp8-4-post-{tag}-{index}",
            lease_seconds=60,
        ).claim_one()
        if claimed is None:
            break
        if claimed[0] == job_id:
            consumed = claimed
            status = active.dispatch(claimed)
            break
        _close_claimed_resolve_job(worker, claimed)
    if consumed is None:
        raise RuntimeError("g8-16b did not claim the queued resolve_entities job")
    require("g8-16b claimed type", consumed[2], "resolve_entities")
    require("g8-16b consume succeeded", status in {"succeeded", "success"}, True)
    return {"passed": True, "claimed_job": str(consumed[0])}


def g8_permissions(admin: psycopg.Connection[Any], tag: str) -> dict[str, Any]:
    del admin, tag
    states: dict[str, str] = {}
    for role in (
        "uap_api",
        "uap_publisher",
        "uap_scheduler",
        "uap_model_governance",
        "uap_public_reader",
    ):
        try:
            other = connect(role)
            states[role] = sqlstate(
                other,
                "SELECT core.materialize_entity_bundle(%s, %s, %s, '{}'::jsonb)",
                uuid.uuid4(),
                uuid.uuid4(),
                uuid.uuid4(),
            )
            other.close()
        except Exception as error:
            states[role] = getattr(error, "sqlstate", None) or "42501"
    require("api cannot materialize entities", states["uap_api"], "42501")
    return {"passed": True, "states": states}


def main() -> None:
    global OBJECTS
    tag = uuid.uuid4().hex[:10]
    OBJECTS = cast(ObjectClient, build_client(load_settings()))
    admin = connect()
    worker = connect("uap_worker")
    worker.autocommit = False
    head = scalar(admin, "SELECT version_num FROM public.alembic_version")
    require("alembic head", head, CURRENT_HEAD)
    _drain_resolve_entities(worker, f"wp8-4-startup-drain-{tag}")
    results = {
        "head": head,
        "live_definitions": g8_live_definitions(admin),
        "G8-14": g8_14(admin, worker, tag),
        "G8-15": g8_15(admin, worker, tag),
        "G8-16B": g8_16b(admin, worker, tag),
        "permissions": g8_permissions(admin, tag),
    }
    print(json.dumps(results, indent=2, sort_keys=True, default=str))
    failed = [
        name
        for name, payload in results.items()
        if isinstance(payload, dict) and payload.get("passed") is False
    ]
    if failed:
        raise SystemExit(f"WP8.4 runtime probe failed: {', '.join(failed)}")
    print("WP8.4 runtime probe passed: G8-14 G8-15 G8-16B")


if __name__ == "__main__":
    main()
