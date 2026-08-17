"""Exercise the G5 source-config provenance and WP4 job lifecycle in PostgreSQL."""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any, cast

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from uap_platform.collectors import (
    CollectionResult,
    FetchClassification,
    FetchResponse,
    PostgresSourceRunStore,
    RssSourceRunRunner,
)
from uap_platform.object_registry import ObjectClient


def role_connection(admin_url: str) -> psycopg.Connection[Any]:
    params = conninfo_to_dict(admin_url.replace("postgresql+psycopg://", "postgresql://"))
    params.pop("user", None)
    params.pop("password", None)
    base = make_conninfo(**params)  # type: ignore[arg-type]
    return psycopg.connect(
        make_conninfo(
            base,
            user="uap_worker",
            password=os.environ["UAP_WORKER_PASSWORD"],
        )
    )


def enqueue(connection: psycopg.Connection[Any], key: str) -> uuid.UUID:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT ops.enqueue_job(
                'fetch_source', '{}'::jsonb, 'rss.v1', %s, 1000::smallint,
                clock_timestamp(), 3, 60
            )
            """,
            (key,),
        )
        row = cursor.fetchone()
    connection.commit()
    if row is None:
        raise RuntimeError("enqueue_job returned no job id")
    return uuid.UUID(str(row[0]))


def main() -> None:
    connection = role_connection(os.environ["UAP_DATABASE_URL"])
    source_id = uuid.UUID("00000000-0000-7500-8000-000000000001")
    other_source_id = uuid.UUID("00000000-0000-7500-8000-000000000002")
    config_id = uuid.UUID("00000000-0000-7500-8000-000000000011")
    principal_id = uuid.UUID("00000000-0000-7000-8000-000000000001")
    probe_key = uuid.uuid4().hex
    now = datetime.now(UTC)
    with connection.cursor() as cursor:
        for current_id, slug in (
            (source_id, "wp5-runtime-a"),
            (other_source_id, "wp5-runtime-b"),
        ):
            cursor.execute(
                """
                INSERT INTO ingest.sources
                    (id, slug, name, source_type, homepage_url)
                VALUES (%s, %s, %s, 'rss', %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (current_id, slug, slug, f"https://{slug}.example.test"),
            )
        cursor.execute(
            """
            INSERT INTO ingest.source_config_versions
                (id, source_id, version_no, configuration, configuration_sha256,
                 effective_from, changed_by, change_reason)
            VALUES (%s, %s, 1, '{}'::jsonb, repeat('a', 64), %s, %s, 'G5 runtime probe')
            ON CONFLICT (id) DO NOTHING
            """,
            (config_id, source_id, now, principal_id),
        )
    connection.commit()

    job_id = enqueue(connection, f"wp5-runtime-success-{probe_key}")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM ops.claim_job('worker', 'wp5-runtime-worker', ARRAY['fetch_source'], 60)"
        )
        claim = cursor.fetchone()
    connection.commit()
    if claim is None:
        raise RuntimeError("claim_job returned no claim")
    job_id = uuid.UUID(str(claim[0]))
    attempt_id = uuid.UUID(str(claim[1]))
    lease_token = uuid.UUID(str(claim[7]))

    store = PostgresSourceRunStore(connection, cast(ObjectClient, None))
    result = RssSourceRunRunner(
        lambda _url, _headers: FetchResponse(200, b""),
        store,
    ).run(
        source_id,
        job_id,
        f"wp5-runtime-success-{probe_key}",
        "https://wp5-runtime-a.example.test/feed",
        attempt_id=attempt_id,
        lease_token=lease_token,
        source_config_version_id=config_id,
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT outcome FROM ingest.source_runs WHERE job_id = %s", (job_id,)
        )
        run_outcome = cursor.fetchone()
        cursor.execute("SELECT status FROM ops.jobs WHERE id = %s", (job_id,))
        job_status = cursor.fetchone()
    if (
        result.classification.value != "empty"
        or run_outcome != ("empty",)
        or job_status != ("succeeded",)
    ):
        raise RuntimeError(f"unexpected lifecycle result: {result}, {run_outcome}, {job_status}")

    enqueue(connection, f"wp5-runtime-retry-{probe_key}")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM ops.claim_job('worker', 'wp5-runtime-worker', ARRAY['fetch_source'], 60)"
        )
        retry_claim = cursor.fetchone()
    connection.commit()
    if retry_claim is None:
        raise RuntimeError("claim_job returned no retry claim")
    retry_job_id = uuid.UUID(str(retry_claim[0]))
    retry_result = RssSourceRunRunner(
        lambda _url, _headers: (_ for _ in ()).throw(TimeoutError("probe timeout")),
        store,
    ).run(
        source_id,
        retry_job_id,
        f"wp5-runtime-retry-{probe_key}",
        "https://wp5-runtime-a.example.test/feed",
        attempt_id=uuid.UUID(str(retry_claim[1])),
        lease_token=uuid.UUID(str(retry_claim[7])),
        source_config_version_id=config_id,
    )
    with connection.cursor() as cursor:
        cursor.execute("SELECT status FROM ops.jobs WHERE id = %s", (retry_job_id,))
        retry_status = cursor.fetchone()
    if retry_result.error_code != "timeout" or retry_status != ("retry_wait",):
        raise RuntimeError(f"unexpected retry lifecycle result: {retry_result}, {retry_status}")

    atomic_enqueued_job = enqueue(connection, f"wp5-runtime-atomic-{probe_key}")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM ops.claim_job('worker', 'wp5-runtime-worker', ARRAY['fetch_source'], 1)"
        )
        atomic_claim = cursor.fetchone()
    connection.commit()
    if atomic_claim is None:
        raise RuntimeError("claim_job returned no atomicity claim")
    atomic_job_id = uuid.UUID(str(atomic_claim[0]))
    if atomic_job_id != atomic_enqueued_job:
        raise RuntimeError("atomicity probe did not claim its newly enqueued job")
    atomic_attempt_id = uuid.UUID(str(atomic_claim[1]))
    atomic_lease_token = uuid.UUID(str(atomic_claim[7]))
    atomic_run_id = store.start_source_run(
        source_id,
        atomic_job_id,
        f"wp5-runtime-atomic-{probe_key}",
        datetime.now(UTC),
        config_id,
    )
    time.sleep(1.2)
    try:
        store.finish_source_run_and_job(
            atomic_run_id,
            atomic_job_id,
            atomic_attempt_id,
            atomic_lease_token,
            CollectionResult(FetchClassification.EMPTY, 200, 0),
            datetime.now(UTC),
        )
    except psycopg.errors.SerializationFailure:
        pass
    else:
        raise RuntimeError("expired lease unexpectedly committed source run and job")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT outcome, finished_at FROM ingest.source_runs WHERE id = %s",
            (atomic_run_id,),
        )
        atomic_run_state = cursor.fetchone()
    if (
        atomic_run_state is None
        or atomic_run_state[0] != "failed"
        or atomic_run_state[1] is not None
    ):
        raise RuntimeError(f"source run was not rolled back atomically: {atomic_run_state}")

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM ops.claim_job('worker', 'wp5-runtime-worker', ARRAY['fetch_source'], 60)"
        )
        atomic_retry_claim = cursor.fetchone()
    connection.commit()
    if atomic_retry_claim is None or uuid.UUID(str(atomic_retry_claim[0])) != atomic_job_id:
        raise RuntimeError("expired job could not be reclaimed for source-run retry")
    atomic_retry_result = RssSourceRunRunner(
        lambda _url, _headers: FetchResponse(200, b""),
        store,
    ).run(
        source_id,
        atomic_job_id,
        f"wp5-runtime-atomic-retry-{probe_key}",
        "https://wp5-runtime-a.example.test/feed",
        attempt_id=uuid.UUID(str(atomic_retry_claim[1])),
        lease_token=uuid.UUID(str(atomic_retry_claim[7])),
        source_config_version_id=config_id,
    )
    if atomic_retry_result.classification.value != "empty":
        raise RuntimeError("reclaimed source-run retry did not complete")

    cross_source_job = enqueue(connection, f"wp5-runtime-cross-source-{probe_key}")
    try:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO ingest.source_runs
                        (id, source_id, source_config_version_id, job_id, run_key,
                         outcome, started_at)
                    VALUES (%s, %s, %s, %s, %s, 'failed', %s)
                    """,
                    (uuid.uuid4(), other_source_id, config_id, cross_source_job,
                     f"wp5-runtime-cross-source-{probe_key}", now),
                )
    except psycopg.errors.ForeignKeyViolation as error:
        if error.sqlstate != "23503":
            raise
    else:
        raise RuntimeError("cross-source config reference was accepted")

    print(
        {
            "source_run_outcome": run_outcome[0],
            "job_status": job_status[0],
            "retry_job_status": retry_status[0],
            "atomic_retry": "reclaimed and completed",
            "cross_source_config_sqlstate": "23503",
        }
    )


if __name__ == "__main__":
    main()
