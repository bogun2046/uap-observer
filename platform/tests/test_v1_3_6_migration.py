"""Static and disposable-PostgreSQL contracts for migration 0036."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

PLATFORM = Path(__file__).resolve().parents[1]
MIGRATION = PLATFORM / "alembic/versions/0036_v133_full_rebuild_publication_evidence_guard.py"
FROZEN_0030 = PLATFORM / "alembic/versions/0030_v131_publication_editorial_revision_binding.py"
DB_TEST_CONFIGURED = all(
    os.environ.get(name)
    for name in (
        "UAP_V133_0036_TEST_DATABASE_URL",
        "UAP_V133_0036_TEST_DATABASE_NAME",
        "UAP_V133_0036_TEST_DB_IS_DISPOSABLE",
        "UAP_V133_0036_PUBLISHED_GRANT_ID",
        "UAP_V133_0036_WITHDRAWN_GRANT_ID",
    )
)


def migration_text() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_0036_is_linear_and_migration_only() -> None:
    source = migration_text()
    assert 'revision = "0036_v133_full_rebuild_publication_evidence_guard"' in source
    assert 'down_revision = "0035_v133_rebuild_identifier_fix"' in source
    for prohibited in (
        "CREATE TABLE",
        "ALTER TABLE",
        "INSERT INTO audit.",
        "INSERT INTO public.",
        "UPDATE audit.",
        "UPDATE public.",
        "DELETE FROM audit.",
        "DELETE FROM public.",
    ):
        assert prohibited not in source
    assert "CREATE FUNCTION ops._v133_full_rebuild_grant_eligible" in source
    assert "'ops.rebuild_public_projection(uuid)'::regprocedure" in source
    assert "ops.rebuild_public_projection(uuid, uuid)" not in source


def test_0036_eligibility_binds_all_grant_types_to_valid_manifest_and_applied_event() -> None:
    source = migration_text()
    for grant_table in (
        "document_publication_grants",
        "claim_publication_grants",
        "entity_publication_grants",
    ):
        assert source.count(f"'{grant_table}'") >= 3
    for required in (
        "grant_status = 'active'::audit.grant_status",
        "manifest.review_case_id = grant_row.review_case_id",
        "manifest.decision_id = grant_row.decision_id",
        "publication_payload_sha256",
        "audit._publication_manifest_sha(",
        "_wp10_3_grant_has_unresolved_quarantine",
        "event.event_type = 'publication.granted'",
        "event.published_at IS NOT NULL",
        "event.terminal_at IS NULL",
        "event.payload ->> 'grant_id'",
        "event.payload ->> 'payload_sha256'",
    ):
        assert required in source
    assert "editorial_revision_id" in source
    assert "editorial_revision_no" in source


def test_0036_patches_digest_and_projection_filters_exactly_twice_per_grant_type() -> None:
    source = migration_text()
    assert "v133_full_rebuild_evidence_inventory_mismatch" in source
    assert "v133_full_rebuild_evidence_patch_mismatch" in source
    assert "regexp_replace(" in source
    assert "<> 2 THEN" in source
    assert "v_pattern := '(AND NOT ops\\._wp10_3_grant_has_unresolved_quarantine\\('" in source
    assert "FROM audit.publication_rebuild_runs" not in source


def test_0036_preserves_operator_acl_and_scoped_rebuild_contract() -> None:
    source = migration_text()
    assert "ALTER FUNCTION ops.rebuild_public_projection(uuid) OWNER TO uap_owner" in source
    assert "REVOKE ALL ON FUNCTION ops.rebuild_public_projection(uuid)" in source
    assert "GRANT EXECUTE ON FUNCTION ops.rebuild_public_projection(uuid) TO uap_migrator" in source
    assert "uap_api, uap_worker, uap_scheduler, uap_publisher" in source
    assert "ops.rebuild_public_projection(uuid, uuid)" not in source
    assert "DROP FUNCTION ops._v133_full_rebuild_grant_eligible(text, uuid)" in source
    assert "does not\nreplace or alter the two-argument scoped rebuild contract" in source


def test_0030_frozen_migration_digest_is_unchanged() -> None:
    assert hashlib.sha256(FROZEN_0030.read_bytes()).hexdigest() == (
        "6229aad96909fbb69528eacfac0ce002a327610e4692d4216d9600723ea81e8e"
    )


def test_0036_frozen_functions_and_runtime_acls() -> None:
    source = migration_text()
    assert "ops.rebuild_public_projection(uuid, uuid)" not in source
    assert "ops.rebuild_public_projection(uuid)" in source
    assert "uap_api, uap_worker, uap_scheduler, uap_publisher" in source


@pytest.fixture
def disposable_publication_db() -> Iterator[tuple[psycopg.Connection, str, str]]:
    if not DB_TEST_CONFIGURED:
        pytest.skip("disposable 0036 PostgreSQL contract database is not configured")
    if os.environ["UAP_V133_0036_TEST_DB_IS_DISPOSABLE"] != "YES":
        pytest.fail("database integration tests require explicit disposable-database opt-in")

    connection = psycopg.connect(os.environ["UAP_V133_0036_TEST_DATABASE_URL"])
    connection.autocommit = False
    published_grant_id = os.environ["UAP_V133_0036_PUBLISHED_GRANT_ID"]
    withdrawn_grant_id = os.environ["UAP_V133_0036_WITHDRAWN_GRANT_ID"]
    try:
        identity = connection.execute(
            """
            SELECT current_database(), role.rolsuper,
                   (SELECT version_num FROM public.alembic_version)
              FROM pg_roles AS role
             WHERE role.rolname = session_user
            """
        ).fetchone()
        assert identity is not None
        current_db, is_superuser, alembic_head = identity
        assert current_db == os.environ["UAP_V133_0036_TEST_DATABASE_NAME"]
        assert is_superuser is True
        assert alembic_head == "0036_v133_full_rebuild_publication_evidence_guard"
        published_state = connection.execute(
            """
            SELECT grant_row.grant_status::text,
                   btrim(grant_row.publication_payload_sha256::text)
                       = btrim(manifest.manifest_sha256::text)
                   AND btrim(manifest.manifest_sha256::text)
                       = btrim(audit._publication_manifest_sha(
                           ops._wp10_3_document_manifest_payload(grant_row.id)
                       )),
                   event.published_at IS NOT NULL,
                   event.terminal_at IS NULL
              FROM audit.document_publication_grants AS grant_row
              JOIN audit.document_publication_manifests AS manifest
                ON manifest.grant_id = grant_row.id
              JOIN ops.outbox_events AS event
                ON event.aggregate_type = 'document_publication_grants'
               AND event.aggregate_id = grant_row.id
               AND event.event_type = 'publication.granted'
             WHERE grant_row.id = %s::uuid
            """,
            (published_grant_id,),
        ).fetchone()
        withdrawn_state = connection.execute(
            """
            SELECT grant_row.grant_status::text,
                   event.published_at IS NOT NULL,
                   event.terminal_at IS NULL,
                   NOT EXISTS (
                       SELECT 1 FROM public.documents AS document
                        WHERE document.document_grant_id = grant_row.id
                   )
              FROM audit.document_publication_grants AS grant_row
              JOIN ops.outbox_events AS event
                ON event.aggregate_type = 'document_publication_grants'
               AND event.aggregate_id = grant_row.id
               AND event.event_type = 'publication.granted'
             WHERE grant_row.id = %s::uuid
            """,
            (withdrawn_grant_id,),
        ).fetchone()
        assert published_state == ("active", True, True, True)
        assert withdrawn_state == ("withdrawn", True, True, True)
        yield connection, published_grant_id, withdrawn_grant_id
    finally:
        connection.rollback()
        connection.close()


def _grant_document_id(connection: psycopg.Connection, grant_id: str) -> str:
    row = connection.execute(
        """
        SELECT manifest.document_id::text
          FROM audit.document_publication_manifests AS manifest
         WHERE manifest.grant_id = %s::uuid
        """,
        (grant_id,),
    ).fetchone()
    assert row is not None
    assert isinstance(row[0], str)
    return row[0]


def _call_full_rebuild(connection: psycopg.Connection, rebuild_id: str) -> tuple[object, ...]:
    connection.execute("SET SESSION AUTHORIZATION uap_migrator")
    result = connection.execute(
        """
        SELECT rebuild_id, input_digest, result_digest, counts, status, replayed
          FROM ops.rebuild_public_projection(%s::uuid)
        """,
        (rebuild_id,),
    ).fetchone()
    connection.execute("RESET SESSION AUTHORIZATION")
    assert result is not None
    return result


def _public_count(connection: psycopg.Connection, grant_id: str) -> int:
    row = connection.execute(
        "SELECT count(*) FROM public.documents WHERE document_grant_id = %s::uuid",
        (grant_id,),
    ).fetchone()
    assert row is not None
    assert isinstance(row[0], int)
    return row[0]


def _public_search_count(connection: psycopg.Connection, grant_id: str) -> int:
    row = connection.execute(
        """
        SELECT count(*)
          FROM public.search_documents AS search_row
          JOIN public.documents AS document ON document.id = search_row.document_id
         WHERE document.document_grant_id = %s::uuid
        """,
        (grant_id,),
    ).fetchone()
    assert row is not None
    assert isinstance(row[0], int)
    return row[0]


def _delete_document_public_projection(connection: psycopg.Connection, grant_id: str) -> None:
    connection.execute(
        "DELETE FROM public.documents WHERE document_grant_id = %s::uuid",
        (grant_id,),
    )


def test_db_full_rebuild_predicate_and_scoped_function_are_separate(
    disposable_publication_db: tuple[psycopg.Connection, str, str],
) -> None:
    connection, _, _ = disposable_publication_db
    full_definition, scoped_definition, acl = connection.execute(
        """
        SELECT pg_get_functiondef('ops.rebuild_public_projection(uuid)'::regprocedure),
               pg_get_functiondef('ops.rebuild_public_projection(uuid, uuid)'::regprocedure),
               (SELECT jsonb_build_object(
                    'owner', pg_get_userbyid(function_row.proowner),
                    'migrator', has_function_privilege('uap_migrator', function_row.oid, 'EXECUTE'),
                    'api', has_function_privilege('uap_api', function_row.oid, 'EXECUTE'),
                    'publisher', has_function_privilege(
                        'uap_publisher', function_row.oid, 'EXECUTE'
                    ),
                    'public_execute', EXISTS (
                        SELECT 1 FROM aclexplode(coalesce(
                            function_row.proacl,
                            acldefault('f', function_row.proowner)
                        )) AS acl
                         WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'
                    )
                )
                  FROM pg_proc AS function_row
                 WHERE function_row.oid =
                       'ops.rebuild_public_projection(uuid)'::regprocedure)
        """
    ).fetchone() or (None, None, None)
    assert isinstance(full_definition, str)
    assert isinstance(scoped_definition, str)
    assert isinstance(acl, dict)
    for grant_table in (
        "document_publication_grants",
        "claim_publication_grants",
        "entity_publication_grants",
    ):
        needle = f"ops._v133_full_rebuild_grant_eligible('{grant_table}', grant_row.id)"
        assert full_definition.count(needle) == 2
    assert "_v133_full_rebuild_grant_eligible" not in scoped_definition
    assert "_v133_document_grant_was_published" in scoped_definition
    assert acl == {
        "owner": "uap_owner",
        "migrator": True,
        "api": False,
        "publisher": False,
        "public_execute": False,
    }


def _create_active_never_published_clone(
    connection: psycopg.Connection,
    withdrawn_grant_id: str,
) -> str:
    new_grant_id = str(uuid4())
    new_decision_id = str(uuid4())
    new_event_id = str(uuid4())
    connection.execute("SET LOCAL session_replication_role = replica")
    connection.execute(
        """
        INSERT INTO audit.review_decisions (
            id, review_case_id, sequence_no, decision, reason, structured_changes,
            decided_by, supersedes_decision_id, decided_at
        )
        SELECT %s::uuid, grant_row.review_case_id,
               coalesce((SELECT max(decision.sequence_no) + 1
                           FROM audit.review_decisions AS decision
                          WHERE decision.review_case_id = grant_row.review_case_id), 1),
               'approve'::audit.review_decision,
               'Temporary isolated 0036 contract fixture', '{}'::jsonb,
               (SELECT decision.decided_by
                  FROM audit.review_decisions AS decision
                 WHERE decision.review_case_id = grant_row.review_case_id
                 ORDER BY decision.sequence_no DESC
                 LIMIT 1),
               NULL, clock_timestamp()
          FROM audit.document_publication_grants AS grant_row
         WHERE grant_row.id = %s::uuid
        """,
        (new_decision_id, withdrawn_grant_id),
    )
    connection.execute(
        """
        INSERT INTO audit.document_publication_grants (
            id, review_case_id, document_version_id, decision_id, revision_no,
            grant_status, granted_at, publication_payload_sha256,
            editorial_revision_id, editorial_revision_no
        )
        SELECT %s::uuid, grant_row.review_case_id, grant_row.document_version_id,
               %s::uuid, grant_row.revision_no + 1, 'active'::audit.grant_status,
               clock_timestamp(), repeat('0', 64), grant_row.editorial_revision_id,
               grant_row.editorial_revision_no
          FROM audit.document_publication_grants AS grant_row
         WHERE grant_row.id = %s::uuid
        """,
        (new_grant_id, new_decision_id, withdrawn_grant_id),
    )
    connection.execute(
        """
        INSERT INTO audit.document_publication_manifests (
            grant_id, review_case_id, decision_id, document_id, document_version_id,
            title, summary, category, fact_status, source_name, canonical_source_url,
            source_published_at, summary_analysis_result_id, manifest_sha256,
            created_at, editorial_revision_id, editorial_revision_no
        )
        SELECT %s::uuid, manifest.review_case_id, %s::uuid,
               manifest.document_id, manifest.document_version_id, manifest.title,
               manifest.summary, manifest.category, manifest.fact_status,
               manifest.source_name, manifest.canonical_source_url,
               manifest.source_published_at, manifest.summary_analysis_result_id,
               repeat('0', 64), clock_timestamp(), manifest.editorial_revision_id,
               manifest.editorial_revision_no
          FROM audit.document_publication_manifests AS manifest
         WHERE manifest.grant_id = %s::uuid
        """,
        (new_grant_id, new_decision_id, withdrawn_grant_id),
    )
    connection.execute(
        """
        UPDATE audit.document_publication_manifests AS manifest
           SET manifest_sha256 = audit._publication_manifest_sha(
               ops._wp10_3_document_manifest_payload(manifest.grant_id)
           )
         WHERE manifest.grant_id = %s::uuid
        """,
        (new_grant_id,),
    )
    connection.execute(
        """
        UPDATE audit.document_publication_grants AS grant_row
           SET publication_payload_sha256 = manifest.manifest_sha256
          FROM audit.document_publication_manifests AS manifest
         WHERE grant_row.id = %s::uuid
           AND manifest.grant_id = grant_row.id
        """,
        (new_grant_id,),
    )
    connection.execute(
        """
        INSERT INTO ops.outbox_events (
            id, aggregate_type, aggregate_id, event_type, event_key, payload,
            occurred_at, published_at, publish_attempts, terminal_at,
            terminal_error_code
        )
        SELECT %s::uuid, 'document_publication_grants', grant_row.id,
               'publication.granted', '0036-contract-grant:' || grant_row.id::text,
               jsonb_build_object(
                   'schema', 'publication-outbox.v2',
                   'grant_id', lower(grant_row.id::text),
                   'decision_id', lower(grant_row.decision_id::text),
                   'subject_type', 'document',
                   'subject_id', lower(grant_row.document_version_id::text),
                   'revision_no', grant_row.revision_no,
                   'editorial_revision_id', lower(grant_row.editorial_revision_id::text),
                   'editorial_revision_no', grant_row.editorial_revision_no,
                   'payload_sha256', btrim(grant_row.publication_payload_sha256::text)
               ),
               clock_timestamp(), NULL, 0, NULL, NULL
          FROM audit.document_publication_grants AS grant_row
         WHERE grant_row.id = %s::uuid
        """,
        (new_event_id, new_grant_id),
    )
    connection.execute("SET LOCAL session_replication_role = origin")
    return new_grant_id


def _set_event_state(
    connection: psycopg.Connection,
    grant_id: str,
    state: str,
) -> None:
    connection.execute("SET LOCAL session_replication_role = replica")
    if state == "no_granted_event":
        connection.execute(
            """
            UPDATE ops.outbox_events
               SET event_type = 'publication.contract_test_non_grant'
             WHERE aggregate_type = 'document_publication_grants'
               AND aggregate_id = %s::uuid
               AND event_type = 'publication.granted'
            """,
            (grant_id,),
        )
    elif state == "mismatched_payload":
        connection.execute(
            """
            UPDATE ops.outbox_events
               SET payload = jsonb_set(
                   payload, '{decision_id}', to_jsonb(lower(gen_random_uuid()::text)), true
               )
             WHERE aggregate_type = 'document_publication_grants'
               AND aggregate_id = %s::uuid
               AND event_type = 'publication.granted'
            """,
            (grant_id,),
        )
    elif state == "terminal":
        connection.execute(
            """
            UPDATE ops.outbox_events
               SET published_at = NULL,
                   terminal_at = clock_timestamp(),
                   terminal_error_code = 'contract_test_terminal',
                   publish_attempts = 1,
                   last_error_code = 'contract_test_terminal'
             WHERE aggregate_type = 'document_publication_grants'
               AND aggregate_id = %s::uuid
               AND event_type = 'publication.granted'
            """,
            (grant_id,),
        )
    elif state == "retryable":
        connection.execute(
            """
            UPDATE ops.outbox_events
               SET published_at = NULL, terminal_at = NULL,
                   terminal_error_code = NULL, publish_attempts = 1,
                   last_error_code = 'contract_test_retryable'
             WHERE aggregate_type = 'document_publication_grants'
               AND aggregate_id = %s::uuid
               AND event_type = 'publication.granted'
            """,
            (grant_id,),
        )
    else:
        connection.execute(
            """
            UPDATE ops.outbox_events
               SET published_at = NULL, terminal_at = NULL,
                   terminal_error_code = NULL, publish_attempts = 0,
                   last_error_code = NULL
             WHERE aggregate_type = 'document_publication_grants'
               AND aggregate_id = %s::uuid
               AND event_type = 'publication.granted'
            """,
            (grant_id,),
        )
    connection.execute("SET LOCAL session_replication_role = origin")


def test_db_full_rebuild_recovers_missing_projection_and_replays_idempotently(
    disposable_publication_db: tuple[psycopg.Connection, str, str],
) -> None:
    connection, published_grant_id, withdrawn_grant_id = disposable_publication_db
    _delete_document_public_projection(connection, published_grant_id)
    assert _public_count(connection, published_grant_id) == 0
    rebuild_id = str(uuid4())
    first = _call_full_rebuild(connection, rebuild_id)
    assert first[4] == "succeeded"
    assert first[5] is False
    assert _public_count(connection, published_grant_id) == 1
    assert _public_count(connection, withdrawn_grant_id) == 0
    assert _public_search_count(connection, published_grant_id) == 1
    assert _public_search_count(connection, withdrawn_grant_id) == 0
    replay = _call_full_rebuild(connection, rebuild_id)
    assert replay[4] == "succeeded"
    assert replay[5] is True
    assert replay[1] == first[1]


def test_db_rebuild_id_conflicts_when_published_eligibility_changes(
    disposable_publication_db: tuple[psycopg.Connection, str, str],
) -> None:
    connection, published_grant_id, _ = disposable_publication_db
    rebuild_id = str(uuid4())
    first = _call_full_rebuild(connection, rebuild_id)
    assert first[4] == "succeeded"
    _set_event_state(connection, published_grant_id, "queued")
    with pytest.raises(psycopg.Error) as error:
        with connection.transaction():
            _call_full_rebuild(connection, rebuild_id)
    assert error.value.sqlstate == "40001"
    assert "publication_rebuild_id_conflict" in str(error.value)
    assert _public_count(connection, published_grant_id) == 1


def test_db_mixed_population_projects_only_currently_authorized_published_grants(
    disposable_publication_db: tuple[psycopg.Connection, str, str],
) -> None:
    connection, published_grant_id, withdrawn_grant_id = disposable_publication_db
    never_published_grant_id = _create_active_never_published_clone(
        connection, withdrawn_grant_id
    )
    result = _call_full_rebuild(connection, str(uuid4()))
    assert result[4] == "succeeded"
    assert _public_count(connection, published_grant_id) == 1
    assert _public_count(connection, withdrawn_grant_id) == 0
    assert _public_count(connection, never_published_grant_id) == 0
    assert _public_search_count(connection, published_grant_id) == 1
    assert _public_search_count(connection, withdrawn_grant_id) == 0
    assert _public_search_count(connection, never_published_grant_id) == 0

    connection.execute("SET LOCAL session_replication_role = replica")
    connection.execute(
        "DELETE FROM ops.outbox_events WHERE aggregate_id = %s::uuid",
        (never_published_grant_id,),
    )
    connection.execute(
        "DELETE FROM audit.document_publication_manifests WHERE grant_id = %s::uuid",
        (never_published_grant_id,),
    )
    connection.execute(
        "DELETE FROM audit.document_publication_grants WHERE id = %s::uuid",
        (never_published_grant_id,),
    )
    connection.execute(
        "DELETE FROM audit.review_decisions WHERE reason = %s",
        ("Temporary isolated 0036 contract fixture",),
    )
    connection.execute("SET LOCAL session_replication_role = origin")
    without_never_published = _call_full_rebuild(connection, str(uuid4()))
    assert without_never_published[4] == "succeeded"
    assert without_never_published[1] == result[1]


@pytest.mark.parametrize(
    "event_state",
    ["queued", "retryable", "terminal", "no_granted_event", "mismatched_payload"],
)
def test_db_full_rebuild_never_publishes_without_successful_apply(
    disposable_publication_db: tuple[psycopg.Connection, str, str],
    event_state: str,
) -> None:
    connection, published_grant_id, _ = disposable_publication_db
    _set_event_state(connection, published_grant_id, event_state)
    result = _call_full_rebuild(connection, str(uuid4()))
    assert result[4] == "succeeded"
    assert _public_count(connection, published_grant_id) == 0
    assert _public_search_count(connection, published_grant_id) == 0
    counts = result[3]
    assert isinstance(counts, dict)
    assert counts["documents"] == 0


def test_db_full_rebuild_excludes_superseded_grant_with_historical_success(
    disposable_publication_db: tuple[psycopg.Connection, str, str],
) -> None:
    connection, published_grant_id, _ = disposable_publication_db
    connection.execute("SET LOCAL session_replication_role = replica")
    connection.execute(
        """
        UPDATE audit.document_publication_grants
           SET grant_status = 'superseded'::audit.grant_status
         WHERE id = %s::uuid
        """,
        (published_grant_id,),
    )
    connection.execute("SET LOCAL session_replication_role = origin")
    result = _call_full_rebuild(connection, str(uuid4()))
    assert result[4] == "succeeded"
    assert _public_count(connection, published_grant_id) == 0
    assert _public_search_count(connection, published_grant_id) == 0


def test_db_full_rebuild_fails_closed_on_invalid_manifest_hash(
    disposable_publication_db: tuple[psycopg.Connection, str, str],
) -> None:
    connection, published_grant_id, _ = disposable_publication_db
    _delete_document_public_projection(connection, published_grant_id)
    connection.execute("SET LOCAL session_replication_role = replica")
    connection.execute(
        """
        UPDATE audit.document_publication_manifests
           SET manifest_sha256 = repeat('0', 64)
         WHERE grant_id = %s::uuid
        """,
        (published_grant_id,),
    )
    connection.execute("SET LOCAL session_replication_role = origin")
    result = _call_full_rebuild(connection, str(uuid4()))
    assert result[4] == "failed"
    counts = result[3]
    assert isinstance(counts, dict)
    assert counts["blocked"] is True
    assert _public_count(connection, published_grant_id) == 0
    assert _public_search_count(connection, published_grant_id) == 0


def test_db_unresolved_quarantine_excludes_a_published_candidate(
    disposable_publication_db: tuple[psycopg.Connection, str, str],
) -> None:
    connection, published_grant_id, _ = disposable_publication_db
    connection.execute("SET LOCAL session_replication_role = replica")
    connection.execute(
        """
        INSERT INTO audit.publication_quarantine (
            id, grant_table, grant_id, reason_code
        ) VALUES (%s::uuid, 'document_publication_grants', %s::uuid,
                  'publication_manifest_required')
        """,
        (str(uuid4()), published_grant_id),
    )
    connection.execute("SET LOCAL session_replication_role = origin")
    result = _call_full_rebuild(connection, str(uuid4()))
    assert result[4] == "succeeded"
    assert _public_count(connection, published_grant_id) == 0
    assert _public_search_count(connection, published_grant_id) == 0


def test_db_scoped_rebuild_contract_remains_successful_for_published_document(
    disposable_publication_db: tuple[psycopg.Connection, str, str],
) -> None:
    connection, published_grant_id, _ = disposable_publication_db
    document_id = _grant_document_id(connection, published_grant_id)
    _delete_document_public_projection(connection, published_grant_id)
    connection.execute("SET SESSION AUTHORIZATION uap_migrator")
    try:
        result = connection.execute(
            """
            SELECT status, replayed
              FROM ops.rebuild_public_projection(%s::uuid, %s::uuid)
            """,
            (str(uuid4()), document_id),
        ).fetchone()
    finally:
        connection.execute("RESET SESSION AUTHORIZATION")
    assert result == ("succeeded", False)
    assert _public_count(connection, published_grant_id) == 1


def test_db_scoped_rebuild_still_blocks_never_published_document(
    disposable_publication_db: tuple[psycopg.Connection, str, str],
) -> None:
    connection, published_grant_id, _ = disposable_publication_db
    document_id = _grant_document_id(connection, published_grant_id)
    _delete_document_public_projection(connection, published_grant_id)
    _set_event_state(connection, published_grant_id, "queued")
    connection.execute("SET SESSION AUTHORIZATION uap_migrator")
    try:
        with pytest.raises(psycopg.Error) as error:
            with connection.transaction():
                connection.execute(
                    "SELECT * FROM ops.rebuild_public_projection(%s::uuid, %s::uuid)",
                    (str(uuid4()), document_id),
                )
        assert error.value.sqlstate == "22023"
        assert "publication_rebuild_mismatch" in str(error.value)
    finally:
        connection.execute("RESET SESSION AUTHORIZATION")
