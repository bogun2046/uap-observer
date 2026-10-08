"""Exact migration contracts and opt-in disposable PostgreSQL regression checks."""

from __future__ import annotations

import importlib.util
import os
import re
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from tools import validate_wp10, validate_wp10_6

PLATFORM = Path(__file__).resolve().parents[1]
MIGRATION = PLATFORM / "alembic/versions/0037_v133_deferred_integrity_trigger_security.py"


def migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("deferred_security", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_exact_linear_successor_and_function_inventory() -> None:
    module = migration()
    assert module.revision == "0037_v133_deferred_integrity_trigger_security"
    assert module.down_revision == "0036_v133_full_rebuild_publication_evidence_guard"
    assert set(module.EXPECTED_BODIES) == {
        "require_claim_has_evidence",
        "prevent_last_claim_evidence_removal",
        "require_document_entity_revision_match",
        "require_linked_document_entity_revision_match",
    }


def test_bodies_exactly_match_frozen_0004_and_have_safe_object_lookups() -> None:
    old = (PLATFORM / "alembic/versions/0004_g3_semantic_repairs.py").read_text()
    bodies = re.findall(
        (
            "CREATE FUNCTION public\\.(\\w+)\\(\\) RETURNS trigger\\s+LANGUAGE "
            "plpgsql AS \\$(\\w+)\\$(.*?)\\$\\2\\$;"
        ),
        old.split("def downgrade")[0],
        re.S,
    )
    assert migration().EXPECTED_BODIES == {name: body for (name, _, body) in bodies}
    for _, _, body in bodies:
        assert not re.search("\\bEXECUTE\\b", body, re.I)
        for table in re.findall(
            "\\b(?:FROM|JOIN)\\s+([\\w.]+)", re.sub("DISTINCT\\s+FROM", "DISTINCT_FROM", body)
        ):
            assert table.startswith("public.")
        assert "USING ERRCODE='23514'" in body


@pytest.mark.parametrize("upgrade", [True, False])
def test_bidirectional_fail_closed_guards_and_no_acl_or_trigger_mutation(upgrade: bool) -> None:
    statement = migration()._sql(upgrade=upgrade)
    for marker in (
        "SET ROLE uap_owner",
        "RESET ROLE",
        "pg_get_functiondef",
        "original.proowner",
        "original.prosrc IS DISTINCT FROM item.body",
        "original.prosecdef",
        "original.proconfig",
        "original.prorettype",
        "original.prolang",
        "original.provolatile",
        "original.proisstrict",
        "original.proleakproof",
        "original.proparallel",
        "original.procost",
        "changed.proacl IS DISTINCT FROM original.proacl",
        "changed_triggers IS DISTINCT FROM original_triggers",
        "NOT t.tgdeferrable",
        "NOT t.tginitdeferred",
    ):
        assert marker in statement
    assert f"SECURITY {('DEFINER' if upgrade else 'INVOKER')}" in statement
    assert ("SET search_path = public, pg_catalog" in statement) is upgrade
    for forbidden in ("GRANT ", "REVOKE ", "DROP FUNCTION", "DROP TRIGGER", "ALTER TRIGGER"):
        assert forbidden not in statement
    assert "ops.rebuild_public_projection" not in statement


def test_entrypoints_execute_only_the_guarded_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    module = migration()
    statements: list[str] = []
    monkeypatch.setattr(module.op, "execute", statements.append)
    module.upgrade()
    module.downgrade()
    assert statements == [module._sql(upgrade=True), module._sql(upgrade=False)]


@pytest.mark.parametrize(
    "path",
    [
        ("platform/alembic/versions/0037_v133_deferred_integrity_trigger_security.py"),
        "platform/tests/test_v133_deferred_trigger_security.py",
    ],
)
def test_exact_changed_paths_pass_both_validators(path: str) -> None:
    assert validate_wp10.classify_git_paths([path]) == ("allowed", [])
    checks = validate_wp10_6.evaluate(PLATFORM, paths=[path])
    assert next(
        c for c in checks if c.name == "WP10.6 changes stay in the authorized surface"
    ).passed
    adjacent = path.replace(".py", "_extra.py")
    assert validate_wp10.classify_git_paths([adjacent]) == ("forbidden", [adjacent])


@pytest.fixture
def db() -> Iterator[psycopg.Connection[Any]]:
    url = os.environ.get("UAP_V133_TRIGGER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("disposable 0037 PostgreSQL database is not configured")
    assert os.environ.get("UAP_V133_TRIGGER_TEST_DB_IS_DISPOSABLE") == "YES"
    connection = psycopg.connect(url)
    identity = connection.execute(
        "SELECT current_database(),rolsuper FROM pg_roles WHERE rolname=session_user"
    ).fetchone()
    assert identity and identity[0].startswith("trigger_security_") and identity[1]
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def inventory(connection: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    return connection.execute(
        (
            "SELECT "
            "proname,pg_get_userbyid(proowner),prosecdef,proconfig,proacl,prosrc"
            " FROM pg_proc WHERE pronamespace='public'::regnamespace AND "
            "proname=ANY(%s) ORDER BY proname"
        ),
        (list(migration().EXPECTED_BODIES),),
    ).fetchall()


def test_db_roundtrip_preserves_body_acl_owner_and_trigger_definitions(
    db: psycopg.Connection[Any],
) -> None:
    before = inventory(db)
    triggers = db.execute(
        "SELECT tgname,pg_get_triggerdef(oid),tgdeferrable,tginitdeferred "
        "FROM pg_trigger WHERE NOT tgisinternal AND tgdeferrable AND "
        "tgrelid IN (SELECT oid FROM pg_class WHERE "
        "relnamespace='public'::regnamespace) ORDER BY tgname"
    ).fetchall()
    assert len(triggers) == 5
    assert all(row[2:] == (True, True) for row in triggers)
    assert all(
        row[1:4] == ("uap_owner", True, ["search_path=public, pg_catalog"]) for row in before
    )
    db.execute(migration()._sql(upgrade=False))
    downgraded = inventory(db)
    assert all(row[2:4] == (False, None) for row in downgraded)
    assert [row[:2] + row[4:] for row in before] == [row[:2] + row[4:] for row in downgraded]
    db.execute(migration()._sql(upgrade=True))
    assert inventory(db) == before
    assert (
        db.execute(
            "SELECT tgname,pg_get_triggerdef(oid),tgdeferrable,tginitdeferred "
            "FROM pg_trigger WHERE NOT tgisinternal AND tgdeferrable AND "
            "tgrelid IN (SELECT oid FROM pg_class WHERE "
            "relnamespace='public'::regnamespace) ORDER BY tgname"
        ).fetchall()
        == triggers
    )


@pytest.mark.parametrize("tamper", ["body", "owner", "config"])
def test_db_rejects_unexpected_current_definition(db: psycopg.Connection[Any], tamper: str) -> None:
    if tamper == "body":
        db.execute(
            "CREATE OR REPLACE FUNCTION public.require_claim_has_evidence() "
            "RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET "
            "search_path=public,pg_catalog AS 'BEGIN RETURN NULL; END'"
        )
    elif tamper == "owner":
        db.execute("ALTER FUNCTION public.require_claim_has_evidence() OWNER TO uap_api")
    else:
        db.execute("ALTER FUNCTION public.require_claim_has_evidence() RESET ALL")
    with pytest.raises(psycopg.errors.RaiseException, match="definition_mismatch"):
        db.execute(migration()._sql(upgrade=False))


@pytest.mark.parametrize("immediate", [False, True])
def test_db_hardened_migrator_rebuild_and_commit_without_table_privileges(
    db: psycopg.Connection[Any], immediate: bool
) -> None:
    assert db.execute(
        "SELECT r.rolinherit,m.inherit_option,m.set_option,m.admin_option "
        "FROM pg_roles r JOIN pg_auth_members m ON m.member=r.oid JOIN "
        "pg_roles o ON o.oid=m.roleid WHERE r.rolname='uap_migrator' AND "
        "o.rolname='uap_owner'"
    ).fetchone() == (False, False, True, False)
    for table in ("claims", "claim_evidence", "document_entities", "relations"):
        assert db.execute(
            "SELECT has_table_privilege('uap_migrator',%s,'SELECT')", ("public." + table,)
        ).fetchone() == (False,)
    params = dict(db.info.get_parameters())
    params["user"] = "uap_migrator"
    params.pop("password", None)
    with psycopg.connect(make_conninfo(**params)) as caller:
        assert caller.execute("SELECT session_user,current_user").fetchone() == (
            "uap_migrator",
            "uap_migrator",
        )
        report = caller.execute(
            "SELECT * FROM ops.rebuild_public_projection(%s)", (uuid4(),)
        ).fetchone()
        assert report is not None and report[4] == "succeeded"
        assert report[3]["claims"] > 0 and report[3]["document_entities"] > 0
        if immediate:
            caller.execute("SET CONSTRAINTS ALL IMMEDIATE")
        caller.commit()
        assert caller.execute("SELECT session_user,current_user").fetchone() == (
            "uap_migrator",
            "uap_migrator",
        )


def seed_revision_fixture(c: psycopg.Connection[Any]) -> tuple[Any, Any, Any, Any]:
    principal = required_row(c.execute("SELECT id FROM audit.principals LIMIT 1"))[0]
    entity1 = required_row(c.execute("SELECT id FROM core.entities LIMIT 1"))[0]
    (entity2, relation, public_entity2, public_relation) = [uuid4() for _ in range(4)]
    c.execute(
        (
            "INSERT INTO core.entities(id,entity_type,canonical_name) "
            "VALUES(%s,'organization','Trigger prototype entity')"
        ),
        (entity2,),
    )
    c.execute(
        (
            "INSERT INTO "
            "core.relations(id,subject_entity_id,object_entity_id,predicate,relation_status,created_by)"
            " VALUES(%s,%s,%s,'supports','reported',%s)"
        ),
        (relation, entity1, entity2, principal),
    )
    grants = []
    for kind, target in [("entity", entity2), ("relation", relation)]:
        (case, decision, grant) = [uuid4() for _ in range(3)]
        c.execute(
            sql.SQL(
                "INSERT INTO "
                "audit.review_cases(id,{},case_type,status,opened_by,opened_at,closed_at)"
                " VALUES(%s,%s,%s,'approved',%s,now(),now())"
            ).format(sql.Identifier(kind + "_id")),
            (case, target, kind, principal),
        )
        c.execute(
            (
                "INSERT INTO "
                "audit.review_decisions(id,review_case_id,sequence_no,decision,reason,decided_by,decided_at)"
                " VALUES(%s,%s,1,'approve','Trigger security prototype',%s,now())"
            ),
            (decision, case, principal),
        )
        c.execute(
            sql.SQL(
                "INSERT INTO "
                "audit.{}(id,review_case_id,{},decision_id,revision_no,grant_status,granted_at,publication_payload_sha256)"
                " VALUES(%s,%s,%s,%s,1,'active',now(),repeat('a',64))"
            ).format(sql.Identifier(kind + "_publication_grants"), sql.Identifier(kind + "_id")),
            (grant, case, target, decision),
        )
        grants.append(grant)
    public_entity1 = required_row(c.execute("SELECT id FROM public.entities LIMIT 1"))[0]
    c.execute(
        (
            "INSERT INTO "
            "public.entities(id,entity_grant_id,slug,entity_type,name,published_at,revision_no)"
            " VALUES(%s,%s,%s,'organization','Trigger prototype "
            "entity',now(),1)"
        ),
        (public_entity2, grants[0], "e-" + public_entity2.hex),
    )
    (claim, _doc, rev) = required_row(
        c.execute(
            "SELECT c.id,c.document_id,c.revision_no FROM public.claims c JOIN"
            " public.document_entities de ON de.basis_claim_id=c.id LIMIT 1"
        )
    )
    c.execute(
        (
            "INSERT INTO "
            "public.relations(id,subject_entity_id,object_entity_id,relation_grant_id,predicate,relation_status,published_at,revision_no)"
            " VALUES(%s,%s,%s,%s,'supports','reported',now(),%s)"
        ),
        (public_relation, public_entity1, public_entity2, grants[1], rev),
    )
    link = required_row(
        c.execute(
            ("SELECT id FROM public.document_entities WHERE basis_claim_id=%s LIMIT 1"), (claim,)
        )
    )[0]
    c.execute(
        ("UPDATE public.document_entities SET basis_relation_id=%s WHERE id=%s"),
        (public_relation, link),
    )
    c.execute("SET CONSTRAINTS ALL IMMEDIATE")
    c.execute("SET CONSTRAINTS ALL DEFERRED")
    return (claim, public_relation, link, rev)


@pytest.mark.parametrize("case", ["claim", "last_evidence", "document_revision", "linked_revision"])
def test_db_all_integrity_functions_remain_fail_closed(
    db: psycopg.Connection[Any], case: str
) -> None:
    (claim, relation, link, _) = seed_revision_fixture(db)
    unused = db.execute(
        "SELECT id FROM audit.claim_publication_grants WHERE id NOT "
        "IN(SELECT claim_grant_id FROM public.claims) LIMIT 1"
    ).fetchone()
    assert unused is not None
    expected = (
        "public claim requires at least one evidence row"
        if case in ("claim", "last_evidence")
        else "document entity claim and relation revisions must match"
    )
    with pytest.raises(psycopg.errors.CheckViolation, match=expected) as rejected:
        with db.transaction():
            db.execute("SET LOCAL ROLE uap_owner")
            if case == "claim":
                db.execute(
                    (
                        "INSERT INTO "
                        "public.claims(id,document_id,claim_grant_id,ordinal,claim_text,claim_type,assertion_status,revision_no)"
                        " SELECT %s,document_id,%s,999,'Invalid "
                        "prototype','observation','reported',revision_no FROM "
                        "public.claims WHERE id=%s"
                    ),
                    (uuid4(), unused[0], claim),
                )
            elif case == "last_evidence":
                db.execute("DELETE FROM public.claim_evidence WHERE claim_id=%s", (claim,))
            elif case == "document_revision":
                db.execute(
                    "UPDATE public.claims SET revision_no=revision_no+1 WHERE id=%s", (claim,)
                )
                db.execute(
                    ("UPDATE public.document_entities SET basis_relation_id=%s WHERE id=%s"),
                    (relation, link),
                )
                db.execute("SET CONSTRAINTS public_document_entity_revision IMMEDIATE")
            else:
                db.execute(
                    "UPDATE public.relations SET revision_no=revision_no+1 WHERE id=%s", (relation,)
                )
            db.execute("SET CONSTRAINTS ALL IMMEDIATE")
    assert rejected.value.sqlstate == "23514"
    assert rejected.value.diag.message_primary == expected


def test_db_failed_rebuild_preserves_projection_digest_and_counts(
    db: psycopg.Connection[Any],
) -> None:
    before = db.execute("SELECT ops._wp10_3_projection_digest()").fetchone()
    counts_sql = (
        "SELECT (SELECT count(*) FROM public.claims),(SELECT count(*) FROM"
        " public.claim_evidence),(SELECT count(*) FROM "
        "public.document_entities),(SELECT count(*) FROM public.documents)"
    )
    counts = db.execute(counts_sql).fetchone()
    db.execute("SET LOCAL session_replication_role=replica")
    db.execute("UPDATE audit.document_publication_manifests SET manifest_sha256=repeat('0',64)")
    db.execute("SET LOCAL session_replication_role=origin")
    try:
        db.execute("SET SESSION AUTHORIZATION uap_migrator")
        report = db.execute(
            "SELECT * FROM ops.rebuild_public_projection(%s)", (uuid4(),)
        ).fetchone()
        assert report is not None and report[4] == "failed"
    finally:
        db.execute("RESET SESSION AUTHORIZATION")
    assert db.execute("SELECT ops._wp10_3_projection_digest()").fetchone() == before
    assert db.execute(counts_sql).fetchone() == counts


def test_db_downgrade_reproduces_old_deferred_privilege_failure(
    db: psycopg.Connection[Any],
) -> None:
    db.execute(migration()._sql(upgrade=False))
    db.commit()
    params = dict(db.info.get_parameters())
    params["user"] = "uap_migrator"
    params.pop("password", None)
    try:
        with psycopg.connect(make_conninfo(**params)) as caller:
            report = caller.execute(
                "SELECT * FROM ops.rebuild_public_projection(%s)", (uuid4(),)
            ).fetchone()
            assert report is not None and report[4] == "succeeded"
            with pytest.raises(psycopg.errors.InsufficientPrivilege) as rejected:
                caller.commit()
            assert rejected.value.sqlstate == "42501"
            caller.rollback()
    finally:
        db.execute(migration()._sql(upgrade=True))
        db.commit()


def test_db_publisher_api_applies_real_event_and_preserves_direct_dml_boundary(
    db: psycopg.Connection[Any],
) -> None:
    for table in ("claims", "claim_evidence", "document_entities", "relations"):
        for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            assert db.execute(
                "SELECT has_table_privilege('uap_publisher',%s,%s)", ("public." + table, privilege)
            ).fetchone() == (False,)
    row = db.execute(
        "SELECT e.id,e.published_at,e.terminal_at,e.available_at FROM "
        "ops.outbox_events e JOIN public.claims c ON "
        "c.claim_grant_id=e.aggregate_id WHERE "
        "e.event_type='publication.granted' AND "
        "e.aggregate_type='claim_publication_grants' LIMIT 1"
    ).fetchone()
    assert row is not None
    db.execute("SET LOCAL session_replication_role=replica")
    db.execute(
        (
            "UPDATE ops.outbox_events SET "
            "published_at=NULL,terminal_at=NULL,available_at=now(),lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL"
            " WHERE id=%s"
        ),
        (row[0],),
    )
    db.commit()
    params = dict(db.info.get_parameters())
    params["user"] = "uap_publisher"
    params.pop("password", None)
    try:
        with psycopg.connect(make_conninfo(**params)) as publisher:
            leased = publisher.execute(
                "SELECT event_id,lease_token FROM "
                "ops.claim_publication_outbox('trigger-test',300,100)"
            ).fetchall()
            token = next(token for (event, token) in leased if event == row[0])
            report = publisher.execute(
                "SELECT * FROM ops.apply_publication_event(%s,%s)", (row[0], token)
            ).fetchone()
            assert report is not None and report[1] is not None and (report[4] is False)
            publisher.execute("SET CONSTRAINTS ALL IMMEDIATE")
            publisher.commit()
    finally:
        db.execute("SET LOCAL session_replication_role=replica")
        db.execute(
            (
                "UPDATE ops.outbox_events SET "
                "published_at=%s,terminal_at=%s,available_at=%s WHERE id=%s"
            ),
            (row[1], row[2], row[3], row[0]),
        )
        db.commit()


def required_row(cursor: psycopg.Cursor[tuple[Any, ...]]) -> tuple[Any, ...]:
    row = cursor.fetchone()
    assert row is not None
    return row
