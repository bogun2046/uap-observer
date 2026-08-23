"""Exercise WP8.3 claim materialization as uap_worker: G8-11, G8-12, G8-13, G8-16A."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any, cast

_PLATFORM_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLATFORM_ROOT / "src"
for _path in (str(_PLATFORM_ROOT), str(_SRC)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import psycopg  # noqa: E402
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

CURRENT_HEAD = "0011_claim_materialization"
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
    extraction_id = insert_extraction(admin, document_version_id, tag, input_sha, extractor)
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
        "start": start,
        "end": end,
    }


def _payload_for(admin: psycopg.Connection[Any], job_id: uuid.UUID) -> dict[str, object]:
    raw = scalar(admin, "SELECT payload FROM ops.jobs WHERE id=%s", job_id)
    return dict(raw)


def _valid_bundle(payload: dict[str, object], start: int, end: int) -> dict[str, object]:
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
                        "evidence_text": FIXTURE_TEXT[start:end],
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
    for name, value in cases.items():
        world = _seed_claim_world(admin, f"{tag}-13-{name[:8]}")
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
    }
    for name in tampers:
        world = _seed_claim_world(admin, f"{tag}-13b-{name[:8]}")
        payload = _payload_for(admin, world["job_id"])
        attempt_id, token = grant_running_lease(
            admin, world["job_id"], f"wp8-3-13b-{name}-{tag}"
        )
        bundle = _valid_bundle(payload, world["start"], world["end"])
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


def g8_13(
    admin: psycopg.Connection[Any],
    worker: psycopg.Connection[Any],
    tag: str,
) -> dict[str, Any]:
    outcomes = _g8_13_payload_cases(admin, worker, tag)
    bundle_outcomes = _g8_13_bundle_tamper(admin, worker, tag)

    replay = _seed_claim_world(admin, f"{tag}-13-replay")
    payload = _payload_for(admin, replay["job_id"])
    attempt_id, token = grant_running_lease(admin, replay["job_id"], f"wp8-3-replay-{tag}")
    bundle = _valid_bundle(payload, replay["start"], replay["end"])
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
    return {"passed": True, "tamper": outcomes, "bundle": bundle_outcomes}


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
        "g8-16a inactive claim_job_types empty",
        inactive.claim_job_types == (),
        True,
    )
    pre_claim = inactive.claim_one()
    require("g8-16a not claimed before activation", pre_claim is None, True)

    active = ResolveClaimsWorker.from_settings(
        worker,
        worker_id=f"wp8-3-post-{tag}",
        claims_handler_active=True,
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
    for index in range(16):
        claimed = ResolveClaimsWorker.from_settings(
            worker,
            worker_id=f"wp8-3-post-{tag}-{index}",
            claims_handler_active=True,
            lease_seconds=60,
        ).claim_one()
        if claimed is None:
            break
        if claimed[0] == job_id:
            consumed = claimed
            status = active.dispatch(claimed)
            break
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
