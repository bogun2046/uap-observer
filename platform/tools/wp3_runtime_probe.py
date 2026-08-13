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
    "uap_migrator": "UAP_MIGRATOR_PASSWORD",
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
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    except psycopg.Error as error:
        return str(error.sqlstate)
    raise RuntimeError(f"database accepted forbidden statement: {statement.split()[0]}")


def require_sqlstate(name: str, actual: str, expected: str) -> None:
    if actual != expected:
        raise RuntimeError(f"{name} returned SQLSTATE {actual}; expected {expected}")


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


def prepare_semantic_fixture(connection: psycopg.Connection[Any]) -> None:
    """Create valid internal review data and public projection prerequisites."""

    principal = identifier(1)
    document = identifier(21)
    document_version = identifier(22)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO core.documents
                (id, source_id, source_item_key, document_kind, first_seen_at, last_seen_at)
            VALUES (%s, %s, 'wp3-semantic-fixture', 'article', now(), now())
            ON CONFLICT (id) DO NOTHING
            """,
            (document, identifier(3)),
        )
        cursor.execute(
            """
            INSERT INTO core.document_versions
                (id, document_id, artifact_version_id, version_no,
                 normalized_content_sha256)
            VALUES (%s, %s, %s, 1, repeat('0', 64))
            ON CONFLICT (id) DO NOTHING
            """,
            (document_version, document, identifier(7)),
        )
        for evidence_id, locator_type, locator_hash, positions in (
            (identifier(23), "text", "1", (0, 10, None, None, None, None)),
            (identifier(64), "pdf", "2", (None, None, 1, 1, None, None)),
            (identifier(65), "video", "3", (None, None, None, None, 0, 1000)),
        ):
            cursor.execute(
                """
                INSERT INTO core.evidence_spans
                    (id, document_version_id, evidence_text, locator_type,
                     char_start, char_end, page_start, page_end,
                     time_start_ms, time_end_ms, locator, locator_sha256)
                VALUES (%s, %s, 'valid semantic evidence', %s,
                        %s, %s, %s, %s, %s, %s, '{}'::jsonb, repeat(%s, 64))
                ON CONFLICT (id) DO NOTHING
                """,
                (evidence_id, document_version, locator_type, *positions, locator_hash),
            )
        for entity_id, name in (
            (identifier(25), "WP3 Entity One"),
            (identifier(26), "WP3 Entity Two"),
        ):
            cursor.execute(
                """
                INSERT INTO core.entities (id, entity_type, canonical_name)
                VALUES (%s, 'organization', %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (entity_id, name),
            )
        for claim_id, claim_text, fingerprint in (
            (identifier(24), "WP3 valid public claim", "5"),
            (identifier(58), "WP3 orphan-claim guard probe", "6"),
        ):
            cursor.execute(
                """
                INSERT INTO core.claims
                    (id, claim_text, claim_fingerprint, claim_type,
                     assertion_status, created_by)
                VALUES (%s, %s, repeat(%s, 64), 'observation', 'reported', %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (claim_id, claim_text, fingerprint, principal),
            )
        for relation_id, predicate in (
            (identifier(28), "supports"),
            (identifier(52), "references"),
        ):
            cursor.execute(
                """
                INSERT INTO core.relations
                    (id, subject_entity_id, object_entity_id, predicate,
                     relation_status, created_by)
                VALUES (%s, %s, %s, %s, 'reported', %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (relation_id, identifier(25), identifier(26), predicate, principal),
            )

        cases = (
            (identifier(29), "document_version_id", document_version, "document"),
            (identifier(30), "claim_id", identifier(24), "claim"),
            (identifier(31), "entity_id", identifier(25), "entity"),
            (identifier(32), "entity_id", identifier(26), "entity"),
            (identifier(33), "relation_id", identifier(28), "relation"),
            (identifier(53), "relation_id", identifier(52), "relation"),
            (identifier(59), "claim_id", identifier(58), "claim"),
        )
        for case_id, subject_column, subject_id, case_type in cases:
            cursor.execute(
                sql.SQL(
                    """
                    INSERT INTO audit.review_cases
                        (id, {}, case_type, status, opened_by, opened_at, closed_at)
                    VALUES (%s, %s, %s, 'approved', %s, now(), now())
                    ON CONFLICT (id) DO NOTHING
                    """
                ).format(sql.Identifier(subject_column)),
                (case_id, subject_id, case_type, principal),
            )

        decisions = (
            (identifier(34), identifier(29)),
            (identifier(35), identifier(30)),
            (identifier(36), identifier(31)),
            (identifier(37), identifier(32)),
            (identifier(38), identifier(33)),
            (identifier(54), identifier(53)),
            (identifier(60), identifier(59)),
        )
        for decision_id, case_id in decisions:
            cursor.execute(
                """
                INSERT INTO audit.review_decisions
                    (id, review_case_id, sequence_no, decision, reason,
                     decided_by, decided_at)
                VALUES (%s, %s, 1, 'approve', 'WP3 semantic fixture', %s, now())
                ON CONFLICT (id) DO NOTHING
                """,
                (decision_id, case_id, principal),
            )

        grants = (
            (
                "document_publication_grants",
                "document_version_id",
                identifier(39),
                identifier(29),
                document_version,
                identifier(34),
                1,
            ),
            (
                "claim_publication_grants",
                "claim_id",
                identifier(40),
                identifier(30),
                identifier(24),
                identifier(35),
                1,
            ),
            (
                "entity_publication_grants",
                "entity_id",
                identifier(41),
                identifier(31),
                identifier(25),
                identifier(36),
                1,
            ),
            (
                "entity_publication_grants",
                "entity_id",
                identifier(42),
                identifier(32),
                identifier(26),
                identifier(37),
                1,
            ),
            (
                "relation_publication_grants",
                "relation_id",
                identifier(43),
                identifier(33),
                identifier(28),
                identifier(38),
                1,
            ),
            (
                "relation_publication_grants",
                "relation_id",
                identifier(55),
                identifier(53),
                identifier(52),
                identifier(54),
                2,
            ),
            (
                "claim_publication_grants",
                "claim_id",
                identifier(61),
                identifier(59),
                identifier(58),
                identifier(60),
                1,
            ),
        )
        for table, subject_column, grant_id, case_id, subject_id, decision_id, revision in grants:
            cursor.execute(
                sql.SQL(
                    """
                    INSERT INTO audit.{}
                        (id, review_case_id, {}, decision_id, revision_no,
                         grant_status, granted_at, publication_payload_sha256)
                    VALUES (%s, %s, %s, %s, %s, 'active', now(), repeat('a', 64))
                    ON CONFLICT (id) DO NOTHING
                    """
                ).format(sql.Identifier(table), sql.Identifier(subject_column)),
                (grant_id, case_id, subject_id, decision_id, revision),
            )

        cursor.execute(
            """
            INSERT INTO public.documents
                (id, document_grant_id, slug, title, category, fact_status,
                 source_name, canonical_source_url, published_at, revision_no)
            VALUES (%s, %s, 'wp3-semantic-document', 'WP3 semantic document',
                    'test', 'reported', 'WP3 Probe',
                    'https://example.invalid/wp3-semantic-document', now(), 1)
            ON CONFLICT (id) DO NOTHING
            """,
            (identifier(44), identifier(39)),
        )
        cursor.execute(
            """
            INSERT INTO public.evidence
                (id, document_id, excerpt, locator_type, public_locator,
                 locator_sha256, source_url)
            VALUES (%s, %s, 'reviewed evidence', 'text', '{}'::jsonb,
                    repeat('7', 64), 'https://example.invalid/wp3-evidence')
            ON CONFLICT (id) DO NOTHING
            """,
            (identifier(45), identifier(44)),
        )
        for entity_id, grant_id, slug, name in (
            (identifier(48), identifier(41), "wp3-entity-one", "WP3 Entity One"),
            (identifier(49), identifier(42), "wp3-entity-two", "WP3 Entity Two"),
        ):
            cursor.execute(
                """
                INSERT INTO public.entities
                    (id, entity_grant_id, slug, entity_type, name, published_at, revision_no)
                VALUES (%s, %s, %s, 'organization', %s, now(), 1)
                ON CONFLICT (id) DO NOTHING
                """,
                (entity_id, grant_id, slug, name),
            )
        for relation_id, grant_id, predicate, revision in (
            (identifier(50), identifier(43), "supports", 1),
            (identifier(56), identifier(55), "references", 2),
        ):
            cursor.execute(
                """
                INSERT INTO public.relations
                    (id, subject_entity_id, object_entity_id, relation_grant_id,
                     predicate, relation_status, published_at, revision_no)
                VALUES (%s, %s, %s, %s, %s, 'reported', now(), %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (relation_id, identifier(48), identifier(49), grant_id, predicate, revision),
            )
    connection.commit()


def probe_repaired_semantics(connection: psycopg.Connection[Any]) -> dict[str, object]:
    """Prove each G3-R2 semantic repair with positive and negative transactions."""

    mixed_locator_state = expect_rejected(
        connection,
        """
        INSERT INTO core.evidence_spans
            (id, document_version_id, evidence_text, locator_type,
             char_start, char_end, page_start, page_end, locator, locator_sha256)
        VALUES ('00000000-0000-7000-8000-000000000063',
                '00000000-0000-7000-8000-000000000022', 'invalid mixed PDF',
                'pdf', 0, 1, 1, 1, '{}'::jsonb, repeat('4', 64))
        """,
    )
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.claims
                    (id, document_id, claim_grant_id, ordinal, claim_text,
                     claim_type, assertion_status, revision_no)
                VALUES (%s, %s, %s, 0, 'WP3 valid public claim',
                        'observation', 'reported', 1)
                ON CONFLICT (id) DO NOTHING
                """,
                (identifier(46), identifier(44), identifier(40)),
            )
            cursor.execute(
                """
                INSERT INTO public.claim_evidence (id, claim_id, evidence_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (identifier(47), identifier(46), identifier(45)),
            )
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.document_entities
                    (id, document_id, entity_id, basis_evidence_id,
                     basis_claim_id, basis_relation_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    identifier(51),
                    identifier(44),
                    identifier(48),
                    identifier(45),
                    identifier(46),
                    identifier(50),
                ),
            )
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    orphan_claim_state = expect_rejected(
        connection,
        """
        INSERT INTO public.claims
            (id, document_id, claim_grant_id, ordinal, claim_text,
             claim_type, assertion_status, revision_no)
        VALUES ('00000000-0000-7000-8000-000000000062',
                '00000000-0000-7000-8000-000000000044',
                '00000000-0000-7000-8000-000000000061', 1,
                'claim without evidence', 'observation', 'reported', 1)
        """,
    )
    last_evidence_state = expect_rejected(
        connection,
        """
        DELETE FROM public.claim_evidence
         WHERE id='00000000-0000-7000-8000-000000000047'
        """,
    )
    cross_revision_state = expect_rejected(
        connection,
        """
        INSERT INTO public.document_entities
            (id, document_id, entity_id, basis_evidence_id,
             basis_claim_id, basis_relation_id)
        VALUES ('00000000-0000-7000-8000-000000000057',
                '00000000-0000-7000-8000-000000000044',
                '00000000-0000-7000-8000-000000000049',
                '00000000-0000-7000-8000-000000000045',
                '00000000-0000-7000-8000-000000000046',
                '00000000-0000-7000-8000-000000000056')
        """,
    )
    linked_revision_update_state = expect_rejected(
        connection,
        """
        UPDATE public.relations SET revision_no=2
         WHERE id='00000000-0000-7000-8000-000000000050'
        """,
    )
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                (SELECT count(*) FROM public.claims WHERE id=%s),
                (SELECT count(*) FROM public.claim_evidence WHERE id=%s),
                (SELECT count(*) FROM public.document_entities WHERE id=%s),
                (SELECT count(*) FROM core.evidence_spans
                  WHERE id IN (%s, %s, %s))
            """,
            (
                identifier(46),
                identifier(47),
                identifier(51),
                identifier(23),
                identifier(64),
                identifier(65),
            ),
        )
        row = cursor.fetchone()
    if row != (1, 1, 1, 3):
        raise RuntimeError(f"legal G3 semantic fixture did not commit: {row}")
    for name, state in (
        ("mixed locator", mixed_locator_state),
        ("orphan public claim", orphan_claim_state),
        ("last evidence removal", last_evidence_state),
        ("cross-revision document entity", cross_revision_state),
        ("linked revision update", linked_revision_update_state),
    ):
        require_sqlstate(name, state, "23514")
    return {
        "legal_claim_evidence_committed": True,
        "legal_document_entity_committed": True,
        "valid_locator_variants": row[3],
        "mixed_locator_sqlstate": mixed_locator_state,
        "orphan_public_claim_sqlstate": orphan_claim_state,
        "last_evidence_removal_sqlstate": last_evidence_state,
        "cross_revision_sqlstate": cross_revision_state,
        "linked_revision_update_sqlstate": linked_revision_update_state,
    }


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
        if head != "0004_g3_semantic_repairs" or table_count != EXPECTED_TABLE_COUNT:
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

        prepare_semantic_fixture(connection)
        repaired_semantics = probe_repaired_semantics(connection)

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
        if foreign_keys != 108:
            raise RuntimeError(f"foreign key count changed: {foreign_keys}; expected 108")

        for name, state, expected in (
            ("wrong storage domain", wrong_domain_state, "23514"),
            ("append-only artifact", append_only_state, "55000"),
            ("invalid analysis selection", bad_selection_state, "23503"),
            ("invalid publication grant", bad_grant_state, "23503"),
            ("orphan relation", orphan_relation_state, "23503"),
        ):
            require_sqlstate(name, state, expected)

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
    for role, expected in (
        ("uap_public_reader", "42501"),
        ("uap_worker", "42501"),
        ("uap_backup", "25006"),
    ):
        require_sqlstate(f"{role} denied operation", permission_results[role], expected)

    migrator_login_rejected = False
    try:
        with role_connection(settings.psycopg_database_url, "uap_migrator"):
            pass
    except psycopg.OperationalError:
        migrator_login_rejected = True
    if not migrator_login_rejected:
        raise RuntimeError("uap_migrator can still log in after the deployment window")
    with psycopg.connect(settings.psycopg_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT rolcanlogin, rolinherit FROM pg_roles WHERE rolname='uap_migrator'"
            )
            migrator_state = cursor.fetchone()
    if migrator_state != (False, False):
        raise RuntimeError(f"unexpected migrator role state: {migrator_state}")

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
        "repaired_semantics": repaired_semantics,
        "migrator_login_rejected": migrator_login_rejected,
        "migrator_role_state": {"can_login": migrator_state[0], "inherits": migrator_state[1]},
        "foreign_keys_checked": foreign_keys,
        "foreign_key_orphans": orphan_rows,
        "denied_role_sqlstates": permission_results,
    }


def main() -> None:
    print(json.dumps(probe(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
