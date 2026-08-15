"""Exercise WP4 durable jobs, retry policy, dead letters, and Outbox leases."""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

import psycopg
from psycopg.conninfo import make_conninfo

from uap_platform.config import load_settings

ROLE_PASSWORDS = {
    "uap_worker": "UAP_WORKER_PASSWORD",
    "uap_scheduler": "UAP_SCHEDULER_PASSWORD",
    "uap_publisher": "UAP_PUBLISHER_PASSWORD",
}


def id_for(number: int) -> uuid.UUID:
    return uuid.UUID(f"00000000-0000-7400-8000-{number:012d}")


def connection_for(admin_url: str, role: str) -> psycopg.Connection[Any]:
    password = os.environ.get(ROLE_PASSWORDS[role])
    if not password:
        raise RuntimeError(f"missing password for {role}")
    connection = psycopg.connect(make_conninfo(admin_url, user=role, password=password))
    connection.autocommit = True
    return connection


def scalar(connection: psycopg.Connection[Any], statement: str, *params: object) -> Any:
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("probe query returned no row")
    return row[0]


def rejected(connection: psycopg.Connection[Any], statement: str, *params: object) -> str:
    try:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute(statement, params)
    except psycopg.Error as error:
        return str(error.sqlstate)
    raise RuntimeError("probe accepted a forbidden operation")


def rejected_in_open_transaction(
    connection: psycopg.Connection[Any], statement: str, *params: object
) -> str:
    try:
        with connection.cursor() as cursor:
            cursor.execute(statement, params)
    except psycopg.Error as error:
        state = str(error.sqlstate)
        connection.rollback()
        return state
    raise RuntimeError("probe accepted an expired lease in an open transaction")


