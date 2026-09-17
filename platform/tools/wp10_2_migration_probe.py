"""Exercise the WP10.2 empty-state and fail-closed downgrade matrix.

The probe creates disposable databases only when an administrator DSN is
provided.  It never prints a DSN or subprocess output because those values may
contain credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from pathlib import Path

import psycopg
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url

from alembic import command

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_EVIDENCE: list[dict[str, object]] = []


def libpq_url(value: str) -> str:
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def database_url(admin_url: str, database: str) -> str:
    parsed = make_url(admin_url)
    if parsed.drivername == "postgresql":
        parsed = parsed.set(drivername="postgresql+psycopg")
    return parsed.set(database=database).render_as_string(hide_password=False)


def run_alembic(database: str, *arguments: str) -> None:
    if len(arguments) != 2 or arguments[0] not in {"upgrade", "downgrade"}:
        raise ValueError("probe only supports one Alembic upgrade/downgrade target")
    config = Config(str(PLATFORM_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PLATFORM_ROOT / "alembic"))
    previous_url = os.environ.get("UAP_DATABASE_URL")
    os.environ["UAP_DATABASE_URL"] = database
    try:
        getattr(command, arguments[0])(config, arguments[1])
    except Exception as error:
        original = getattr(error, "orig", error)
        diagnostic = getattr(original, "diag", None)
        sqlstate = getattr(original, "sqlstate", None)
        primary = getattr(diagnostic, "message_primary", None)
        detail = "/".join(str(value) for value in (sqlstate, primary) if value)
        raise RuntimeError(
            f"alembic command failed: {arguments}; {detail or 'no stable error marker'}"
        ) from error
    finally:
        if previous_url is None:
            os.environ.pop("UAP_DATABASE_URL", None)
        else:
            os.environ["UAP_DATABASE_URL"] = previous_url


def create_database(admin_url: str, name: str) -> None:
    with psycopg.connect(libpq_url(admin_url), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def drop_database(admin_url: str, name: str) -> None:
    with psycopg.connect(libpq_url(admin_url), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


def scalar(connection: psycopg.Connection[object], statement: str, *params: object) -> object:
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("probe query returned no row")
    if not isinstance(row, tuple):
        raise RuntimeError("probe query returned no row")
    return row[0]


def wp10_1_outbox_state_roundtrip(database: str) -> None:
    """Prove ordinary 0020 outbox state is preserved by a 0021 downgrade."""

    prefix = f"g10-2-allowed-{uuid.uuid4()}"
    with psycopg.connect(libpq_url(database), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ops.outbox_events (
                    id, aggregate_type, aggregate_id, event_type, event_key, payload,
                    occurred_at, published_at, available_at
                ) VALUES (%s, 'legacy_jobs', %s, 'job.completed', %s, '{}'::jsonb,
                          clock_timestamp(), clock_timestamp(), clock_timestamp())
                """,
                (uuid.uuid4(), uuid.uuid4(), f"{prefix}-published"),
            )
            cursor.execute(
                """
                INSERT INTO ops.outbox_events (
                    id, aggregate_type, aggregate_id, event_type, event_key, payload,
                    occurred_at, terminal_at, terminal_error_code, available_at
                ) VALUES (%s, 'legacy_jobs', %s, 'job.failed', %s, '{}'::jsonb,
                          clock_timestamp(), clock_timestamp(), 'legacy_terminal',
                          clock_timestamp())
                """,
                (uuid.uuid4(), uuid.uuid4(), f"{prefix}-terminal"),
            )
            cursor.execute(
                """
                INSERT INTO ops.outbox_events (
                    id, aggregate_type, aggregate_id, event_type, event_key, payload,
                    occurred_at, publish_attempts, last_error_code, last_error_summary,
                    available_at
                ) VALUES (%s, 'legacy_jobs', %s, 'job.retry', %s, '{}'::jsonb,
                          clock_timestamp(), 1, 'legacy_retry', 'legacy retry',
                          clock_timestamp() + interval '60 seconds')
                """,
                (uuid.uuid4(), uuid.uuid4(), f"{prefix}-retry"),
            )
            cursor.execute(
                """
                INSERT INTO ops.outbox_events (
                    id, aggregate_type, aggregate_id, event_type, event_key, payload,
                    occurred_at, lease_owner, lease_token, lease_expires_at, available_at
                ) VALUES (%s, 'legacy_jobs', %s, 'job.leased', %s, '{}'::jsonb,
                          clock_timestamp(), 'legacy-worker', %s,
                          clock_timestamp() + interval '60 seconds', clock_timestamp())
                """,
                (uuid.uuid4(), uuid.uuid4(), f"{prefix}-lease", uuid.uuid4()),
            )
            for aggregate_type, subject_type in (
                ("document_publication_grants", "document"),
                ("entity_publication_grants", "entity"),
            ):
                cursor.execute(
                    """
                    INSERT INTO ops.outbox_events (
                        id, aggregate_type, aggregate_id, event_type, event_key, payload,
                        occurred_at, available_at
                    ) VALUES (%s, %s, %s, 'publication.granted', %s,
                              jsonb_build_object(
                                  'schema', 'publication-outbox.v2',
                                  'subject_type', %s::text
                              ), clock_timestamp(), clock_timestamp() + interval '60 seconds')
                    """,
                    (
                        uuid.uuid4(),
                        aggregate_type,
                        uuid.uuid4(),
                        f"{prefix}-{subject_type}-v2",
                        subject_type,
                    ),
                )
    run_alembic(database, "downgrade", "0020_wp10_publication_contract")
    with psycopg.connect(libpq_url(database), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT version_num FROM alembic_version")
            if cursor.fetchone() != ("0020_wp10_publication_contract",):
                raise RuntimeError("0021 downgrade changed the 0020 version unexpectedly")
            cursor.execute(
                "SELECT count(*) FROM ops.outbox_events WHERE event_key LIKE %s",
                (f"{prefix}-%",),
            )
            if cursor.fetchone() != (6,):
                raise RuntimeError("0021 downgrade did not preserve 0020 outbox state")
            cursor.execute("SELECT to_regclass('audit.publication_delivery_attempts')")
            if cursor.fetchone() != (None,):
                raise RuntimeError("0021 downgrade retained its delivery-attempt table")
    run_alembic(database, "upgrade", "0021_wp10_publisher_projection")
    MIGRATION_EVIDENCE.append(
        {
            "case": "wp10_1_outbox_state_not_blocked",
            "status": "passed",
            "outbox_rows_preserved": 6,
        }
    )


def stateful_guard(database: str) -> None:
    """Seed running/retry/terminal states and prove downgrade is rejected before DROP."""

    with psycopg.connect(libpq_url(database), autocommit=True) as connection:
        running_event_id = uuid.uuid4()
        retry_event_id = uuid.uuid4()
        terminal_event_id = uuid.uuid4()
        token = uuid.uuid4()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ops.outbox_events (
                    id, aggregate_type, aggregate_id, event_type, event_key,
                    payload, occurred_at, available_at, publish_attempts,
                    lease_owner, lease_token, lease_expires_at
                ) VALUES (
                    %s, 'document_publication_grants', %s, 'publication.granted', %s,
                    '{"schema":"publication-outbox.v2","subject_type":"document"}'::jsonb,
                    clock_timestamp(), clock_timestamp(), 1, 'g10-2-probe', %s,
                    clock_timestamp() + interval '60 seconds'
                )
                """,
                (
                    running_event_id,
                    uuid.uuid4(),
                    f"g10-2-guard-{running_event_id}",
                    token,
                ),
            )
            cursor.execute(
                """
                INSERT INTO ops.outbox_events (
                    id, aggregate_type, aggregate_id, event_type, event_key,
                    payload, occurred_at, available_at, publish_attempts,
                    last_error_code
                ) VALUES (
                    %s, 'entity_publication_grants', %s, 'publication.granted', %s,
                    '{"schema":"publication-outbox.v2","subject_type":"entity"}'::jsonb,
                    clock_timestamp(), clock_timestamp() + interval '60 seconds', 1,
                    'publication_dependency_not_ready'
                )
                """,
                (
                    retry_event_id,
                    uuid.uuid4(),
                    f"g10-2-retry-{retry_event_id}",
                ),
            )
            cursor.execute(
                """
                INSERT INTO ops.outbox_events (
                    id, aggregate_type, aggregate_id, event_type, event_key,
                    payload, occurred_at, publish_attempts, terminal_at,
                    terminal_error_code
                ) VALUES (
                    %s, 'document_publication_grants', %s, 'publication.granted', %s,
                    '{"schema":"publication-outbox.v2","subject_type":"document"}'::jsonb,
                    clock_timestamp(), 1, clock_timestamp(),
                    'publication_event_schema_unsupported'
                )
                """,
                (
                    terminal_event_id,
                    uuid.uuid4(),
                    f"g10-2-terminal-{terminal_event_id}",
                ),
            )
            cursor.execute(
                """
                INSERT INTO audit.publication_delivery_attempts (
                    event_id, attempt_no, dispatcher, lease_token_hash, outcome, started_at
                ) VALUES (
                    %s, 1, 'g10-2-probe',
                    audit._payload_sha256(jsonb_build_object('lease_token', lower(%s::text))),
                    'running'::ops.attempt_outcome, clock_timestamp()
                )
                """,
                (running_event_id, token),
            )
            cursor.execute(
                """
                INSERT INTO audit.publication_delivery_attempts (
                    event_id, attempt_no, dispatcher, lease_token_hash, outcome,
                    sanitized_error_code, sanitized_error_summary, started_at,
                    finished_at, available_at
                ) VALUES (
                    %s, 1, 'g10-2-probe', repeat('a', 64),
                    'retryable_failure'::ops.attempt_outcome,
                    'publication_dependency_not_ready',
                    'publication dependency is not ready', clock_timestamp(),
                    clock_timestamp(), clock_timestamp() + interval '60 seconds'
                )
                """,
                (retry_event_id,),
            )
            cursor.execute(
                """
                INSERT INTO audit.publication_delivery_attempts (
                    event_id, attempt_no, dispatcher, lease_token_hash, outcome,
                    sanitized_error_code, sanitized_error_summary, started_at,
                    finished_at, terminal_at
                ) VALUES (
                    %s, 1, 'g10-2-probe', repeat('b', 64),
                    'terminal_failure'::ops.attempt_outcome,
                    'publication_event_schema_unsupported',
                    'publication event schema is unsupported', clock_timestamp(),
                    clock_timestamp(), clock_timestamp()
                )
                """,
                (terminal_event_id,),
            )
    try:
        run_alembic(database, "downgrade", "0020_wp10_publication_contract")
    except RuntimeError as error:
        if "22023/publication_contract_state_blocks_downgrade" not in str(error):
            raise RuntimeError("downgrade failed without the stable guard code") from error
        with psycopg.connect(libpq_url(database), autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT version_num FROM alembic_version")
                if cursor.fetchone() != ("0021_wp10_publisher_projection",):
                    raise RuntimeError(
                        "downgrade guard did not preserve migration version"
                    ) from None
                cursor.execute("SELECT to_regclass('audit.publication_delivery_attempts')")
                if cursor.fetchone() != ("audit.publication_delivery_attempts",):
                    raise RuntimeError("G10-10 downgrade guard ran after destructive DDL") from None
                cursor.execute("SELECT count(*) FROM audit.publication_delivery_attempts")
                if cursor.fetchone() != (3,):
                    raise RuntimeError("downgrade guard did not preserve attempt history") from None
        MIGRATION_EVIDENCE.append(
            {
                "case": "stateful_downgrade_guard",
                "status": "passed",
                "sqlstate": "22023",
                "primary": "publication_contract_state_blocks_downgrade",
                "attempt_rows_preserved": 3,
            }
        )
        return
    raise RuntimeError("stateful 0021 downgrade unexpectedly succeeded")


def event_only_guard(admin_url: str, state: str) -> None:
    """Prove a terminal or retry publication event blocks downgrade without attempts."""

    if state not in {"terminal", "retry"}:
        raise ValueError("event-only guard state must be terminal or retry")
    name = f"uap_wp10_2_{state}_{uuid.uuid4().hex[:8]}"
    create_database(admin_url, name)
    isolated = database_url(admin_url, name)
    event_id = uuid.uuid4()
    try:
        run_alembic(isolated, "upgrade", "0021_wp10_publisher_projection")
        with psycopg.connect(libpq_url(isolated), autocommit=True) as connection:
            with connection.cursor() as cursor:
                if state == "terminal":
                    cursor.execute(
                        """
                        INSERT INTO ops.outbox_events (
                            id, aggregate_type, aggregate_id, event_type, event_key,
                            payload, occurred_at, available_at, terminal_at,
                            terminal_error_code
                        ) VALUES (
                            %s, 'document_publication_grants', %s,
                            'publication.granted', %s,
                            '{"schema":"publication-outbox.v2",'
                            '"subject_type":"document"}'::jsonb,
                            clock_timestamp(), clock_timestamp(), clock_timestamp(),
                            'publication_event_schema_unsupported'
                        )
                        """,
                        (event_id, uuid.uuid4(), f"g10-2-terminal-only-{event_id}"),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO ops.outbox_events (
                            id, aggregate_type, aggregate_id, event_type, event_key,
                            payload, occurred_at, available_at, publish_attempts,
                            last_error_code, last_error_summary
                        ) VALUES (
                            %s, 'entity_publication_grants', %s,
                            'publication.granted', %s,
                            '{"schema":"publication-outbox.v2",'
                            '"subject_type":"entity"}'::jsonb,
                            clock_timestamp(), clock_timestamp() + interval '60 seconds', 1,
                            'publication_dependency_not_ready',
                            'publication dependency is not ready'
                        )
                        """,
                        (event_id, uuid.uuid4(), f"g10-2-retry-only-{event_id}"),
                    )
                cursor.execute(
                    """
                    SELECT jsonb_build_object(
                        'id', id, 'publish_attempts', publish_attempts,
                        'published_at', published_at, 'available_at', available_at,
                        'terminal_at', terminal_at,
                        'terminal_error_code', terminal_error_code,
                        'last_error_code', last_error_code,
                        'last_error_summary', last_error_summary,
                        'lease_owner', lease_owner, 'lease_token', lease_token,
                        'lease_expires_at', lease_expires_at
                    )
                    FROM ops.outbox_events WHERE id = %s
                    """,
                    (event_id,),
                )
                before = cursor.fetchone()
                if before is None:
                    raise RuntimeError(f"{state}-only event fixture was not created")
                attempt_count = scalar(
                    connection,
                    "SELECT count(*) FROM audit.publication_delivery_attempts",
                )
                if attempt_count != 0:
                    raise RuntimeError(f"{state}-only fixture unexpectedly created an attempt")
        try:
            run_alembic(isolated, "downgrade", "0020_wp10_publication_contract")
        except RuntimeError as error:
            if "22023/publication_contract_state_blocks_downgrade" not in str(error):
                raise RuntimeError(
                    f"{state}-only downgrade failed without the stable guard code"
                ) from error
        else:
            raise RuntimeError(f"{state}-only 0021 downgrade unexpectedly succeeded")
        with psycopg.connect(libpq_url(isolated), autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT version_num FROM alembic_version")
                if cursor.fetchone() != ("0021_wp10_publisher_projection",):
                    raise RuntimeError(f"{state}-only guard changed migration version")
                cursor.execute(
                    """
                    SELECT jsonb_build_object(
                        'id', id, 'publish_attempts', publish_attempts,
                        'published_at', published_at, 'available_at', available_at,
                        'terminal_at', terminal_at,
                        'terminal_error_code', terminal_error_code,
                        'last_error_code', last_error_code,
                        'last_error_summary', last_error_summary,
                        'lease_owner', lease_owner, 'lease_token', lease_token,
                        'lease_expires_at', lease_expires_at
                    )
                    FROM ops.outbox_events WHERE id = %s
                    """,
                    (event_id,),
                )
                after = cursor.fetchone()
                if after != before:
                    raise RuntimeError(f"{state}-only guard changed event state")
                attempt_count = scalar(
                    connection,
                    "SELECT count(*) FROM audit.publication_delivery_attempts",
                )
                if attempt_count != 0:
                    raise RuntimeError(f"{state}-only guard changed attempt state")
        MIGRATION_EVIDENCE.append(
            {
                "case": f"{state}_event_without_attempt_guard",
                "status": "passed",
                "sqlstate": "22023",
                "primary": "publication_contract_state_blocks_downgrade",
                "delivery_attempt_rows_before": 0,
                "delivery_attempt_rows_after": 0,
                "event_state_unchanged": True,
                "migration_version_preserved": "0021_wp10_publisher_projection",
            }
        )
    finally:
        drop_database(admin_url, name)


def run(admin_url: str) -> None:
    name = f"uap_wp10_2_{uuid.uuid4().hex[:12]}"
    create_database(admin_url, name)
    isolated = database_url(admin_url, name)
    try:
        run_alembic(isolated, "upgrade", "0020_wp10_publication_contract")
        run_alembic(isolated, "upgrade", "0021_wp10_publisher_projection")
        wp10_1_outbox_state_roundtrip(isolated)
        run_alembic(isolated, "downgrade", "0020_wp10_publication_contract")
        run_alembic(isolated, "upgrade", "0021_wp10_publisher_projection")
        MIGRATION_EVIDENCE.append({"case": "empty_state_roundtrip", "status": "passed"})
        stateful_guard(isolated)
        with psycopg.connect(libpq_url(isolated), autocommit=True) as connection:
            if scalar(connection, "SELECT count(*) FROM audit.publication_delivery_attempts") != 3:
                raise RuntimeError("G10-10 delivery-attempt guard fixture was not retained")
        event_only_guard(admin_url, "terminal")
        event_only_guard(admin_url, "retry")
        print(json.dumps(MIGRATION_EVIDENCE, ensure_ascii=False, indent=2))
    finally:
        drop_database(admin_url, name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--admin-url", required=True)
    parser.add_argument("--evidence-out", type=Path)
    args = parser.parse_args()
    try:
        run(args.admin_url)
    except Exception:
        if args.evidence_out:
            args.evidence_out.parent.mkdir(parents=True, exist_ok=True)
            args.evidence_out.write_text(
                json.dumps(
                    {
                        "schema": "wp10.2-migration-evidence.v1",
                        "status": "failed",
                        "checks": MIGRATION_EVIDENCE,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        raise
    if args.evidence_out:
        args.evidence_out.parent.mkdir(parents=True, exist_ok=True)
        args.evidence_out.write_text(
            json.dumps(
                {
                    "schema": "wp10.2-migration-evidence.v1",
                    "status": "passed",
                    "checks": MIGRATION_EVIDENCE,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
