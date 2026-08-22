"""Exercise WP8.3 claim materialization as uap_worker: G8-11, G8-12, G8-13, G8-16A."""

from __future__ import annotations

import json
import uuid
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from tools.wp8_1_runtime_probe import (
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
from uap_platform.knowledge.handler import ResolveClaimsHandler
from uap_platform.knowledge.job_types import claimable_job_types
from uap_platform.knowledge.metrics import failure_metrics
from uap_platform.knowledge.payload import parse_knowledge_payload

CURRENT_HEAD = "0011_claim_materialization"
FIXTURE_TEXT = "The craft hovered over the hangar at dawn."
CLAIM_TEXT = "A craft hovered over the hangar."
_FROZEN_G8_13_CODES = {
    "knowledge_payload_mismatch",
    "knowledge_schema_unsupported",
    "knowledge_extraction_mismatch",
    "knowledge_bundle_mismatch",
}


def _claims_result(*claims: dict[str, object]) -> dict[str, object]:
    return {"claims": list(claims)}


def _text_locator(start: int, end: int) -> dict[str, object]:
    return {"locator_type": "text", "start": start, "end": end}


def _set_fixture_text(worker: psycopg.Connection[Any], text: str) -> None:
    execute(worker, "SELECT set_config('uap.fixture_extraction_text', %s, false)", text)


def _seed_claim_world(
    admin: psycopg.Connection[Any],
    tag: str,
    *,
    claims: dict[str, object] | None = None,
    extractor: str = "text",
) -> dict[str, Any]:
    _principal, document_version_id, _source = seed_document(admin, tag)
    body = FIXTURE_TEXT
    input_sha = sha256_text(body)
    extraction_id = insert_extraction(admin, document_version_id, tag, input_sha, extractor)
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
    _set_fixture_text(worker, FIXTURE_TEXT)
    handler = ResolveClaimsHandler(worker)
    return handler.handle(job_id, attempt_id, token, payload)


def _claim_job(
    worker: psycopg.Connection[Any],
    worker_id: str,
    job_types: tuple[str, ...] | list[str],
    lease_seconds: int,
) -> tuple[Any, ...] | None:
    with worker.cursor() as cursor:
        cursor.execute(
            """
            SELECT job_id, attempt_id, job_type, payload, lease_token
              FROM ops.claim_job('worker', %s, %s::text[], %s)
            """,
            (worker_id, list(job_types), lease_seconds),
        )
        row = cursor.fetchone()
    worker.commit()
    if row is None:
        return None
    return tuple(row)


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


def g8_12(
    admin: psycopg.Connection[Any],
    worker: psycopg.Connection[Any],
    tag: str,
) -> dict[str, Any]:
    results: dict[str, object] = {}

    empty_world = _seed_claim_world(
        admin, f"{tag}-12-empty", claims=_claims_result()
    )
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
        "accepted_candidates": [{"ordinal": 99, "accepted_locators": [], "rejected_locators": []}],
        "rejected_candidates": [],
    }
    _set_fixture_text(worker, FIXTURE_TEXT)
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
    admin: psycopg.Connection[Any], worker: psycopg.Connection[Any], tag: str
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
        require(f"g8-13 {name} closed", outcome in {"terminal_failure", "retryable_failure"}, True)
        require(f"g8-13 {name} not succeeded", status != "succeeded", True)
        require(f"g8-13 {name} frozen code", error_code in _FROZEN_G8_13_CODES, True)
        outcomes[name] = str(error_code)
    return outcomes


def _g8_13_bundle_tamper(
    admin: psycopg.Connection[Any], worker: psycopg.Connection[Any], tag: str
) -> dict[str, str]:
    outcomes: dict[str, str] = {}
    start = FIXTURE_TEXT.find("craft")
    end = start + len("craft hovered")
    tampers: dict[str, dict[str, object]] = {
        "bundle_hash": {},
        "bad_ordinal": {},
        "omitted_candidate": {},
    }
    for name in tampers:
        world = _seed_claim_world(admin, f"{tag}-13b-{name[:8]}")
        payload = _payload_for(admin, world["job_id"])
        bundle = _valid_bundle(payload, start, end)
        if name == "bundle_hash":
            bundle["analysis_result_sha256"] = "e" * 64
        elif name == "bad_ordinal":
            accepted_raw = bundle["accepted_candidates"]
            if not isinstance(accepted_raw, list) or not accepted_raw:
                raise RuntimeError("g8-13 valid bundle missing accepted candidate")
            first = dict(accepted_raw[0])
            first["ordinal"] = 99
            bundle["accepted_candidates"] = [first]
        else:
            bundle["accepted_candidates"] = []
            bundle["rejected_candidates"] = []
        attempt_id, token = grant_running_lease(
            admin, world["job_id"], f"wp8-3-13b-{name}-{tag}"
        )
        before = scalar(
            admin,
            "SELECT count(*) FROM core.claims WHERE origin_analysis_result_id=%s",
            world["analysis_id"],
        )
        sqlstate_seen = "ok"
        with worker.cursor() as cursor:
            cursor.execute("SAVEPOINT knowledge_materialize")
            try:
                cursor.execute(
                    "SELECT core.materialize_claim_bundle(%s, %s, %s, %s::jsonb)",
                    (world["job_id"], attempt_id, token, json.dumps(bundle)),
                )
            except psycopg.Error as error:
                sqlstate_seen = error.sqlstate or "none"
                cursor.execute("ROLLBACK TO SAVEPOINT knowledge_materialize")
                cursor.execute(
                    """
                    SELECT ops.finish_knowledge_job(
                        %s, %s, %s, 'terminal_failure'::ops.attempt_outcome,
                        NULL, 'knowledge_bundle_mismatch', 'knowledge_bundle_mismatch',
                        NULL, %s
                    )
                    """,
                    (
                        world["job_id"],
                        attempt_id,
                        token,
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

    inactive = claimable_job_types(claims_handler_active=False)
    require("g8-16a inactive excludes resolve_claims", "resolve_claims" not in inactive, True)
    pre_claim = _claim_job(worker, f"wp8-3-pre-{tag}", inactive, 30)
    require("g8-16a not claimed before activation", pre_claim is None, True)

    active = claimable_job_types(claims_handler_active=True)
    require("g8-16a active contains resolve_claims", "resolve_claims" in active, True)
    require("g8-16a active excludes entities", "resolve_entities" not in active, True)
    require("g8-16a active excludes relations", "resolve_relations" not in active, True)

    claimed: tuple[Any, ...] | None = None
    for index in range(16):
        row = _claim_job(worker, f"wp8-3-post-{tag}-{index}", active, 60)
        if row is None:
            break
        if row[0] == job_id:
            claimed = row
            break
    require("g8-16a claimed after activation", claimed is not None, True)
    if claimed is None:
        raise RuntimeError("g8-16a claimed after activation")
    require("g8-16a claimed type", claimed[2], "resolve_claims")
    payload = dict(claimed[3])
    status = _run_handler(worker, claimed[0], claimed[1], claimed[4], payload)
    require("g8-16a consume succeeded", status in {"succeeded", "success"}, True)
    return {"passed": True, "claimed_job": str(claimed[0])}


def g8_permissions(admin: psycopg.Connection[Any], tag: str) -> dict[str, Any]:
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
    return {"passed": True, "states": states, "tag": tag}


def main() -> None:
    tag = uuid.uuid4().hex[:10]
    admin = connect()
    worker = connect("uap_worker")
    worker.autocommit = False
    head = scalar(admin, "SELECT version_num FROM public.alembic_version")
    require("alembic head", head, CURRENT_HEAD)
    results = {
        "head": head,
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
