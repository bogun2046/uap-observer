"""Validate the frozen WP7 AI model-governance contract without external services."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    actual: object
    expected: object


def check(name: str, passed: bool, actual: object, expected: object = True) -> Check:
    return Check(name, passed, actual, expected)


def evaluate(platform: Path) -> list[Check]:
    platform = platform.resolve()
    repository = platform.parent
    required = (
        "docs/wp7/implementation-ticket.md",
        "docs/wp7/acceptance-ticket.md",
        "docs/wp7/development-self-review.md",
        "platform/alembic/versions/0001_roles_and_schemas.py",
        "platform/alembic/versions/0008_ai_model_governance.py",
        "platform/alembic/versions/0009_model_governance_boundaries.py",
        "platform/src/uap_platform/model_governance/__init__.py",
        "platform/src/uap_platform/model_governance/contracts.py",
        "platform/src/uap_platform/model_governance/providers.py",
        "platform/src/uap_platform/model_governance/persistence.py",
        "platform/src/uap_platform/model_governance/schemas.py",
        "platform/src/uap_platform/model_governance/workflow.py",
        "platform/tests/test_model_governance.py",
        "platform/tools/wp7_runtime_probe.py",
        "platform/tools/build_wp7_evidence.py",
        "platform/tools/configure_roles.py",
        "platform/tools/ensure_model_governance_role.py",
        "platform/scripts/bootstrap-env.sh",
        "platform/.env.example",
        "platform/scripts/migrate-platform.sh",
        "platform/compose.yaml",
        "platform/Dockerfile",
    )
    missing = [path for path in required if not (repository / path).is_file()]
    source = "\n".join(
        (repository / path).read_text(encoding="utf-8")
        for path in required
        if (repository / path).is_file() and not path.endswith(".md")
    )
    acceptance_path = repository / "docs/wp7/acceptance-ticket.md"
    acceptance = acceptance_path.read_text(encoding="utf-8") if acceptance_path.is_file() else ""
    migration_sources = (
        repository / "platform/alembic/versions/0008_ai_model_governance.py",
        repository / "platform/alembic/versions/0009_model_governance_boundaries.py",
    )
    migration_source = "\n".join(
        path.read_text(encoding="utf-8") for path in migration_sources if path.is_file()
    )
    return [
        check("required_files", not missing, missing, []),
        check(
            "frozen_cases",
            all(f"G7-0{i}" in acceptance for i in range(1, 9)),
            [f"G7-0{i}" for i in range(1, 9) if f"G7-0{i}" not in acceptance],
            [],
        ),
        check(
            "versioned_contract",
            all(
                token in source
                for token in (
                    "MODEL_PAYLOAD_SCHEMA_VERSION",
                    'model.v1',
                    "ModelTaskType",
                    "ProviderError",
                )
            ),
            True,
        ),
        check(
            "strict_schema_boundary",
            all(
                token in source
                for token in ("extra=\"forbid\"", "validate_output", "schema_validation_failed")
            ),
            True,
        ),
        check(
            "append_only_provenance",
            all(
                token in source
                for token in (
                    "PostgresModelGovernanceStore",
                    "ops.model_runs",
                    "core.analysis_results",
                    "StorageDomain.MODEL_IO",
                    "idempotency_key",
                    "semantic_idempotency_key",
                    "acquire_semantic_request",
                    "MODEL_MAX_INPUT_BYTES",
                    "MODEL_MAX_OUTPUT_BYTES",
                    "MODEL_MAX_CALLS_PER_SEMANTIC_KEY",
                    "MODEL_MAX_COST_MINOR_UNITS",
                    "finish_model_job",
                    "ON CONFLICT",
                )
            ),
            True,
        ),
        check(
            "job_lifecycle_and_errors",
            all(
                token in source
                for token in (
                    "ops.finish_job",
                    "retryable_failure",
                    "terminal_failure",
                    "finish_job_only",
                    "ProviderError",
                    "401",
                    "403",
                    "model provider upstream failure",
                )
            ),
            True,
        ),
        check(
            "migration_constraints",
            all(
                token in migration_source
                for token in (
                    "0008_ai_model_governance",
                    "0009_model_governance_boundaries",
                    "uq_prompt_versions_active_task",
                    "fk_model_run_prompt_same_task",
                    "ck_model_run_lifecycle",
                    "ck_model_run_currency",
                    "uq_model_runs_semantic_success",
                    "finish_model_job",
                    "guard_model_governance_object_domain",
                    "uap_model_governance",
                    "REVOKE CONNECT",
                    "REVOKE USAGE ON SCHEMA core, ops",
                )
            ),
            True,
        ),
        check(
            "required_ci_probe",
            all(
                token in (repository / ".github/workflows/platform-ci.yml").read_text(
                    encoding="utf-8"
                )
                for token in ("validate_wp7.py", "build_wp7_evidence.py", "wp7_runtime_probe.py")
            ),
            True,
        ),
    ]


def main() -> None:
    checks = evaluate(Path(__file__).resolve().parents[1])
    print(json.dumps([asdict(item) for item in checks], indent=2, sort_keys=True))
    failed = [item.name for item in checks if not item.passed]
    if failed:
        raise SystemExit(f"WP7 contract checks failed: {', '.join(failed)}")
    print(f"WP7 contract checks passed: {len(checks)}/{len(checks)}")


if __name__ == "__main__":
    main()