def main() -> None:
    settings = load_settings()
    admin_url = settings.psycopg_database_url
    admin = psycopg.connect(admin_url)
    admin.autocommit = True
    run_tag = uuid.uuid4().hex
    runtime_key = f"wp4-runtime-job-{run_tag}"
    forbidden_key = f"wp4-forbidden-publish-{run_tag}"
    publisher_key = f"wp4-publisher-job-{run_tag}"
    expired_key = f"wp4-expired-job-{run_tag}"
    clock_job_key = f"wp4-clock-job-{run_tag}"
    event_key = f"wp4-event-key-{run_tag}"
    clock_event_key = f"wp4-clock-event-{run_tag}"
    worker = connection_for(admin_url, "uap_worker")
    scheduler = connection_for(admin_url, "uap_scheduler")
    publisher = connection_for(admin_url, "uap_publisher")
    try:
        for http_status, error_code, expected in (
            (408, None, "retryable_failure"),
            (429, None, "retryable_failure"),
            (503, "upstream_5xx", "retryable_failure"),
            (None, "timeout", "retryable_failure"),
            (403, "forbidden", "terminal_failure"),
        ):
            classified = scalar(
                worker,
                "SELECT ops.classify_failure(%s::smallint, %s::text)::text",
                http_status,
                error_code,
            )
            if classified != expected:
                raise RuntimeError(
                    f"failure classification {http_status}/{error_code} was {classified}"
                )

        job_id = scalar(
            scheduler,
            "SELECT ops.enqueue_job(%s, %s::jsonb, '1', %s, 100::smallint, now(), 3, 30)",
            "fetch_source",
            '{"source":"probe"}',
            runtime_key,
        )
        duplicate_id = scalar(
            scheduler,
            "SELECT ops.enqueue_job(%s, %s::jsonb, '1', %s, 100::smallint, now(), 3, 30)",
            "fetch_source",
            '{"source":"different-payload"}',
            runtime_key,
        )
        if job_id != duplicate_id:
            raise RuntimeError("idempotent enqueue returned two job IDs")

        claim = scalar(
            worker,
            """SELECT row_to_json(claimed)::jsonb
                 FROM ops.claim_job(
                     'worker', 'wp4-worker-a', ARRAY['fetch_source'], 30
                 ) AS claimed""",
        )
        if claim is None:
            raise RuntimeError("worker failed to claim queued job")
        if claim["job_id"] != str(job_id):
            raise RuntimeError("worker claimed a stale job from another probe run")
        attempt_id = claim["attempt_id"]
        token = claim["lease_token"]
        second_claim = scalar(
            worker,
            """SELECT count(*)
                 FROM ops.claim_job('worker', 'wp4-worker-b', ARRAY['fetch_source'], 30)""",
        )
        if second_claim != 0:
            raise RuntimeError("two workers claimed one job")

        forbidden = rejected(
            worker,
            "SELECT ops.enqueue_job('publish_document', '{}'::jsonb, '1', %s, "
            "0::smallint, now(), 1, 30)",
            forbidden_key,
        )
        if forbidden != "42501":
            raise RuntimeError(f"worker publisher enqueue SQLSTATE was {forbidden}")

        retry_status = scalar(
            worker,
            """SELECT ops.finish_job(
                %s::uuid, %s::uuid, %s::uuid,
                'retryable_failure'::ops.attempt_outcome,
                503::smallint, 'upstream_5xx'::text, 'probe retry'::text, 0
            )""",
            job_id,
            attempt_id,
            token,
        )
        if retry_status != "retry_wait":
            raise RuntimeError(f"503 did not enter retry_wait: {retry_status}")

        clock_job_id = scalar(
            scheduler,
            "SELECT ops.enqueue_job('fetch_source', '{}'::jsonb, '1', %s, "
            "32767::smallint, now(), 1, 30)",
            clock_job_key,
        )
        long_worker = connection_for(admin_url, "uap_worker")
        long_worker.autocommit = False
        try:
            clock_claim = scalar(
                long_worker,
                """SELECT row_to_json(claimed)::jsonb
                     FROM ops.claim_job(
                         'worker', 'wp4-worker-clock', ARRAY['fetch_source'], 1
                     ) AS claimed""",
            )
            time.sleep(1.2)
            expired_finish_state = rejected_in_open_transaction(
                long_worker,
                "SELECT ops.finish_job(%s::uuid, %s::uuid, %s::uuid, "
                "'succeeded'::ops.attempt_outcome)",
                clock_job_id,
                clock_claim["attempt_id"],
                clock_claim["lease_token"],
            )
            if expired_finish_state != "40001":
                raise RuntimeError(
                    f"long-transaction job finish returned SQLSTATE {expired_finish_state}"
                )
        finally:
            long_worker.close()
        cleanup_clock_job = scalar(
            worker,
            "SELECT row_to_json(claimed)::jsonb "
            "FROM ops.claim_job('worker', 'wp4-clock-cleanup', "
            "ARRAY['fetch_source'], 30) AS claimed",
        )
        if cleanup_clock_job["job_id"] != str(clock_job_id):
            raise RuntimeError("clock lease fixture cleanup claimed the wrong job")
        scalar(
            worker,
            "SELECT ops.finish_job(%s::uuid, %s::uuid, %s::uuid, "
            "'succeeded'::ops.attempt_outcome)",
            clock_job_id,
            cleanup_clock_job["attempt_id"],
            cleanup_clock_job["lease_token"],
        )

        publisher_job_id = scalar(
            scheduler,
            "SELECT ops.enqueue_job('publish_document', '{}'::jsonb, '1', %s, "
            "32767::smallint, now(), 1, 30)",
            publisher_key,
        )
        worker_publish_claim = rejected(
            worker,
            """SELECT *
                 FROM ops.claim_job(
                     'worker', 'wp4-worker-c', ARRAY['publish_document'], 30
                 )""",
        )
        if worker_publish_claim != "42501":
            raise RuntimeError(f"worker publisher claim SQLSTATE was {worker_publish_claim}")
        publisher_claim = scalar(
            publisher,
            """SELECT row_to_json(claimed)::jsonb
                 FROM ops.claim_job(
                     'publisher', 'wp4-publisher-a', ARRAY['publish_document'], 30
                 ) AS claimed""",
        )
        if publisher_claim["job_id"] != str(publisher_job_id):
            raise RuntimeError("publisher claimed the wrong job")
        publisher_dead_status = scalar(
            publisher,
            """SELECT ops.finish_job(
                %s::uuid, %s::uuid, %s::uuid,
                'terminal_failure'::ops.attempt_outcome,
                403::smallint, 'forbidden'::text, 'publisher dead-letter fixture'::text
            )""",
            publisher_job_id,
            publisher_claim["attempt_id"],
            publisher_claim["lease_token"],
        )
        if publisher_dead_status != "dead":
            raise RuntimeError("publisher dead-letter fixture did not enter dead state")
        worker_requeue_publisher = rejected(
            worker,
            "SELECT ops.requeue_dead_letter(%s::uuid, 'worker boundary probe'::text)",
            publisher_job_id,
        )
        if worker_requeue_publisher != "42501":
            raise RuntimeError(
                f"worker requeued a publisher dead letter with SQLSTATE {worker_requeue_publisher}"
            )

        event_id = scalar(
            scheduler,
            "SELECT ops.emit_outbox(%s, 'document', %s, 'publish_requested', %s, '{}'::jsonb)",
            publisher_job_id,
            id_for(1),
            event_key,
        )
        duplicate_event_id = scalar(
            scheduler,
            """SELECT ops.emit_outbox(
                %s, 'document', %s, 'publish_requested', %s,
                '{\"duplicate\":true}'::jsonb
            )""",
            publisher_job_id,
            id_for(1),
            event_key,
        )
        if event_id != duplicate_event_id:
            raise RuntimeError("idempotent Outbox emit returned two IDs")
        event = scalar(
            publisher,
            """SELECT row_to_json(claimed)::jsonb
                 FROM ops.claim_outbox('wp4-dispatcher-a', 30, 10) AS claimed""",
        )
        if event["event_id"] != str(event_id):
            raise RuntimeError("publisher claimed the wrong Outbox event")
        with admin.cursor() as cursor:
            cursor.execute(
                "UPDATE ops.outbox_events SET lease_expires_at=now()-interval '1 second' "
                "WHERE id=%s",
                (event_id,),
            )
        for statement, params in (
            (
                "SELECT ops.ack_outbox(%s::uuid, %s::uuid)",
                (event_id, event["lease_token"]),
            ),
            (
                "SELECT ops.publish_outbox_failure(%s::uuid, %s::uuid, 'timeout', 'expired', 0)",
                (event_id, event["lease_token"]),
            ),
            (
                "SELECT ops.release_outbox(%s::uuid, %s::uuid, 'timeout', 'expired')",
                (event_id, event["lease_token"]),
            ),
        ):
            expired_outbox_state = rejected(publisher, statement, *params)
            if expired_outbox_state != "40001":
                raise RuntimeError(f"expired Outbox lease returned SQLSTATE {expired_outbox_state}")
        reclaimed_event = scalar(
            publisher,
            "SELECT row_to_json(claimed)::jsonb "
            "FROM ops.claim_outbox('wp4-dispatcher-expired', 30, 10) AS claimed",
        )
        if reclaimed_event["event_id"] != str(event_id):
            raise RuntimeError("expired Outbox event was not reclaimed")
        scalar(
            publisher,
            "SELECT ops.publish_outbox_failure(%s, %s, 'timeout', 'probe retry', 0)",
            event_id,
            reclaimed_event["lease_token"],
        )
        retried_event = scalar(
            publisher,
            "SELECT row_to_json(claimed)::jsonb "
            "FROM ops.claim_outbox('wp4-dispatcher-b', 30, 10) AS claimed",
        )
        if retried_event["event_id"] != str(event_id):
            raise RuntimeError("failed Outbox event was not available for retry")
        scalar(publisher, "SELECT ops.ack_outbox(%s, %s)", event_id, retried_event["lease_token"])
        if (
            scalar(
                publisher,
                "SELECT published_at IS NOT NULL FROM ops.outbox_events WHERE id=%s",
                event_id,
            )
            is not True
        ):
            raise RuntimeError("Outbox acknowledgement did not publish event")

        clock_event_id = scalar(
            scheduler,
            "SELECT ops.emit_outbox(%s, 'document', %s, 'clock_probe', %s, '{}'::jsonb)",
            job_id,
            id_for(4),
            clock_event_key,
        )
        long_publisher = connection_for(admin_url, "uap_publisher")
        long_publisher.autocommit = False
        try:
            clock_event = scalar(
                long_publisher,
                "SELECT row_to_json(claimed)::jsonb "
                "FROM ops.claim_outbox('wp4-dispatcher-clock', 1, 10) AS claimed",
            )
            time.sleep(1.2)
            expired_ack_state = rejected_in_open_transaction(
                long_publisher,
                "SELECT ops.ack_outbox(%s::uuid, %s::uuid)",
                clock_event_id,
                clock_event["lease_token"],
            )
            if expired_ack_state != "40001":
                raise RuntimeError(
                    f"long-transaction Outbox ack returned SQLSTATE {expired_ack_state}"
                )
        finally:
            long_publisher.close()
        cleanup_clock_event = scalar(
            publisher,
            "SELECT row_to_json(claimed)::jsonb "
            "FROM ops.claim_outbox('wp4-dispatcher-clock-cleanup', 30, 10) AS claimed",
        )
        scalar(
            publisher,
            "SELECT ops.ack_outbox(%s::uuid, %s::uuid)",
            clock_event_id,
            cleanup_clock_event["lease_token"],
        )

        expired_job_id = scalar(
            scheduler,
            "SELECT ops.enqueue_job('fetch_source', '{}'::jsonb, '1', %s, "
            "32767::smallint, now(), 2, 30)",
            expired_key,
        )
        expired_claim = scalar(
            worker,
            """SELECT row_to_json(claimed)::jsonb
                 FROM ops.claim_job(
                     'worker', 'wp4-worker-expire', ARRAY['fetch_source'], 1
                 ) AS claimed""",
        )
        if expired_claim["job_id"] != str(expired_job_id):
            raise RuntimeError("lease-expiry fixture was not claimed before expiry injection")
        with admin.cursor() as cursor:
            cursor.execute(
                "UPDATE ops.jobs SET lease_expires_at=now()-interval '1 second' WHERE id=%s",
                (expired_job_id,),
            )
        recovered_claim = scalar(
            worker,
            """SELECT row_to_json(claimed)::jsonb
                 FROM ops.claim_job(
                     'worker', 'wp4-worker-recover', ARRAY['fetch_source'], 30
                 ) AS claimed""",
        )
        if recovered_claim["job_id"] != str(expired_job_id) or recovered_claim["attempt_no"] != 2:
            raise RuntimeError("expired lease was not recovered as the next attempt")
        if (
            scalar(worker, "SELECT recovery_reason FROM ops.jobs WHERE id=%s", expired_job_id)
            != "lease_expired"
        ):
            raise RuntimeError("lease recovery reason was not retained")
        stale_finish = rejected(
            worker,
            "SELECT ops.finish_job(%s::uuid, %s::uuid, %s::uuid, 'succeeded'::ops.attempt_outcome)",
            expired_job_id,
            expired_claim["attempt_id"],
            expired_claim["lease_token"],
        )
        if stale_finish != "40001":
            raise RuntimeError(f"stale lease completion SQLSTATE was {stale_finish}")
        scalar(
            worker,
            """SELECT ops.finish_job(
                %s::uuid, %s::uuid, %s::uuid,
                'retryable_failure'::ops.attempt_outcome,
                503::smallint, 'upstream_5xx'::text, 'probe max-attempt retry'::text
            )""",
            expired_job_id,
            recovered_claim["attempt_id"],
            recovered_claim["lease_token"],
        )
        if (
            scalar(worker, "SELECT status::text FROM ops.jobs WHERE id=%s", expired_job_id)
            != "dead"
        ):
            raise RuntimeError("maximum attempts did not create a dead job")
        if (
            scalar(worker, "SELECT count(*) FROM ops.dead_letters WHERE job_id=%s", expired_job_id)
            != 1
        ):
            raise RuntimeError("dead letter was not persisted")
        scalar(
            worker,
            "SELECT ops.requeue_dead_letter(%s::uuid, 'manual probe retry'::text)",
            expired_job_id,
        )
        if (
            scalar(worker, "SELECT status::text FROM ops.jobs WHERE id=%s", expired_job_id)
            != "queued"
        ):
            raise RuntimeError("dead letter was not requeued")

        rollback_key = f"wp4-rollback-job-{run_tag}"
        try:
            with scheduler.transaction():
                with scheduler.cursor() as cursor:
                    cursor.execute(
                        "SELECT ops.enqueue_job('fetch_source', '{}'::jsonb, '1', %s, "
                        "0::smallint, now(), 1, 30)",
                        (rollback_key,),
                    )
                    cursor.execute(
                        """SELECT ops.emit_outbox(
                            NULL, 'document', %s, 'rollback_probe', %s, '{}'::jsonb
                        )""",
                        (id_for(2), f"{rollback_key}-event"),
                    )
                    raise RuntimeError("rollback sentinel")
        except RuntimeError as error:
            if str(error) != "rollback sentinel":
                raise
        if (
            scalar(admin, "SELECT count(*) FROM ops.jobs WHERE idempotency_key=%s", rollback_key)
            != 0
        ):
            raise RuntimeError("rolled-back job remained visible")
        if (
            scalar(
                admin,
                "SELECT count(*) FROM ops.outbox_events WHERE event_key=%s",
                f"{rollback_key}-event",
            )
            != 0
        ):
            raise RuntimeError("rolled-back Outbox event remained visible")

        print(
            "WP4 runtime probe passed: idempotent enqueue, single claim, "
            "worker/publisher boundary, retry classification, lease recovery, "
            "dead-letter requeue, Outbox deduplication and publisher acknowledgement."
        )
    finally:
        admin.close()
        worker.close()
        scheduler.close()
        publisher.close()


if __name__ == "__main__":
    main()
