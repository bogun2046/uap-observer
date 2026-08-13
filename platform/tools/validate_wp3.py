"""Validate the frozen WP3 migration and storage contract without services."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

SCHEMAS = {"ingest", "core", "ops", "audit", "public"}
ROLES = {
    "uap_owner",
    "uap_migrator",
    "uap_api",
    "uap_worker",
    "uap_scheduler",
    "uap_publisher",
    "uap_public_reader",
    "uap_audit_reader",
    "uap_backup",
}
WP3_REQUIRED = (
    "docs/wp3/README.md",
    "docs/wp3/acceptance-cases.md",
    "docs/wp3/acceptance-ticket.md",
    "docs/wp3/development-self-review.md",
    "docs/wp3/g3-rejection-record.md",
    "docs/wp3/g3-r2-remediation-report.md",
    "docs/wp3/implementation-ticket.md",
    "platform/alembic/env.py",
    "platform/alembic/versions/0001_roles_and_schemas.py",
    "platform/alembic/versions/0002_authoritative_schema.py",
    "platform/alembic/versions/0003_permissions_and_guards.py",
    "platform/alembic/versions/0004_g3_semantic_repairs.py",
    "platform/scripts/backup-platform.sh",
    "platform/scripts/deploy-staging.sh",
    "platform/scripts/restore-platform.sh",
    "platform/scripts/migrate-platform.sh",
    "platform/scripts/verify-migrator-failure-close.sh",
    "platform/scripts/verify-migration-chain.sh",
    "platform/src/uap_platform/object_registry.py",
    "platform/tools/object_backup.py",
    "platform/tools/build_wp3_evidence.py",
    "platform/tools/configure_roles.py",
    "platform/tools/wp3_runtime_probe.py",
)


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    actual: object
    expected: object


def result(name: str, passed: bool, actual: object, expected: object) -> Check:
    return Check(name, passed, actual, expected)


def dictionary_tables(repository: Path) -> set[str]:
    text = (repository / "docs/wp1/data-model.md").read_text(encoding="utf-8")
    names: set[str] = set()
    for heading in re.findall(r"^### (.+)$", text, flags=re.MULTILINE):
        names.update(
            name
            for name in re.findall(r"`((?:ingest|core|ops|audit|public)\.[a-z_]+)`", heading)
        )
    return names


def migration_tables(migration: Path) -> set[str]:
    text = migration.read_text(encoding="utf-8")
    return {
        f"{schema}.{table}"
        for schema, table in re.findall(
            r"CREATE TABLE (ingest|core|ops|audit|public)\.([a-z_]+)", text
        )
    }


def evaluate(platform: Path) -> list[Check]:
    platform = platform.resolve()
    repository = platform.parent
    config = Config(str(platform / "alembic.ini"))
    config.set_main_option("script_location", str(platform / "alembic"))
    script = ScriptDirectory.from_config(config)
    revisions = list(script.walk_revisions(base="base", head="heads"))
    heads = script.get_heads()
    migration = platform / "alembic/versions/0002_authoritative_schema.py"
    permissions = "\n".join(
        (platform / path).read_text(encoding="utf-8")
        for path in (
            "alembic/versions/0003_permissions_and_guards.py",
            "alembic/versions/0004_g3_semantic_repairs.py",
        )
    )
    role_source = (platform / "alembic/versions/0001_roles_and_schemas.py").read_text(
        encoding="utf-8"
    )
    lifecycle_source = "\n".join(
        (platform / path).read_text(encoding="utf-8")
        for path in (
            "scripts/migrate-platform.sh",
            "scripts/verify-migrator-failure-close.sh",
            "tools/configure_roles.py",
        )
    )
    evidence_source = (platform / "tools/build_wp3_evidence.py").read_text(encoding="utf-8")
    expected_tables = dictionary_tables(repository)
    actual_tables = migration_tables(migration)
    missing_files = [path for path in WP3_REQUIRED if not (repository / path).is_file()]
    checks = [
        result("required_delivery_files", not missing_files, missing_files, []),
        result("single_head", len(heads) == 1, heads, ["0004_g3_semantic_repairs"]),
        result(
            "linear_revision_chain",
            [revision.revision for revision in revisions]
            == [
                "0004_g3_semantic_repairs",
                "0003_permissions_and_guards",
                "0002_authoritative_schema",
                "0001_roles_and_schemas",
            ],
            [revision.revision for revision in revisions],
            "0004 -> 0003 -> 0002 -> 0001",
        ),
        result(
            "frozen_49_tables",
            actual_tables == expected_tables and len(actual_tables) == 49,
            {"count": len(actual_tables), "missing": sorted(expected_tables - actual_tables)},
            {"count": 49, "missing": []},
        ),
        result(
            "five_schemas",
            all(f"{schema}." in migration.read_text(encoding="utf-8") for schema in SCHEMAS),
            sorted(SCHEMAS),
            sorted(SCHEMAS),
        ),
        result(
            "least_privilege_roles",
            all(role in role_source + permissions for role in ROLES)
            and "ON ALL TABLES IN SCHEMA public TO uap_worker" not in permissions,
            sorted(ROLES),
            sorted(ROLES),
        ),
        result(
            "semantic_guards",
            all(
                token in permissions
                for token in (
                    "artifact_versions_append_only",
                    "analysis_results_append_only",
                    "review_decisions_append_only",
                    "audit_events_append_only",
                    "validate_publication_grant",
                    "require_claim_has_evidence",
                    "ck_evidence_locator_fields",
                    "require_document_entity_revision_match",
                    "require_linked_document_entity_revision_match",
                )
            ),
            True,
            True,
        ),
        result(
            "migrator_window_closes",
            all(
                token in lifecycle_source
                for token in (
                    "enable-migrator",
                    "disable-migrator",
                    "trap close_migrator EXIT HUP INT TERM",
                    "ALTER ROLE uap_migrator {}",
                    "migration failure left uap_migrator LOGIN enabled",
                )
            ),
            True,
            True,
        ),
        result(
            "evidence_manifest_scope",
            all(path in evidence_source for path in WP3_REQUIRED),
            [path for path in WP3_REQUIRED if path not in evidence_source],
            [],
        ),
    ]
    return checks


def main() -> None:
    checks = evaluate(Path(__file__).resolve().parents[1])
    print(json.dumps([asdict(check) for check in checks], indent=2, sort_keys=True))
    failed = [check.name for check in checks if not check.passed]
    if failed:
        raise SystemExit(f"WP3 contract checks failed: {', '.join(failed)}")
    print(f"WP3 contract checks passed: {len(checks)}/{len(checks)}")


if __name__ == "__main__":
    main()
