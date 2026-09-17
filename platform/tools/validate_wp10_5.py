"""Static validator for the frozen WP10.5 Admin API scope."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

HEAD = "0024_wp10_admin_replay"
PARENT = "0023_wp10_api_read_indexes"
SIGNED_SHA = "19ee4f436179740c002f05f597e456d42b1a17db"
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
    "permissions.md": "8c1ff2a849454dd288545177fb97f61c5549f246b17610a1ce84986b952ea2ed",
    "error-codes.md": "91ea691ee205b9b0c251ec2bda1378c462d2e9a3839c884175a59ed6fa0d90d2",
    "projection-contract.md": "afde58255fc53d8291ceb32987fc7347949dc32c9b2972cca9b804cabcdb3ad9",
    "adr/0023-http-consistency-and-auth.md": (
        "01f2035e4596dee5f6104ce9eba459585f0fd021855449f77d9c4d4300453b99"
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
    migration_path = platform / "alembic/versions/0024_wp10_admin_replay.py"
    migration = migration_path.read_text(encoding="utf-8") if migration_path.is_file() else ""
    package = platform / "src/uap_platform/admin_api"
    required_files = (
        migration_path,
        package / "contracts.py",
        package / "config.py",
        package / "cursor.py",
        package / "oidc.py",
        package / "errors.py",
        package / "pool.py",
        package / "service.py",
        package / "handler.py",
        package / "server.py",
        platform / "tools/wp10_5_migration_probe.py",
        platform / "tools/wp10_5_runtime_probe.py",
        platform / "tests/test_wp10_5_admin_api.py",
        platform / "tests/test_wp10_5_oidc.py",
        docs / "implementation-start-wp10.5.md",
        docs / "wp10.5-migration-evidence.json",
        docs / "wp10.5-runtime-evidence.json",
        docs / "wp10.5-w-matrix-evidence.json",
    )
    config = Config(str(platform / "alembic.ini"))
    config.set_main_option("script_location", str(platform / "alembic"))
    script = ScriptDirectory.from_config(config)
    revision = script.get_revision(HEAD)
    service = (
        (package / "service.py").read_text(encoding="utf-8")
        if (package / "service.py").is_file()
        else ""
    )
    handler = (
        (package / "handler.py").read_text(encoding="utf-8")
        if (package / "handler.py").is_file()
        else ""
    )
    server = (
        (package / "server.py").read_text(encoding="utf-8")
        if (package / "server.py").is_file()
        else ""
    )
    config_source = (
        (package / "config.py").read_text(encoding="utf-8")
        if (package / "config.py").is_file()
        else ""
    )
    oidc_source = (
        (package / "oidc.py").read_text(encoding="utf-8") if (package / "oidc.py").is_file() else ""
    )
    runtime_probe = (
        (platform / "tools/wp10_5_runtime_probe.py").read_text(encoding="utf-8")
        if (platform / "tools/wp10_5_runtime_probe.py").is_file()
        else ""
    )
    start = (
        (docs / "implementation-start-wp10.5.md").read_text(encoding="utf-8")
        if (docs / "implementation-start-wp10.5.md").is_file()
        else ""
    )
    frozen_actual = {
        name: _sha256(docs / name) if (docs / name).is_file() else None
        for name in FROZEN_DOC_HASHES
    }
    evidence_ok = False
    matrix_ok = False
    migration_ok = False
    if (docs / "wp10.5-runtime-evidence.json").is_file():
        runtime_evidence = json.loads((docs / "wp10.5-runtime-evidence.json").read_text())
        evidence_ok = runtime_evidence.get("status") == "passed" and not any(
            item.get("passed") is False for item in runtime_evidence.get("checks", [])
        )
    if (docs / "wp10.5-w-matrix-evidence.json").is_file():
        matrix = json.loads((docs / "wp10.5-w-matrix-evidence.json").read_text())
        instances = matrix.get("instances", [])
        ids = {item.get("id") for item in instances}
        expected_ids = {
            f"G10-W{index:02d}-{kind}"
            for index in range(1, 12)
            for kind in ("S", "P", "M", "R", "C", "T")
        }
        evidence_ok_items = True
        for item in instances:
            before = item.get("before")
            after = item.get("after")
            if item.get("passed") is not True:
                evidence_ok_items = False
            if (
                not isinstance(before, dict)
                or not isinstance(after, dict)
                or not before
                or not after
            ):
                evidence_ok_items = False
            kind = str(item.get("id") or "").rsplit("-", 1)[-1]
            if kind == "T":
                if item.get("unchanged") is not True or item.get("retry_status") != 200:
                    evidence_ok_items = False
                if not item.get("inject_tables"):
                    evidence_ok_items = False
                if not item.get("retry_before") or not item.get("retry_after"):
                    evidence_ok_items = False
        matrix_ok = (
            matrix.get("status") == "passed"
            and ids == expected_ids
            and len(instances) == 66
            and evidence_ok_items
        )
    if (docs / "wp10.5-migration-evidence.json").is_file():
        migration_evidence = json.loads((docs / "wp10.5-migration-evidence.json").read_text())
        migration_ok = (
            migration_evidence.get("status") == "passed"
            and migration_evidence.get("schema") == "wp10.5-migration-evidence.v1"
        )

    route_tokens = (
        "/admin/v1/review-cases",
        "/admin/v1/analysis-results",
        "/admin/v1/entity-candidates",
        "/admin/v1/evidence-spans",
        "/admin/v1/entities",
        "/admin/v1/publication-events",
        "/admin/v1/claims/manual",
        "/admin/v1/entities/merges",
        "/admin/v1/grants",
        "api_capability_closed",
        "Authorization",
        "Bearer",
    )
    missing_routes = [token for token in route_tokens if token not in handler]
    coverage_tokens = (
        "G10-21",
        "G10-22",
        "G10-23",
        "G10-24",
        "G10-W01-S",
        "G10-W11-T",
        "42501",
        "core.merge_entities",
        "requeue_publication_event",
    )
    missing_coverage = [token for token in coverage_tokens if token not in runtime_probe]

    return [
        check("single WP10.5 head", script.get_heads() == [HEAD], script.get_heads(), [HEAD]),
        check(
            "WP10.5 parent",
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
            "0024 is replay-function only",
            "CREATE FUNCTION audit.requeue_publication_event" in migration
            and "CREATE TABLE" not in migration
            and "core.merge_entities" not in migration
            and "core.reverse_entity_merge" not in migration,
            migration_path.as_posix(),
        ),
        check(
            "admin routes and closed grant/relation surface",
            not missing_routes,
            missing_routes,
            [],
        ),
        check(
            "write wrappers only, no bottom SQL",
            all(
                token in service
                for token in (
                    "open_review_case",
                    "assign_review_case",
                    "close_review_case",
                    "record_review_decision",
                    "select_analysis_result",
                    "accept_entity_candidate",
                    "bind_entity_candidate",
                    "apply_entity_merge",
                    "apply_entity_merge_reverse",
                    "create_manual_claim",
                    "audit.requeue_publication_event",
                )
            )
            and "INSERT INTO" not in service
            and "DELETE FROM" not in service
            and "core.merge_entities" not in service
            and "enqueue_publication_outbox" not in service,
            True,
        ),
        check(
            "uap_api-only isolated configuration",
            "uap_api" in config_source
            and all(
                token in config_source
                for token in (
                    "UAP_PUBLIC_DATABASE_URL",
                    "UAP_PUBLISHER_DATABASE_URL",
                    "UAP_WORKER_DATABASE_URL",
                    "UAP_OWNER_DATABASE_URL",
                )
            )
            and "uap_public_reader" in config_source,
            True,
        ),
        check(
            "strict OIDC allowlist",
            "RS256" in oidc_source
            and "ALLOWED_ALGS" in oidc_source
            and "api_token_invalid" in oidc_source,
            True,
        ),
        check(
            "dedicated process entrypoint",
            "ThreadingHTTPServer" in server
            and 'uap-platform-admin-api = "uap_platform.admin_api.server:main"'
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
                    "codex/wp10.5",
                    "G10-GATE-10.5",
                    "WP10.6",
                    "Do not sign",
                )
            ),
            True,
        ),
        check("migration evidence passed", migration_ok, migration_ok),
        check("runtime evidence passed", evidence_ok, evidence_ok),
        check("W-matrix 66 instances passed", matrix_ok, matrix_ok),
        check(
            "G10-21 through G10-24 and W matrix coverage",
            not missing_coverage,
            missing_coverage,
            [],
        ),
        check(
            "forbidden stage scope absent",
            not any(
                token in (service + handler + server).lower()
                for token in ("graphql", "websocket", "text/event-stream", "csrf")
            )
            and "/v1/search" not in handler,
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
        raise SystemExit("WP10.5 validation failed: " + ", ".join(failed))
    print("WP10.5 static contract validation passed; run migration/runtime probes.")


if __name__ == "__main__":
    main()
