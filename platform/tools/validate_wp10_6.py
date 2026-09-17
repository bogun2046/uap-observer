"""Static contract validator for WP10.6-B/C / G10-26/G10-27.

This validator checks that the live matrix implementation is present and
fail-closed.  It never turns a static check into live evidence; the migration
probe must be run separately against disposable PostgreSQL 16 databases.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from tools.validate_wp10 import classify_git_paths, git_changed_paths

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PLATFORM_ROOT.parent
START_SHA = "34c57bcadfeb67053c4c47f8cde237a3af185ba8"
PROBE = PLATFORM_ROOT / "tools/wp10_6_migration_probe.py"
PERFORMANCE_PROBE = PLATFORM_ROOT / "tools/wp10_6_performance_probe.py"
MIGRATION_0020 = PLATFORM_ROOT / "alembic/versions/0020_wp10_publication_contract.py"
WORKFLOW = REPOSITORY_ROOT / ".github/workflows/platform-ci.yml"
MAKEFILE = PLATFORM_ROOT / "Makefile"
EXPECTED_SCENARIO_COUNT = 14
REQUIRED_TOKENS = (
    "publication_contract_state_blocks_downgrade",
    'EXPECTED_SQLSTATE = "22023"',
    '"before"',
    '"after"',
    '"not_run"',
    "temporary_databases_remaining",
    "session_replication_role",
    "drop_database",
    "set_migrator_login",
    "run_acl_roundtrip",
    '"A_native_0019"',
    '"C_downgraded_0019"',
    '"E_native_0024"',
    '"C_minus_A"',
    '"D_minus_B"',
    "real_role_permissions",
)


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    actual: object
    expected: object = True


def _check(name: str, passed: bool, actual: object, expected: object = True) -> Check:
    return Check(name, passed, actual, expected)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def changed_paths() -> list[str]:
    git = shutil.which("git")
    if git is None:
        return []
    try:
        diff = subprocess.run(  # noqa: S603
            [
                git,
                "-c",
                f"safe.directory={REPOSITORY_ROOT}",
                "diff",
                "--name-only",
                START_SHA,
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        untracked = subprocess.run(  # noqa: S603
            [
                git,
                "-c",
                f"safe.directory={REPOSITORY_ROOT}",
                "ls-files",
                "--others",
                "--exclude-standard",
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return []
    return sorted(
        {
            line.strip()
            for line in diff.stdout.splitlines() + untracked.stdout.splitlines()
            if line.strip()
        }
    )


def evaluate(platform: Path = PLATFORM_ROOT, *, paths: list[str] | None = None) -> list[Check]:
    platform = platform.resolve()
    probe = _read(platform / "tools/wp10_6_migration_probe.py")
    performance_probe = _read(platform / "tools/wp10_6_performance_probe.py")
    migration_0020 = _read(platform / "alembic/versions/0020_wp10_publication_contract.py")
    workflow = _read(platform.parent / ".github/workflows/platform-ci.yml")
    makefile = _read(platform / "Makefile")
    scenarios = re.findall(r'^    "([a-z0-9_]+)",$', probe, flags=re.M)
    migration_files = [path.name for path in (platform / "alembic/versions").glob("*.py")]
    path_status, path_extra = (
        git_changed_paths(platform.parent) if paths is None else classify_git_paths(paths)
    )
    return [
        _check("G10-26 probe exists", PROBE.is_file(), str(PROBE)),
        _check(
            "14 matrix scenario IDs",
            len(scenarios) == EXPECTED_SCENARIO_COUNT,
            len(scenarios),
            EXPECTED_SCENARIO_COUNT,
        ),
        _check("matrix scenario IDs are unique", len(scenarios) == len(set(scenarios)), scenarios),
        _check(
            "probe records frozen failure and digest evidence",
            all(token in probe for token in REQUIRED_TOKENS),
            [token for token in REQUIRED_TOKENS if token not in probe],
        ),
        _check(
            "probe emits machine-readable evidence",
            "json.dumps" in probe and "started_at" in probe and "finished_at" in probe,
            True,
        ),
        _check(
            "probe owns one disposable database per variant",
            "uap_g10_26_" in probe and "drop_database(admin_url, name)" in probe,
            True,
        ),
        _check(
            "no 0025 migration",
            not any(name.startswith("0025") for name in migration_files),
            migration_files,
        ),
        _check(
            "0020 downgrade restores only exact 0019 direct DML",
            "GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA core, ops TO uap_api"
            not in migration_0020[migration_0020.index("def downgrade()") :]
            and (
                "GRANT INSERT, UPDATE ON\n"
                "            core.stored_objects, core.documents, core.document_versions, "
                "core.extractions\n"
                "            TO uap_api;"
            )
            in migration_0020,
            True,
        ),
        _check(
            "0020 downgrade creates no uap_api core/ops/audit default DML",
            "ALTER DEFAULT PRIVILEGES FOR ROLE uap_owner IN SCHEMA core, ops, audit\n"
            "            GRANT INSERT, UPDATE, DELETE ON TABLES TO uap_api;"
            not in migration_0020[migration_0020.index("def downgrade()") :],
            True,
        ),
        _check(
            "Makefile wires G10-26 live probe",
            "wp10-6-b-migration:" in makefile
            and "tools.wp10_6_migration_probe" in makefile
            and "UAP_WP10_6_ADMIN_URL" in makefile,
            True,
        ),
        _check(
            "CI wires G10-26 evidence",
            "wp10_6_migration_probe" in workflow
            and "UAP_WP10_6_ADMIN_URL" in workflow
            and "wp10-6-migration-evidence" in workflow,
            True,
        ),
        _check(
            "CI does not pass DSN as probe argv",
            "--admin-url" not in workflow,
            "--admin-url" not in workflow,
            True,
        ),
        _check(
            "G10-27 probe freezes capacity and latency thresholds",
            all(
                token in performance_probe
                for token in (
                    "DEFAULT_DOCUMENTS = 100_000",
                    "DOCUMENT_P95_MS = 300.0",
                    "DOCUMENT_P99_MS = 800.0",
                    "SEARCH_P95_MS = 500.0",
                    "SEARCH_P99_MS = 1200.0",
                    "FRESHNESS_P95_SECONDS = 120.0",
                )
            ),
            True,
        ),
        _check(
            "G10-27 uses real grant rebuild Publisher and HTTP paths",
            all(
                token in performance_probe
                for token in (
                    "audit.record_review_decision",
                    "ops.rebuild_public_projection",
                    "uap_platform.publishing.loop",
                    "uap_platform.public_api.server",
                    "latency-samples.jsonl",
                    "freshness-samples.jsonl",
                    "EXPLAIN (ANALYZE, BUFFERS, SETTINGS, FORMAT JSON)",
                )
            ),
            True,
        ),
        _check(
            "G10-27 does not disable integrity or seed public projection directly",
            "session_replication_role" not in performance_probe
            and "DISABLE TRIGGER" not in performance_probe
            and "COPY public." not in performance_probe
            and "INSERT INTO public.documents" not in performance_probe,
            True,
        ),
        _check(
            "WP10.6 changes stay in the authorized surface",
            path_status == "allowed",
            {"status": path_status, "extra": path_extra},
            "allowed",
        ),
    ]


def main() -> int:
    checks = evaluate()
    output = [
        {"name": item.name, "passed": item.passed, "actual": item.actual, "expected": item.expected}
        for item in checks
    ]
    print(
        json.dumps(
            {
                "schema": "wp10.6-b-validator.v1",
                "passed": all(item.passed for item in checks),
                "checks": output,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    return 0 if all(item.passed for item in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
