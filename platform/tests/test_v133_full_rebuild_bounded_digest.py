"""Static contracts and opt-in disposable PostgreSQL tests for digest v2."""

from __future__ import annotations

import hashlib
import importlib.util
import os
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest

from tools import hardened_head_runtime_probe, validate_wp10
from tools.wp10_runtime_probe import FROZEN_STEPS
from tools.wp10_stage_revisions import MAIN_DB_ADVANCE_BEFORE, STEP_REQUIRED_REVISION

PLATFORM = Path(__file__).resolve().parents[1]
MIGRATION = PLATFORM / "alembic/versions/0039_v133_full_rebuild_bounded_digest.py"


def module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bounded_digest_contract", MIGRATION)
    assert spec is not None and spec.loader is not None
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_revision_and_current_head() -> None:
    migration = module()
    assert migration.revision == "0039_v133_full_rebuild_bounded_digest"
    assert migration.down_revision == "0038_v133_full_rebuild_compaction"
    assert hardened_head_runtime_probe.REQUIRED_HEAD == migration.revision


def test_four_baseline_definitions_and_metadata_are_fail_closed() -> None:
    migration = module()
    assert len(migration.SIGNATURES) == len(migration.BASELINE_SHA256) == 4
    assert len(migration.V2_SHA256) == 4
    for value in (*migration.BASELINE_SHA256, *migration.V2_SHA256):
        assert len(value) == 64
        assert set(value) <= set("0123456789abcdef")
    source = migration._sql(upgrade=True)
    assert "v133_bounded_digest_definition_mismatch" in source
    assert "v133_bounded_digest_postcondition_mismatch" in source
    assert "original.proowner <> 'uap_owner'::regrole" in source
    assert "to_jsonb(changed)-'prosrc'" in source
    assert "original_comments" in source


@pytest.mark.parametrize("index", [2, 4])
@pytest.mark.parametrize("forbidden", ["jsonb_agg(", "json_agg(", "array_agg(", "string_agg("])
def test_v2_inputs_have_no_dataset_aggregate(index: int, forbidden: str) -> None:
    assert forbidden not in module().HELPER_DDL[index]


@pytest.mark.parametrize("forbidden", ["jsonb_agg(", "json_agg(", "array_agg(", "string_agg("])
def test_v2_projection_has_no_dataset_aggregate(forbidden: str) -> None:
    assert forbidden not in module().REPLACEMENTS[2][0][1]


def test_transition_is_fixed_hash_domain_separated_and_ordered() -> None:
    migration = module()
    fold, aggregate = migration.HELPER_DDL[:2]
    assert "publication-rebuild-digest-fold.v2" in fold
    for marker in ("'previous',state", "'domain',domain", "'item_sha256',item_sha256"):
        assert marker in fold
    assert "^[0-9a-f]{64}$" in fold
    assert "STYPE=text" in aggregate
    assert "INITCOND='" + "0" * 64 + "'" in aggregate
    for query in (
        migration.HELPER_DDL[2],
        migration.HELPER_DDL[4],
        migration.REPLACEMENTS[2][0][1],
    ):
        assert "ORDER BY section,kind,stable_key" in query
        assert '"marker":"begin"' in query
        assert '"marker":"end"' in query
        assert '"sections":' in query
        assert "AS item_sha256" in query


def test_projection_fields_are_exactly_the_legacy_surface() -> None:
    migration = module()
    legacy, v2 = migration.REPLACEMENTS[2][0]
    for fields in (
        "SELECT id, document_grant_id, slug, title, summary,",
        "canonical_source_url, source_published_at, revision_no",
        "SELECT id, entity_grant_id, slug, entity_type, name,",
        "SELECT id, document_id, claim_grant_id, ordinal, claim_text,",
        "SELECT id, document_id, excerpt, locator_type, page_start,",
        "SELECT id, claim_id, evidence_id FROM public.claim_evidence",
        "basis_claim_id FROM public.document_entities",
        "SELECT document_id, search_vector::text, display_text, facets",
    ):
        assert fields in legacy and fields in v2
    for uncovered in ("published_at, revised_at", "basis_relation_id"):
        assert uncovered not in v2


def test_input_eligibility_and_row_payloads_are_preserved() -> None:
    implementation = module().HELPER_DDL[2]
    for table in (
        "document_publication_grants",
        "entity_publication_grants",
        "claim_publication_grants",
    ):
        assert f"ops._v133_full_rebuild_grant_eligible('{table}', grant_row.id)" in implementation
        assert f"'{table}', grant_row.id" in implementation
    assert implementation.count("grant_row.grant_status = 'active'::audit.grant_status") == 3
    assert implementation.count("AND manifest.grant_id IS NOT NULL") == 3
    assert implementation.count("_wp10_3_grant_has_unresolved_quarantine") == 3
    assert implementation.count("manifest.grant_id IS NOT NULL,") == 3
    assert implementation.count("btrim(grant_row.publication_payload_sha256::text)") == 3


