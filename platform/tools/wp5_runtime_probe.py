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


def admin_connection(admin_url: str) -> psycopg.Connection[Any]:
    return psycopg.connect(
        admin_url.replace("postgresql+psycopg://", "postgresql://")
    )


def enqueue(connection: psycopg.Connection[Any], key: str) -> uuid.UUID:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT ops.enqueue_job(
                'fetch_source', '{}'::jsonb, 'rss.v1', %s, 32767::smallint,
                'epoch'::timestamptz, 3, 60
            )
            """,
            (key,),
        )
        row = cursor.fetchone()
    connection.commit()
    if row is None:
        raise RuntimeError("enqueue_job returned no job id")
    return uuid.UUID(str(row[0]))


def claim_expected(
    connection: psycopg.Connection[Any],
    expected_job_id: uuid.UUID,
    lease_seconds: int,
) -> tuple[Any, ...]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM ops.claim_job("
            "'worker', 'wp5-runtime-worker', ARRAY['fetch_source'], %s)",
            (lease_seconds,),
        )
        claim = cursor.fetchone()
    connection.commit()
    if claim is None:
        raise RuntimeError(f"claim_job returned no claim for {expected_job_id}")
    claimed_job_id = uuid.UUID(str(claim[0]))
    if claimed_job_id != expected_job_id:
        raise RuntimeError(
            f"claim_job returned {claimed_job_id}; expected {expected_job_id}"
        )
    return cast(tuple[Any, ...], claim)


def main() -> None:
    database_url = os.environ["UAP_DATABASE_URL"]
    connection = role_connection(database_url)
    administrator = admin_connection(database_url)
    source_id = uuid.uuid4()
    other_source_id = uuid.uuid4()
    config_id = uuid.uuid4()
    other_config_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    probe_key = uuid.uuid4().hex
    now = datetime.now(UTC)
    with administrator.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO audit.principals
                (id, principal_type, service_name, display_name)
            VALUES (%s, 'service', %s, 'WP5 runtime probe')
            """,
            (principal_id, f"wp5-runtime-{probe_key}"),
        )
    administrator.commit()
    administrator.close()

    with connection.cursor() as cursor:
        for current_id, slug in (
            (source_id, f"wp5-runtime-a-{probe_key}"),
            (other_source_id, f"wp5-runtime-b-{probe_key}"),
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
        for current_config_id, current_source_id, digest in (
            (config_id, source_id, "a"),
            (other_config_id, other_source_id, "b"),
        ):
            cursor.execute(
                """
                INSERT INTO ingest.source_config_versions
                    (id, source_id, version_no, configuration, configuration_sha256,
                     effective_from, changed_by, change_reason)
                VALUES (%s, %s, 1, '{}'::jsonb, repeat(%s, 64), %s, %s,
                        'G5 runtime probe')
                """,
                (current_config_id, current_source_id, digest, now, principal_id),
            )
    connection.commit()

    job_id = enqueue(connection, f"wp5-runtime-success-{probe_key}")
    claim = claim_expected(connection, job_id, 60)
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

    retry_job_id = enqueue(connection, f"wp5-runtime-retry-{probe_key}")
    retry_claim = claim_expected(connection, retry_job_id, 60)
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
    atomic_claim = claim_expected(connection, atomic_enqueued_job, 1)
    atomic_job_id = uuid.UUID(str(atomic_claim[0]))
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

    atomic_retry_claim = claim_expected(connection, atomic_job_id, 60)
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

    try:
        store.start_source_run(
            other_source_id,
            atomic_job_id,
            f"wp5-runtime-provenance-relabel-{probe_key}",
            datetime.now(UTC),
            other_config_id,
        )
    except RuntimeError as error:
        if "provenance" not in str(error):
            raise
    else:
        raise RuntimeError("source-run retry changed its provenance")
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT source_id, source_config_version_id
              FROM ingest.source_runs
             WHERE id = %s
            """,
            (atomic_run_id,),
        )
        provenance = cursor.fetchone()
    if provenance != (source_id, config_id):
        raise RuntimeError(f"source-run provenance was rewritten: {provenance}")

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
            "provenance_relabel": "rejected",
            "cross_source_config_sqlstate": "23503",
        }
    )


if __name__ == "__main__":
    main()
