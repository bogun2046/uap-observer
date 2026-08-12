"""Perform semantic validation of the frozen WP1 design package."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REQUIRED_FILES = (
    "README.md",
    "acceptance-cases.md",
    "acceptance-record-template.md",
    "architecture.md",
    "data-model.md",
    "development-self-review.md",
    "development-self-review-r2.md",
    "development-self-review-r3.md",
    "g1-rejection-record.md",
    "g1-remediation-report.md",
    "g1-remediation-round2-report.md",
    "g1-remediation-round3-report.md",
    "g1-second-rejection-record.md",
    "g1-third-rejection-record.md",
    "module-boundaries.md",
    "openapi-examples.json",
    "openapi.yaml",
    "permissions.md",
    "service-targets.md",
    "adr/0001-modular-monolith-and-workers.md",
    "adr/0002-postgresql-and-single-migration-authority.md",
    "adr/0003-content-addressed-object-storage.md",
    "adr/0004-durable-jobs-and-transactional-outbox.md",
    "adr/0005-append-only-model-results.md",
    "adr/0006-separate-public-read-model.md",
    "adr/0007-postgresql-full-text-search-first.md",
)
DETAIL_SCHEMAS = ("DocumentDetail", "EntityDetail", "ReviewCaseDetail")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def count(pattern: str, text: str) -> int:
    return len(re.findall(pattern, text, flags=re.MULTILINE))


def check(name: str, passed: bool, actual: Any, expected: Any) -> dict[str, Any]:
    return {"name": name, "passed": passed, "actual": actual, "expected": expected}


def matrix_public_cell(markdown: str, role: str) -> str | None:
    prefix = f"| `{role}` |"
    line = next((item for item in markdown.splitlines() if item.startswith(prefix)), None)
    if line is None:
        return None
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return cells[-1] if len(cells) == 6 else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--docs", type=Path, default=Path("docs/wp1"))
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/wp1-design-remediation-r3-20260812")
    )
    parser.add_argument(
        "--schema-tools",
        type=Path,
        default=Path(os.environ.get("WP1_SCHEMA_TOOLS", "/private/tmp/uap-wp1-schema-tools")),
        help="Directory populated from tools/requirements-wp1-validation.txt",
    )
    return parser.parse_args()


def load_schema_tools(path: Path) -> tuple[Any, Any, Any, Any]:
    sys.path.insert(0, str(path))
    import yaml  # type: ignore[import-not-found]
    from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-not-found]
    from openapi_spec_validator import validate  # type: ignore[import-not-found]

    return yaml, Draft202012Validator, FormatChecker, validate


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    docs = root / args.docs
    output = root / args.output
    output.mkdir(parents=True, exist_ok=True)
    checks: list[dict[str, Any]] = []

    missing = [relative for relative in REQUIRED_FILES if not (docs / relative).is_file()]
    checks.append(check("required_files", not missing, missing, []))

    data_model = (docs / "data-model.md").read_text(encoding="utf-8")
    table_headings = re.findall(r"^### (.+)$", data_model, flags=re.MULTILINE)
    logical_tables = sorted(
        table
        for heading in table_headings
        for table in re.findall(r"`([a-z_]+\.[a-z_]+)`", heading)
    )
    table_sections = len(table_headings)
    owner_entries = count(r"^- 所有者：", data_model)
    primary_key_entries = count(r"^- 主键：", data_model)
    unique_entries = count(r"^- 唯一约束：", data_model)
    checks.extend(
        [
            check("logical_table_count", len(set(logical_tables)) == 49, len(set(logical_tables)), 49),
            check(
                "logical_table_names_unique",
                len(logical_tables) == len(set(logical_tables)),
                len(logical_tables) - len(set(logical_tables)),
                0,
            ),
            check("table_owner_coverage", owner_entries == table_sections, owner_entries, table_sections),
            check(
                "table_primary_key_coverage",
                primary_key_entries == table_sections,
                primary_key_entries,
                table_sections,
            ),
            check(
                "table_unique_constraint_coverage",
                unique_entries == table_sections,
                unique_entries,
                table_sections,
            ),
        ]
    )

    openapi_text = (docs / "openapi.yaml").read_text(encoding="utf-8")
    public_paths = count(r"^  /v1/", openapi_text)
    admin_paths = count(r"^  /admin/", openapi_text)
    forbidden_fields = re.findall(
        r"^\s+(raw_content|extracted_content|object_key|provider_response_id|"
        r"reviewer_id|input_tokens|output_tokens|cost_minor_units):",
        openapi_text,
        flags=re.MULTILINE,
    )
    checks.extend(
        [
            check("openapi_version", openapi_text.startswith("openapi: 3.1.0\n"), True, True),
            check("public_api_paths", public_paths == 7, public_paths, 7),
            check("admin_api_paths", admin_paths == 5, admin_paths, 5),
            check("forbidden_api_fields", not forbidden_fields, forbidden_fields, []),
        ]
    )

    schema_tool_error = None
    schema_versions: dict[str, str] = {}
    try:
        yaml, draft_validator, format_checker, validate_openapi = load_schema_tools(
            args.schema_tools
        )
        schema_versions = {
            package: importlib.metadata.version(package)
            for package in ("PyYAML", "jsonschema", "openapi-spec-validator")
        }
        spec = yaml.safe_load(openapi_text)
        validate_openapi(spec)
        checks.append(check("openapi_spec_validation", True, "valid OpenAPI 3.1", True))

        examples = json.loads((docs / "openapi-examples.json").read_text(encoding="utf-8"))
        positive_errors: dict[str, list[str]] = {}
        negative_rejections: dict[str, bool] = {}
        for schema_name in DETAIL_SCHEMAS:
            schema = {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$ref": f"#/components/schemas/{schema_name}",
                "components": spec["components"],
            }
            draft_validator.check_schema(schema)
            validator = draft_validator(
                schema,
                format_checker=format_checker(),
            )
            positive_errors[schema_name] = [
                error.message for error in validator.iter_errors(examples[schema_name])
            ]
            invalid = copy.deepcopy(examples[schema_name])
            invalid["unexpected_internal_field"] = "must be rejected"
            negative_rejections[schema_name] = bool(list(validator.iter_errors(invalid)))
        checks.append(
            check(
                "detail_schema_positive_instances",
                all(not errors for errors in positive_errors.values()),
                positive_errors,
                {name: [] for name in DETAIL_SCHEMAS},
            )
        )
        checks.append(
            check(
                "detail_schema_additional_property_rejection",
                all(negative_rejections.values()),
                negative_rejections,
                {name: True for name in DETAIL_SCHEMAS},
            )
        )
    except Exception as error:  # noqa: BLE001 - evidence must capture tool failures
        schema_tool_error = f"{type(error).__name__}: {error}"
        checks.append(check("openapi_spec_validation", False, schema_tool_error, "valid OpenAPI 3.1"))
        checks.append(check("detail_schema_positive_instances", False, schema_tool_error, "valid"))
        checks.append(
            check("detail_schema_additional_property_rejection", False, schema_tool_error, "rejected")
        )

    architecture = (docs / "architecture.md").read_text(encoding="utf-8")
    modules = (docs / "module-boundaries.md").read_text(encoding="utf-8")
    permissions = (docs / "permissions.md").read_text(encoding="utf-8")
    service_targets = (docs / "service-targets.md").read_text(encoding="utf-8")
    adr_objects = (docs / "adr/0003-content-addressed-object-storage.md").read_text(
        encoding="utf-8"
    )
    adr_ai = (docs / "adr/0005-append-only-model-results.md").read_text(encoding="utf-8")
    adr_public = (docs / "adr/0006-separate-public-read-model.md").read_text(
        encoding="utf-8"
    )
    all_markdown = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(docs.rglob("*.md"))
    )
    semantic_assertions = {
        "data_layers": all(
            term in architecture for term in ("`ingest`", "`core`", "`ops`", "`audit`", "`public`")
        ),
        "jobs_not_business_tables": "业务表不承担任务状态" in architecture,
        "stored_object_registry": all(
            term in data_model
            for term in (
                "### `core.stored_objects`",
                "`(storage_domain, content_sha256)`",
                "不同 artifact version 可以共同引用同一 `stored_object_id`",
                "`(text_object_id, storage_domain, output_sha256) -> core.stored_objects",
                "`(request_object_id, storage_domain) -> core.stored_objects",
            )
        )
        and "统一物理对象登记" in adr_objects
        and "object_registry" in modules,
        "publication_authorization_coverage": all(
            term in data_model
            for term in (
                "audit.document_publication_grants",
                "audit.claim_publication_grants",
                "audit.entity_publication_grants",
                "audit.relation_publication_grants",
                "document_grant_id -> audit.document_publication_grants.id",
                "claim_grant_id -> audit.claim_publication_grants.id",
                "entity_grant_id -> audit.entity_publication_grants.id",
                "relation_grant_id -> audit.relation_publication_grants.id",
                "### `public.document_entities`",
                "(basis_evidence_id, document_id) -> public.evidence(id, document_id)",
            )
        )
        and "四类真实外键授权表" in adr_public,
        "analysis_selection_composite_fk": (
            "(analysis_result_id, document_version_id, result_type) -> core.analysis_results"
            in data_model
            and "同文档、同任务类型" in adr_ai
        ),
        "entity_candidate_provenance": all(
            term in data_model
            for term in (
                "### `core.entity_candidates`",
                "(analysis_result_id, document_version_id, result_type) -> core.analysis_results",
                "resolved_entity_id -> core.entities.id",
                "(evidence_span_id, document_version_id) -> core.evidence_spans",
            )
        ),
        "postgres_authority": "PostgreSQL" in all_markdown and "唯一可部署 DDL" in all_markdown,
        "single_migration_chain": (
            "alembic/versions/" in all_markdown
            and "不得再建立 `src/.../migrations` 副本" in all_markdown
        ),
        "real_relation_foreign_keys": "两端都是 `core.entities` 外键" in architecture,
        "publisher_process_boundary": all(
            term in architecture
            for term in (
                'publisher["Publisher 进程',
                'publisher -->|"uap_publisher：领取发布任务并写 public"| postgres',
                "participant P as Publisher",
                "P->>U: 事务性重建或撤回公开投影",
                "应用 API、Worker、调度器、Publisher",
            )
        )
        and "W->>U:" not in architecture
        and "worker --> public" not in architecture
        and "├── publisher/" in modules
        and "普通 Worker 不加载该 handler" in all_markdown,
        "model_run_ops_layer_flow": all(
            term in architecture
            for term in (
                "participant OP as ops.model_runs",
                "W->>OP: 追加 model_run",
                "W->>C: 追加 analysis_result",
            )
        )
        and "W->>C: 追加 model_run" not in architecture,
        "dictionary_relationship_fk_completeness": all(
            term in data_model
            for term in (
                "source_document_version_id -> core.document_versions.id null",
                "三表的 `origin_analysis_result_id -> core.analysis_results.id null`",
            )
        ),
        "relation_grant_withdrawal_closure": all(
            term in data_model
            for term in (
                "`relation_grant_id` 唯一",
                "(withdrawn_by_decision_id, review_case_id)",
                "确保授权及撤回决定都属于审核该 subject 的同一 case",
            )
        ),
        "recovery_object_registry_consistency": all(
            term in service_targets
            for term in (
                "`ingest.artifact_versions.stored_object_id -> core.stored_objects.id`",
                "`core.stored_objects.object_key/content_sha256/byte_length`",
            )
        )
        and "`artifact_versions.object_key/content_sha256/byte_length`"
        not in service_targets
        and all(
            term in data_model
            for term in (
                "### `core.stored_objects`",
                "(stored_object_id, storage_domain) -> core.stored_objects",
                "版本记录不再拥有 `object_key`",
            )
        ),
    }
    for name, passed_assertion in semantic_assertions.items():
        checks.append(check(name, passed_assertion, passed_assertion, True))

    runtime_public_writers = sorted(
        role
        for role in ("uap_api", "uap_worker", "uap_scheduler", "uap_publisher", "uap_public_reader", "uap_audit_reader", "uap_backup")
        if "W" in (matrix_public_cell(permissions, role) or "")
    )
    checks.append(
        check(
            "worker_publisher_permission_isolation",
            matrix_public_cell(permissions, "uap_worker") == "—"
            and runtime_public_writers == ["uap_publisher"],
            {
                "uap_worker_public": matrix_public_cell(permissions, "uap_worker"),
                "runtime_public_writers": runtime_public_writers,
            },
            {"uap_worker_public": "—", "runtime_public_writers": ["uap_publisher"]},
        )
    )

    markdown_fence_errors = []
    for path in sorted(docs.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        if text.count("```") % 2:
            markdown_fence_errors.append(str(path.relative_to(root)))
    checks.append(
        check("balanced_markdown_fences", not markdown_fence_errors, markdown_fence_errors, [])
    )

    files = sorted(path for path in docs.rglob("*") if path.is_file()) + [
        root / "tools/requirements-wp1-validation.txt",
        root / "tools/validate_wp1_design.py",
    ]
    manifest = [
        {
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted(files)
    ]
    passed = all(item["passed"] for item in checks)
    report = {
        "generated_at": utc_now(),
        "git_head": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        ).stdout.strip(),
        "passed": passed,
        "schema_tool_path": str(args.schema_tools),
        "schema_tool_versions": schema_versions,
        "schema_tool_error": schema_tool_error,
        "checks": checks,
        "manifest": manifest,
    }
    report_path = output / "design-validation.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output / "MANIFEST.sha256").write_text(
        "\n".join(f"{item['sha256']}  {item['path']}" for item in manifest) + "\n",
        encoding="utf-8",
    )
    print(f"passed={passed} checks={len(checks)} files={len(files)}")
    print(report_path)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