def test_legacy_is_only_selected_for_existing_version_one() -> None:
    migration = module()
    for index in (0, 1):
        _, selector = migration.REPLACEMENTS[index][0]
        assert selector.index("FOR UPDATE;") < selector.index("digest_v2")
        new_branch, remaining = selector.split("ELSIF run_row.digest_version = 1 THEN", 1)
        legacy_branch, v2_branch = remaining.split("ELSIF run_row.digest_version = 2 THEN", 1)
        assert "legacy_v1" not in new_branch
        assert "legacy_v1" in legacy_branch
        assert "legacy_v1" not in v2_branch
        assert "publication_rebuild_unknown_digest_version" in v2_branch
        assert "running', v_now, 2)" in migration.REPLACEMENTS[index][2][1]


def test_version_is_immutable_and_downgrade_is_history_guarded() -> None:
    migration = module()
    upgrade = migration._sql(upgrade=True)
    downgrade = migration._sql(upgrade=False)
    assert "digest_version smallint NOT NULL DEFAULT 1" in upgrade
    assert "CHECK (digest_version IN (1,2))" in upgrade
    assert "NEW.digest_version IS DISTINCT FROM OLD.digest_version" in upgrade
    assert "WHERE digest_version=2" in downgrade
    assert "publication_rebuild_v2_state_blocks_downgrade" in downgrade
    assert "DELETE FROM audit.publication_rebuild_runs" not in downgrade
    assert "DROP COLUMN digest_version" in downgrade


def test_transformations_do_not_touch_writes_locks_reset_or_returned_history() -> None:
    for pairs in module().REPLACEMENTS[:2]:
        for old, new in pairs:
            assert "TRUNCATE TABLE" not in old + new
            assert "LOCK TABLE" not in old + new
            assert "pg_advisory_xact_lock" not in old + new
            assert "result_digest := run_row.result_digest" not in old + new
            assert "INSERT INTO public." not in old + new


def test_internal_helpers_deny_public_and_runtime_roles() -> None:
    migration = module()
    sql = migration._sql(upgrade=True)
    for signature in migration.HELPER_SIGNATURES:
        assert f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC" in sql
    for role in ("uap_migrator", "uap_publisher", "uap_api", "uap_public_reader"):
        assert role in migration.DENIED_ROLES
    assert "GRANT EXECUTE" not in sql


def test_frozen_historical_topology_is_unchanged() -> None:
    assert len(FROZEN_STEPS) == 23
    assert all("0039" not in value for value in STEP_REQUIRED_REVISION.values())
    assert all("0039" not in value for value in MAIN_DB_ADVANCE_BEFORE.values())


@pytest.mark.parametrize("path", sorted(validate_wp10.ALLOWED_V133_BOUNDED_DIGEST_PATHS))
def test_only_exact_paths_are_authorized(path: str) -> None:
    assert validate_wp10.classify_git_paths([path]) == ("allowed", [])
    assert validate_wp10.classify_git_paths([path + ".extra"]) == ("forbidden", [path + ".extra"])


@pytest.fixture
def db() -> Iterator[psycopg.Connection[Any]]:
    url = os.environ.get("UAP_V133_0039_TEST_DATABASE_URL")
    if not url:
        pytest.skip("disposable 0038 fixture not configured")
    if os.environ.get("UAP_V133_0039_TEST_DB_IS_DISPOSABLE") != "YES":
        pytest.fail("0039 database tests require disposable opt-in")
    connection = psycopg.connect(url)
    try:
        identity = connection.execute(
            "SELECT current_database(),rolsuper,(SELECT version_num FROM public.alembic_version) "
            "FROM pg_roles WHERE rolname=session_user"
        ).fetchone()
        assert identity is not None
        assert identity[0].startswith("bounded_digest_0038_")
        assert identity[1:] == (True, "0038_v133_full_rebuild_compaction")
        yield connection
    finally:
        connection.rollback()
        connection.close()


