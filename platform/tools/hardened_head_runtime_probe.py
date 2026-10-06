"""Validate current-head runtime with fresh, non-inheriting migrator fixtures.

This entrypoint is separate from the frozen 0019--0024 stage orchestrator.
It applies real migrations, reuses existing runtime fixtures and API probes,
and preserves its disposable databases for inspection. It never patches SQL.
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from pathlib import Path
from typing import Any

import psycopg
from alembic.config import Config
from alembic.script import ScriptDirectory

from tools import (
    configure_roles,
    wp10_2_runtime_probe,
    wp10_3_runtime_probe,
    wp10_4_runtime_probe,
    wp10_5_runtime_probe,
)
from tools.wp10_2_runtime_probe import connect_role
from tools.wp10_6_migration_probe import (
    PLATFORM_ROOT,
    create_database,
    database_url,
    libpq_url,
    run_alembic,
    set_migrator_login,
)
from uap_platform.publishing.service import PublicationService

REQUIRED_HEAD = "0037_v133_deferred_integrity_trigger_security"
PUBLIC_TABLES = ("claims", "claim_evidence", "document_entities", "relations")
TRIGGER_FUNCTIONS = (
    "require_claim_has_evidence",
    "prevent_last_claim_evidence_removal",
    "require_document_entity_revision_match",
    "require_linked_document_entity_revision_match",
)
PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")


def require(condition: bool, marker: str) -> None:
    if not condition:
        raise RuntimeError(marker)


def current_head() -> str:
    config = Config(str(PLATFORM_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PLATFORM_ROOT / "alembic"))
    head = ScriptDirectory.from_config(config).get_current_head()
    require(head == REQUIRED_HEAD, "unexpected current Alembic head")
    return REQUIRED_HEAD


def fresh_database(admin_url: str, name: str) -> str:
    create_database(admin_url, name)
    url = database_url(admin_url, name)
    bootstrap = run_alembic(url, "upgrade", "0001_roles_and_schemas", role=None)
    require(bootstrap.exit_code == 0, "fresh 0001 bootstrap failed")
    previous = os.environ.get("UAP_DATABASE_URL")
    os.environ["UAP_DATABASE_URL"] = url
    try:
        configure_roles.configure()
        set_migrator_login(admin_url, True)
        result = run_alembic(url, "upgrade", current_head())
        require(result.exit_code == 0, "fresh current-head upgrade failed")
    finally:
        set_migrator_login(admin_url, False)
        if previous is None:
            os.environ.pop("UAP_DATABASE_URL", None)
        else:
            os.environ["UAP_DATABASE_URL"] = previous
    return url


def privilege_snapshot(connection: psycopg.Connection[Any]) -> dict[str, bool]:
    result: dict[str, bool] = {}
    for table in PUBLIC_TABLES:
        for privilege in PRIVILEGES:
            row = connection.execute(
                "SELECT has_table_privilege('uap_migrator',%s,%s)",
                ("public." + table, privilege),
            ).fetchone()
            require(row is not None, "privilege inventory missing")
            if row is None:
                raise RuntimeError("privilege inventory missing")
            result[f"{table}.{privilege}"] = bool(row[0])
    return result


def verify_database(url: str) -> dict[str, object]:
    with psycopg.connect(libpq_url(url)) as admin:
        require(
            admin.execute("SELECT version_num FROM public.alembic_version").fetchone()
            == (REQUIRED_HEAD,),
            "database is not at current head",
        )
        membership = admin.execute(configure_roles.MIGRATOR_MEMBERSHIP_QUERY).fetchone()
        configure_roles.verify_migrator_membership(membership)
        configure_roles.verify_alembic_version_access(
            admin.execute(configure_roles.ALEMBIC_VERSION_ACCESS_QUERY).fetchone()
        )
        rows = admin.execute(
            "SELECT p.proname,pg_get_userbyid(p.proowner),p.prosecdef,p.proconfig "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='public' AND p.proname=ANY(%s) ORDER BY p.proname",
            (list(TRIGGER_FUNCTIONS),),
        ).fetchall()
        require(len(rows) == 4, "deferred function inventory mismatch")
        require(
            all(row[1:] == ("uap_owner", True, ["search_path=public, pg_catalog"]) for row in rows),
            "formal 0037 deferred security contract mismatch",
        )
        privileges = privilege_snapshot(admin)
        require(not any(privileges.values()), "migrator has public projection privileges")
        return {"head": REQUIRED_HEAD, "membership": membership, "privileges": privileges}


def publication_and_rebuild(url: str) -> dict[str, object]:
    probe = wp10_3_runtime_probe
    with psycopg.connect(libpq_url(url), autocommit=True) as admin:
        with connect_role(url, "uap_publisher") as publisher:
            service = PublicationService(publisher, dispatcher_id="hardened-head", batch_limit=10)
            document = wp10_2_runtime_probe.document_manifest(admin, "head-" + uuid.uuid4().hex)
            entity = wp10_2_runtime_probe.entity_manifest(admin, "head-" + uuid.uuid4().hex)
            probe.publish_document(admin, service, document)
            probe.publish_entity(admin, service, entity)
            claim = probe.claim_manifest(
                admin, document, "head-claim", subject_entity_id=entity[0], ai=True
            )
            event = probe.claim_event(admin, claim, "publication.granted")
            report = service.apply(probe.claim_for(service, event))
            publisher.execute("SET CONSTRAINTS ALL IMMEDIATE")
            publisher.commit()
            require(not report.replayed, "fresh publisher event unexpectedly replayed")
        with connect_role(url, "uap_public_reader") as reader:
            for table in PUBLIC_TABLES:
                reader.execute(
                    psycopg.sql.SQL("SELECT count(*) FROM public.{}").format(
                        psycopg.sql.Identifier(table)
                    )
                )
            reader.commit()
    with connect_role(url, "uap_migrator") as migrator:
        require(
            migrator.execute("SELECT session_user,current_user").fetchone()
            == ("uap_migrator", "uap_migrator"),
            "real migrator login required",
        )
        result = migrator.execute(
            "SELECT * FROM ops.rebuild_public_projection(%s)", (uuid.uuid4(),)
        ).fetchone()
        require(result is not None and result[4] == "succeeded", "head rebuild failed")
        require(
            result is not None and result[3]["claims"] > 0 and result[3]["document_entities"] > 0,
            "empty rebuild fixture",
        )
        migrator.execute("SET CONSTRAINTS ALL IMMEDIATE")
        migrator.commit()
        require(
            migrator.execute("SELECT current_user").fetchone() == ("uap_migrator",),
            "owner identity escaped rebuild",
        )
        try:
            with migrator.transaction():
                migrator.execute("TRUNCATE public.search_documents")
        except psycopg.errors.InsufficientPrivilege:
            pass
        else:
            raise RuntimeError("direct migrator truncate accepted")
        migrator.execute("SET ROLE uap_owner")
        require(
            migrator.execute("SELECT current_user").fetchone() == ("uap_owner",),
            "explicit owner elevation failed",
        )
        migrator.execute("RESET ROLE")
        migrator.commit()
    return {
        "publisher": "PASS",
        "public_reader": "PASS",
        "rebuild_call": "PASS",
        "set_constraints": "PASS",
        "commit": "PASS",
        "direct_truncate": "DENIED",
        "explicit_owner": "ALLOWED",
    }


def run(admin_url: str, evidence_path: Path) -> dict[str, Any]:
    head = current_head()
    evidence: dict[str, Any] = {
        "schema": "hardened-head-runtime-evidence.v1",
        "status": "failed",
        "head": head,
        "role_fixture": {
            "mode": "current-hardened",
            "revision": head,
            "rolinherit": False,
            "membership_inherit": False,
            "membership_set": True,
            "membership_admin": False,
        },
        "historical_stage_contract_modified": False,
        "databases": [],
        "checks": {},
    }
    try:
        for kind in ("publication", "publisher_protocol", "public_api"):
            name = "membership_head_" + kind + "_" + uuid.uuid4().hex[:12]
            url = fresh_database(admin_url, name)
            evidence["databases"].append(name)
            boundary = verify_database(url)
            evidence["checks"][kind + "_boundary"] = boundary
            before = boundary["privileges"]
            set_migrator_login(admin_url, True)
            if kind == "publication":
                evidence["checks"][kind] = publication_and_rebuild(url)
                evidence["rebuild_call"] = "pass"
                evidence["set_constraints_all_immediate"] = "pass"
                evidence["commit"] = "pass"
            elif kind == "publisher_protocol":
                wp10_2_runtime_probe.run(url)
                evidence["checks"][kind] = "PASS"
            else:
                with (
                    connect_role(url, "uap_publisher") as publisher,
                    connect_role(url, "uap_public_reader") as reader,
                ):
                    wp10_4_runtime_probe.run(url, publisher.info.dsn, reader.info.dsn)
                evidence["checks"][kind] = "PASS"
            after = verify_database(url)["privileges"]
            require(before == after, "migrator public privilege drift")
            set_migrator_login(admin_url, False)
        # The existing admin API probe creates a separate empty database and
        # applies the real current migration head itself. Its historical
        # assertions are reused unchanged; it is not a new frozen stage.
        wp10_5_runtime_probe.run(admin_url)
        evidence["checks"]["admin_api"] = "PASS"
        evidence["migrator_public_privilege_diff"] = "NONE"
        evidence["status"] = "passed"
    except Exception as error:
        evidence["error_type"] = type(error).__name__
        # Error messages and DSNs may contain credentials; don't serialize them.
        raise
    finally:
        try:
            set_migrator_login(admin_url, False)
            with psycopg.connect(libpq_url(admin_url)) as admin:
                configure_roles.verify_migrator_membership(
                    admin.execute(configure_roles.MIGRATOR_MEMBERSHIP_QUERY).fetchone()
                )
                require(
                    admin.execute(
                        "SELECT rolcanlogin FROM pg_roles WHERE rolname='uap_migrator'"
                    ).fetchone()
                    == (False,),
                    "migrator login closure failed",
                )
            evidence["final_migrator_nologin"] = True
            evidence["final_migrator_noinherit"] = True
        except Exception as error:
            evidence["status"] = "failed"
            evidence["closure_error_type"] = type(error).__name__
            raise
        finally:
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_text(json.dumps(evidence, indent=2, default=str) + "\n")
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-out", type=Path, required=True)
    args = parser.parse_args()
    result = run(os.environ["UAP_DATABASE_URL"], args.evidence_out)
    print(json.dumps({"status": result["status"], "head": result["head"]}))


if __name__ == "__main__":
    main()
