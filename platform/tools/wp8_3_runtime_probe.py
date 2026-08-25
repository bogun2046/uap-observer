"""Exercise WP8.3 claim materialization as uap_worker: G8-11, G8-12, G8-13, G8-16A."""

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
from uap_platform.knowledge.handler import ResolveClaimsHandler  # noqa: E402
from uap_platform.knowledge.job_types import PRE_CLAIM_HANDLER_JOB_TYPES  # noqa: E402
from uap_platform.knowledge.metrics import failure_metrics  # noqa: E402
from uap_platform.knowledge.payload import parse_knowledge_payload  # noqa: E402
from uap_platform.knowledge.worker import ResolveClaimsWorker  # noqa: E402
from uap_platform.object_registry import (  # noqa: E402
    ObjectClient,
    StorageDomain,
    put_verified,
    read_verified_object,
)
from uap_platform.object_store_init import build_client  # noqa: E402

CURRENT_HEAD = "0015_review_case_lifecycle"
FIXTURE_TEXT = "The craft hovered over the hangar at dawn."
CLAIM_TEXT = "A craft hovered over the hangar."
_FROZEN_G8_13_CODES = {
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


def _claims_result(*claims: dict[str, object]) -> dict[str, object]:
    return {"claims": list(claims)}


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


def _seed_claim_world(
    admin: psycopg.Connection[Any],
    tag: str,
    *,
    claims: dict[str, object] | None = None,
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
        task_type="claim_extraction",
        input_sha256=input_sha,
        tag=tag,
    )
    start = body.find("craft")
    end = start + len("craft hovered")
    result = claims or _claims_result(
        {"claim": CLAIM_TEXT, "evidence": [_text_locator(start, end)]}
    )
    analysis_id = insert_analysis(
        admin,
        model_run_id=model_run_id,
        document_version_id=document_version_id,
        result_type="claim_extraction",
        result=result,
    )
    job = job_for(admin, analysis_id, "claim_extraction")
    return {
        "document_version_id": document_version_id,
        "extraction_id": extraction_id,
        "model_run_id": model_run_id,
        "analysis_id": analysis_id,
        "job_id": job[0],
        "payload": job[3] if len(job) > 3 else None,
        "input_sha": input_sha,
        "frozen_text": body,
        "start": start,
        "end": end,
    }


def _payload_for(admin: psycopg.Connection[Any], job_id: uuid.UUID) -> dict[str, object]:
    raw = scalar(admin, "SELECT payload FROM ops.jobs WHERE id=%s", job_id)
    return dict(raw)


def _valid_bundle(
    payload: dict[str, object], frozen_text: str, start: int, end: int
) -> dict[str, object]:
    return {
        "bundle_schema_version": "knowledge-bundle.v2",
        "analysis_result_id": payload["analysis_result_id"],
        "analysis_result_sha256": payload["analysis_result_sha256"],
        "accepted_candidates": [
            {
                "ordinal": 0,
                "accepted_locators": [
                    {
                        "locator_ordinal": 0,
                        "evidence_text": frozen_text[start:end],
                        "char_start": start,
                        "char_end": end,
                        "page_start": None,
                        "page_end": None,
                        "time_start_ms": None,
                        "time_end_ms": None,
                    }
                ],
                "rejected_locators": [],
            }
        ],
        "rejected_candidates": [],
    }


def _run_handler(
    worker: psycopg.Connection[Any],
    job_id: uuid.UUID,
    attempt_id: uuid.UUID,
    token: uuid.UUID,
    payload: dict[str, object],
) -> str:
    handler = ResolveClaimsHandler(worker, _client())
    return handler.handle(job_id, attempt_id, token, payload)


def _frozen_slice(world: dict[str, Any]) -> str:
    return str(world["frozen_text"])[int(world["start"]) : int(world["end"])]


def _require_evidence_slice(
    admin: psycopg.Connection[Any], analysis_id: uuid.UUID, world: dict[str, Any], name: str
) -> None:
    stored = scalar(
        admin,
        """
        SELECT span.evidence_text
          FROM core.evidence_spans AS span
          JOIN core.claim_evidence AS link ON link.evidence_span_id = span.id
          JOIN core.claims AS claim ON claim.id = link.claim_id
         WHERE claim.origin_analysis_result_id = %s
        """,
        analysis_id,
    )
    require(name, stored, _frozen_slice(world))


def _knowledge_counts(
    admin: psycopg.Connection[Any], analysis_id: uuid.UUID
) -> tuple[int, int]:
    claims = scalar(
        admin, "SELECT count(*) FROM core.claims WHERE origin_analysis_result_id=%s", analysis_id
    )
    evidence = scalar(
        admin,
        """
        SELECT count(*) FROM core.claim_evidence AS evidence
          JOIN core.claims AS claim ON claim.id = evidence.claim_id
         WHERE claim.origin_analysis_result_id = %s
        """,
        analysis_id,
    )
    return int(claims), int(evidence)


def _extraction_object(
    admin: psycopg.Connection[Any], extraction_id: uuid.UUID
) -> tuple[Any, ...]:
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


def _claim_one_resolve(
    conn: psycopg.Connection[Any], worker_id: str
) -> tuple[Any, ...] | None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT job_id, attempt_id, job_type, payload, lease_token
              FROM ops.claim_job('worker', %s, ARRAY['resolve_claims'], 60)
            """,
            (worker_id,),
        )
        row = cast(tuple[Any, ...] | None, cursor.fetchone())
    conn.commit()
    return None if row is None else tuple(row)


def _close_claimed_resolve_job(
    conn: psycopg.Connection[Any],
    claimed: tuple[Any, ...],
    handler: ResolveClaimsHandler | None = None,
) -> str:
    """Close a claimed resolve_claims job through the production handler.

    There is no pre-handler succeeded bypass. Queued leftovers go to
    ResolveClaimsHandler. Handler failures never fall back to succeeded.
    """

    job_id = uuid.UUID(str(claimed[0]))
    attempt_id = uuid.UUID(str(claimed[1]))
    payload = claimed[3]
    token = uuid.UUID(str(claimed[4]))
    active = handler if handler is not None else ResolveClaimsHandler(conn, _client())
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
            raise RuntimeError(f"g8-13 did not claim {job_id}")
        if row[0] == job_id:
            return row
        _close_claimed_resolve_job(conn, row)
    raise RuntimeError(f"g8-13 claim loop missed {job_id}")


def _drain_resolve_claims(conn: psycopg.Connection[Any], worker_id: str, limit: int = 256) -> None:
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
) -> str:
    payload = _payload_for(admin, world["job_id"])
    before_claims, before_evidence = _knowledge_counts(admin, world["analysis_id"])
    attempt_id, token = grant_running_lease(admin, world["job_id"], f"wp8-3-13-{name}-{tag}")
    status = _run_handler(worker, world["job_id"], attempt_id, token, payload)
    after_claims, after_evidence = _knowledge_counts(admin, world["analysis_id"])
    outcome = scalar(
        admin, "SELECT outcome::text FROM ops.job_attempts WHERE id=%s", attempt_id
    )
    error_code = scalar(admin, "SELECT error_code FROM ops.job_attempts WHERE id=%s", attempt_id)
    require(f"g8-13 {name} no new claims", after_claims, before_claims)
    require(f"g8-13 {name} no new evidence", after_evidence, before_evidence)
    require(
        f"g8-13 {name} closed",
        outcome in {"terminal_failure", "retryable_failure"},
        True,
    )
    require(f"g8-13 {name} not succeeded", status != "succeeded", True)
    require(f"g8-13 {name} frozen code", error_code in _FROZEN_G8_13_CODES, True)
    return str(error_code)


def g8_11(
    admin: psycopg.Connection[Any],
    worker: psycopg.Connection[Any],
    tag: str,
) -> dict[str, Any]:
    world = _seed_claim_world(admin, f"{tag}-11")
    job_id = world["job_id"]
    payload = _payload_for(admin, job_id)
    parse_knowledge_payload(payload)
    attempt_id, token = grant_running_lease(admin, job_id, f"wp8-3-g811-{tag}")
    status = _run_handler(worker, job_id, attempt_id, token, payload)
    claim_row = one(
        admin,
        """
        SELECT claim_text, claim_type::text, assertion_status::text, subject_entity_id,
               origin_analysis_result_id, ordinal, document_version_id, claim_fingerprint
          FROM core.claims
         WHERE origin_analysis_result_id = %s
        """,
        world["analysis_id"],
    )
    expected_fp = scalar(admin, "SELECT core.compute_claim_fingerprint(%s)", CLAIM_TEXT)
    supports = scalar(
        admin,
        """
        SELECT count(*) FROM core.claim_evidence AS evidence
          JOIN core.claims AS claim ON claim.id = evidence.claim_id
         WHERE claim.origin_analysis_result_id = %s
           AND evidence.support_type = 'supports'
        """,
        world["analysis_id"],
    )
    job_status = scalar(admin, "SELECT status::text FROM ops.jobs WHERE id=%s", job_id)
    outcome = scalar(
        admin, "SELECT outcome::text FROM ops.job_attempts WHERE id=%s", attempt_id
    )
    require("g8-11 handler status", status in {"succeeded", "success"}, True)
    require("g8-11 claim text original", claim_row[0], CLAIM_TEXT)
    require("g8-11 claim_type", claim_row[1], "other")
    require("g8-11 assertion_status", claim_row[2], "reported")
    require("g8-11 subject null", claim_row[3], None)
    require("g8-11 ordinal", claim_row[5], 0)
    require("g8-11 fingerprint", claim_row[7], expected_fp)
    require("g8-11 supports", int(supports) >= 1, True)
    _require_evidence_slice(admin, world["analysis_id"], world, "g8-11 evidence slice")
    require("g8-11 job succeeded", job_status, "succeeded")
    require("g8-11 attempt succeeded", outcome, "succeeded")
    return {"passed": True, "job_status": job_status, "fingerprint": claim_row[7]}


def g8_metrics_trunc(admin: psycopg.Connection[Any]) -> dict[str, Any]:
    """Non-empty rejected_by_code must use trunc(numeric), not truncate()."""

    ok_metrics = failure_metrics("knowledge_payload_mismatch")
    execute(
        admin,
        """
        SELECT ops.validate_knowledge_attempt_metrics(
            %s::jsonb, 'terminal_failure'::ops.attempt_outcome
        )
        """,
        json.dumps(ok_metrics),
    )
    bad = dict(ok_metrics)
    bad["rejected_by_code"] = {"knowledge_payload_mismatch": 1.5}
    fractional = sqlstate(
        admin,
        """
        SELECT ops.validate_knowledge_attempt_metrics(
            %s::jsonb, 'terminal_failure'::ops.attempt_outcome
        )
        """,
        json.dumps(bad),
    )
    require("g8 metrics fractional rejected_by_code", fractional, "22023")
    return {"passed": True}


def g8_12(
    admin: psycopg.Connection[Any],
    worker: psycopg.Connection[Any],
    tag: str,
) -> dict[str, Any]:
    results: dict[str, object] = {}

    empty_world = _seed_claim_world(admin, f"{tag}-12-empty", claims=_claims_result())
    empty_payload = _payload_for(admin, empty_world["job_id"])
    empty_attempt, empty_token = grant_running_lease(
        admin, empty_world["job_id"], f"wp8-3-empty-{tag}"
    )
    empty_status = _run_handler(
        worker, empty_world["job_id"], empty_attempt, empty_token, empty_payload
    )
    empty_claims = scalar(
        admin,
        "SELECT count(*) FROM core.claims WHERE origin_analysis_result_id=%s",
        empty_world["analysis_id"],
    )
    empty_metrics = scalar(
        admin, "SELECT metrics FROM ops.job_attempts WHERE id=%s", empty_attempt
    )
    require("g8-12 empty status", empty_status in {"succeeded", "success"}, True)
    require("g8-12 empty claims", int(empty_claims), 0)
    require("g8-12 empty_valid_result", empty_metrics["empty_valid_result"], True)
    results["empty"] = True

    empty_missing_world = _seed_claim_world(
        admin, f"{tag}-12-empty-keys", claims=_claims_result()
    )
    empty_missing_payload = _payload_for(admin, empty_missing_world["job_id"])
    empty_missing_attempt, empty_missing_token = grant_running_lease(
        admin, empty_missing_world["job_id"], f"wp8-3-empty-keys-{tag}"
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
                "SELECT core.materialize_claim_bundle(%s, %s, %s, %s::jsonb)",
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
    require("g8-12 empty missing keys sqlstate", missing_keys_state, "22023")
    empty_missing_outcome = scalar(
        admin,
        "SELECT outcome::text FROM ops.job_attempts WHERE id=%s",
        empty_missing_attempt,
    )
    require("g8-12 empty missing keys closed", empty_missing_outcome, "terminal_failure")
    results["empty_missing_keys"] = True

    start = FIXTURE_TEXT.find("craft")
    end = start + len("craft hovered")
    mixed = _claims_result(
        {"claim": CLAIM_TEXT, "evidence": [_text_locator(start, end)]},
        {"claim": "Bad range", "evidence": [_text_locator(0, 9999)]},
        {"claim": "Inverted", "evidence": [_text_locator(10, 4)]},
    )
    mixed_world = _seed_claim_world(admin, f"{tag}-12-mix", claims=mixed)
    mixed_payload = _payload_for(admin, mixed_world["job_id"])
    mixed_attempt, mixed_token = grant_running_lease(
        admin, mixed_world["job_id"], f"wp8-3-mix-{tag}"
    )
    mixed_status = _run_handler(
        worker, mixed_world["job_id"], mixed_attempt, mixed_token, mixed_payload
    )
    mixed_count = scalar(
        admin,
        "SELECT count(*) FROM core.claims WHERE origin_analysis_result_id=%s",
        mixed_world["analysis_id"],
    )
    require("g8-12 partial succeeded", mixed_status in {"succeeded", "success"}, True)
    require("g8-12 partial one claim", int(mixed_count), 1)
    results["partial"] = True

    unmap = _claims_result(
        {"claim": "Nope", "evidence": [_text_locator(0, 9999)]},
        {"claim": "Also no", "evidence": [_text_locator(8, 3)]},
    )
    unmap_world = _seed_claim_world(admin, f"{tag}-12-unmap", claims=unmap)
    unmap_payload = _payload_for(admin, unmap_world["job_id"])
    unmap_attempt, unmap_token = grant_running_lease(
        admin, unmap_world["job_id"], f"wp8-3-unmap-{tag}"
    )
    unmap_status = _run_handler(
        worker, unmap_world["job_id"], unmap_attempt, unmap_token, unmap_payload
    )
    unmap_count = scalar(
        admin,
        "SELECT count(*) FROM core.claims WHERE origin_analysis_result_id=%s",
        unmap_world["analysis_id"],
    )
    unmap_outcome = scalar(
        admin, "SELECT outcome::text FROM ops.job_attempts WHERE id=%s", unmap_attempt
    )
    require("g8-12 unmappable not succeeded", unmap_status in {"failed", "dead"}, True)
    require("g8-12 unmappable zero claims", int(unmap_count), 0)
    require("g8-12 unmappable terminal", unmap_outcome, "terminal_failure")
    results["unmappable"] = True

    raise_world = _seed_claim_world(admin, f"{tag}-12-raise")
    raise_payload = _payload_for(admin, raise_world["job_id"])
    raise_attempt, raise_token = grant_running_lease(
        admin, raise_world["job_id"], f"wp8-3-raise-{tag}"
    )
    bad_bundle = {
        "bundle_schema_version": "knowledge-bundle.v2",
        "analysis_result_id": raise_payload["analysis_result_id"],
        "analysis_result_sha256": raise_payload["analysis_result_sha256"],
        "accepted_candidates": [
            {"ordinal": 99, "accepted_locators": [], "rejected_locators": []}
        ],
        "rejected_candidates": [],
    }
    with worker.cursor() as cursor:
        cursor.execute("SAVEPOINT knowledge_materialize")
        try:
            cursor.execute(
                "SELECT core.materialize_claim_bundle(%s, %s, %s, %s::jsonb)",
                (raise_world["job_id"], raise_attempt, raise_token, json.dumps(bad_bundle)),
            )
            materialize_state = "ok"
        except psycopg.Error as error:
            materialize_state = error.sqlstate or "none"
            cursor.execute("ROLLBACK TO SAVEPOINT knowledge_materialize")
    worker.commit()
    require("g8-12 materialize raise", materialize_state, "22023")
    still_running = scalar(
        admin, "SELECT outcome::text FROM ops.job_attempts WHERE id=%s", raise_attempt
    )
    require("g8-12 raise attempt still running before finish", still_running, "running")
    results["sql_raise"] = True

    expire_world = _seed_claim_world(admin, f"{tag}-12-exp")
    expire_payload = _payload_for(admin, expire_world["job_id"])
    expire_attempt, expire_token = grant_running_lease(
        admin, expire_world["job_id"], f"wp8-3-exp-{tag}"
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
        _run_handler(
            worker, expire_world["job_id"], expire_attempt, expire_token, expire_payload
        )
    except psycopg.Error as error:
        state_40001 = error.sqlstate or "none"
    expire_outcome = scalar(
        admin, "SELECT outcome::text FROM ops.job_attempts WHERE id=%s", expire_attempt
    )
    require("g8-12 expired lease 40001", state_40001, "40001")
    require("g8-12 expired not closed", expire_outcome, "running")
    results["lease_40001"] = True

    return {"passed": True, **results}


def _g8_13_payload_cases(
    admin: psycopg.Connection[Any],
    worker: psycopg.Connection[Any],
    tag: str,
) -> dict[str, str]:
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
    outcomes: dict[str, str] = {}
    seed_tags = [f"{tag}-13-{name}" for name in cases]
    require("g8-13 payload seed tags unique", len(seed_tags) == len(set(seed_tags)), True)
    for name, value in cases.items():
        world = _seed_claim_world(admin, f"{tag}-13-{name}")
        payload = _payload_for(admin, world["job_id"])
        if name == "missing_key":
            del payload["model_run_id"]
        elif name == "illegal_uuid":
            payload["document_version_id"] = value
        else:
            payload[name] = value
        attempt_id, token = grant_running_lease(
            admin, world["job_id"], f"wp8-3-13-{name}-{tag}"
        )
        before = scalar(
            admin,
            "SELECT count(*) FROM core.claims WHERE origin_analysis_result_id=%s",
            world["analysis_id"],
        )
        status = _run_handler(worker, world["job_id"], attempt_id, token, payload)
        after = scalar(
            admin,
            "SELECT count(*) FROM core.claims WHERE origin_analysis_result_id=%s",
            world["analysis_id"],
        )
        outcome = scalar(
            admin, "SELECT outcome::text FROM ops.job_attempts WHERE id=%s", attempt_id
        )
        error_code = scalar(
            admin, "SELECT error_code FROM ops.job_attempts WHERE id=%s", attempt_id
        )
        require(f"g8-13 {name} no new claims", int(after), int(before))
        require(
            f"g8-13 {name} closed",
            outcome in {"terminal_failure", "retryable_failure"},
            True,
        )
        require(f"g8-13 {name} not succeeded", status != "succeeded", True)
        require(f"g8-13 {name} frozen code", error_code in _FROZEN_G8_13_CODES, True)
        outcomes[name] = str(error_code)
    return outcomes


def _g8_13_bundle_tamper(
    admin: psycopg.Connection[Any],
    worker: psycopg.Connection[Any],
    tag: str,
) -> dict[str, str]:
    outcomes: dict[str, str] = {}
    tampers = {
        "hash": "bundle hash",
        "ordinal": "accepted ordinal",
        "missing_candidate": "omitted candidate",
        "missing_keys": "missing accepted/rejected keys",
        "locator_ordinal": "locator ordinal",
        "locator_axes": "locator content axes",
    }
    seed_tags = [f"{tag}-13b-{name}" for name in tampers]
    require("g8-13 bundle seed tags unique", len(seed_tags) == len(set(seed_tags)), True)
    for name in tampers:
        world = _seed_claim_world(admin, f"{tag}-13b-{name}")
        payload = _payload_for(admin, world["job_id"])
        attempt_id, token = grant_running_lease(
            admin, world["job_id"], f"wp8-3-13b-{name}-{tag}"
        )
        bundle = _valid_bundle(
            payload, str(world["frozen_text"]), int(world["start"]), int(world["end"])
        )
        if name == "hash":
            bundle["analysis_result_sha256"] = "e" * 64
        elif name == "ordinal":
            accepted_raw = bundle["accepted_candidates"]
            if not isinstance(accepted_raw, list) or not accepted_raw:
                raise RuntimeError("valid bundle missing accepted candidate")
            first = dict(accepted_raw[0])
            first["ordinal"] = 99
            bundle["accepted_candidates"] = [first]
        elif name == "missing_candidate":
            bundle["accepted_candidates"] = []
            bundle["rejected_candidates"] = []
        elif name == "locator_ordinal":
            accepted_raw = bundle["accepted_candidates"]
            if not isinstance(accepted_raw, list) or not accepted_raw:
                raise RuntimeError("valid bundle missing accepted candidate")
            candidate = dict(accepted_raw[0])
            locators_raw = candidate["accepted_locators"]
            if not isinstance(locators_raw, list) or not locators_raw:
                raise RuntimeError("valid bundle missing accepted locator")
            locator = dict(locators_raw[0])
            locator["locator_ordinal"] = 99
            candidate["accepted_locators"] = [locator]
            bundle["accepted_candidates"] = [candidate]
        elif name == "locator_axes":
            accepted_raw = bundle["accepted_candidates"]
            if not isinstance(accepted_raw, list) or not accepted_raw:
                raise RuntimeError("valid bundle missing accepted candidate")
            candidate = dict(accepted_raw[0])
            locators_raw = candidate["accepted_locators"]
            if not isinstance(locators_raw, list) or not locators_raw:
                raise RuntimeError("valid bundle missing accepted locator")
            locator = dict(locators_raw[0])
            locator["char_start"] = int(locator["char_start"]) + 1
            candidate["accepted_locators"] = [locator]
            bundle["accepted_candidates"] = [candidate]
        else:
            del bundle["accepted_candidates"]
            del bundle["rejected_candidates"]
        before = scalar(
            admin,
            "SELECT count(*) FROM core.claims WHERE origin_analysis_result_id=%s",
            world["analysis_id"],
        )
        with worker.cursor() as cursor:
            cursor.execute("SAVEPOINT knowledge_materialize")
            try:
                cursor.execute(
                    "SELECT core.materialize_claim_bundle(%s, %s, %s, %s::jsonb)",
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
        after = scalar(
            admin,
            "SELECT count(*) FROM core.claims WHERE origin_analysis_result_id=%s",
            world["analysis_id"],
        )
        outcome = scalar(
            admin, "SELECT outcome::text FROM ops.job_attempts WHERE id=%s", attempt_id
        )
        require(f"g8-13 bundle {name} sqlstate", sqlstate_seen, "22023")
        require(f"g8-13 bundle {name} no rows", int(after), int(before))
        require(f"g8-13 bundle {name} closed", outcome, "terminal_failure")
        outcomes[name] = "knowledge_bundle_mismatch"
    return outcomes


def _g8_13_object_and_slice(
    admin: psycopg.Connection[Any],
    worker: psycopg.Connection[Any],
    tag: str,
) -> dict[str, str]:
    """Handler slice comes from the hash-verified derived object, not a Python GUC."""

    outcomes: dict[str, str] = {}
    happy = _seed_claim_world(admin, f"{tag}-13-slice")
    payload = _payload_for(admin, happy["job_id"])
    attempt_id, token = grant_running_lease(admin, happy["job_id"], f"wp8-3-13-slice-{tag}")
    status = _run_handler(worker, happy["job_id"], attempt_id, token, payload)
    require("g8-13 handler slice succeeded", status in {"succeeded", "success"}, True)
    _require_evidence_slice(admin, happy["analysis_id"], happy, "g8-13 evidence slice")
    outcomes["slice"] = "ok"

    content = _seed_claim_world(admin, f"{tag}-13-object-content")
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

    length = _seed_claim_world(admin, f"{tag}-13-object-length")
    _bucket, _key, _digest, _length, stored_id = _extraction_object(admin, length["extraction_id"])
    execute(
        admin,
        "UPDATE core.stored_objects SET byte_length = byte_length + 1 WHERE id=%s",
        stored_id,
    )
    outcomes["object_length"] = _require_handler_fail_closed(
        admin, worker, length, tag, "object_length"
    )
    # Do not UPDATE stored_objects.content_sha256 after extraction exists:
    # extractions_text_object_id_storage_domain_output_sha256_fkey makes that
    # 23503 before the handler runs. object_content already covers a SHA
    # mismatch between registered digest and object bytes.
    return outcomes


def _g8_13_at_least_once(
    admin: psycopg.Connection[Any],
    worker: psycopg.Connection[Any],
    tag: str,
) -> dict[str, Any]:
    """Two-connection claim race plus lease expiry re-claim. At most one succeeded."""

    _drain_resolve_claims(worker, f"wp8-3-drain-{tag}")
    race = _seed_claim_world(admin, f"{tag}-13-race")
    job_id = race["job_id"]
    worker_b = connect("uap_worker")
    worker_b.autocommit = False
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(_claim_one_resolve, worker, f"wp8-3-race-a-{tag}")
            future_b = pool.submit(_claim_one_resolve, worker_b, f"wp8-3-race-b-{tag}")
            claimed_a = future_a.result()
            claimed_b = future_b.result()
        hits = [
            row for row in (claimed_a, claimed_b) if row is not None and row[0] == job_id
        ]
        require("g8-13 race at most one claim", len(hits) <= 1, True)
        require("g8-13 race claimed our job", len(hits), 1)
        winner = hits[0]
        if claimed_a is not None and claimed_a[0] == job_id:
            winner_conn = worker
        else:
            winner_conn = worker_b
        payload = _payload_for(admin, job_id)
        status = ResolveClaimsHandler(winner_conn, _client()).handle(
            winner[0], winner[1], winner[4], payload
        )
        require("g8-13 race winner succeeded", status in {"succeeded", "success"}, True)
        _require_evidence_slice(admin, race["analysis_id"], race, "g8-13 race evidence slice")

        reclaim = _seed_claim_world(admin, f"{tag}-13-reclaim")
        first = _claim_target_job(
            worker, worker_id=f"wp8-3-reclaim-a-{tag}", job_id=reclaim["job_id"]
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
            worker_b, worker_id=f"wp8-3-reclaim-b-{tag}", job_id=reclaim["job_id"]
        )
        require("g8-13 expiry new attempt", second[1] != attempt_a, True)
        expired_outcome = scalar(
            admin, "SELECT outcome::text FROM ops.job_attempts WHERE id=%s", attempt_a
        )
        expired_code = scalar(
            admin, "SELECT error_code FROM ops.job_attempts WHERE id=%s", attempt_a
        )
        require("g8-13 expired attempt closed", expired_outcome, "retryable_failure")
        require("g8-13 expired error_code", expired_code, "lease_expired")

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
        require("g8-13 expired first 40001", state_40001, "40001")

        second_payload = _payload_for(admin, reclaim["job_id"])
        reclaim_status = ResolveClaimsHandler(worker_b, _client()).handle(
            second[0], second[1], second[4], second_payload
        )
        require(
            "g8-13 reclaim succeeded", reclaim_status in {"succeeded", "success"}, True
        )
        _require_evidence_slice(
            admin, reclaim["analysis_id"], reclaim, "g8-13 reclaim evidence slice"
        )

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
        require("g8-13 at most one succeeded", succeeded <= 1, True)
        require("g8-13 exactly one succeeded", succeeded, 1)
        require("g8-13 no running attempts", running, 0)
        require("g8-13 all attempts closed", open_attempts, 0)
        return {
            "race_claimed": 1,
            "reclaim_succeeded": 1,
            "expired_closed": expired_outcome,
        }
    finally:
        worker_b.close()


def g8_13(
    admin: psycopg.Connection[Any],
    worker: psycopg.Connection[Any],
    tag: str,
) -> dict[str, Any]:
    outcomes = _g8_13_payload_cases(admin, worker, tag)
    bundle_outcomes = _g8_13_bundle_tamper(admin, worker, tag)
    object_outcomes = _g8_13_object_and_slice(admin, worker, tag)

    replay = _seed_claim_world(admin, f"{tag}-13-replay")
    payload = _payload_for(admin, replay["job_id"])
    attempt_id, token = grant_running_lease(admin, replay["job_id"], f"wp8-3-replay-{tag}")
    bundle = _valid_bundle(
        payload, str(replay["frozen_text"]), int(replay["start"]), int(replay["end"])
    )
    with worker.cursor() as cursor:
        cursor.execute(
            "SELECT core.materialize_claim_bundle(%s, %s, %s, %s::jsonb)",
            (replay["job_id"], attempt_id, token, json.dumps(bundle)),
        )
        cursor.execute(
            "SELECT core.materialize_claim_bundle(%s, %s, %s, %s::jsonb)",
            (replay["job_id"], attempt_id, token, json.dumps(bundle)),
        )
        metrics = {
            "schema_version": "knowledge-attempt-metrics.v1",
            "input_candidates": 1,
            "materialized_candidates": 1,
            "input_locators": 1,
            "materialized_locators": 1,
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
    replay_count = scalar(
        admin,
        "SELECT count(*) FROM core.claims WHERE origin_analysis_result_id=%s",
        replay["analysis_id"],
    )
    replay_spans = scalar(
        admin,
        """
        SELECT count(*) FROM core.claim_evidence AS evidence
          JOIN core.claims AS claim ON claim.id = evidence.claim_id
         WHERE claim.origin_analysis_result_id = %s
        """,
        replay["analysis_id"],
    )
    require("g8-13 replay one claim", int(replay_count), 1)
    require("g8-13 replay one evidence", int(replay_spans), 1)
    _require_evidence_slice(admin, replay["analysis_id"], replay, "g8-13 replay evidence slice")
    once = _g8_13_at_least_once(admin, worker, tag)
    return {
        "passed": True,
        "tamper": outcomes,
        "bundle": bundle_outcomes,
        "object": object_outcomes,
        "at_least_once": once,
    }


def g8_live_definitions(admin: psycopg.Connection[Any]) -> dict[str, Any]:
    """Assert the live 0011 function bodies, not just the migration source."""

    validator = scalar(
        admin,
        """
        SELECT pg_get_functiondef(
            'ops.validate_knowledge_attempt_metrics(jsonb, ops.attempt_outcome)'::regprocedure
        )
        """,
    )
    materialize = scalar(
        admin,
        """
        SELECT pg_get_functiondef(
            'core.materialize_claim_bundle(uuid, uuid, uuid, jsonb)'::regprocedure
        )
        """,
    )
    keys_helper = scalar(
        admin,
        """
        SELECT pg_get_functiondef(
            'core._jsonb_keys_exact(jsonb, text[])'::regprocedure
        )
        """,
    )
    require("live validator uses trunc", "trunc(" in validator, True)
    require("live validator not truncate", "truncate(" not in validator, True)
    require(
        "live validator has hash conflict",
        "knowledge_locator_hash_conflict" in validator,
        True,
    )
    require("live materialize uses v_claim_id", "v_claim_id" in materialize, True)
    require(
        "live materialize no ambiguous claim_id",
        "WHERE claim_id = claim_id" not in materialize,
        True,
    )
    require("live materialize exact keys", "_jsonb_keys_exact" in materialize, True)
    require("live keys helper exists", "object_keys(key)" in keys_helper, True)
    return {"passed": True}


def g8_16a(
    admin: psycopg.Connection[Any],
    worker: psycopg.Connection[Any],
    tag: str,
) -> dict[str, Any]:
    world = _seed_claim_world(admin, f"{tag}-16a")
    job_id = world["job_id"]
    queued = scalar(
        admin,
        "SELECT status::text FROM ops.jobs WHERE id=%s AND job_type='resolve_claims'",
        job_id,
    )
    require("g8-16a queued", queued, "queued")

    inactive = ResolveClaimsWorker.from_settings(
        worker,
        worker_id=f"wp8-3-pre-{tag}",
        claims_handler_active=False,
        lease_seconds=30,
    )
    require(
        "g8-16a inactive types omit claims",
        "resolve_claims" not in inactive.job_types,
        True,
    )
    require(
        "g8-16a inactive platform set is pre-claim",
        inactive.job_types == PRE_CLAIM_HANDLER_JOB_TYPES,
        True,
    )
    require(
        "g8-16a inactive claims consumer requests no types",
        inactive.claim_job_types == (),
        True,
    )
    pre_claim = inactive.claim_one()
    require("g8-16a not claimed before activation", pre_claim is None, True)
    still_queued = scalar(
        admin,
        "SELECT status::text FROM ops.jobs WHERE id=%s",
        job_id,
    )
    require("g8-16a still queued after inactive claim_one", still_queued, "queued")

    # Probe SQL only: a sibling worker that owns fetch/extract/translate/analyze
    # may call claim_job with PRE_CLAIM types. That set must not select this
    # queued resolve_claims job. Rollback so leftover sibling jobs stay queued.
    with worker.cursor() as cursor:
        cursor.execute("SAVEPOINT g8_16a_preclaim_control")
        try:
            cursor.execute(
                """
                SELECT job_id, job_type
                  FROM ops.claim_job('worker', %s, %s::text[], 30)
                """,
                (
                    f"wp8-3-preclaim-control-{tag}",
                    list(PRE_CLAIM_HANDLER_JOB_TYPES),
                ),
            )
            control = cursor.fetchone()
            if control is not None:
                require(
                    "g8-16a pre-claim control type",
                    str(control[1]) != "resolve_claims",
                    True,
                )
                require(
                    "g8-16a pre-claim control missed our job",
                    control[0] != job_id,
                    True,
                )
        finally:
            cursor.execute("ROLLBACK TO SAVEPOINT g8_16a_preclaim_control")
    worker.commit()
    queued_after_control = scalar(
        admin,
        "SELECT status::text FROM ops.jobs WHERE id=%s",
        job_id,
    )
    require("g8-16a still queued after pre-claim control", queued_after_control, "queued")

    active = ResolveClaimsWorker.from_settings(
        worker,
        worker_id=f"wp8-3-post-{tag}",
        lease_seconds=60,
    )
    require(
        "g8-16a active contains resolve_claims",
        "resolve_claims" in active.job_types,
        True,
    )
    require(
        "g8-16a production default claim_job_types",
        active.claim_job_types == ("resolve_claims",),
        True,
    )
    require(
        "g8-16a active excludes entities",
        "resolve_entities" not in active.job_types,
        True,
    )
    require(
        "g8-16a active excludes relations",
        "resolve_relations" not in active.job_types,
        True,
    )

    consumed: tuple[Any, ...] | None = None
    status = None
    for index in range(64):
        claimed = ResolveClaimsWorker.from_settings(
            worker,
            worker_id=f"wp8-3-post-{tag}-{index}",
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
        raise RuntimeError("g8-16a did not claim the queued resolve_claims job")
    require("g8-16a claimed type", consumed[2], "resolve_claims")
    require("g8-16a consume succeeded", status in {"succeeded", "success"}, True)
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
                "SELECT core.materialize_claim_bundle(%s, %s, %s, '{}'::jsonb)",
                uuid.uuid4(),
                uuid.uuid4(),
                uuid.uuid4(),
            )
            other.close()
        except Exception as error:
            states[role] = getattr(error, "sqlstate", None) or "42501"
    require("api cannot materialize", states["uap_api"], "42501")
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
    _drain_resolve_claims(worker, f"wp8-3-startup-drain-{tag}")
    results = {
        "head": head,
        "live_definitions": g8_live_definitions(admin),
        "metrics_trunc": g8_metrics_trunc(admin),
        "G8-11": g8_11(admin, worker, tag),
        "G8-12": g8_12(admin, worker, tag),
        "G8-13": g8_13(admin, worker, tag),
        "G8-16A": g8_16a(admin, worker, tag),
        "permissions": g8_permissions(admin, tag),
    }
    print(json.dumps(results, indent=2, sort_keys=True, default=str))
    failed = [
        name
        for name, payload in results.items()
        if isinstance(payload, dict) and payload.get("passed") is False
    ]
    if failed:
        raise SystemExit(f"WP8.3 runtime probe failed: {', '.join(failed)}")
    print("WP8.3 runtime probe passed: G8-11 G8-12 G8-13 G8-16A")


if __name__ == "__main__":
    main()