def state(connection: psycopg.Connection[Any]) -> tuple[Any, ...]:
    row = connection.execute(
        "SELECT jsonb_build_object('definition',pg_get_functiondef(p.oid),"
        "'metadata',to_jsonb(p)-'prosrc','comment',obj_description(p.oid,'pg_proc')) "
        "FROM pg_proc p WHERE p.oid=ANY(SELECT to_regprocedure(s) "
        "FROM unnest(%s::text[]) s) ORDER BY p.oid",
        (list(module().SIGNATURES),),
    ).fetchall()
    return tuple(row)


def call(
    connection: psycopg.Connection[Any], identifier: UUID, document: UUID | None = None
) -> tuple[Any, ...]:
    try:
        with connection.transaction():
            connection.execute("SET SESSION AUTHORIZATION uap_migrator")
            if document is None:
                row = connection.execute(
                    "SELECT * FROM ops.rebuild_public_projection(%s)", (identifier,)
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM ops.rebuild_public_projection(%s,%s)",
                    (identifier, document),
                ).fetchone()
    finally:
        connection.execute("RESET SESSION AUTHORIZATION")
    assert row is not None
    return cast(tuple[Any, ...], row)


def version(connection: psycopg.Connection[Any], identifier: UUID) -> int:
    row = connection.execute(
        "SELECT digest_version FROM audit.publication_rebuild_runs WHERE rebuild_id=%s",
        (identifier,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def document_id(connection: psycopg.Connection[Any]) -> UUID:
    row = connection.execute(
        "SELECT document_id FROM audit.document_publication_manifests ORDER BY document_id LIMIT 1"
    ).fetchone()
    assert row is not None
    return cast(UUID, row[0])


def event_state_module() -> ModuleType:
    path = PLATFORM / "tests/test_v1_3_6_migration.py"
    spec = importlib.util.spec_from_file_location("publication_evidence_fixture", path)
    assert spec is not None and spec.loader is not None
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_db_upgrade_downgrade_roundtrip_and_v1_backfill(db: psycopg.Connection[Any]) -> None:
    identifier = uuid4()
    legacy = call(db, identifier)
    before = state(db)
    migration = module()
    db.execute(migration._sql(upgrade=True))
    assert version(db, identifier) == 1
    assert call(db, identifier)[1:5] == legacy[1:5]
    assert call(db, identifier)[5] is True
    db.execute(migration._sql(upgrade=False))
    assert state(db) == before
    db.execute(migration._sql(upgrade=True))
    assert version(db, identifier) == 1


@pytest.mark.parametrize("scoped", [False, True])
def test_db_new_v2_replay_different_id_and_downgrade_block(
    db: psycopg.Connection[Any],
    scoped: bool,
) -> None:
    migration = module()
    db.execute(migration._sql(upgrade=True))
    document = document_id(db) if scoped else None
    identifier = uuid4()
    first = call(db, identifier, document)
    assert first[4:] == ("succeeded", False)
    assert version(db, identifier) == 2
    assert call(db, identifier, document)[1:5] == first[1:5]
    assert call(db, identifier, document)[5] is True
    db.execute("SET CONSTRAINTS ALL IMMEDIATE")
    db.execute("SET CONSTRAINTS ALL DEFERRED")
    distinct = call(db, uuid4(), document)
    assert distinct[4:] == ("succeeded", False)
    assert distinct[1:4] == first[1:4]
    db.execute("SET CONSTRAINTS ALL IMMEDIATE")
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="v2_state_blocks"):
        with db.transaction():
            db.execute(migration._sql(upgrade=False))
    assert version(db, identifier) == 2


@pytest.mark.parametrize("scoped", [False, True])
def test_db_legacy_full_and_scoped_replay_keeps_stored_v1_result(
    db: psycopg.Connection[Any],
    scoped: bool,
) -> None:
    document = document_id(db) if scoped else None
    identifier = uuid4()
    legacy = call(db, identifier, document)
    db.execute(module()._sql(upgrade=True))
    assert version(db, identifier) == 1
    replay = call(db, identifier, document)
    assert replay[1:5] == legacy[1:5]
    assert replay[5] is True
    stored = db.execute(
        "SELECT result_digest FROM audit.publication_rebuild_runs WHERE rebuild_id=%s",
        (identifier,),
    ).fetchone()
    assert stored is not None and stored[0] == legacy[2]


@pytest.mark.parametrize("legacy", [False, True])
def test_db_full_changed_input_conflicts_for_both_versions(
    db: psycopg.Connection[Any],
    legacy: bool,
) -> None:
    identifier = uuid4()
    if not legacy:
        db.execute(module()._sql(upgrade=True))
    first = call(db, identifier)
    if legacy:
        db.execute(module()._sql(upgrade=True))
    row = db.execute(
        "SELECT document_grant_id FROM public.documents ORDER BY id LIMIT 1"
    ).fetchone()
    assert row is not None
    # Existing isolated fixture helper changes only event publication state;
    # it restores origin mode immediately. No migration/function is patched.
    event_state_module()._set_event_state(db, str(row[0]), "queued")
    with pytest.raises(psycopg.errors.SerializationFailure, match="rebuild_id_conflict"):
        call(db, identifier)
    assert (
        db.execute(
            "SELECT input_digest,result_digest FROM audit.publication_rebuild_runs "
            "WHERE rebuild_id=%s",
            (identifier,),
        ).fetchone()
        == first[1:3]
    )


def test_db_version_immutable_and_invalid_version_rejected(db: psycopg.Connection[Any]) -> None:
    db.execute(module()._sql(upgrade=True))
    identifier = uuid4()
    db.execute(
        "INSERT INTO audit.publication_rebuild_runs "
        "(rebuild_id,input_digest,status,started_at,digest_version) "
        "VALUES (%s,repeat('0',64),'running',clock_timestamp(),2)",
        (identifier,),
    )
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="immutable"):
        with db.transaction():
            db.execute(
                "UPDATE audit.publication_rebuild_runs SET digest_version=1 WHERE rebuild_id=%s",
                (identifier,),
            )
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction():
            db.execute(
                "INSERT INTO audit.publication_rebuild_runs "
                "(rebuild_id,input_digest,status,started_at,digest_version) "
                "VALUES (%s,repeat('0',64),'running',clock_timestamp(),3)",
                (uuid4(),),
            )


