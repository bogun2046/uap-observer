"""Static and opt-in PostgreSQL contracts for the 0038 rebuild reset."""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest

from tools import hardened_head_runtime_probe, validate_wp10, validate_wp10_6
from tools.wp10_6_migration_probe import run_alembic, set_migrator_login
from tools.wp10_runtime_probe import FROZEN_STEPS
from tools.wp10_stage_revisions import MAIN_DB_ADVANCE_BEFORE, STEP_REQUIRED_REVISION

PLATFORM = Path(__file__).resolve().parents[1]
MIGRATION = PLATFORM / "alembic/versions/0038_v133_full_rebuild_compaction.py"
RESET_TABLES = (
    "document_entities",
    "claim_evidence",
    "relation_evidence",
    "relations",
    "claims",
    "evidence",
    "search_documents",
    "documents",
    "entities",
)


def migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("v133_full_rebuild_compaction", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_is_the_exact_successor_of_0037() -> None:
    module = migration()
    assert module.revision == "0038_v133_full_rebuild_compaction"
    assert module.down_revision == "0037_v133_deferred_integrity_trigger_security"


def test_reset_markers_are_exact_and_unique() -> None:
    module = migration()
    assert module.DELETE_RESET.count("DELETE FROM public.") == 7
    assert module.TRUNCATE_RESET.count("TRUNCATE TABLE") == 1
    assert module.TRUNCATE_RESET.count("LOCK TABLE public.relations") == 1


def test_truncate_table_set_is_explicit_and_ordered() -> None:
    reset = migration().TRUNCATE_RESET
    statement = reset.split("TRUNCATE TABLE ", 1)[1].split(" CONTINUE IDENTITY", 1)[0]
    actual = tuple(part.strip().removeprefix("ONLY public.") for part in statement.split(","))
    assert actual == RESET_TABLES
    assert all(part.strip().startswith("ONLY public.") for part in statement.split(","))


def test_truncate_is_restrictive_and_preserves_identity() -> None:
    reset = migration().TRUNCATE_RESET
    assert "CONTINUE IDENTITY RESTRICT;" in reset
    assert "CASCADE" not in reset
    assert "RESTART IDENTITY" not in reset


def test_relation_tables_are_locked_and_checked_before_reset() -> None:
    reset = migration().TRUNCATE_RESET
    assert reset.index("LOCK TABLE public.relations") < reset.index("IF EXISTS")
    assert reset.index("IF EXISTS (SELECT 1 FROM public.relations)") < reset.index("TRUNCATE TABLE")
    assert "OR EXISTS (SELECT 1 FROM public.relation_evidence)" in reset
    assert "SHARE ROW EXCLUSIVE MODE" in reset


def test_relation_guard_keeps_the_existing_fail_closed_marker() -> None:
    reset = migration().TRUNCATE_RESET
    assert "RAISE EXCEPTION 'publication_rebuild_mismatch'" in reset
    assert "USING ERRCODE = '22023'" in reset


def test_only_the_formal_reset_block_is_transformed() -> None:
    sql = migration()._sql(upgrade=True)
    assert "definition := pg_get_functiondef(original.oid)" in sql
    assert "rewritten := replace(" in sql
    assert "pg_get_functiondef(changed.oid) IS DISTINCT FROM rewritten" in sql
    assert "v133_full_rebuild_reset_marker_mismatch: upgrade" in sql


def test_downgrade_is_the_exact_reverse_transformation() -> None:
    sql = migration()._sql(upgrade=False)
    assert "v133_full_rebuild_reset_marker_mismatch: downgrade" in sql
    assert "v133_expected_reset" in sql
    assert "v133_replacement_reset" in sql
    assert migration().DELETE_RESET in sql


def test_function_security_owner_and_acl_are_postcondition_checked() -> None:
    sql = migration()._sql(upgrade=True)
    for marker in (
        "original.proowner <> 'uap_owner'::regrole",
        "original.prosecdef IS DISTINCT FROM true",
        "original.proconfig IS DISTINCT FROM ARRAY[",
        "changed.proowner IS DISTINCT FROM original.proowner",
        "changed.proacl IS DISTINCT FROM original.proacl",
        "changed.prosecdef IS DISTINCT FROM original.prosecdef",
        "changed.proconfig IS DISTINCT FROM original.proconfig",
    ):
        assert marker in sql
    assert "GRANT EXECUTE" not in sql
    assert "REVOKE EXECUTE" not in sql


def test_maintenance_comment_requires_quiescence_until_commit() -> None:
    comment = migration().MAINTENANCE_COMMENT
    assert "approved maintenance window" in comment
    assert "public read traffic quiesced before the transaction starts and until commit" in comment
    assert "not safe for concurrent public reads" in comment
    assert "expected_comment text := NULL" in migration()._sql(upgrade=True)
    assert "replacement_comment text := NULL" in migration()._sql(upgrade=False)


def test_descendant_and_foreign_key_scope_fail_closed() -> None:
    sql = migration()._sql(upgrade=True)
    assert "relation.relkind <> 'r'" in sql
    assert "FROM pg_inherits AS inheritance" in sql
    assert "inheritance.inhparent = ANY(target_tables)" in sql
    assert "foreign_key.confrelid = ANY(target_tables)" in sql
    assert "NOT foreign_key.conrelid = ANY(target_tables)" in sql


def test_delete_and_truncate_trigger_inventory_fails_closed() -> None:
    sql = migration()._sql(upgrade=True)
    assert "delete_trigger_count <> 1" in sql
    assert "(trigger_row.tgtype & 32) <> 0" in sql
    assert "v133_full_rebuild_trigger_inventory_mismatch" in sql


def test_only_known_delete_constraint_trigger_is_accepted() -> None:
    sql = migration()._sql(upgrade=True)
    for marker in (
        "public_claim_evidence_required",
        "public.prevent_last_claim_evidence_removal()",
        "trigger_row.tgconstraint <> 0",
        "trigger_row.tgdeferrable",
        "trigger_row.tginitdeferred",
        "(trigger_row.tgtype & 8) <> 0",
    ):
        assert marker in sql


def test_function_comment_is_checked_before_upgrade_and_downgrade() -> None:
    upgrade = migration()._sql(upgrade=True)
    downgrade = migration()._sql(upgrade=False)
    assert "obj_description(original.oid, 'pg_proc') IS DISTINCT FROM expected_comment" in upgrade
    assert "obj_description(original.oid, 'pg_proc') IS DISTINCT FROM expected_comment" in downgrade
    assert "COMMENT ON FUNCTION ops.rebuild_public_projection(uuid) IS %L" in upgrade
    assert "COMMENT ON FUNCTION ops.rebuild_public_projection(uuid) IS %L" in downgrade


def test_projection_filtering_and_result_contract_are_outside_reset_replacement() -> None:
    sql = migration()._sql(upgrade=True)
    assert "pg_get_functiondef(original.oid)" in sql
    assert "pg_get_functiondef(changed.oid) IS DISTINCT FROM rewritten" in sql
    assert "input_digest" not in sql
    assert "publication.granted" not in sql
    assert "result_digest" not in sql


def test_0038_is_not_added_to_the_frozen_23_step_or_0019_0024_topology() -> None:
    assert len(FROZEN_STEPS) == 23
    assert FROZEN_STEPS[-1].step_id == "WP10.5-runtime"
    assert all("0038" not in revision for revision in STEP_REQUIRED_REVISION.values())
    assert all("0038" not in revision for revision in MAIN_DB_ADVANCE_BEFORE.values())


def test_hardened_head_runtime_requires_0038_and_tracks_full_projection_surface() -> None:
    probe = (PLATFORM / "tools/hardened_head_runtime_probe.py").read_text()
    assert 'REQUIRED_HEAD = "0038_v133_full_rebuild_compaction"' in probe
    assert "hardened-head-runtime-evidence.v1" in probe
    assert 'for table in ("documents", "search_documents")' in probe
    assert "PUBLIC_PROJECTION_TABLES" in probe


def test_validator_declares_0038_as_the_current_linear_head() -> None:
    checks = validate_wp10.evaluate(PLATFORM, git_paths=[])
    failures = [item for item in checks if not item.passed]
    assert failures == []
    chain = next(item for item in checks if item.name == "0001-0038 v1.3.3 migration chain")
    assert chain.passed
    assert "0038_v133_full_rebuild_compaction" in str(chain.actual)


@pytest.mark.parametrize(
    "path",
    [
        "platform/alembic/versions/0038_v133_full_rebuild_compaction.py",
        "platform/tests/test_v133_full_rebuild_compaction.py",
        "platform/tools/hardened_head_runtime_probe.py",
        "platform/tests/test_hardened_head_runtime.py",
        "platform/tools/validate_wp10.py",
        "platform/tests/test_wp10_6_chain.py",
        "docs/wp10/full-rebuild-maintenance.md",
    ],
)
def test_exact_compaction_paths_are_authorized(path: str) -> None:
    assert validate_wp10.classify_git_paths([path]) == ("allowed", [])
    checks = validate_wp10_6.evaluate(PLATFORM, paths=[path])
    check = next(
        item for item in checks if item.name == "WP10.6 changes stay in the authorized surface"
    )
    assert check.passed
    adjacent = path.replace(".py", "_extra.py")
    if adjacent != path:
        assert validate_wp10.classify_git_paths([adjacent]) == ("forbidden", [adjacent])


def test_0038_changes_do_not_touch_the_100k_probe_source() -> None:
    assert "platform/tools/wp10_6_performance_probe.py" not in {
        "platform/alembic/versions/0038_v133_full_rebuild_compaction.py",
        "platform/tests/test_v133_full_rebuild_compaction.py",
        "platform/tools/hardened_head_runtime_probe.py",
        "platform/tests/test_hardened_head_runtime.py",
        "platform/tools/validate_wp10.py",
        "platform/tests/test_wp10_6_chain.py",
        "docs/wp10/full-rebuild-maintenance.md",
    }


def _trigger_security_test_module() -> ModuleType:
    helper_path = PLATFORM / "tests/test_v133_deferred_trigger_security.py"
    spec = importlib.util.spec_from_file_location("trigger_security_seed_helper", helper_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def disposable_database_url() -> Iterator[str]:
    url = os.environ.get("UAP_V133_0038_TEST_DATABASE_URL")
    if not url:
        pytest.skip("disposable 0037 PostgreSQL database is not configured")
    if os.environ.get("UAP_V133_0038_TEST_DB_IS_DISPOSABLE") != "YES":
        pytest.fail("0038 database tests require explicit disposable-database opt-in")
    for variable in (
        "UAP_MIGRATOR_PASSWORD",
        "UAP_PUBLISHER_PASSWORD",
        "UAP_PUBLIC_READER_PASSWORD",
    ):
        if not os.environ.get(variable):
            pytest.skip(f"{variable} is required for the current-head fixture")
    try:
        with psycopg.connect(url) as connection:
            identity = connection.execute(
                "SELECT current_database(),role.rolsuper,"
                "(SELECT version_num FROM public.alembic_version) "
                "FROM pg_roles AS role WHERE role.rolname=session_user"
            ).fetchone()
            assert identity is not None
            name, superuser, head = identity
            assert name.startswith("compaction_contract_0037_")
            assert superuser is True
            assert head == "0037_v133_deferred_integrity_trigger_security"
            count = connection.execute("SELECT count(*) FROM public.documents").fetchone()
            assert count is not None
            if count[0] == 0:
                set_migrator_login(url, True)
                try:
                    hardened_head_runtime_probe.publication_and_rebuild(url)
                finally:
                    set_migrator_login(url, False)
    except Exception as error:
        raise RuntimeError(
            f"disposable 0037 fixture setup failed ({type(error).__name__}); details withheld"
        ) from None
    yield url


@pytest.fixture
def db(disposable_database_url: str) -> Iterator[psycopg.Connection[Any]]:
    connection = psycopg.connect(disposable_database_url)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def function_state(connection: psycopg.Connection[Any]) -> tuple[Any, ...]:
    row = connection.execute(
        "SELECT pg_get_functiondef(p.oid),pg_get_userbyid(p.proowner),p.proacl,"
        "p.prosecdef,p.proconfig,obj_description(p.oid,'pg_proc') "
        "FROM pg_proc AS p WHERE p.oid='ops.rebuild_public_projection(uuid)'::regprocedure"
    ).fetchone()
    assert row is not None
    return cast(tuple[Any, ...], row)


def projection_state(connection: psycopg.Connection[Any]) -> tuple[Any, ...]:
    row = connection.execute(
        "SELECT ops._wp10_3_projection_digest(),jsonb_build_object("
        "'documents',(SELECT count(*) FROM public.documents),"
        "'entities',(SELECT count(*) FROM public.entities),"
        "'claims',(SELECT count(*) FROM public.claims),"
        "'evidence',(SELECT count(*) FROM public.evidence),"
        "'claim_evidence',(SELECT count(*) FROM public.claim_evidence),"
        "'document_entities',(SELECT count(*) FROM public.document_entities),"
        "'search_documents',(SELECT count(*) FROM public.search_documents))"
    ).fetchone()
    assert row is not None
    return cast(tuple[Any, ...], row)


def call_rebuild_as_migrator(
    connection: psycopg.Connection[Any], rebuild_id: Any
) -> tuple[Any, ...]:
    connection.execute("SET SESSION AUTHORIZATION uap_migrator")
    try:
        row = connection.execute(
            "SELECT rebuild_id,input_digest,result_digest,counts,status,replayed "
            "FROM ops.rebuild_public_projection(%s)",
            (rebuild_id,),
        ).fetchone()
    finally:
        connection.execute("RESET SESSION AUTHORIZATION")
    assert row is not None
    return cast(tuple[Any, ...], row)


def test_db_upgrade_downgrade_roundtrip_preserves_all_non_reset_definition(
    db: psycopg.Connection[Any],
) -> None:
    module = migration()
    before = function_state(db)
    db.execute(module._sql(upgrade=True))
    upgraded = function_state(db)
    assert upgraded[0].replace(module.TRUNCATE_RESET, module.DELETE_RESET) == before[0]
    assert upgraded[1:5] == before[1:5]
    assert upgraded[5] == module.MAINTENANCE_COMMENT
    db.execute(module._sql(upgrade=False))
    downgraded = function_state(db)
    assert downgraded == before
    db.execute(module._sql(upgrade=True))
    assert function_state(db)[0] == upgraded[0]


def test_db_rebuild_digest_replay_atomicity_acl_and_direct_truncate_boundary(
    db: psycopg.Connection[Any],
) -> None:
    module = migration()
    original = function_state(db)
    upgraded = False
    try:
        baseline = call_rebuild_as_migrator(db, uuid4())
        assert baseline[4] == "succeeded"
        db.execute("SET CONSTRAINTS ALL IMMEDIATE")
        db.commit()
        baseline_state = projection_state(db)

        db.execute(module._sql(upgrade=True))
        upgraded = True
        hardened = function_state(db)
        assert hardened[1:5] == original[1:5]
        assert hardened[5] == module.MAINTENANCE_COMMENT
        compact_id = uuid4()
        compacted = call_rebuild_as_migrator(db, compact_id)
        assert compacted[4:] == ("succeeded", False)
        db.execute("SET CONSTRAINTS ALL IMMEDIATE")
        db.commit()
        assert projection_state(db) == baseline_state
        assert compacted[2] == baseline[2]
        assert compacted[3] == baseline[3]

        replay = call_rebuild_as_migrator(db, compact_id)
        assert replay[4:] == ("succeeded", True)
        db.commit()
        distinct = call_rebuild_as_migrator(db, uuid4())
        assert distinct[4:] == ("succeeded", False)
        db.execute("SET CONSTRAINTS ALL IMMEDIATE")
        db.commit()

        before_failure = projection_state(db)
        db.execute(
            "CREATE FUNCTION public._v133_test_fail_rebuild_insert() RETURNS trigger "
            "LANGUAGE plpgsql AS $test$ BEGIN "
            "RAISE EXCEPTION 'publication_rebuild_mismatch' USING ERRCODE='22023'; "
            "END $test$"
        )
        db.execute(
            "CREATE TRIGGER _v133_test_fail_rebuild_insert BEFORE INSERT ON public.documents "
            "FOR EACH ROW EXECUTE FUNCTION public._v133_test_fail_rebuild_insert()"
        )
        failed = call_rebuild_as_migrator(db, uuid4())
        assert failed[4] == "failed"
        assert projection_state(db) == before_failure
        db.execute(
            "DROP TRIGGER _v133_test_fail_rebuild_insert ON public.documents"
        )
        db.execute("DROP FUNCTION public._v133_test_fail_rebuild_insert()")
        db.commit()

        for table in ("documents", "search_documents"):
            savepoint = f"direct_truncate_{table}"
            db.execute(f"SAVEPOINT {savepoint}")
            db.execute("SET SESSION AUTHORIZATION uap_migrator")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                db.execute(f"TRUNCATE TABLE ONLY public.{table} CONTINUE IDENTITY RESTRICT")
            db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            db.execute(f"RELEASE SAVEPOINT {savepoint}")
        assert not any(hardened_head_runtime_probe.privilege_snapshot(db).values())
    finally:
        db.rollback()
        if upgraded:
            db.execute(module._sql(upgrade=False))
            db.commit()


def test_db_relations_and_relation_evidence_fail_closed_before_reset(
    db: psycopg.Connection[Any],
) -> None:
    module = migration()
    helper = _trigger_security_test_module()
    _, relation_id, _, _ = helper.seed_revision_fixture(db)
    db.execute(
        "INSERT INTO public.relation_evidence(id,relation_id,evidence_id) "
        "SELECT %s,%s,evidence_id FROM public.claim_evidence LIMIT 1",
        (uuid4(), relation_id),
    )
    db.execute("SET CONSTRAINTS ALL IMMEDIATE")
    db.execute("SET CONSTRAINTS ALL DEFERRED")
    before = projection_state(db)
    db.execute(module._sql(upgrade=True))
    result = call_rebuild_as_migrator(db, uuid4())
    assert result[4] == "failed"
    assert projection_state(db) == before
    assert db.execute("SELECT count(*) FROM public.relations").fetchone() == (1,)
    assert db.execute("SELECT count(*) FROM public.relation_evidence").fetchone() == (1,)


def test_db_0037_deferred_integrity_remains_enforced_after_0038_transform(
    db: psycopg.Connection[Any],
) -> None:
    module = migration()
    helper = _trigger_security_test_module()
    claim_id, relation_id, _, _ = helper.seed_revision_fixture(db)
    db.execute(module._sql(upgrade=True))
    with pytest.raises(
        psycopg.errors.CheckViolation,
        match="public claim requires at least one evidence row",
    ):
        with db.transaction():
            db.execute("DELETE FROM public.claim_evidence WHERE claim_id=%s", (claim_id,))
            db.execute("SET CONSTRAINTS ALL IMMEDIATE")
    with pytest.raises(
        psycopg.errors.CheckViolation,
        match="document entity claim and relation revisions must match",
    ):
        with db.transaction():
            db.execute(
                "UPDATE public.relations SET revision_no=revision_no+1 WHERE id=%s",
                (relation_id,),
            )
            db.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_db_migration_fails_closed_when_comment_does_not_match_baseline(
    db: psycopg.Connection[Any],
) -> None:
    db.execute(
        "COMMENT ON FUNCTION ops.rebuild_public_projection(uuid) IS 'unexpected comment'"
    )
    with pytest.raises(psycopg.errors.RaiseException, match="definition_mismatch"):
        db.execute(migration()._sql(upgrade=True))


def test_db_alembic_roundtrip_and_failed_migration_close_migrator(
    disposable_database_url: str,
) -> None:
    url = disposable_database_url
    alembic_url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    with psycopg.connect(url) as admin:
        admin.execute(
            "COMMENT ON FUNCTION ops.rebuild_public_projection(uuid) "
            "IS 'unexpected comment'"
        )
    set_migrator_login(url, True)
    try:
        failed = run_alembic(alembic_url, "upgrade", "head", role="migrator")
    finally:
        set_migrator_login(url, False)
    assert failed.exit_code != 0
    with psycopg.connect(url) as admin:
        state = admin.execute(
            "SELECT (SELECT version_num FROM public.alembic_version),rolcanlogin,"
            "obj_description('ops.rebuild_public_projection(uuid)'::regprocedure,'pg_proc') "
            "FROM pg_roles WHERE rolname='uap_migrator'"
        ).fetchone()
        assert state == (
            "0037_v133_deferred_integrity_trigger_security", False, "unexpected comment"
        )
        admin.execute("COMMENT ON FUNCTION ops.rebuild_public_projection(uuid) IS NULL")

    for action, target, expected_head in (
        ("upgrade", "head", "0038_v133_full_rebuild_compaction"),
        (
            "downgrade",
            "0037_v133_deferred_integrity_trigger_security",
            "0037_v133_deferred_integrity_trigger_security",
        ),
        ("upgrade", "head", "0038_v133_full_rebuild_compaction"),
    ):
        set_migrator_login(url, True)
        try:
            result = run_alembic(alembic_url, action, target, role="migrator")
        finally:
            set_migrator_login(url, False)
        assert result.exit_code == 0, f"{action} {target} failed ({result.sqlstate})"
        with psycopg.connect(url) as admin:
            state = admin.execute(
                "SELECT (SELECT version_num FROM public.alembic_version),rolcanlogin "
                "FROM pg_roles WHERE rolname='uap_migrator'"
            ).fetchone()
            assert state == (expected_head, False)

    set_migrator_login(url, True)
    try:
        downgraded = run_alembic(
            alembic_url,
            "downgrade",
            "0037_v133_deferred_integrity_trigger_security",
            role="migrator",
        )
    finally:
        set_migrator_login(url, False)
    assert downgraded.exit_code == 0
