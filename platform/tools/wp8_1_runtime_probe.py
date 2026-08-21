"""Exercise WP8.1 knowledge handover, permissions, and database constraints."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from tools.configure_roles import ROLE_PASSWORDS
from uap_platform.config import load_settings

CURRENT_HEAD = "0010_knowledge_foundation"
EXPECTED_TABLE_COUNT = 50
WP3_ORIGINAL_TABLE_COUNT = 49
PAYLOAD_KEYS = {
    "payload_schema_version",
    "analysis_result_id",
    "analysis_result_sha256",
    "analysis_schema_version",
    "document_version_id",
    "result_type",
    "model_run_id",
    "input_sha256",
    "extraction_anchor_status",
    "extraction_id",
}
EMPTY_METRICS = {
    "schema_version": "knowledge-attempt-metrics.v1",
    "input_candidates": 0,
    "materialized_candidates": 0,
    "input_locators": 0,
    "materialized_locators": 0,
    "rejected_candidates": 0,
    "rejected_locators": 0,
    "empty_valid_result": True,
    "rejected_by_code": {},
    "samples": [],
}


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def admin_url() -> str:
    return load_settings().psycopg_database_url


def connect(role: str | None = None) -> psycopg.Connection[Any]:
    url = admin_url()
    if role is None:
        connection = psycopg.connect(url)
        connection.autocommit = True
        return connection
    password = os.environ.get(ROLE_PASSWORDS[role])
    if not password:
        raise RuntimeError(f"missing password for {role}")
    params = conninfo_to_dict(url)
    params.pop("user", None)
    params.pop("password", None)
    base = make_conninfo(**params)  # type: ignore[arg-type]
    connection = psycopg.connect(make_conninfo(base, user=role, password=password))
    connection.autocommit = True
    return connection


def scalar(connection: psycopg.Connection[Any], statement: str, *params: object) -> Any:
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("probe query returned no row")
    return row[0]


def one(connection: psycopg.Connection[Any], statement: str, *params: object) -> tuple[Any, ...]:
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("probe query returned no row")
    return tuple(row)


def execute(connection: psycopg.Connection[Any], statement: str, *params: object) -> None:
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


def sqlstate(connection: psycopg.Connection[Any], statement: str, *params: object) -> str:
    return sqlerror(connection, statement, *params)[0]


def sqlerror(
    connection: psycopg.Connection[Any], statement: str, *params: object
) -> tuple[str, str]:
    try:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute(statement, params)
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    except psycopg.Error as error:
        primary = ""
        if error.diag is not None and error.diag.message_primary:
            primary = error.diag.message_primary
        return str(error.sqlstate), primary
    raise RuntimeError(f"probe accepted a forbidden statement: {statement.split()[0]}")


def require(name: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise RuntimeError(f"{name}: expected {expected!r}, got {actual!r}")


def empty_metrics() -> str:
    return json.dumps(EMPTY_METRICS)


def insert_job_attempt(admin: psycopg.Connection[Any], key: str) -> uuid.UUID:
    job_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    execute(
        admin,
        """
        INSERT INTO ops.jobs (
            id, job_type, payload, payload_schema_version, idempotency_key,
            status, priority, available_at, max_attempts, timeout_seconds, completed_at
        ) VALUES (
            %s, 'analyze_document', '{}'::jsonb, 'model.v1', %s,
            'succeeded', 0, now(), 1, 30, now()
        )
        """,
        job_id,
        key,
    )
    execute(
        admin,
        """
        INSERT INTO ops.job_attempts (
            id, job_id, attempt_no, worker_id, started_at, finished_at, outcome
        ) VALUES (%s, %s, 1, 'wp8-1-probe', now(), now(), 'succeeded')
        """,
        attempt_id,
        job_id,
    )
    return attempt_id


def grant_running_lease(
    admin: psycopg.Connection[Any],
    job_id: uuid.UUID,
    worker_id: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    token = uuid.uuid4()
    attempt_id = uuid.uuid4()
    execute(
        admin,
        """
        UPDATE ops.jobs
           SET status = 'running',
               lease_owner = %s,
               lease_token = %s,
               lease_expires_at = clock_timestamp() + interval '60 seconds',
               attempt_count = 1,
               updated_at = clock_timestamp()
         WHERE id = %s
        """,
        worker_id,
        token,
        job_id,
    )
    execute(
        admin,
        """
        INSERT INTO ops.job_attempts (
            id, job_id, attempt_no, worker_id, lease_token, started_at, outcome
        ) VALUES (%s, %s, 1, %s, %s, clock_timestamp(), 'running')
        """,
        attempt_id,
        job_id,
        worker_id,
        token,
    )
    return attempt_id, token


def seed_document(
    admin: psycopg.Connection[Any], tag: str
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    principal_id = uuid.uuid4()
    source_id = uuid.uuid4()
    document_id = uuid.uuid4()
    document_version_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    artifact_version_id = uuid.uuid4()
    source_run_id = uuid.uuid4()
    stored_id = uuid.uuid4()
    config_id = uuid.uuid4()
    raw_hash = sha256_text(f"raw-{tag}")
    now = datetime.now(UTC)
    execute(
        admin,
        """
        INSERT INTO audit.principals (id, principal_type, service_name, display_name)
        VALUES (%s, 'service', %s, 'WP8.1 probe')
        """,
        principal_id,
        f"wp8-1-{tag}",
    )
    execute(
        admin,
        """
        INSERT INTO ingest.sources (id, slug, name, source_type, homepage_url)
        VALUES (%s, %s, 'WP8.1 probe', 'web', 'https://example.test')
        """,
        source_id,
        f"wp8-1-{tag}",
    )
    execute(
        admin,
        """
        INSERT INTO ingest.source_config_versions (
            id, source_id, version_no, configuration, configuration_sha256,
            effective_from, changed_by, change_reason
        ) VALUES (%s, %s, 1, '{}'::jsonb, repeat('b', 64), %s, %s, 'wp8.1')
        """,
        config_id,
        source_id,
        now,
        principal_id,
    )
    source_job_key = f"wp8-1-source-{tag}"
    source_attempt = insert_job_attempt(admin, source_job_key)
    source_job_id = scalar(admin, "SELECT job_id FROM ops.job_attempts WHERE id=%s", source_attempt)
    execute(
        admin,
        """
        INSERT INTO ingest.source_runs (
            id, source_id, source_config_version_id, job_id, run_key, outcome,
            started_at, finished_at
        ) VALUES (%s, %s, %s, %s, %s, 'succeeded', %s, %s)
        """,
        source_run_id,
        source_id,
        config_id,
        source_job_id,
        f"wp8-1-run-{tag}",
        now,
        now,
    )
    execute(
        admin,
        """
        INSERT INTO core.stored_objects (
            id, storage_domain, bucket_name, object_key, content_sha256,
            byte_length, media_type, verified_at
        ) VALUES (%s, 'raw', 'raw', %s, %s, 12, 'text/plain', %s)
        """,
        stored_id,
        f"raw/{tag}",
        raw_hash,
        now,
    )
    execute(
        admin,
        """
        INSERT INTO ingest.artifacts (
            id, source_id, canonical_locator, artifact_kind, first_seen_at, last_seen_at
        ) VALUES (%s, %s, %s, 'html', %s, %s)
        """,
        artifact_id,
        source_id,
        f"https://example.test/wp8/{tag}",
        now,
        now,
    )
    execute(
        admin,
        """
        INSERT INTO ingest.artifact_versions (
            id, artifact_id, source_run_id, stored_object_id, retrieved_at
        ) VALUES (%s, %s, %s, %s, %s)
        """,
        artifact_version_id,
        artifact_id,
        source_run_id,
        stored_id,
        now,
    )
    execute(
        admin,
        """
        INSERT INTO core.documents (
            id, source_id, source_item_key, canonical_url, document_kind,
            first_seen_at, last_seen_at
        ) VALUES (%s, %s, %s, %s, 'article', %s, %s)
        """,
        document_id,
        source_id,
        tag,
        f"https://example.test/wp8/{tag}",
        now,
        now,
    )
    execute(
        admin,
        """
        INSERT INTO core.document_versions (
            id, document_id, artifact_version_id, version_no, normalized_content_sha256
        ) VALUES (%s, %s, %s, 1, %s)
        """,
        document_version_id,
        document_id,
        artifact_version_id,
        sha256_text(f"doc-{tag}"),
    )
    return principal_id, document_version_id, source_id


def insert_extraction(
    admin: psycopg.Connection[Any],
    document_version_id: uuid.UUID,
    tag: str,
    output_sha256: str,
    extractor_name: str = "text",
) -> uuid.UUID:
    extraction_id = uuid.uuid4()
    text_object_id = uuid.uuid4()
    now = datetime.now(UTC)
    execute(
        admin,
        """
        INSERT INTO core.stored_objects (
            id, storage_domain, bucket_name, object_key, content_sha256,
            byte_length, media_type, verified_at
        ) VALUES (%s, 'derived', 'derived', %s, %s, 24, 'text/plain', %s)
        """,
        text_object_id,
        f"derived/{tag}/{extractor_name}",
        output_sha256,
        now,
    )
    attempt_id = insert_job_attempt(admin, f"wp8-1-extract-{tag}-{extractor_name}")
    execute(
        admin,
        """
        INSERT INTO core.extractions (
            id, document_version_id, job_attempt_id, extractor_name, extractor_version,
            outcome, text_object_id, storage_domain, output_sha256, location_map
        ) VALUES (
            %s, %s, %s, %s, '1.0.0', 'succeeded', %s, 'derived', %s, '[]'::jsonb
        )
        """,
        extraction_id,
        document_version_id,
        attempt_id,
        extractor_name,
        text_object_id,
        output_sha256,
    )
    return extraction_id


def insert_prompt(admin: psycopg.Connection[Any], task_type: str, tag: str) -> uuid.UUID:
    prompt_id = uuid.uuid4()
    principal_id = scalar(
        admin,
        "SELECT id FROM audit.principals"
        " WHERE service_name LIKE %s ORDER BY created_at DESC LIMIT 1",
        "wp8-1-%",
    )
    execute(
        admin,
        """
        INSERT INTO ops.prompt_versions (
            id, task_type, version, system_template, user_template, output_schema,
            content_sha256, active, created_by
        ) VALUES (
            %s, %s, %s, 'Return JSON.', 'Extract.', '{}'::jsonb, %s, false, %s
        )
        """,
        prompt_id,
        task_type,
        tag,
        sha256_text(f"prompt-{task_type}-{tag}"),
        principal_id,
    )
    return prompt_id


def insert_model_run(
    admin: psycopg.Connection[Any],
    *,
    document_version_id: uuid.UUID,
    task_type: str,
    input_sha256: str,
    tag: str,
    status: str = "succeeded",
) -> uuid.UUID:
    run_id = uuid.uuid4()
    prompt_id = insert_prompt(admin, task_type, tag)
    attempt_id = insert_job_attempt(admin, f"wp8-1-model-{tag}")
    error_code = None if status == "succeeded" else "invalid_output"
    execute(
        admin,
        """
        INSERT INTO ops.model_runs (
            id, job_attempt_id, prompt_version_id, document_version_id, task_type,
            provider, model, input_sha256, idempotency_key, semantic_idempotency_key,
            status, error_code, started_at, finished_at
        ) VALUES (
            %s, %s, %s, %s, %s, 'static', 'probe', %s, %s, %s, %s, %s, now(), now()
        )
        """,
        run_id,
        attempt_id,
        prompt_id,
        document_version_id,
        task_type,
        input_sha256,
        f"model-{tag}",
        f"model-semantic-{tag}",
        status,
        error_code,
    )
    return run_id


def insert_analysis(
    connection: psycopg.Connection[Any],
    *,
    model_run_id: uuid.UUID,
    document_version_id: uuid.UUID,
    result_type: str,
    result: dict[str, object],
    validation_status: str = "valid",
) -> uuid.UUID:
    analysis_id = uuid.uuid4()
    encoded = json.dumps(result, separators=(",", ":"), ensure_ascii=False)
    execute(
        connection,
        """
        INSERT INTO core.analysis_results (
            id, model_run_id, document_version_id, result_type, schema_version,
            result, result_sha256, validation_status
        ) VALUES (%s, %s, %s, %s, 'ai.v1', %s::jsonb, %s, %s)
        """,
        analysis_id,
        model_run_id,
        document_version_id,
        result_type,
        encoded,
        sha256_text(encoded),
        validation_status,
    )
    return analysis_id


def job_for(
    admin: psycopg.Connection[Any], analysis_id: uuid.UUID, result_type: str
) -> tuple[Any, ...]:
    prefix = "resolve-claims:" if result_type == "claim_extraction" else "resolve-entities:"
    return one(
        admin,
        """
        SELECT id, job_type, payload_schema_version, payload, idempotency_key,
               attempt_count, priority, max_attempts, timeout_seconds
          FROM ops.jobs
         WHERE idempotency_key = %s
        """,
        f"{prefix}{analysis_id}",
    )


def g8_01(
    admin: psycopg.Connection[Any], governance: psycopg.Connection[Any], tag: str
) -> dict[str, object]:
    _principal_id, document_version_id, _source_id = seed_document(admin, f"{tag}-g801")
    input_hash = sha256_text(f"matched-{tag}")
    extraction_id = insert_extraction(admin, document_version_id, f"{tag}-g801", input_hash)
    first_run = insert_model_run(
        admin,
        document_version_id=document_version_id,
        task_type="claim_extraction",
        input_sha256=input_hash,
        tag=f"{tag}-claim-1",
    )
    second_run = insert_model_run(
        admin,
        document_version_id=document_version_id,
        task_type="claim_extraction",
        input_sha256=input_hash,
        tag=f"{tag}-claim-2",
    )
    entity_run = insert_model_run(
        admin,
        document_version_id=document_version_id,
        task_type="entity_extraction",
        input_sha256=input_hash,
        tag=f"{tag}-entity-1",
    )
    require(
        "no selections before insert",
        scalar(
            admin,
            "SELECT count(*) FROM core.analysis_selections WHERE document_version_id=%s",
            document_version_id,
        ),
        0,
    )
    first = insert_analysis(
        governance,
        model_run_id=first_run,
        document_version_id=document_version_id,
        result_type="claim_extraction",
        result={"claims": []},
    )
    second = insert_analysis(
        governance,
        model_run_id=second_run,
        document_version_id=document_version_id,
        result_type="claim_extraction",
        result={"claims": []},
    )
    entity = insert_analysis(
        governance,
        model_run_id=entity_run,
        document_version_id=document_version_id,
        result_type="entity_extraction",
        result={"entities": []},
    )
    principal_id = scalar(
        admin,
        "SELECT id FROM audit.principals WHERE service_name=%s",
        f"wp8-1-{tag}-g801",
    )
    execute(
        admin,
        """
        INSERT INTO core.analysis_selections (
            id, document_version_id, analysis_result_id, result_type,
            selected_by, selection_reason
        ) VALUES (%s, %s, %s, 'claim_extraction', %s, 'select first only')
        """,
        uuid.uuid4(),
        document_version_id,
        first,
        principal_id,
    )
    first_job = job_for(admin, first, "claim_extraction")
    second_job = job_for(admin, second, "claim_extraction")
    entity_job = job_for(admin, entity, "entity_extraction")
    for job in (first_job, second_job, entity_job):
        payload = job[3]
        require("payload keys", set(payload.keys()), PAYLOAD_KEYS)
        require("payload schema", payload["payload_schema_version"], "knowledge.v2")
        require("column schema", job[2], "knowledge.v2")
        require("anchor", payload["extraction_anchor_status"], "matched")
        require("extraction id", payload["extraction_id"], str(extraction_id))
        require("no attempts", job[5], 0)
        require("priority", job[6], 0)
        require("max_attempts", job[7], 8)
        require("timeout_seconds", job[8], 60)
    require("claim job type", first_job[1], "resolve_claims")
    require("entity job type", entity_job[1], "resolve_entities")
    require("distinct claim jobs", first_job[0] != second_job[0], True)
    require(
        "no relation jobs",
        scalar(admin, "SELECT count(*) FROM ops.jobs WHERE job_type='resolve_relations'"),
        0,
    )
    require(
        "selection did not block second job",
        scalar(
            admin,
            "SELECT count(*) FROM ops.jobs WHERE id IN (%s, %s, %s)",
            first_job[0],
            second_job[0],
            entity_job[0],
        ),
        3,
    )
    require(
        "wp8 did not add extra selections",
        scalar(
            admin,
            "SELECT count(*) FROM core.analysis_selections WHERE document_version_id=%s",
            document_version_id,
        ),
        1,
    )
    with connect("uap_model_governance") as rollback_gov:
        rollback_gov.autocommit = False
        rolled_run = insert_model_run(
            admin,
            document_version_id=document_version_id,
            task_type="claim_extraction",
            input_sha256=input_hash,
            tag=f"{tag}-rollback-setup",
        )
        rolled = insert_analysis(
            rollback_gov,
            model_run_id=rolled_run,
            document_version_id=document_version_id,
            result_type="claim_extraction",
            result={"claims": []},
        )
        rollback_gov.rollback()
        require(
            "rolled analysis absent",
            scalar(admin, "SELECT count(*) FROM core.analysis_results WHERE id=%s", rolled),
            0,
        )
        require(
            "rolled job absent",
            scalar(
                admin,
                "SELECT count(*) FROM ops.jobs WHERE idempotency_key=%s",
                f"resolve-claims:{rolled}",
            ),
            0,
        )
    return {"passed": True, "jobs": [str(first_job[0]), str(second_job[0]), str(entity_job[0])]}


def g8_02(
    admin: psycopg.Connection[Any], governance: psycopg.Connection[Any], tag: str
) -> dict[str, object]:
    _principal_id, document_version_id, _source_id = seed_document(admin, f"{tag}-g802")
    input_hash = sha256_text(f"g802-{tag}")
    insert_extraction(admin, document_version_id, f"{tag}-g802", input_hash)
    invalid_run = insert_model_run(
        admin,
        document_version_id=document_version_id,
        task_type="claim_extraction",
        input_sha256=input_hash,
        tag=f"{tag}-invalid",
    )
    summary_run = insert_model_run(
        admin,
        document_version_id=document_version_id,
        task_type="summary",
        input_sha256=input_hash,
        tag=f"{tag}-summary",
    )
    classification_run = insert_model_run(
        admin,
        document_version_id=document_version_id,
        task_type="classification",
        input_sha256=input_hash,
        tag=f"{tag}-class",
    )
    invalid_id = insert_analysis(
        governance,
        model_run_id=invalid_run,
        document_version_id=document_version_id,
        result_type="claim_extraction",
        result={"claims": []},
        validation_status="invalid",
    )
    summary_id = insert_analysis(
        governance,
        model_run_id=summary_run,
        document_version_id=document_version_id,
        result_type="summary",
        result={"summary": "ok", "bullets": ["one"]},
    )
    class_id = insert_analysis(
        governance,
        model_run_id=classification_run,
        document_version_id=document_version_id,
        result_type="classification",
        result={"labels": []},
    )
    for analysis_id, prefix in (
        (invalid_id, "resolve-claims:"),
        (summary_id, "resolve-claims:"),
        (class_id, "resolve-entities:"),
    ):
        require(
            "non-target not enqueued",
            scalar(
                admin,
                "SELECT count(*) FROM ops.jobs WHERE idempotency_key=%s",
                f"{prefix}{analysis_id}",
            ),
            0,
        )
    require(
        "knowledge tables unchanged",
        scalar(admin, "SELECT count(*) FROM core.claims WHERE claim_text=%s", f"wp8-ghost-{tag}"),
        0,
    )
    return {"passed": True, "skipped": [str(invalid_id), str(summary_id), str(class_id)]}


def g8_03(
    admin: psycopg.Connection[Any], governance: psycopg.Connection[Any], tag: str
) -> dict[str, object]:
    _principal_id, document_version_id, _source_id = seed_document(admin, f"{tag}-g803")
    input_hash = sha256_text(f"g803-{tag}")
    insert_extraction(admin, document_version_id, f"{tag}-g803", input_hash)
    run_id = insert_model_run(
        admin,
        document_version_id=document_version_id,
        task_type="claim_extraction",
        input_sha256=input_hash,
        tag=f"{tag}-idem",
    )
    analysis_id = insert_analysis(
        governance,
        model_run_id=run_id,
        document_version_id=document_version_id,
        result_type="claim_extraction",
        result={"claims": []},
    )

    def enqueue_once() -> str:
        with connect() as conn:
            return str(scalar(conn, "SELECT ops.enqueue_followup_job(%s)", analysis_id))

    with ThreadPoolExecutor(max_workers=2) as pool:
        concurrent = list(pool.map(lambda _index: enqueue_once(), range(2)))
    require("concurrent same id", concurrent[0], concurrent[1])
    job_id = uuid.UUID(concurrent[0])
    execute(
        admin,
        "UPDATE ops.jobs SET updated_at = TIMESTAMPTZ '2000-01-01T00:00:00Z' WHERE id=%s",
        job_id,
    )
    replay = scalar(admin, "SELECT ops.enqueue_followup_job(%s)", analysis_id)
    require("replay same id", replay, job_id)
    require(
        "replay does not update",
        scalar(
            admin,
            "SELECT updated_at = TIMESTAMPTZ '2000-01-01T00:00:00Z' FROM ops.jobs WHERE id=%s",
            job_id,
        ),
        True,
    )
    require(
        "no attempts created",
        scalar(admin, "SELECT count(*) FROM ops.job_attempts WHERE job_id=%s", job_id),
        0,
    )
    conflict_analysis = uuid.uuid4()
    execute(
        admin,
        """
        UPDATE ops.jobs
           SET payload = payload || jsonb_build_object('analysis_result_id', %s)
         WHERE id = %s
        """,
        str(conflict_analysis),
        job_id,
    )
    conflict_state, conflict_msg = sqlerror(
        admin, "SELECT ops.enqueue_followup_job(%s)", analysis_id
    )
    require("payload conflict sqlstate", conflict_state, "23505")
    require(
        "payload conflict message",
        conflict_msg,
        "knowledge_idempotency_payload_conflict",
    )
    execute(
        admin,
        """
        UPDATE ops.jobs
           SET payload = payload || jsonb_build_object('analysis_result_id', %s)
         WHERE id = %s
        """,
        str(analysis_id),
        job_id,
    )
    return {"passed": True, "job_id": str(job_id), "conflict_sqlstate": conflict_state}


def g8_04(
    admin: psycopg.Connection[Any], scheduler: psycopg.Connection[Any], tag: str
) -> dict[str, object]:
    _principal_id, document_version_id, _source_id = seed_document(admin, f"{tag}-g804")
    input_hash = sha256_text(f"g804-{tag}")
    insert_extraction(admin, document_version_id, f"{tag}-g804", input_hash)
    execute(
        admin,
        "ALTER TABLE core.analysis_results DISABLE TRIGGER analysis_results_enqueue_knowledge",
    )
    try:
        claim_run = insert_model_run(
            admin,
            document_version_id=document_version_id,
            task_type="claim_extraction",
            input_sha256=input_hash,
            tag=f"{tag}-backfill-claim",
        )
        entity_run = insert_model_run(
            admin,
            document_version_id=document_version_id,
            task_type="entity_extraction",
            input_sha256=input_hash,
            tag=f"{tag}-backfill-entity",
        )
        invalid_run = insert_model_run(
            admin,
            document_version_id=document_version_id,
            task_type="claim_extraction",
            input_sha256=input_hash,
            tag=f"{tag}-backfill-invalid",
        )
        summary_run = insert_model_run(
            admin,
            document_version_id=document_version_id,
            task_type="summary",
            input_sha256=input_hash,
            tag=f"{tag}-backfill-summary",
        )
        claim_id = insert_analysis(
            admin,
            model_run_id=claim_run,
            document_version_id=document_version_id,
            result_type="claim_extraction",
            result={"claims": []},
        )
        entity_id = insert_analysis(
            admin,
            model_run_id=entity_run,
            document_version_id=document_version_id,
            result_type="entity_extraction",
            result={"entities": []},
        )
        invalid_id = insert_analysis(
            admin,
            model_run_id=invalid_run,
            document_version_id=document_version_id,
            result_type="claim_extraction",
            result={"claims": []},
            validation_status="invalid",
        )
        summary_id = insert_analysis(
            admin,
            model_run_id=summary_run,
            document_version_id=document_version_id,
            result_type="summary",
            result={"summary": "ok", "bullets": ["one"]},
        )
        preset_payload = scalar(
            admin,
            "SELECT ops.enqueue_followup_job(%s)",
            claim_id,
        )
    finally:
        execute(
            admin,
            "ALTER TABLE core.analysis_results ENABLE TRIGGER analysis_results_enqueue_knowledge",
        )

    cutoff = scalar(admin, "SELECT clock_timestamp()")
    first_page = []
    with scheduler.cursor() as cursor:
        cursor.execute(
            """
            SELECT analysis_result_id, job_id, extraction_anchor_status,
                   next_after_created_at, next_after_id
              FROM ops.reconcile_knowledge_jobs(NULL, NULL, %s, 1)
            """,
            (cutoff,),
        )
        first_page = cursor.fetchall()
    require("first page size", len(first_page), 1)
    second_page = []
    with scheduler.cursor() as cursor:
        cursor.execute(
            """
            SELECT analysis_result_id, job_id, extraction_anchor_status,
                   next_after_created_at, next_after_id
              FROM ops.reconcile_knowledge_jobs(%s, %s, %s, 10)
            """,
            (first_page[0][3], first_page[0][4], cutoff),
        )
        second_page = cursor.fetchall()
    processed = {str(row[0]) for row in first_page + second_page}
    require("target analyses reconciled", {str(claim_id), str(entity_id)}.issubset(processed), True)
    require("invalid skipped", str(invalid_id) in processed, False)
    require("summary skipped", str(summary_id) in processed, False)
    require(
        "preset job reused",
        scalar(
            admin, "SELECT id FROM ops.jobs WHERE idempotency_key=%s", f"resolve-claims:{claim_id}"
        ),
        preset_payload,
    )
    replay = []
    with scheduler.cursor() as cursor:
        cursor.execute(
            """
            SELECT job_id FROM ops.reconcile_knowledge_jobs(NULL, NULL, %s, 1000)
            """,
            (cutoff,),
        )
        replay = [row[0] for row in cursor.fetchall()]
    original = [row[1] for row in first_page + second_page]
    require("replay same jobs", set(replay) >= set(original), True)
    dead_job = job_for(admin, claim_id, "claim_extraction")[0]
    dead_attempt = uuid.uuid4()
    execute(
        admin,
        """
        INSERT INTO ops.job_attempts (
            id, job_id, attempt_no, worker_id, started_at, finished_at, outcome
        ) VALUES (%s, %s, 1, 'wp8-1-dead-fixture', now(), now(), 'terminal_failure')
        """,
        dead_attempt,
        dead_job,
    )
    execute(
        admin,
        """
        UPDATE ops.jobs
           SET status = 'dead',
               completed_at = clock_timestamp(),
               lease_owner = NULL,
               lease_expires_at = NULL,
               lease_token = NULL,
               attempt_count = 1
         WHERE id = %s
        """,
        dead_job,
    )
    execute(
        admin,
        """
        INSERT INTO ops.dead_letters (
            id, job_id, last_attempt_id, reason_code, payload_snapshot, dead_at
        ) VALUES (%s, %s, %s, 'probe_dead', '{}'::jsonb, clock_timestamp())
        """,
        uuid.uuid4(),
        dead_job,
        dead_attempt,
    )
    require(
        "dead once",
        scalar(admin, "SELECT status::text FROM ops.jobs WHERE id=%s", dead_job),
        "dead",
    )
    require(
        "dead enqueue reuses key",
        scalar(admin, "SELECT ops.enqueue_followup_job(%s)", claim_id),
        dead_job,
    )
    require(
        "no second key",
        scalar(
            admin,
            "SELECT count(*) FROM ops.jobs WHERE idempotency_key=%s",
            f"resolve-claims:{claim_id}",
        ),
        1,
    )
    execute(scheduler, "SELECT ops.requeue_dead_letter(%s::uuid, 'wp8.1 probe'::text)", dead_job)
    require(
        "requeued",
        scalar(admin, "SELECT status::text FROM ops.jobs WHERE id=%s", dead_job),
        "queued",
    )
    conflict_run = insert_model_run(
        admin,
        document_version_id=document_version_id,
        task_type="claim_extraction",
        input_sha256=input_hash,
        tag=f"{tag}-conflict-recon",
    )
    execute(
        admin,
        "ALTER TABLE core.analysis_results DISABLE TRIGGER analysis_results_enqueue_knowledge",
    )
    try:
        conflict_id = insert_analysis(
            admin,
            model_run_id=conflict_run,
            document_version_id=document_version_id,
            result_type="claim_extraction",
            result={"claims": []},
        )
        execute(
            admin,
            """
            INSERT INTO ops.jobs (
                id, job_type, payload, payload_schema_version, idempotency_key,
                priority, available_at, max_attempts, timeout_seconds
            ) VALUES (
                %s, 'resolve_claims', '{"payload_schema_version":"other"}'::jsonb,
                'other.v1', %s, 0, now(), 8, 60
            )
            """,
            uuid.uuid4(),
            f"resolve-claims:{conflict_id}",
        )
    finally:
        execute(
            admin,
            "ALTER TABLE core.analysis_results ENABLE TRIGGER analysis_results_enqueue_knowledge",
        )
    conflict_state, conflict_msg = sqlerror(
        scheduler,
        "SELECT * FROM ops.reconcile_knowledge_jobs(NULL, NULL, clock_timestamp(), 1000)",
    )
    require("reconcile conflict", conflict_state, "23505")
    require("reconcile conflict message", conflict_msg, "knowledge_idempotency_payload_conflict")
    return {"passed": True, "first_page": len(first_page), "second_page": len(second_page)}


def g8_05(
    admin: psycopg.Connection[Any],
    worker: psycopg.Connection[Any],
    tag: str,
) -> dict[str, object]:
    states: dict[str, str] = {}
    for role in ("uap_worker", "uap_api"):
        with connect(role) as role_conn:
            states[f"{role}.insert_claims"] = sqlstate(
                role_conn,
                """
                INSERT INTO core.claims (
                    id, document_version_id, claim_text, claim_fingerprint,
                    claim_type, assertion_status, created_by
                ) VALUES (%s, %s, 'x', repeat('1', 64), 'other', 'reported', %s)
                """,
                uuid.uuid4(),
                uuid.uuid4(),
                uuid.uuid4(),
            )
            states[f"{role}.update_entities"] = sqlstate(
                role_conn,
                "UPDATE core.entities SET canonical_name='x' WHERE false",
            )
            states[f"{role}.delete_spans"] = sqlstate(
                role_conn,
                "DELETE FROM core.evidence_spans WHERE false",
            )
    states["worker.update_metrics"] = sqlstate(
        worker,
        "UPDATE ops.job_attempts SET metrics='{\"x\":1}'::jsonb WHERE false",
    )
    for role in (
        "uap_model_governance",
        "uap_publisher",
        "uap_scheduler",
        "uap_public_reader",
    ):
        with connect(role) as role_conn:
            states[f"{role}.finish"] = sqlstate(
                role_conn,
                """
                SELECT ops.finish_knowledge_job(
                    %s, %s, %s, 'succeeded'::ops.attempt_outcome,
                    NULL, NULL, NULL, NULL, %s::jsonb
                )
                """,
                uuid.uuid4(),
                uuid.uuid4(),
                uuid.uuid4(),
                empty_metrics(),
            )
    states["worker.model_runs"] = sqlstate(worker, "SELECT count(*) FROM ops.model_runs")
    states["worker.prompt_versions"] = sqlstate(worker, "SELECT count(*) FROM ops.prompt_versions")
    analysis_ok = scalar(worker, "SELECT count(*) FROM core.analysis_results")
    require("worker analysis_results select", isinstance(analysis_ok, int), True)

    _principal_id, document_version_id, _source_id = seed_document(admin, f"{tag}-g805")
    input_hash = sha256_text(f"g805-{tag}")
    insert_extraction(admin, document_version_id, f"{tag}-g805", input_hash)
    run_id = insert_model_run(
        admin,
        document_version_id=document_version_id,
        task_type="claim_extraction",
        input_sha256=input_hash,
        tag=f"{tag}-empty",
    )
    analysis_id = insert_analysis(
        connect("uap_model_governance"),
        model_run_id=run_id,
        document_version_id=document_version_id,
        result_type="claim_extraction",
        result={"claims": []},
    )
    job_id = job_for(admin, analysis_id, "claim_extraction")[0]
    attempt_id, token = grant_running_lease(admin, job_id, f"wp8-1-empty-{tag}")
    status = scalar(
        worker,
        """
        SELECT ops.finish_knowledge_job(
            %s::uuid, %s::uuid, %s::uuid, 'succeeded'::ops.attempt_outcome,
            NULL, NULL, NULL, NULL, %s::jsonb
        )::text
        """,
        job_id,
        attempt_id,
        token,
        empty_metrics(),
    )
    require("empty success", status, "succeeded")
    require(
        "metrics written",
        scalar(
            admin, "SELECT metrics->>'schema_version' FROM ops.job_attempts WHERE id=%s", attempt_id
        ),
        "knowledge-attempt-metrics.v1",
    )

    fetch_key = f"wp8-1-fetch-{tag}"
    fetch_id = scalar(
        connect("uap_scheduler"),
        "SELECT ops.enqueue_job('fetch_source', '{}'::jsonb, '1', %s, 0::smallint, now(), 1, 30)",
        fetch_key,
    )
    fetch_attempt, fetch_token = grant_running_lease(admin, fetch_id, f"wp8-1-fetch-worker-{tag}")
    states["non_resolve_finish"] = sqlstate(
        worker,
        """
        SELECT ops.finish_knowledge_job(
            %s::uuid, %s::uuid, %s::uuid, 'succeeded'::ops.attempt_outcome,
            NULL, NULL, NULL, NULL, %s::jsonb
        )
        """,
        fetch_id,
        fetch_attempt,
        fetch_token,
        empty_metrics(),
    )
    require(
        "fetch job still running",
        scalar(admin, "SELECT status::text FROM ops.jobs WHERE id=%s", fetch_id),
        "running",
    )

    expired_run = insert_model_run(
        admin,
        document_version_id=document_version_id,
        task_type="entity_extraction",
        input_sha256=input_hash,
        tag=f"{tag}-expired",
    )
    expired_analysis = insert_analysis(
        connect("uap_model_governance"),
        model_run_id=expired_run,
        document_version_id=document_version_id,
        result_type="entity_extraction",
        result={"entities": []},
    )
    expired_job = job_for(admin, expired_analysis, "entity_extraction")[0]
    expired_attempt, expired_token = grant_running_lease(admin, expired_job, f"wp8-1-expired-{tag}")
    execute(
        admin,
        """
        UPDATE ops.jobs
           SET lease_expires_at = clock_timestamp() - interval '1 second'
         WHERE id = %s
        """,
        expired_job,
    )
    states["expired_lease"] = sqlstate(
        worker,
        """
        SELECT ops.finish_knowledge_job(
            %s::uuid, %s::uuid, %s::uuid, 'succeeded'::ops.attempt_outcome,
            NULL, NULL, NULL, NULL, %s::jsonb
        )
        """,
        expired_job,
        expired_attempt,
        expired_token,
        empty_metrics(),
    )
    require(
        "expired metrics unchanged",
        scalar(admin, "SELECT metrics FROM ops.job_attempts WHERE id=%s", expired_attempt),
        {},
    )

    metrics_run = insert_model_run(
        admin,
        document_version_id=document_version_id,
        task_type="claim_extraction",
        input_sha256=input_hash,
        tag=f"{tag}-metrics",
    )
    metrics_analysis = insert_analysis(
        connect("uap_model_governance"),
        model_run_id=metrics_run,
        document_version_id=document_version_id,
        result_type="claim_extraction",
        result={"claims": []},
    )
    metrics_job = job_for(admin, metrics_analysis, "claim_extraction")[0]
    metrics_attempt, metrics_token = grant_running_lease(admin, metrics_job, f"wp8-1-metrics-{tag}")
    bad = dict(EMPTY_METRICS)
    bad["unknown"] = "nope"
    states["unknown_metrics"] = sqlstate(
        worker,
        """
        SELECT ops.finish_knowledge_job(
            %s::uuid, %s::uuid, %s::uuid, 'succeeded'::ops.attempt_outcome,
            NULL, NULL, NULL, NULL, %s::jsonb
        )
        """,
        metrics_job,
        metrics_attempt,
        metrics_token,
        json.dumps(bad),
    )
    require(
        "bad metrics still running",
        scalar(admin, "SELECT outcome::text FROM ops.job_attempts WHERE id=%s", metrics_attempt),
        "running",
    )
    expected_42501 = (
        "uap_worker.insert_claims",
        "uap_api.insert_claims",
        "uap_worker.update_entities",
        "uap_api.update_entities",
        "uap_worker.delete_spans",
        "uap_api.delete_spans",
        "worker.update_metrics",
        "uap_model_governance.finish",
        "uap_publisher.finish",
        "uap_scheduler.finish",
        "uap_public_reader.finish",
        "worker.model_runs",
        "worker.prompt_versions",
        "non_resolve_finish",
    )
    for name in expected_42501:
        require(name, states[name], "42501")
    require("expired_lease", states["expired_lease"], "40001")
    require("unknown_metrics", states["unknown_metrics"], "22023")
    return {"passed": True, "sqlstates": states}


def insert_span(
    admin: psycopg.Connection[Any],
    document_version_id: uuid.UUID,
    digest: str,
) -> uuid.UUID:
    span_id = uuid.uuid4()
    execute(
        admin,
        """
        INSERT INTO core.evidence_spans (
            id, document_version_id, evidence_text, locator_type,
            char_start, char_end, locator, locator_sha256
        ) VALUES (%s, %s, 'span', 'text', 0, 4, '{}'::jsonb, %s)
        """,
        span_id,
        document_version_id,
        digest,
    )
    return span_id


def g8_06(admin: psycopg.Connection[Any], tag: str) -> dict[str, object]:
    head = scalar(admin, "SELECT version_num FROM public.alembic_version")
    with admin.cursor() as cursor:
        cursor.execute(
            """
            SELECT schemaname || '.' || tablename
              FROM pg_tables
             WHERE schemaname IN ('ingest','core','ops','audit','public')
               AND tablename <> 'alembic_version'
            """
        )
        table_list = {row[0] for row in cursor.fetchall()}
    require("head", head, CURRENT_HEAD)
    require("table count", len(table_list), EXPECTED_TABLE_COUNT)
    require(
        "original tables",
        len(table_list - {"core.entity_candidate_evidence"}),
        WP3_ORIGINAL_TABLE_COUNT,
    )
    _principal_id, document_version_id, _source_id = seed_document(admin, f"{tag}-g806")
    other_document = seed_document(admin, f"{tag}-g806-other")[1]
    input_hash = sha256_text(f"g806-{tag}")
    insert_extraction(admin, document_version_id, f"{tag}-g806", input_hash)
    claim_run = insert_model_run(
        admin,
        document_version_id=document_version_id,
        task_type="claim_extraction",
        input_sha256=input_hash,
        tag=f"{tag}-origin-claim",
    )
    entity_run = insert_model_run(
        admin,
        document_version_id=document_version_id,
        task_type="entity_extraction",
        input_sha256=input_hash,
        tag=f"{tag}-origin-entity",
    )
    summary_run = insert_model_run(
        admin,
        document_version_id=document_version_id,
        task_type="summary",
        input_sha256=input_hash,
        tag=f"{tag}-origin-summary",
    )
    claim_analysis = insert_analysis(
        connect("uap_model_governance"),
        model_run_id=claim_run,
        document_version_id=document_version_id,
        result_type="claim_extraction",
        result={"claims": [{"text": "x"}]},
    )
    entity_analysis = insert_analysis(
        connect("uap_model_governance"),
        model_run_id=entity_run,
        document_version_id=document_version_id,
        result_type="entity_extraction",
        result={"entities": [{"name": "x"}]},
    )
    summary_analysis = insert_analysis(
        connect("uap_model_governance"),
        model_run_id=summary_run,
        document_version_id=document_version_id,
        result_type="summary",
        result={"summary": "ok", "bullets": ["one"]},
    )
    span_id = insert_span(admin, document_version_id, sha256_text(f"span-{tag}"))
    other_span = insert_span(admin, other_document, sha256_text(f"other-span-{tag}"))
    invalid_origin = sqlstate(
        admin,
        """
        INSERT INTO core.claims (
            id, origin_analysis_result_id, document_version_id, ordinal, claim_text,
            claim_fingerprint, claim_type, assertion_status
        ) VALUES (%s, %s, %s, 0, 'bad origin', repeat('c', 64), 'other', 'reported')
        """,
        uuid.uuid4(),
        summary_analysis,
        document_version_id,
    )
    bare_ai = sqlstate(
        admin,
        """
        INSERT INTO core.claims (
            id, origin_analysis_result_id, document_version_id, ordinal, claim_text,
            claim_fingerprint, claim_type, assertion_status
        ) VALUES (%s, %s, %s, 0, 'bare ai', repeat('d', 64), 'other', 'reported')
        """,
        uuid.uuid4(),
        claim_analysis,
        document_version_id,
    )
    claim_id = uuid.uuid4()
    admin.autocommit = False
    try:
        execute(
            admin,
            """
            INSERT INTO core.claims (
                id, origin_analysis_result_id, document_version_id, ordinal, claim_text,
                claim_fingerprint, claim_type, assertion_status
            ) VALUES (%s, %s, %s, 0, 'supported', repeat('e', 64), 'other', 'reported')
            """,
            claim_id,
            claim_analysis,
            document_version_id,
        )
        execute(
            admin,
            """
            INSERT INTO core.claim_evidence (
                id, claim_id, evidence_span_id, document_version_id, support_type
            ) VALUES (%s, %s, %s, %s, 'supports')
            """,
            uuid.uuid4(),
            claim_id,
            span_id,
            document_version_id,
        )
        admin.commit()
    except Exception:
        admin.rollback()
        raise
    finally:
        admin.autocommit = True
    cross_version = sqlstate(
        admin,
        """
        INSERT INTO core.claim_evidence (
            id, claim_id, evidence_span_id, document_version_id, support_type
        ) VALUES (%s, %s, %s, %s, 'supports')
        """,
        uuid.uuid4(),
        claim_id,
        other_span,
        document_version_id,
    )
    invalid_entity_origin = sqlstate(
        admin,
        """
        INSERT INTO core.entity_candidates (
            id, analysis_result_id, document_version_id, ordinal, proposed_entity_type,
            proposed_name, candidate_payload
        ) VALUES (%s, %s, %s, 0, 'person', 'x', '{}'::jsonb)
        """,
        uuid.uuid4(),
        claim_analysis,
        document_version_id,
    )
    no_evidence_candidate = sqlstate(
        admin,
        """
        INSERT INTO core.entity_candidates (
            id, analysis_result_id, document_version_id, ordinal, proposed_entity_type,
            proposed_name, candidate_payload
        ) VALUES (%s, %s, %s, 1, 'person', 'x', '{}'::jsonb)
        """,
        uuid.uuid4(),
        entity_analysis,
        document_version_id,
    )
    candidate_id = uuid.uuid4()
    admin.autocommit = False
    try:
        execute(
            admin,
            """
            INSERT INTO core.entity_candidates (
                id, analysis_result_id, document_version_id, ordinal, proposed_entity_type,
                proposed_name, candidate_payload, evidence_span_id
            ) VALUES (%s, %s, %s, 0, 'person', 'multi', '{}'::jsonb, NULL)
            """,
            candidate_id,
            entity_analysis,
            document_version_id,
        )
        for index in range(20):
            link_span = insert_span(admin, document_version_id, sha256_text(f"multi-{tag}-{index}"))
            execute(
                admin,
                """
                INSERT INTO core.entity_candidate_evidence (
                    id, entity_candidate_id, evidence_span_id, document_version_id, evidence_ordinal
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                uuid.uuid4(),
                candidate_id,
                link_span,
                document_version_id,
                index,
            )
        admin.commit()
    except Exception:
        admin.rollback()
        raise
    finally:
        admin.autocommit = True
    require(
        "twenty evidence rows",
        scalar(
            admin,
            "SELECT count(*) FROM core.entity_candidate_evidence WHERE entity_candidate_id=%s",
            candidate_id,
        ),
        20,
    )
    require(
        "legacy column unused",
        scalar(
            admin, "SELECT evidence_span_id FROM core.entity_candidates WHERE id=%s", candidate_id
        ),
        None,
    )
    downgrade_blocked, downgrade_msg = sqlerror(
        admin,
        """
        DO $protect$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM core.entity_candidate_evidence
                 GROUP BY entity_candidate_id
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION 'knowledge_entity_evidence_downgrade_blocked'
                    USING ERRCODE = '55000';
            END IF;
        END
        $protect$;
        """,
    )
    require("invalid origin", invalid_origin, "23514")
    require("bare ai", bare_ai, "23514")
    require("cross version", cross_version, "23503")
    require("invalid entity origin", invalid_entity_origin, "23514")
    require("no evidence candidate", no_evidence_candidate, "23514")
    require("downgrade blocked", downgrade_blocked, "55000")
    require("downgrade message", downgrade_msg, "knowledge_entity_evidence_downgrade_blocked")
    require(
        "multi evidence retained",
        scalar(
            admin,
            "SELECT count(*) FROM core.entity_candidate_evidence WHERE entity_candidate_id=%s",
            candidate_id,
        ),
        20,
    )
    fingerprint = scalar(admin, "SELECT core.compute_claim_fingerprint(%s)", "  Hello   World  ")
    require("fingerprint length", len(str(fingerprint)), 64)
    envelope = {
        "locator_schema_version": "evidence-locator.v2",
        "document_version_id": str(document_version_id),
    }
    digest = scalar(
        admin, "SELECT core.compute_evidence_locator_sha256(%s::jsonb)", json.dumps(envelope)
    )
    require("locator hash length", len(str(digest)), 64)
    return {
        "passed": True,
        "tables": len(table_list),
        "invalid_origin": invalid_origin,
        "cross_version": cross_version,
        "downgrade_blocked": downgrade_blocked,
    }


def main() -> None:
    tag = uuid.uuid4().hex[:12]
    admin = connect()
    governance = connect("uap_model_governance")
    scheduler = connect("uap_scheduler")
    worker = connect("uap_worker")
    results = {
        "head": scalar(admin, "SELECT version_num FROM public.alembic_version"),
        "G8-01": g8_01(admin, governance, tag),
        "G8-02": g8_02(admin, governance, tag),
        "G8-03": g8_03(admin, governance, tag),
        "G8-04": g8_04(admin, scheduler, tag),
        "G8-05": g8_05(admin, worker, tag),
        "G8-06": g8_06(admin, tag),
    }
    print(json.dumps(results, indent=2, sort_keys=True, default=str))
    failed = [
        name
        for name, payload in results.items()
        if name.startswith("G8-") and isinstance(payload, dict) and not payload.get("passed")
    ]
    if failed:
        raise SystemExit(f"WP8.1 runtime probe failed: {', '.join(failed)}")
    print("WP8.1 runtime probe passed: G8-01 G8-02 G8-03 G8-04 G8-05 G8-06")


if __name__ == "__main__":
    main()