def test_db_v2_projection_fields_determinism_and_private_acl(db: psycopg.Connection[Any]) -> None:
    before_privileges = hardened_head_runtime_probe.privilege_snapshot(db)
    before_state = state(db)
    db.execute(module()._sql(upgrade=True))
    after_state = state(db)
    assert [row[0]["metadata"] for row in before_state] == [
        row[0]["metadata"] for row in after_state
    ]
    assert [row[0]["comment"] for row in before_state] == [row[0]["comment"] for row in after_state]
    assert hardened_head_runtime_probe.privilege_snapshot(db) == before_privileges
    assert not any(before_privileges.values())
    first = db.execute("SELECT ops._wp10_3_projection_digest()").fetchone()
    assert first is not None
    assert first == db.execute("SELECT ops._wp10_3_projection_digest()").fetchone()
    db.execute("SET LOCAL enable_seqscan=off")
    assert first == db.execute("SELECT ops._wp10_3_projection_digest()").fetchone()
    db.execute("SET LOCAL enable_seqscan=on")
    db.execute("UPDATE public.documents SET published_at=published_at+interval '1 second'")
    assert first == db.execute("SELECT ops._wp10_3_projection_digest()").fetchone()
    db.execute("UPDATE public.documents SET summary=coalesce(summary,'')||' covered mutation'")
    assert first != db.execute("SELECT ops._wp10_3_projection_digest()").fetchone()
    for signature in module().HELPER_SIGNATURES:
        row = db.execute(
            "SELECT pg_get_userbyid(p.proowner),EXISTS (SELECT 1 FROM "
            "aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a "
            "WHERE a.grantee=0 AND a.privilege_type='EXECUTE') "
            "FROM pg_proc p WHERE p.oid=%s::regprocedure",
            (signature,),
        ).fetchone()
        assert row == ("uap_owner", False)
        for role in module().DENIED_ROLES:
            assert db.execute(
                "SELECT has_function_privilege(%s,%s,'EXECUTE')", (role, signature)
            ).fetchone() == (False,)
    fold = db.execute(
        "SELECT pg_column_size(ops._v133_rebuild_digest_fold_v2("
        "repeat('0',64),'test',repeat('a',64)))"
    ).fetchone()
    assert fold == (68,)


def test_db_exact_baseline_drift_is_rejected_before_any_upgrade(
    db: psycopg.Connection[Any],
) -> None:
    original = db.execute(
        "SELECT pg_get_functiondef('ops._wp10_3_projection_digest()'::regprocedure)"
    ).fetchone()
    assert original is not None
    assert hashlib.sha256(original[0].encode()).hexdigest() == module().BASELINE_SHA256[2]
    db.execute(original[0].replace("SELECT audit._payload_sha256", "SELECT  audit._payload_sha256"))
    with pytest.raises(psycopg.Error, match="bounded_digest_definition_mismatch"):
        with db.transaction():
            db.execute(module()._sql(upgrade=True))
    assert db.execute(
        "SELECT count(*) FROM information_schema.columns WHERE table_schema='audit' "
        "AND table_name='publication_rebuild_runs' AND column_name='digest_version'"
    ).fetchone() == (0,)


