"""Static validator for the frozen WP10.4 public read API scope."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

HEAD = "0023_wp10_api_read_indexes"
WP10_5_HEAD = "0024_wp10_admin_replay"
PARENT = "0022_wp10_claim_search_projection"
SIGNED_SHA = "10773f135ef5fca236db2eebee75fd6b15f3cd0f"
HISTORICAL_IMPLEMENTATION_TICKET_SHA256 = (
    "4400c698445466cbdf8338af67df451e7616b4c3ccdb43d5dafeb826324af3d9"
)
WP106_D_IMPLEMENTATION_TICKET_SHA256 = (
    "be8fc8b99d3d50e403c6725902a330fd4411c65628c6ff5af44549c03859369d"
)
APPROVED_IMPLEMENTATION_TICKET_HASHES = frozenset(
    {HISTORICAL_IMPLEMENTATION_TICKET_SHA256, WP106_D_IMPLEMENTATION_TICKET_SHA256}
)
FROZEN_DOC_HASHES = {
    "implementation-ticket.md": HISTORICAL_IMPLEMENTATION_TICKET_SHA256,
    "api-contract.md": "6842d7d09cae3dbaba1740827bfb1c2e72d5a43a529f2da1bacf5b705a4e25a1",
    "acceptance-cases.md": "f262cfaed3372c155bf8684561721a4d8ecf4ea1a9743fd5f5d0de51743fb7b4",
    "migration-plan.md": "3b8e4eb3096ed4e16e29e8b86af1813677b1534f13352c8f0c8b8ec4e67ce27b",
    "permissions.md": "8c1ff2a849454dd288545177fb97f61c5549f246b17610a1ce84986b952ea2ed",
    "error-codes.md": "91ea691ee205b9b0c251ec2bda1378c462d2e9a3839c884175a59ed6fa0d90d2",
    "adr/0024-relations-remain-closed.md": (
        "685ed0c06af1c8a4bbc64e615301254b07d20b172cda2e26fd7b1717165438e2"
    ),
}


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    actual: object
    expected: object = True


def check(name: str, passed: bool, actual: object, expected: object = True) -> Check:
    return Check(name, passed, actual, expected)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def frozen_docs_are_valid(actual: Mapping[str, str | None]) -> bool:
    return actual.get("implementation-ticket.md") in APPROVED_IMPLEMENTATION_TICKET_HASHES and all(
        actual.get(name) == expected
        for name, expected in FROZEN_DOC_HASHES.items()
        if name != "implementation-ticket.md"
    )


def evaluate(platform: Path) -> list[Check]:
    platform = platform.resolve()
    repository = platform.parent
    docs = repository / "docs/wp10"
    migration_path = platform / "alembic/versions/0023_wp10_api_read_indexes.py"
    migration = migration_path.read_text(encoding="utf-8") if migration_path.is_file() else ""
    package = platform / "src/uap_platform/public_api"
    required_files = (
        migration_path,
        package / "contracts.py",
        package / "config.py",
        package / "cursor.py",
        package / "pool.py",
        package / "service.py",
        package / "handler.py",
        package / "server.py",
        platform / "tools/wp10_4_migration_probe.py",
        platform / "tools/wp10_4_runtime_probe.py",
        platform / "tests/test_wp10_4_public_api.py",
        docs / "implementation-start-wp10.4.md",
        docs / "wp10.4-migration-evidence.json",
        docs / "wp10.4-runtime-evidence.json",
    )
    config = Config(str(platform / "alembic.ini"))
    config.set_main_option("script_location", str(platform / "alembic"))
    script = ScriptDirectory.from_config(config)
    revision = script.get_revision(HEAD)
    service = (package / "service.py").read_text(encoding="utf-8")
    handler = (package / "handler.py").read_text(encoding="utf-8")
    server = (package / "server.py").read_text(encoding="utf-8")
    config_source = (package / "config.py").read_text(encoding="utf-8")
    runtime_probe = (platform / "tools/wp10_4_runtime_probe.py").read_text(encoding="utf-8")
    start = (docs / "implementation-start-wp10.4.md").read_text(encoding="utf-8")
    migration_evidence = json.loads((docs / "wp10.4-migration-evidence.json").read_text())
    runtime_evidence = json.loads((docs / "wp10.4-runtime-evidence.json").read_text())
    frozen_actual = {name: _sha256(docs / name) for name in FROZEN_DOC_HASHES}

    return [
        check(
            "single WP10.4 head",
            script.get_heads() in ([HEAD], [WP10_5_HEAD]),
            script.get_heads(),
            [[HEAD], [WP10_5_HEAD]],
        ),
        check(
            "WP10.4 parent",
            revision is not None and revision.down_revision == PARENT,
            revision.down_revision if revision else None,
            PARENT,
        ),
        check(
            "required implementation and evidence files",
            all(path.is_file() for path in required_files),
            [str(path) for path in required_files if not path.is_file()],
            [],
        ),
        check(
            "0023 is index-only and reversible",
            migration.count("CREATE INDEX") == 6
            and migration.count("DROP INDEX") == 6
            and not any(
                token in migration
                for token in (
                    "CREATE TABLE",
                    "ALTER TABLE",
                    "CREATE TYPE",
                    "INSERT INTO",
                    "UPDATE ",
                    "DELETE FROM",
                )
            ),
            migration_path.as_posix(),
        ),
        check(
            "six anonymous GET routes and relation closed fallback",
            all(
                token in handler
                for token in (
                    "/v1/documents",
                    "/v1/claims/",
                    "/v1/entities",
                    "/v1/search",
                    "/v1/relations",
                    "/v1/relations/",
                    "/healthz",
                    "api_capability_closed",
                )
            ),
            True,
        ),
        check(
            "public-only parameterized query surface",
            "websearch_to_tsquery('simple', %s)" in service
            and "public.search_documents" in service
            and not any(f"{schema}." in service for schema in ("ingest", "core", "ops", "audit"))
            and "public.relations" not in service
            and ' f"""' not in service,
            True,
        ),
        check(
            "reader-only isolated configuration",
            "uap_public_reader" in config_source
            and all(
                token in config_source
                for token in (
                    "UAP_API_DATABASE_URL",
                    "UAP_PUBLISHER_DATABASE_URL",
                    "UAP_WORKER_DATABASE_URL",
                    "UAP_OWNER_DATABASE_URL",
                )
            )
            and "READ ONLY" in (package / "pool.py").read_text(encoding="utf-8"),
            True,
        ),
        check(
            "dedicated process entrypoint",
            "ThreadingHTTPServer" in server
            and 'uap-platform-public-api = "uap_platform.public_api.server:main"'
            in (platform / "pyproject.toml").read_text(encoding="utf-8"),
            True,
        ),
        check(
            "signed frozen docs unchanged",
            frozen_docs_are_valid(frozen_actual),
            frozen_actual,
            {
                **FROZEN_DOC_HASHES,
                "implementation-ticket.md": sorted(APPROVED_IMPLEMENTATION_TICKET_HASHES),
            },
        ),
        check(
            "signed start and stop line",
            all(
                token in start
                for token in (
                    SIGNED_SHA,
                    "codex/wp10.4",
                    "不签署 `G10-GATE-10.4`",
                    "不进入 WP10.5",
                    "不 push",
                )
            ),
            start,
        ),
        check(
            "migration evidence passed",
            migration_evidence.get("status") == "passed"
            and migration_evidence.get("schema") == "wp10.4-migration-evidence.v1",
            migration_evidence.get("status"),
        ),
        check(
            "runtime evidence passed without failed assertions",
            runtime_evidence.get("status") == "passed"
            and not any(item.get("passed") is False for item in runtime_evidence.get("checks", [])),
            {
                "status": runtime_evidence.get("status"),
                "checks": len(runtime_evidence.get("checks", [])),
            },
        ),
        check(
            "G10-16 through G10-20 runtime coverage",
            all(
                token in runtime_probe
                for token in (
                    "G10-16",
                    "G10-17",
                    "G10-18",
                    "G10-19",
                    "G10-20",
                    "42501",
                    "publication.granted",
                    "withdraw_document",
                    "pagination_mutation",
                    "capacity_fixture",
                    "withdraw_delete_count",
                    "before_page",
                    "inserted_rank",
                )
            ),
            True,
        ),
        check(
            "forbidden stage scope absent",
            not any(
                token in (service + handler + server).lower()
                for token in (
                    "fastapi",
                    "oidc",
                    "jwt",
                    "bearer",
                    "admin/v1",
                    "post /",
                    "put /",
                    "delete /",
                )
            ),
            True,
        ),
    ]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("platform", nargs="?", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    checks = evaluate(Path(args.platform))
    print(json.dumps([asdict(item) for item in checks], ensure_ascii=False, indent=2))
    failed = [item.name for item in checks if not item.passed]
    if failed:
        raise SystemExit("WP10.4 validation failed: " + ", ".join(failed))
    print("WP10.4 static contract validation passed; run migration/runtime probes.")


if __name__ == "__main__":
    main()
