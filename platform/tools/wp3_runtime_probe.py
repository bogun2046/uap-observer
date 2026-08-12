"""Exercise the frozen G3 database, object, integrity, and role boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from typing import Any, cast

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from uap_platform.config import load_settings
from uap_platform.object_registry import ObjectClient, StorageDomain, store_and_register
from uap_platform.object_store_init import build_client

EXPECTED_TABLE_COUNT = 49
ROLE_PASSWORDS = {
    "uap_public_reader": "UAP_PUBLIC_READER_PASSWORD",
    "uap_worker": "UAP_WORKER_PASSWORD",
    "uap_backup": "UAP_BACKUP_PASSWORD",
}


def identifier(number: int) -> uuid.UUID:
    return uuid.UUID(f"00000000-0000-7000-8000-{number:012d}")


def expect_rejected(connection: psycopg.Connection[Any], statement: str) -> str:
    try:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute(statement)
    except psycopg.Error as error:
        return str(error.sqlstate)
    raise RuntimeError(f"database accepted forbidden statement: {statement.split()[0]}")


def role_connection(admin_url: str, role: str) -> psycopg.Connection[Any]:
    variable = ROLE_PASSWORDS[role]
    password = os.environ.get(variable)
    if not password:
        raise RuntimeError(f"missing {variable}")
    params = conninfo_to_dict(admin_url)
    params.pop("user", None)
    params.pop("password", None)
    base = psycopg.conninfo.make_conninfo(**params)  # type: ignore[arg-type]
    connection_info = psycopg.conninfo.make_conninfo(base, user=role, password=password)
    return psycopg.connect(connection_info)


def probe() -> dict[str, object]:
    settings = load_settings()
    client = cast(ObjectClient, build_client(settings))
    payload = b"UAP G3 fixed immutable object\x00\x01"
    digest = hashlib.sha256(payload).hexdigest()

    with psycopg.connect(settings.psycopg_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT version_num FROM public.alembic_version")
            head_row = cursor.fetchone()
            if head_row is None:
                raise RuntimeError("missing Alembic revision")
            head = head_row[0]
            cursor.execute(
                """
                SELECT count(*) FROM pg_tables
                 WHERE schemaname IN ('ingest','core','ops','audit','public')
                   AND tablename <> 'alembic_version'
                """
            )
            table_row = cursor.fetchone()
            if table_row is None:
                raise RuntimeError("missing table count")
            table_count = table_row[0]
        if head != "0003_permissions_and_guards" or table_count != EXPECTED_TABLE_COUNT:
            raise RuntimeError("runtime schema does not match the frozen WP3 head")

        raw_first = store_and_register(
            client,
            connection,
            StorageDomain.RAW,
            payload,
            "application/octet-stream",
            expected_sha256=digest,
        )
        raw_second = store_and_register(
            client,
            connection,
            StorageDomain.RAW,
            payload,
            "application/octet-stream",
            expected_sha256=digest,
        )
        derived = store_and_register(
            client,
            connection,
            StorageDomain.DERIVED,
            payload,
            "application/octet-stream",
        )
        if raw_first.id != raw_second.id or not raw_second.reused:
            raise RuntimeError("same-domain object registration was not reused")
        if raw_first.id == derived.id:
            raise RuntimeError("cross-domain object registration was incorrectly reused")

        principal = identifier(1)
        job = identifier(2)
        source = identifier(3)
        run = identifier(4)
        artifact_one = identifier(5)
        artifact_two = identifier(6)
        version_one = identifier(7)
        version_two = identifier(8)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO audit.principals
                    (id, principal_type, service_name, display_name)
                VALUES (%s, 'service', 'wp3-runtime-probe', 'WP3 runtime probe')
                ON CONFLICT (id) DO NOTHING
                """,
                (principal,),
            )
            cursor.execute(
                """
                INSERT INTO ops.jobs
                    (id, job_type, payload_schema_version, idempotency_key,
                     max_attempts, timeout_seconds)
                VALUES (%s, 'probe', '1', 'wp3-runtime-probe', 1, 30)
                ON CONFLICT (id) DO NOTHING
                """,
                (job,),
            )
            cursor.execute(
                """
                INSERT INTO ingest.sources
                    (id, slug, name, source_type, homepage_url)
                VALUES (%s, 'wp3-probe', 'WP3 Probe', 'api', 'https://example.invalid/')
                ON CONFLICT (id) DO NOTHING
                """,
                (source,),
            )
            cursor.execute(
                """
                INSERT INTO ingest.source_runs
                    (id, source_id, job_id, run_key, outcome, started_at, finished_at)
                VALUES (%s, %s, %s, 'wp3-runtime-probe', 'succeeded', now(), now())
                ON CONFLICT (id) DO NOTHING
                """,
                (run, source, job),
            )
            for artifact, locator in (
                (artifact_one, "urn:uap:wp3:artifact:1"),
                (artifact_two, "urn:uap:wp3:artifact:2"),
            ):
                cursor.execute(
                    """
                    INSERT INTO ingest.artifacts
                        (id, source_id, canonical_locator, artifact_kind,
                         first_seen_at, last_seen_at)
                    VALUES (%s, %s, %s, 'binary', now(), now())
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (artifact, source, locator),
                )
            for version, artifact in (
                (version_one, artifact_one),
                (version_two, artifact_two),
            ):
                cursor.execute(
                    """
                    INSERT INTO ingest.artifact_versions
                        (id, artifact_id, source_run_id, stored_object_id, retrieved_at)
                    VALUES (%s, %s, %s, %s, now())
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (version, artifact, run, raw_first.id),
                )
            cursor.execute(
                """
                SELECT count(DISTINCT stored_object_id), count(*)
                  FROM ingest.artifact_versions
                 WHERE id IN (%s, %s)
                """,
                (version_one, version_two),
            )
            reuse_row = cursor.fetchone()
            if reuse_row is None:
                raise RuntimeError("missing artifact reuse result")
            distinct_objects, artifact_versions = reuse_row
        if (distinct_objects, artifact_versions) != (1, 2):
            raise RuntimeError("artifact versions did not reuse one registered object")

        wrong_domain_state = expect_rejected(
            connection,
            """
            INSERT INTO ingest.artifact_versions
                (id, artifact_id, source_run_id, stored_object_id, storage_domain, retrieved_at)
            VALUES ('00000000-0000-7000-8000-000000000009',
                    '00000000-0000-7000-8000-000000000005',
                    '00000000-0000-7000-8000-000000000004',
                    '00000000-0000-7000-8000-000000000000', 'derived', now())
            """.replace("00000000-0000-7000-8000-000000000000", str(raw_first.id)),
        )
        append_only_state = expect_rejected(
            connection,
            """
            UPDATE ingest.artifact_versions SET retrieved_at=now()
             WHERE id='00000000-0000-7000-8000-000000000007'
            """,
        )
        bad_selection_state = expect_rejected(
            connection,
            """
            INSERT INTO core.analysis_selections
                (id, document_version_id, analysis_result_id, result_type,
                 selected_by, selection_reason)
            VALUES ('00000000-0000-7000-8000-000000000010',
                    '00000000-0000-7000-8000-000000000011',
                    '00000000-0000-7000-8000-000000000012',
                    'summary',
                    '00000000-0000-7000-8000-000000000001', 'invalid cross-document probe')
            """,
        )
        bad_grant_state = expect_rejected(
            connection,
            """
            INSERT INTO audit.document_publication_grants
                (id, review_case_id, document_version_id, decision_id,
                 revision_no, grant_status, granted_at, publication_payload_sha256)
            VALUES ('00000000-0000-7000-8000-000000000013',
                    '00000000-0000-7000-8000-000000000014',
                    '00000000-0000-7000-8000-000000000015',
                    '00000000-0000-7000-8000-000000000016',
                    1, 'active', now(), repeat('0', 64))
            """,
        )
        orphan_relation_state = expect_rejected(
            connection,
            """
            INSERT INTO core.relations
                (id, subject_entity_id, object_entity_id, predicate, relation_status)
            VALUES ('00000000-0000-7000-8000-000000000017',
                    '00000000-0000-7000-8000-000000000018',
                    '00000000-0000-7000-8000-000000000019',
                    'invalid', 'reported')
            """,
        )
        bad_evidence_state = expect_rejected(
            connection,
            """
            INSERT INTO core.evidence_spans
                (id, document_version_id, extraction_id, evidence_text,
                 locator_type, char_start, char_end, locator, locator_sha256)
            VALUES ('00000000-0000-7000-8000-000000000020',
                    '00000000-0000-7000-8000-000000000021',
                    '00000000-0000-7000-8000-000000000022',
                    'invalid', 'text', 0, 1, '{}', repeat('0', 64))
            """,
        )
        foreign_keys = 0
        orphan_rows = 0
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT n.nspname, c.relname, con.conname,
                       pn.nspname, pc.relname,
                       ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(attnum, ord)
                             JOIN pg_attribute a ON a.attrelid=con.conrelid AND a.attnum=k.attnum
                            ORDER BY k.ord),
                       ARRAY(SELECT a.attname
                               FROM unnest(con.confkey) WITH ORDINALITY k(attnum, ord)
                             JOIN pg_attribute a ON a.attrelid=con.confrelid AND a.attnum=k.attnum
                            ORDER BY k.ord)
                  FROM pg_constraint con
                  JOIN pg_class c ON c.oid=con.conrelid
                  JOIN pg_namespace n ON n.oid=c.relnamespace
                  JOIN pg_class pc ON pc.oid=con.confrelid
                  JOIN pg_namespace pn ON pn.oid=pc.relnamespace
                 WHERE con.contype='f'
                   AND n.nspname IN ('ingest','core','ops','audit','public')
                """
            )
            constraints = cursor.fetchall()
        foreign_keys = len(constraints)
        for schema, table, _constraint, parent_schema, parent_table, child, parent in constraints:
            not_null = sql.SQL(" AND ").join(
                sql.SQL("child.{} IS NOT NULL").format(sql.Identifier(column))
                for column in child
            )
            join = sql.SQL(" AND ").join(
                sql.SQL("child.{} = parent.{}").format(
                    sql.Identifier(left), sql.Identifier(right)
                )
                for left, right in zip(child, parent, strict=True)
            )
            query = sql.SQL(
                "SELECT count(*) FROM {}.{} child WHERE {} AND NOT EXISTS "
                "(SELECT 1 FROM {}.{} parent WHERE {})"
            ).format(
                sql.Identifier(schema),
                sql.Identifier(table),
                not_null,
                sql.Identifier(parent_schema),
                sql.Identifier(parent_table),
                join,
            )
            with connection.cursor() as cursor:
                cursor.execute(query)
                orphan_row = cursor.fetchone()
            if orphan_row is None:
                raise RuntimeError("missing orphan count")
            orphan_rows += int(orphan_row[0])
        if orphan_rows:
            raise RuntimeError(f"foreign key orphan rows found: {orphan_rows}")

    permission_results: dict[str, str] = {}
    role_statements = {
        "uap_public_reader": (
            "SELECT count(*) FROM public.documents",
            "SELECT count(*) FROM core.stored_objects",
        ),
        "uap_worker": (
            "SELECT count(*) FROM core.stored_objects",
            "INSERT INTO public.search_documents "
            "(document_id, search_vector, display_text, indexed_at) "
            "VALUES ('00000000-0000-7000-8000-000000000099', ''::tsvector, '', now())",
        ),
        "uap_backup": (
            "SELECT count(*) FROM core.stored_objects",
            "DELETE FROM core.stored_objects WHERE false",
        ),
    }
    for role, (allowed, denied) in role_statements.items():
        with role_connection(settings.psycopg_database_url, role) as role_db:
            with role_db.cursor() as cursor:
                cursor.execute(allowed)
                cursor.fetchone()
            permission_results[role] = expect_rejected(role_db, denied)

    return {
        "head": head,
        "business_tables": table_count,
        "object_sha256": digest,
        "same_domain_registration_reused": raw_first.id == raw_second.id,
        "different_domain_registration_separated": raw_first.id != derived.id,
        "artifact_versions": artifact_versions,
        "artifact_distinct_objects": distinct_objects,
        "wrong_domain_sqlstate": wrong_domain_state,
        "append_only_sqlstate": append_only_state,
        "invalid_selection_sqlstate": bad_selection_state,
        "invalid_grant_sqlstate": bad_grant_state,
        "orphan_relation_sqlstate": orphan_relation_state,
        "mismatched_evidence_sqlstate": bad_evidence_state,
        "foreign_keys_checked": foreign_keys,
        "foreign_key_orphans": orphan_rows,
        "denied_role_sqlstates": permission_results,
    }


def main() -> None:
    print(json.dumps(probe(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