def test_db_fold_physical_order_domain_and_order_contract(db: psycopg.Connection[Any]) -> None:
    db.execute(module()._sql(upgrade=True))
    db.execute("CREATE TEMP TABLE digest_items(n integer,domain text,item_sha256 text)")
    db.execute("INSERT INTO digest_items VALUES (1,'A',repeat('a',64)),(2,'B',repeat('b',64))")
    query = "SELECT ops._v133_rebuild_digest_v2(domain,item_sha256 ORDER BY n) FROM digest_items"
    first = db.execute(query).fetchone()
    db.execute("TRUNCATE digest_items")
    db.execute("INSERT INTO digest_items VALUES (2,'B',repeat('b',64)),(1,'A',repeat('a',64))")
    assert db.execute(query).fetchone() == first
    assert db.execute(query.replace("ORDER BY n)", "ORDER BY n DESC)")).fetchone() != first
    db.execute("UPDATE digest_items SET domain='different' WHERE n=1")
    assert db.execute(query).fetchone() != first


def test_db_empty_projection_sections_are_deterministic(db: psycopg.Connection[Any]) -> None:
    db.execute(module()._sql(upgrade=True))
    before = db.execute("SELECT ops._wp10_3_projection_digest()").fetchone()
    db.execute(
        "TRUNCATE TABLE ONLY public.document_entities, ONLY public.claim_evidence, "
        "ONLY public.relation_evidence, ONLY public.relations, ONLY public.claims, "
        "ONLY public.evidence, ONLY public.search_documents, ONLY public.documents, "
        "ONLY public.entities CONTINUE IDENTITY RESTRICT"
    )
    empty = db.execute("SELECT ops._wp10_3_projection_digest()").fetchone()
    assert empty is not None and empty != before
    assert empty == db.execute("SELECT ops._wp10_3_projection_digest()").fetchone()
    assert empty[0] != "0" * 64


@pytest.mark.parametrize("legacy", [False, True])
def test_db_scoped_changed_claim_input_conflicts(db: psycopg.Connection[Any], legacy: bool) -> None:
    identifier = uuid4()
    document = document_id(db)
    if not legacy:
        db.execute(module()._sql(upgrade=True))
    first = call(db, identifier, document)
    if legacy:
        db.execute(module()._sql(upgrade=True))
    # Isolated state fixture only: all production function bodies remain formal.
    db.execute("SET LOCAL session_replication_role=replica")
    db.execute(
        "UPDATE ops.outbox_events SET published_at=NULL WHERE "
        "aggregate_type='claim_publication_grants' AND aggregate_id IN "
        "(SELECT grant_id FROM audit.claim_publication_manifests WHERE document_id=%s)",
        (document,),
    )
    db.execute("SET LOCAL session_replication_role=origin")
    with pytest.raises(psycopg.errors.SerializationFailure, match="rebuild_id_conflict"):
        call(db, identifier, document)
    assert (
        db.execute(
            "SELECT input_digest,result_digest FROM audit.publication_rebuild_runs "
            "WHERE rebuild_id=%s",
            (identifier,),
        ).fetchone()
        == first[1:3]
    )


def test_db_new_full_excludes_unpublished_documents_with_formal_0036_predicate(
    db: psycopg.Connection[Any],
) -> None:
    db.execute(module()._sql(upgrade=True))
    row = db.execute(
        "SELECT document_grant_id FROM public.documents ORDER BY id LIMIT 1"
    ).fetchone()
    assert row is not None
    event_state_module()._set_event_state(db, str(row[0]), "queued")
    db.execute("SET LOCAL session_replication_role=replica")
    db.execute(
        "UPDATE ops.outbox_events SET published_at=NULL WHERE "
        "aggregate_type='claim_publication_grants' AND aggregate_id IN "
        "(SELECT cm.grant_id FROM audit.claim_publication_manifests cm "
        "JOIN audit.document_publication_manifests dm ON dm.document_id=cm.document_id "
        "WHERE dm.grant_id=%s)", (row[0],),
    )
    db.execute("SET LOCAL session_replication_role=origin")
    result = call(db, uuid4())
    assert result[4] == "succeeded"
    assert db.execute(
        "SELECT count(*) FROM public.documents WHERE document_grant_id=%s", (row[0],)
    ).fetchone() == (0,)
    db.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_hardened_head_declares_the_exact_private_helper_surface() -> None:
    assert hardened_head_runtime_probe.DIGEST_V2_HELPERS == module().HELPER_SIGNATURES
