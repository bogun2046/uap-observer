"""Fail-closed WP3 → WP10.5 runtime orchestrator for G10-25.

Existing probes are executed in isolated subprocesses. Credentials stay in the
child environment; OS argv never contains DSN or passwords. A step cannot pass
without fresh, parseable evidence written this run.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import traceback
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

from tools.wp10_object_store_guard import DisposableObjectStoreGuard
from tools.wp10_stage_revisions import (
    DATABASE_TOPOLOGY_ENVS,
    LEGACY_DATABASE_ENV,
    REGRESSION_DATABASE_ENV,
    LiveStageGate,
    StageRevisionError,
    database_url_for_step,
)

TOOLS_DIR = Path(__file__).resolve().parent
PLATFORM_DIR = TOOLS_DIR.parent

SCHEMA = "wp10-runtime-evidence.v1"
STEP_SCHEMA = "wp10-runtime-step-evidence.v1"
SIGNED_SHA = "4e15bdd8cdb92d4406cc46b38f8cef92320a1881"
START_SHA = "34c57bcadfeb67053c4c47f8cde237a3af185ba8"
HEAD = "0024_wp10_admin_replay"
NOT_RUN = "not_run"

SECRET_ENV_MARKERS = ("PASSWORD", "SECRET", "TOKEN", "DSN")
SECRET_FLAG_NAMES = {
    "--admin-url",
    "--publisher-url",
    "--reader-url",
    "--database-url",
}
FORBIDDEN_ORCHESTRATOR_FLAGS = (
    "--password",
    "--admin-password",
    "--publisher-password",
    "--reader-password",
    "--api-password",
    "--admin-url",
    "--publisher-url",
    "--reader-url",
    "--database-url",
)
URL_PASSWORD_RE = re.compile(r"(://[^:/@]+):([^@/]+)@")

WP3_SUCCESS_KEYS = frozenset(
    {
        "head",
        "business_tables",
        "object_sha256",
        "migrator_login_rejected",
        "denied_role_sqlstates",
    }
)
WP5_SUCCESS_KEYS = frozenset(
    {
        "source_run_outcome",
        "job_status",
        "retry_job_status",
        "atomic_retry",
        "provenance_relabel",
        "stale_checkpoint_sqlstate",
        "cross_source_config_sqlstate",
    }
)
WP6_SUCCESS_KEYS = frozenset({"documents", "successful_extractions"})
WP7_SUCCESS_KEYS = frozenset(
    {
        "model_runs",
        "valid_result",
        "invalid_result",
        "semantic_duplicate_without_provider_call",
        "auth_failures_terminal",
        "rate_limit_retry",
        "upstream_failure_closed",
    }
)

GIT_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")

WP10_1_MIGRATION_COUNT_KEYS = frozenset(
    {
        "audit.review_decisions",
        "audit.document_publication_grants",
        "audit.claim_publication_grants",
        "audit.document_publication_manifests",
        "audit.claim_publication_manifests",
        "audit.publication_quarantine",
        "ops.outbox_events",
        "public.documents",
    }
)
WP10_1_RUNTIME_COUNT_KEYS = frozenset(
    {
        "audit.review_decisions",
        "audit.document_publication_grants",
        "audit.claim_publication_grants",
        "audit.entity_publication_grants",
        "audit.document_publication_manifests",
        "audit.claim_publication_manifests",
        "audit.claim_publication_manifest_evidence",
        "audit.entity_publication_manifests",
        "audit.publication_quarantine",
        "audit.audit_events",
        "ops.outbox_events",
        "public.documents",
    }
)
WP10_1_MIGRATION_OBJECT_KEYS = frozenset({"tables", "functions", "constraints"})

WP10_1_MIGRATION_CASE_EXPECTATIONS: dict[str, dict[str, Any]] = {
    "G10-05-legacy-quarantine": {
        "code": "publication_contract_state_blocks_downgrade",
        "sqlstate": "22023",
    },
    "G10-05-empty-roundtrip": {"final_revision": "0020_wp10_publication_contract"},
    "G10-05-upgrade-public-preflight": {
        "code": "publication_manifest_invalid",
        "sqlstate": "23514",
    },
    "G10-05-claim-document-dependency": {
        "code": "publication_document_grant_required",
        "sqlstate": "23514",
    },
    "G10-05-downgrade-v2": {
        "code": "publication_contract_state_blocks_downgrade",
        "sqlstate": "22023",
    },
    "G10-05-downgrade-quarantine": {
        "code": "publication_contract_state_blocks_downgrade",
        "sqlstate": "22023",
    },
    "G10-05-downgrade-terminal": {
        "code": "publication_contract_state_blocks_downgrade",
        "sqlstate": "22023",
    },
    "G10-05-downgrade-public": {
        "code": "publication_contract_state_blocks_downgrade",
        "sqlstate": "22023",
    },
}

WP10_1_RUNTIME_POSITIVE_VALUES: dict[str, tuple[list[str], list[str]]] = {
    "G10-02-positive-01": (["document", "claim", "entity"], ["relation"]),
    "G10-02-positive-02": (
        ["open", "assigned", "approved", "rejected", "disputed", "withdrawn", "closed"],
        [],
    ),
    "G10-02-positive-03": (
        ["person", "organization", "location", "event", "object", "concept"],
        [],
    ),
    "G10-02-positive-04": (["text", "html", "pdf", "video", "audio"], []),
    "G10-02-positive-05": (
        ["observation", "attribution", "event", "assessment", "other"],
        [],
    ),
    "G10-02-positive-06": (
        ["reported", "corroborated", "disputed", "unverified", "false"],
        [],
    ),
    "G10-02-positive-07": (
        [
            "official_report",
            "government_document",
            "military",
            "scientific_research",
            "historical_event",
            "sighting",
            "disputed_event",
            "other",
        ],
        [],
    ),
    "G10-02-positive-08": (
        ["official_record", "corroborated", "source_reported", "unverified", "disputed", "opinion"],
        [],
    ),
}

WP10_1_G10_03_CASE_IDS = frozenset(
    [
        *(f"G10-03-api-dml-{index:02d}" for index in range(1, 16)),
        *(f"G10-03-publisher-dml-{index:02d}" for index in range(1, 4)),
    ]
    + [
        f"G10-03-{role}-{operation}"
        for role in (
            "uap_api",
            "uap_worker",
            "uap_scheduler",
            "uap_publisher",
            "uap_model_governance",
            "uap_public_reader",
            "uap_audit_reader",
            "uap_backup",
        )
        for operation in ("merge", "reverse")
    ]
    + [
        "G10-03-uap_worker-review",
        "G10-03-uap_publisher-review",
        "G10-03-uap_public_reader-review",
    ]
)

WP10_1_G10_04_CASE_EXPECTATIONS: dict[str, dict[str, Any]] = {
    "G10-04-document-approve": {"manifest_hash_equals_grant_and_outbox": True},
    "G10-04-document-revise": {"quarantine_resolutions": 2},
    "G10-04-claim-post-change": {"evidence_count": 1, "manifest_hash_equals_grant": True},
    "G10-04-manifest-append-only": {
        "code": "publication contract history is immutable",
        "sqlstate": "55000",
    },
    "G10-04-entity-post-change": {"manifest_hash_equals_grant": True},
    "G10-04-invalid-unknown-field": {
        "code": "publication_manifest_invalid",
        "sqlstate": "22023",
    },
    "G10-04-invalid-bad-basis": {
        "code": "publication_manifest_invalid",
        "sqlstate": "22023",
    },
    "G10-04-invalid-missing-title": {
        "code": "publication_manifest_invalid",
        "sqlstate": "22023",
    },
    "G10-04-invalid-source-url": {
        "code": "publication_source_url_missing",
        "sqlstate": "22023",
    },
    "G10-04-invalid-no-evidence": {
        "code": "publication_evidence_required",
        "sqlstate": "23514",
    },
    "G10-04-invalid-long-excerpt": {
        "code": "publication_evidence_excerpt_too_long",
        "sqlstate": "22023",
    },
}

WP10_1_RUNTIME_CASE_REQUIREMENTS: dict[str, str] = {
    **{f"G10-02-legacy-{index:02d}": "G10-02" for index in range(1, 11)},
    **{case_id: "G10-02" for case_id in WP10_1_RUNTIME_POSITIVE_VALUES},
    **{case_id: "G10-03" for case_id in WP10_1_G10_03_CASE_IDS},
    **{case_id: "G10-04" for case_id in WP10_1_G10_04_CASE_EXPECTATIONS},
}

WP10_1_MIGRATION_IDENTITIES: dict[str, tuple[str, str]] = {
    "G10-05-legacy-quarantine": (
        "uap_migrator",
        "alembic upgrade and rejected downgrade",
    ),
    "G10-05-empty-roundtrip": ("uap_migrator", "alembic roundtrip"),
    "G10-05-upgrade-public-preflight": ("uap_migrator", "alembic upgrade"),
    "G10-05-claim-document-dependency": (
        "uap_api",
        "audit.record_review_decision approve claim",
    ),
    **{
        case_id: ("uap_migrator", "alembic downgrade")
        for case_id in (
            "G10-05-downgrade-v2",
            "G10-05-downgrade-quarantine",
            "G10-05-downgrade-terminal",
            "G10-05-downgrade-public",
        )
    },
}

WP10_1_LEGACY_CASES: dict[str, tuple[str, str]] = {
    "G10-02-legacy-01": ("audit.review_case_type", "document_version"),
    "G10-02-legacy-02": ("audit.review_status", "candidate"),
    "G10-02-legacy-03": ("audit.review_status", "in_review"),
    "G10-02-legacy-04": ("core.entity_type", "place"),
    "G10-02-legacy-05": ("core.entity_type", "document"),
    "G10-02-legacy-06": ("core.entity_type", "topic"),
    "G10-02-legacy-07": ("core.locator_type", "characters"),
    "G10-02-legacy-08": ("core.locator_type", "page"),
    "G10-02-legacy-09": ("core.claim_type", "source_action"),
    "G10-02-legacy-10": ("core.assertion_status", "official_record"),
}
WP10_1_LEGACY_API_BOUNDARY = "frozen OpenAPI enum DTO; no HTTP input exists for every enum"
WP10_1_POSITIVE_OPERATIONS = {
    f"G10-02-positive-{index:02d}": f"enum_range {enum_name}"
    for index, enum_name in enumerate(
        (
            "audit.review_case_type",
            "audit.review_status",
            "core.entity_type",
            "core.locator_type",
            "core.claim_type",
            "core.assertion_status",
            "public.document_category",
            "public.fact_status",
        ),
        1,
    )
}


def _g10_03_identities() -> dict[str, tuple[str, str]]:
    identities: dict[str, tuple[str, str]] = {}
    targets = (
        "core.documents",
        "core.claims",
        "core.entities",
        "ops.outbox_events",
        "audit.review_cases",
    )
    index = 1
    for target in targets:
        for verb in ("INSERT", "UPDATE", "DELETE"):
            identities[f"G10-03-api-dml-{index:02d}"] = ("uap_api", f"{verb} {target}")
            index += 1
    for index, verb in enumerate(("INSERT", "UPDATE", "DELETE"), 1):
        identities[f"G10-03-publisher-dml-{index:02d}"] = (
            "uap_publisher",
            f"{verb} public.documents",
        )
    for role in (
        "uap_api",
        "uap_worker",
        "uap_scheduler",
        "uap_publisher",
        "uap_model_governance",
        "uap_public_reader",
        "uap_audit_reader",
        "uap_backup",
    ):
        identities[f"G10-03-{role}-merge"] = (role, "core.merge_entities")
        identities[f"G10-03-{role}-reverse"] = (role, "core.reverse_entity_merge")
    for role in ("uap_worker", "uap_publisher", "uap_public_reader"):
        identities[f"G10-03-{role}-review"] = (role, "audit.record_review_decision")
    return identities


WP10_1_RUNTIME_IDENTITIES: dict[str, tuple[str, str]] = {
    **{
        case_id: (
            "uap_owner",
            f"enum cast {database_type} and OpenAPI DTO validation",
        )
        for case_id, (database_type, _legacy_value) in WP10_1_LEGACY_CASES.items()
    },
    **{
        case_id: ("uap_owner", operation)
        for case_id, operation in WP10_1_POSITIVE_OPERATIONS.items()
    },
    **_g10_03_identities(),
    "G10-04-document-approve": (
        "uap_api",
        "audit.record_review_decision approve document",
    ),
    "G10-04-document-revise": (
        "uap_api",
        "audit.record_review_decision revise document",
    ),
    "G10-04-claim-post-change": (
        "uap_api",
        "audit.record_review_decision approve claim",
    ),
    "G10-04-manifest-append-only": (
        "uap_owner",
        "UPDATE audit.claim_publication_manifests",
    ),
    "G10-04-entity-post-change": (
        "uap_api",
        "audit.record_review_decision approve entity",
    ),
    "G10-04-invalid-unknown-field": (
        "uap_api",
        "audit.record_review_decision revise document",
    ),
    "G10-04-invalid-bad-basis": (
        "uap_api",
        "audit.record_review_decision revise document",
    ),
    "G10-04-invalid-missing-title": (
        "uap_api",
        "audit.record_review_decision revise document",
    ),
    "G10-04-invalid-source-url": (
        "uap_api",
        "audit.record_review_decision approve document",
    ),
    "G10-04-invalid-no-evidence": (
        "uap_api",
        "audit.record_review_decision approve claim",
    ),
    "G10-04-invalid-long-excerpt": (
        "uap_api",
        "audit.record_review_decision approve claim",
    ),
}

WP10_1_RUNTIME_DELTAS: dict[str, dict[str, int]] = {
    "G10-04-document-approve": {
        "audit.audit_events": 1,
        "audit.document_publication_grants": 1,
        "audit.document_publication_manifests": 1,
        "audit.review_decisions": 1,
        "ops.outbox_events": 1,
    },
    "G10-04-document-revise": {
        "audit.audit_events": 1,
        "audit.document_publication_grants": 2,
        "audit.document_publication_manifests": 1,
        "audit.publication_quarantine": 4,
        "audit.review_decisions": 2,
        "ops.outbox_events": 3,
    },
    "G10-04-claim-post-change": {
        "audit.audit_events": 1,
        "audit.claim_publication_grants": 1,
        "audit.claim_publication_manifest_evidence": 1,
        "audit.claim_publication_manifests": 1,
        "audit.review_decisions": 1,
        "ops.outbox_events": 1,
    },
    "G10-04-entity-post-change": {
        "audit.audit_events": 1,
        "audit.entity_publication_grants": 1,
        "audit.entity_publication_manifests": 1,
        "audit.review_decisions": 1,
        "ops.outbox_events": 1,
    },
}

WP10_1_RUNTIME_RELATED_IDS: dict[str, frozenset[str]] = {
    "G10-04-document-approve": frozenset(
        {"review_case_id", "decision_id", "grant_id", "document_version_id"}
    ),
    "G10-04-document-revise": frozenset(
        {
            "event_quarantine_id",
            "grant_quarantine_id",
            "legacy_event_id",
            "legacy_grant_id",
            "review_case_id",
        }
    ),
    "G10-04-claim-post-change": frozenset(
        {
            "claim_id",
            "decision_id",
            "document_version_id",
            "evidence_span_id",
            "grant_id",
            "review_case_id",
        }
    ),
    "G10-04-manifest-append-only": frozenset({"grant_id"}),
    "G10-04-entity-post-change": frozenset(
        {"decision_id", "entity_id", "grant_id", "review_case_id"}
    ),
    **{
        case_id: frozenset({"review_case_id"})
        for case_id in (
            "G10-04-invalid-unknown-field",
            "G10-04-invalid-bad-basis",
            "G10-04-invalid-missing-title",
        )
    },
    "G10-04-invalid-source-url": frozenset(
        {"document_id", "document_version_id", "review_case_id"}
    ),
    "G10-04-invalid-no-evidence": frozenset(
        {"claim_id", "document_version_id", "evidence_span_id", "review_case_id"}
    ),
    "G10-04-invalid-long-excerpt": frozenset(
        {"claim_id", "document_version_id", "evidence_span_id", "review_case_id"}
    ),
}


@dataclass(frozen=True)
class Step:
    step_id: str
    group: str
    script: str
    kind: str
    requires_probe_json: bool = False
    stdout_kind: str = "passed-token"


def _step(
    step_id: str,
    group: str,
    script: str,
    kind: str,
    requires_probe_json: bool = False,
    stdout_kind: str = "passed-token",
) -> Step:
    return Step(step_id, group, script, kind, requires_probe_json, stdout_kind)


FROZEN_STEPS: tuple[Step, ...] = (
    _step("WP3", "WP3", "wp3_runtime_probe.py", "runtime", stdout_kind="wp3-json"),
    _step("WP4", "WP4", "wp4_runtime_probe.py", "runtime"),
    _step("WP5", "WP5", "wp5_runtime_probe.py", "runtime", stdout_kind="wp5-mapping"),
    _step("WP6", "WP6", "wp6_runtime_probe.py", "runtime", stdout_kind="wp6-json"),
    _step("WP7", "WP7", "wp7_runtime_probe.py", "runtime", stdout_kind="wp7-json"),
    _step("WP8", "WP8", "wp8_runtime_probe.py", "runtime"),
    _step("WP9.1", "WP9.1", "wp9_1_runtime_probe.py", "runtime"),
    _step("WP9.2", "WP9.2", "wp9_2_runtime_probe.py", "runtime"),
    _step("WP9.3", "WP9.3", "wp9_3_runtime_probe.py", "runtime"),
    _step("WP9.4", "WP9.4", "wp9_4_runtime_probe.py", "runtime"),
    _step("WP9.5", "WP9.5", "wp9_5_runtime_probe.py", "runtime"),
    _step("WP9.6", "WP9.6", "wp9_6_runtime_probe.py", "runtime"),
    _step("WP10.1-migration", "WP10.1", "wp10_1_migration_probe.py", "migration", True),
    _step("WP10.1-runtime", "WP10.1", "wp10_1_runtime_probe.py", "runtime", True),
    _step("WP10.2-migration", "WP10.2", "wp10_2_migration_probe.py", "migration", True),
    _step("WP10.2-runtime", "WP10.2", "wp10_2_runtime_probe.py", "runtime", True),
    _step("WP10.3-migration", "WP10.3", "wp10_3_migration_probe.py", "migration", True),
    _step("WP10.3-runtime", "WP10.3", "wp10_3_runtime_probe.py", "runtime", True),
    _step("WP10.3-wp10.2-regression", "WP10.3", "wp10_2_runtime_probe.py", "runtime", True),
    _step("WP10.4-migration", "WP10.4", "wp10_4_migration_probe.py", "migration", True),
    _step("WP10.4-runtime", "WP10.4", "wp10_4_runtime_probe.py", "runtime", True),
    _step("WP10.5-migration", "WP10.5", "wp10_5_migration_probe.py", "migration", True),
    _step("WP10.5-runtime", "WP10.5", "wp10_5_runtime_probe.py", "runtime", True),
)

REQUIRED_SCRIPTS: tuple[str, ...] = tuple(dict.fromkeys(step.script for step in FROZEN_STEPS))
STEPS_BY_ID = {step.step_id: step for step in FROZEN_STEPS}


class ProbeConfigurationError(Exception):
    """Invalid orchestrator invocation; not a child-probe failure."""


def discover_source_boundary(platform_dir: Path) -> Path:
    """Return the real Git root, or the standalone platform root in containers."""

    platform = platform_dir.resolve()
    if platform.parent == platform:
        raise ProbeConfigurationError("refusing filesystem root as the platform source boundary")
    for candidate in (platform, *platform.parents):
        git_marker = candidate / ".git"
        if not git_marker.is_symlink() and (git_marker.is_dir() or git_marker.is_file()):
            return candidate
    return platform


def _is_within(path: Path, boundary: Path) -> bool:
    try:
        path.relative_to(boundary)
    except ValueError:
        return False
    return True


def _has_source_ancestor(path: Path, boundary: Path) -> bool:
    """Catch source-tree paths even when an ancestor is reached through a symlink alias."""

    current = path
    while True:
        try:
            if _is_within(current.resolve(), boundary):
                return True
        except (OSError, RuntimeError) as exc:
            raise ProbeConfigurationError("unable to resolve the evidence/source boundary") from exc
        if current.parent == current:
            return False
        current = current.parent


def resolve_evidence_directory(evidence_dir: Path, *, source_boundary: Path) -> Path:
    """Resolve an external evidence path while rejecting lexical/symlink traversal."""

    try:
        boundary = source_boundary.resolve()
        lexical = Path(os.path.abspath(os.fspath(evidence_dir)))
        resolved = lexical.resolve()
    except (OSError, RuntimeError) as exc:
        raise ProbeConfigurationError("unable to resolve the evidence/source boundary") from exc
    if boundary.parent == boundary:
        raise ProbeConfigurationError("refusing filesystem root as the source boundary")
    if (
        _is_within(lexical, boundary)
        or _has_source_ancestor(lexical, boundary)
        or _is_within(resolved, boundary)
    ):
        raise ProbeConfigurationError("--evidence-dir must be outside the source boundary")
    return resolved


SOURCE_BOUNDARY = discover_source_boundary(PLATFORM_DIR)


class StageGate(Protocol):
    def ensure(self, step_id: str) -> dict[str, Any]: ...


class ObjectStoreGate(Protocol):
    def preflight(self) -> dict[str, Any]: ...

    def cleanup(self) -> dict[str, Any]: ...


def default_stage_gate() -> StageGate:
    return LiveStageGate()


def default_object_store_gate() -> ObjectStoreGate:
    return DisposableObjectStoreGuard()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")  # noqa: UP017


def secret_values(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    source = os.environ if environ is None else environ
    values: set[str] = set()
    for key, value in source.items():
        if not value or len(value) < 4:
            continue
        if any(marker in key.upper() for marker in SECRET_ENV_MARKERS):
            values.add(value)
        if key.endswith("_URL") and "://" in value:
            match = URL_PASSWORD_RE.search(value)
            if match and match.group(2):
                values.add(match.group(2))
    return frozenset(values)


def redact_text(value: str, secrets: frozenset[str] | None = None) -> str:
    redacted = URL_PASSWORD_RE.sub(r"\1:<redacted>@", value)
    for secret in sorted(secrets or secret_values(), key=len, reverse=True):
        if secret:
            redacted = redacted.replace(secret, "<redacted>")
    return redacted


def redact_argv(argv: list[str], secrets: frozenset[str] | None = None) -> list[str]:
    hidden = secrets or secret_values()
    out: list[str] = []
    hide_next = False
    for item in argv:
        if hide_next:
            out.append("<redacted>")
            hide_next = False
            continue
        if item in SECRET_FLAG_NAMES:
            out.append(item)
            hide_next = True
            continue
        out.append(redact_text(item, hidden))
    return out


def redact_document(payload: Any, secrets: frozenset[str] | None = None) -> Any:
    hidden = secrets or secret_values()
    if isinstance(payload, str):
        return redact_text(payload, hidden)
    if isinstance(payload, list):
        return [redact_document(item, hidden) for item in payload]
    if isinstance(payload, dict):
        return {str(key): redact_document(value, hidden) for key, value in payload.items()}
    return payload


def argv_contains_secret(argv: list[str], secrets: frozenset[str] | None = None) -> bool:
    hidden = secrets or secret_values()
    blob = " ".join(argv)
    if URL_PASSWORD_RE.search(blob):
        return True
    return any(secret in blob for secret in hidden if len(secret) >= 8)


def reject_cli_secrets(argv: list[str]) -> None:
    for item in argv[1:]:
        lowered = item.lower()
        name = item.split("=", 1)[0]
        if name in FORBIDDEN_ORCHESTRATOR_FLAGS or lowered.startswith("--password"):
            raise ProbeConfigurationError(
                "refusing command-line secret or DSN flag; pass URLs and role "
                "passwords through environment variables"
            )
        if ("password" + "=") in lowered or (
            lowered.startswith("postgresql://") and "@" in lowered
        ):
            raise ProbeConfigurationError(
                "refusing command-line password or DSN; use environment variables"
            )


def plan_steps() -> list[dict[str, Any]]:
    return [
        {
            "step_id": step.step_id,
            "group": step.group,
            "script": f"tools/{step.script}",
            "kind": step.kind,
            "status": "planned",
            "requires_probe_json": step.requires_probe_json,
            "stdout_kind": step.stdout_kind,
        }
        for step in FROZEN_STEPS
    ]


def discover_missing_scripts() -> list[str]:
    return [name for name in REQUIRED_SCRIPTS if not (TOOLS_DIR / name).is_file()]


def env_value(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def require_env(*names: str) -> str:
    value = env_value(*names)
    if not value:
        raise ProbeConfigurationError("missing required environment variable: " + " | ".join(names))
    return value


def libpq_url(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def rewrite_role_url(base: str, user: str, password: str) -> str:
    parts = urlsplit(libpq_url(base))
    host = parts.hostname or "localhost"
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    scheme = parts.scheme or "postgresql"
    if scheme == "postgresql+psycopg":
        scheme = "postgresql"
    return urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment))


def slug_for(step: Step) -> str:
    return step.step_id.lower().replace(".", "-")


def evidence_path(evidence_dir: Path, step: Step) -> Path:
    return evidence_dir / f"{slug_for(step)}.json"


def probe_evidence_path(evidence_dir: Path, step: Step) -> Path:
    return evidence_dir / f"{slug_for(step)}.probe.json"


def matrix_evidence_path(evidence_dir: Path) -> Path:
    return evidence_dir / "wp10-5-matrix.json"


def extra_args(step: Step, evidence_dir: Path) -> list[str]:
    """Inner argv for the child argparse. Used only inside the child process."""
    try:
        selected_database_url = database_url_for_step(step.step_id)
    except StageRevisionError as exc:
        raise ProbeConfigurationError(str(exc)) from exc
    admin_url = selected_database_url
    database_url = selected_database_url
    if step.step_id == "WP8":
        return ["--from", "wp8.1"]
    if step.step_id == "WP10.1-migration":
        if not admin_url:
            raise ProbeConfigurationError(
                "WP10.1 migration requires UAP_WP10_ADMIN_URL or UAP_DATABASE_URL"
            )
        return [
            "--admin-url",
            admin_url,
            "--evidence-out",
            str(probe_evidence_path(evidence_dir, step)),
        ]
    if step.step_id == "WP10.1-runtime":
        if not database_url:
            raise ProbeConfigurationError("WP10.1 runtime requires UAP_DATABASE_URL")
        return [
            "--database-url",
            database_url,
            "--evidence-out",
            str(probe_evidence_path(evidence_dir, step)),
        ]
    if step.step_id == "WP10.2-migration":
        if not admin_url:
            raise ProbeConfigurationError(
                "WP10.2 migration requires UAP_WP10_ADMIN_URL or UAP_DATABASE_URL"
            )
        return [
            "--admin-url",
            admin_url,
            "--evidence-out",
            str(probe_evidence_path(evidence_dir, step)),
        ]
    if step.step_id == "WP10.2-runtime":
        if not database_url:
            raise ProbeConfigurationError("WP10.2 runtime requires UAP_DATABASE_URL")
        return [
            "--database-url",
            database_url,
            "--evidence-out",
            str(probe_evidence_path(evidence_dir, step)),
        ]
    if step.step_id == "WP10.3-migration":
        if not admin_url:
            raise ProbeConfigurationError(
                "WP10.3 migration requires UAP_WP10_ADMIN_URL or UAP_DATABASE_URL"
            )
        return [
            "--admin-url",
            admin_url,
            "--evidence-out",
            str(probe_evidence_path(evidence_dir, step)),
        ]
    if step.step_id == "WP10.3-runtime":
        if not database_url:
            raise ProbeConfigurationError("WP10.3 runtime requires UAP_DATABASE_URL")
        return [
            "--database-url",
            database_url,
            "--evidence-out",
            str(probe_evidence_path(evidence_dir, step)),
        ]
    if step.step_id == "WP10.3-wp10.2-regression":
        return [
            "--database-url",
            selected_database_url,
            "--evidence-out",
            str(probe_evidence_path(evidence_dir, step)),
        ]
    if step.step_id == "WP10.4-migration":
        if not admin_url:
            raise ProbeConfigurationError(
                "WP10.4 migration requires UAP_WP10_ADMIN_URL or UAP_DATABASE_URL"
            )
        return [
            "--admin-url",
            admin_url,
            "--evidence-out",
            str(probe_evidence_path(evidence_dir, step)),
        ]
    if step.step_id == "WP10.4-runtime":
        if not admin_url:
            raise ProbeConfigurationError(
                "WP10.4 runtime requires UAP_WP10_ADMIN_URL or UAP_DATABASE_URL"
            )
        publisher_password = require_env("UAP_PUBLISHER_PASSWORD")
        reader_password = require_env("UAP_PUBLIC_READER_PASSWORD")
        publisher_url = env_value("UAP_WP10_4_PUBLISHER_URL") or rewrite_role_url(
            admin_url, "uap_publisher", publisher_password
        )
        reader_url = env_value("UAP_WP10_4_READER_URL") or rewrite_role_url(
            admin_url, "uap_public_reader", reader_password
        )
        return [
            "--admin-url",
            admin_url,
            "--publisher-url",
            publisher_url,
            "--reader-url",
            reader_url,
            "--evidence-out",
            str(probe_evidence_path(evidence_dir, step)),
        ]
    if step.step_id == "WP10.5-migration":
        args = ["--evidence-out", str(probe_evidence_path(evidence_dir, step))]
        if admin_url:
            args.extend(["--admin-url", admin_url])
        elif not env_value("UAP_WP10_5_ADMIN_URL"):
            raise ProbeConfigurationError(
                "WP10.5 migration requires UAP_WP10_ADMIN_URL or UAP_WP10_5_ADMIN_URL"
            )
        return args
    if step.step_id == "WP10.5-runtime":
        if not env_value("UAP_WP10_5_ADMIN_URL", "UAP_WP10_ADMIN_URL", "UAP_DATABASE_URL"):
            raise ProbeConfigurationError(
                "WP10.5 runtime requires UAP_WP10_5_ADMIN_URL or UAP_DATABASE_URL"
            )
        return [
            "--evidence-out",
            str(probe_evidence_path(evidence_dir, step)),
            "--matrix-out",
            str(matrix_evidence_path(evidence_dir)),
        ]
    return []


def os_argv(step: Step, evidence_dir: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "tools.wp10_runtime_probe",
        "--exec-child",
        step.step_id,
        "--evidence-dir",
        str(evidence_dir),
    ]


def child_env(step: Step) -> dict[str, str]:
    environ = os.environ.copy()
    try:
        selected_database_url = database_url_for_step(step.step_id)
    except StageRevisionError as exc:
        raise ProbeConfigurationError(str(exc)) from exc
    environ["UAP_DATABASE_URL"] = selected_database_url
    if step.step_id.startswith("WP10.5"):
        environ["UAP_WP10_5_ADMIN_URL"] = selected_database_url
    return environ


RunSubprocess = Callable[..., subprocess.CompletedProcess[str]]


def run_subprocess(
    argv: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        argv,
        cwd=str(cwd),
        env=dict(env),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sanitized = redact_document(payload)
    path.write_text(json.dumps(sanitized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def step_output_paths(evidence_dir: Path, step: Step) -> list[Path]:
    paths = [evidence_path(evidence_dir, step), probe_evidence_path(evidence_dir, step)]
    if step.step_id == "WP10.5-runtime":
        paths.append(matrix_evidence_path(evidence_dir))
    return paths


def clear_step_evidence(evidence_dir: Path, step: Step) -> None:
    for path in step_output_paths(evidence_dir, step):
        if path.is_file() or path.is_symlink():
            path.unlink()
        if path.exists():
            raise ProbeConfigurationError(f"failed to clear evidence file: {path}")


def _brace_slice(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return ""
    return text[start : end + 1]


def parse_stdout_mapping(stdout: str) -> dict[str, Any] | None:
    stripped = stdout.strip()
    candidates = [stripped, _brace_slice(stripped)]
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            payload = json.loads(candidate)
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            pass
        try:
            payload = ast.literal_eval(candidate)
            if isinstance(payload, dict):
                return {str(key): value for key, value in payload.items()}
        except (ValueError, SyntaxError, MemoryError):
            pass
    return None


def mapping_satisfies(
    payload: dict[str, Any] | None,
    required: frozenset[str],
    expected: Mapping[str, Any] | None = None,
) -> bool:
    if payload is None:
        return False
    if any(key not in payload for key in required):
        return False
    if expected:
        for key, value in expected.items():
            if payload.get(key) != value:
                return False
    return True


def stdout_contract_ok(step: Step, stdout: str) -> tuple[bool, str]:
    if step.requires_probe_json:
        return True, "probe-json"
    kind = step.stdout_kind
    if kind == "passed-token":
        if "passed" in stdout.lower():
            return True, "stdout-token"
        return False, "missing-success-token"
    payload = parse_stdout_mapping(stdout)
    if kind == "wp3-json":
        ok = mapping_satisfies(payload, WP3_SUCCESS_KEYS, {"migrator_login_rejected": True})
        return (True, "wp3-json") if ok else (False, "stdout-contract:wp3-json")
    if kind == "wp5-mapping":
        ok = mapping_satisfies(
            payload,
            WP5_SUCCESS_KEYS,
            {
                "provenance_relabel": "rejected",
                "stale_checkpoint_sqlstate": "40001",
                "cross_source_config_sqlstate": "23503",
            },
        )
        return (True, "wp5-mapping") if ok else (False, "stdout-contract:wp5-mapping")
    if kind == "wp6-json":
        if not mapping_satisfies(payload, WP6_SUCCESS_KEYS):
            return False, "stdout-contract:wp6-json"
        documents = payload.get("documents") if payload else None
        extractions = payload.get("successful_extractions") if payload else None
        if not isinstance(documents, int) or documents < 0:
            return False, "stdout-contract:wp6-json"
        if not isinstance(extractions, int) or extractions < 0:
            return False, "stdout-contract:wp6-json"
        return True, "wp6-json"
    if kind == "wp7-json":
        ok = mapping_satisfies(
            payload,
            WP7_SUCCESS_KEYS,
            {
                "valid_result": True,
                "invalid_result": True,
                "semantic_duplicate_without_provider_call": True,
                "auth_failures_terminal": True,
                "rate_limit_retry": True,
                "upstream_failure_closed": True,
            },
        )
        return (True, "wp7-json") if ok else (False, "stdout-contract:wp7-json")
    return False, f"unknown-stdout-kind:{kind}"


def current_source_commit() -> str | None:
    """Return the independently resolved Git HEAD or fail closed."""

    git_executable = shutil.which("git")
    if git_executable is None:
        return repository_head(SOURCE_BOUNDARY)
    try:
        completed = subprocess.run(  # noqa: S603 - resolved executable and fixed arguments
            [git_executable, "-C", str(SOURCE_BOUNDARY), "rev-parse", "--verify", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = completed.stdout.strip()
    if completed.returncode != 0 or GIT_COMMIT_RE.fullmatch(commit) is None:
        return None
    return commit


def repository_head(repository: Path) -> str | None:
    """Resolve HEAD from read-only Git metadata when the runtime omits Git."""

    try:
        marker = repository / ".git"
        if marker.is_file():
            text = marker.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir: "):
                return None
            git_dir = (repository / text.removeprefix("gitdir: ")).resolve()
        elif marker.is_dir() and not marker.is_symlink():
            git_dir = marker.resolve()
        else:
            return None
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if GIT_COMMIT_RE.fullmatch(head) is not None:
            return head
        if not head.startswith("ref: "):
            return None
        reference = head.removeprefix("ref: ")
        if reference.startswith("/") or ".." in Path(reference).parts:
            return None
        loose = git_dir / reference
        if loose.is_file():
            value = loose.read_text(encoding="utf-8").strip()
            return value if GIT_COMMIT_RE.fullmatch(value) is not None else None
        packed = git_dir / "packed-refs"
        if packed.is_file():
            suffix = f" {reference}"
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.endswith(suffix):
                    value = line.split(" ", 1)[0]
                    return value if GIT_COMMIT_RE.fullmatch(value) is not None else None
    except (OSError, UnicodeError):
        return None
    return None


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


def _stable_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _is_count(value: object, *, nullable: bool = False) -> bool:
    return (nullable and value is None) or (type(value) is int and value >= 0)


def _runtime_snapshot_is_valid(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"counts", "digest"}:
        return False
    counts = value.get("counts")
    return bool(
        isinstance(counts, dict)
        and set(counts) == WP10_1_RUNTIME_COUNT_KEYS
        and all(_is_count(item) for item in counts.values())
        and value.get("digest") == _stable_digest(counts)
    )


def _migration_snapshot_is_valid(value: object, *, legacy: bool = False) -> bool:
    if not isinstance(value, dict) or set(value) != {"revision", "counts", "objects", "digest"}:
        return False
    counts = value.get("counts")
    objects = value.get("objects")
    revision = "0019_manual_claims_binding" if legacy else "0020_wp10_publication_contract"
    unsigned = {key: value[key] for key in ("revision", "counts", "objects")}
    return bool(
        value.get("revision") == revision
        and isinstance(counts, dict)
        and set(counts) == WP10_1_MIGRATION_COUNT_KEYS
        and all(_is_count(item, nullable=legacy) for item in counts.values())
        and isinstance(objects, dict)
        and set(objects) == WP10_1_MIGRATION_OBJECT_KEYS
        and all(_is_count(item) for item in objects.values())
        and value.get("digest") == _stable_digest(unsigned)
    )


def _uuid_mapping_is_valid(value: object, required_keys: frozenset[str]) -> bool:
    if not isinstance(value, dict) or set(value) != required_keys:
        return False
    try:
        return len(set(value.values())) == len(value) and all(
            str(uuid.UUID(item)) == item for item in value.values()
        )
    except (AttributeError, TypeError, ValueError):
        return False


def _count_delta_is_exact(
    before: dict[str, Any], after: dict[str, Any], expected_delta: Mapping[str, int]
) -> bool:
    before_counts = before["counts"]
    after_counts = after["counts"]
    return all(
        after_counts[key] - before_counts[key] == expected_delta.get(key, 0)
        for key in WP10_1_RUNTIME_COUNT_KEYS
    )


def _case_common_is_valid(case: object, *, requirement: str) -> bool:
    if not isinstance(case, dict):
        return False
    return (
        case.get("requirement") == requirement
        and isinstance(case.get("name"), str)
        and bool(case["name"].strip())
        and case.get("status") == "passed"
        and isinstance(case.get("role"), str)
        and bool(case["role"].strip())
        and isinstance(case.get("operation"), str)
        and bool(case["operation"].strip())
        and isinstance(case.get("expected"), dict)
        and isinstance(case.get("actual"), dict)
        and isinstance(case.get("before"), dict)
        and isinstance(case.get("after"), dict)
        and isinstance(case.get("related_ids"), dict)
    )


def _actual_contains(actual: dict[str, Any], expected: Mapping[str, Any]) -> bool:
    return all(actual.get(key) == value for key, value in expected.items())


def _wp10_1_migration_case_is_valid(case: dict[str, Any]) -> bool:
    case_id = case["id"]
    trusted = WP10_1_MIGRATION_CASE_EXPECTATIONS[case_id]
    role, operation = WP10_1_MIGRATION_IDENTITIES[case_id]
    revisions = {
        "initial_revision": (
            "0019_manual_claims_binding"
            if case_id
            in {
                "G10-05-legacy-quarantine",
                "G10-05-empty-roundtrip",
                "G10-05-upgrade-public-preflight",
            }
            else "0020_wp10_publication_contract"
        ),
        "target_revision": (
            "0019_manual_claims_binding"
            if case_id.startswith("G10-05-downgrade-")
            else "0020_wp10_publication_contract"
        ),
        "final_revision": (
            "0019_manual_claims_binding"
            if case_id == "G10-05-upgrade-public-preflight"
            else "0020_wp10_publication_contract"
        ),
    }
    alias = case_id.removeprefix("G10-05-").removeprefix("downgrade-")
    if case_id == "G10-05-claim-document-dependency":
        alias = "negative"
    elif case_id == "G10-05-empty-roundtrip":
        alias = "roundtrip"
    elif case_id == "G10-05-upgrade-public-preflight":
        alias = "preflight"
    elif case_id == "G10-05-legacy-quarantine":
        alias = "legacy"
    related_ids = case["related_ids"]
    if (
        case.get("role") != role
        or case.get("operation") != operation
        or any(case.get(key) != value for key, value in revisions.items())
        or case.get("expected") != trusted
        or not _actual_contains(case["actual"], trusted)
        or related_ids.get("database_alias") != alias
    ):
        return False
    if case_id.startswith("G10-05-downgrade-"):
        if (
            set(related_ids) != {"database_alias", "marker", "marker_id"}
            or related_ids.get("marker") != alias
            or not _uuid_mapping_is_valid(
                {"marker_id": related_ids.get("marker_id")}, frozenset({"marker_id"})
            )
        ):
            return False
    elif set(related_ids) != {"database_alias"}:
        return False
    before = case["before"]
    after = case["after"]
    if case_id == "G10-05-empty-roundtrip":
        return bool(
            before == {"revision": "0019_manual_claims_binding"}
            and _migration_snapshot_is_valid(after)
            and all(value == 0 for value in after["counts"].values())
        )
    if case_id == "G10-05-upgrade-public-preflight":
        expected_counts = {
            key: (
                1
                if key == "public.documents"
                else None
                if "manifests" in key or key == "audit.publication_quarantine"
                else 0
            )
            for key in WP10_1_MIGRATION_COUNT_KEYS
        }
        return bool(
            before == {"public.documents": 1}
            and _migration_snapshot_is_valid(after, legacy=True)
            and after["counts"] == expected_counts
        )
    if case_id == "G10-05-legacy-quarantine":
        unchanged_keys = WP10_1_MIGRATION_COUNT_KEYS - {
            "audit.document_publication_grants",
            "audit.document_publication_manifests",
            "audit.claim_publication_manifests",
            "audit.publication_quarantine",
            "ops.outbox_events",
        }
        return bool(
            _migration_snapshot_is_valid(before, legacy=True)
            and _migration_snapshot_is_valid(after)
            and before["counts"]["audit.document_publication_manifests"] is None
            and before["counts"]["audit.claim_publication_manifests"] is None
            and before["counts"]["audit.publication_quarantine"] is None
            and after["counts"]["audit.document_publication_manifests"] == 0
            and after["counts"]["audit.claim_publication_manifests"] == 0
            and after["counts"]["audit.publication_quarantine"] == 2
            and after["counts"]["audit.document_publication_grants"]
            == before["counts"]["audit.document_publication_grants"] + 1
            and after["counts"]["ops.outbox_events"] == before["counts"]["ops.outbox_events"] + 1
            and all(after["counts"][key] == before["counts"][key] for key in unchanged_keys)
        )
    if case_id == "G10-05-claim-document-dependency":
        return bool(
            before == after
            and set(before) == {"decision_grant_manifest_outbox"}
            and before["decision_grant_manifest_outbox"] == [0, 0, 0, 0]
        )
    if not (_migration_snapshot_is_valid(before) and before == after):
        return False
    marker_counts = {
        "G10-05-downgrade-v2": ("audit.document_publication_manifests", 1),
        "G10-05-downgrade-quarantine": ("audit.publication_quarantine", 1),
        "G10-05-downgrade-terminal": ("ops.outbox_events", 1),
        "G10-05-downgrade-public": ("public.documents", 1),
    }
    marker_key, minimum = marker_counts[case_id]
    marker_value = before["counts"][marker_key]
    return type(marker_value) is int and marker_value >= minimum


def _wp10_1_runtime_case_is_valid(case: dict[str, Any]) -> bool:
    case_id = case["id"]
    role, operation = WP10_1_RUNTIME_IDENTITIES[case_id]
    related_keys = WP10_1_RUNTIME_RELATED_IDS.get(case_id, frozenset())
    if (
        case.get("role") != role
        or case.get("operation") != operation
        or not _uuid_mapping_is_valid(case.get("related_ids"), related_keys)
        or not _runtime_snapshot_is_valid(case.get("before"))
        or not _runtime_snapshot_is_valid(case.get("after"))
    ):
        return False
    actual = case["actual"]
    expected = case["expected"]
    before = case["before"]
    after = case["after"]
    if case_id.startswith("G10-02-legacy-"):
        database_type, legacy_value = WP10_1_LEGACY_CASES[case_id]
        trusted: dict[str, Any] = {
            "api_exception": "ValueError",
            "database_sqlstate": "22P02",
            "http_status": None,
            "request_id": None,
        }
        return bool(
            expected == trusted
            and _actual_contains(actual, trusted)
            and case.get("name") == f"legacy value {legacy_value} rejected"
            and case.get("api_boundary") == WP10_1_LEGACY_API_BOUNDARY
            and actual.get("database_error")
            == f'invalid input value for enum {database_type}: "{legacy_value}"'
            and before == after
        )
    if case_id in WP10_1_RUNTIME_POSITIVE_VALUES:
        api_values, database_only = WP10_1_RUNTIME_POSITIVE_VALUES[case_id]
        trusted = {
            "api_values_accepted_by_database": api_values,
            "documented_db_only_values": database_only,
        }
        return bool(
            expected == trusted
            and actual.get("api_values") == api_values
            and actual.get("database_only_values") == database_only
            and actual.get("database_values") == [*api_values, *database_only]
            and before == after
        )
    if case_id in WP10_1_G10_03_CASE_IDS:
        return bool(
            expected == {"sqlstate": "42501"}
            and actual.get("sqlstate") == "42501"
            and isinstance(actual.get("error"), str)
            and bool(actual["error"].strip())
            and before == after
        )
    trusted = WP10_1_G10_04_CASE_EXPECTATIONS[case_id]
    if expected != trusted:
        return False
    if case_id == "G10-04-document-approve":
        hashes = [actual.get(key) for key in ("grant_sha256", "manifest_sha256", "outbox_sha256")]
        return (
            all(_is_sha256(value) for value in hashes)
            and len(set(hashes)) == 1
            and actual.get("outbox_schema") == "publication-outbox.v2"
            and _count_delta_is_exact(before, after, WP10_1_RUNTIME_DELTAS[case_id])
        )
    if case_id == "G10-04-document-revise":
        return bool(
            actual.get("quarantine_resolutions") == 2
            and _count_delta_is_exact(before, after, WP10_1_RUNTIME_DELTAS[case_id])
        )
    if case_id == "G10-04-claim-post-change":
        return (
            actual.get("evidence_count") == 1
            and _is_sha256(actual.get("grant_sha256"))
            and actual.get("grant_sha256") == actual.get("manifest_sha256")
            and _count_delta_is_exact(before, after, WP10_1_RUNTIME_DELTAS[case_id])
        )
    if case_id == "G10-04-entity-post-change":
        return (
            _is_sha256(actual.get("grant_sha256"))
            and actual.get("grant_sha256") == actual.get("manifest_sha256")
            and _count_delta_is_exact(before, after, WP10_1_RUNTIME_DELTAS[case_id])
        )
    return _actual_contains(actual, trusted) and before == after


def _wp10_1_cases_are_valid(step: Step, cases: list[Any]) -> tuple[bool, str]:
    contract = (
        {case_id: "G10-05" for case_id in WP10_1_MIGRATION_CASE_EXPECTATIONS}
        if step.step_id.endswith("migration")
        else WP10_1_RUNTIME_CASE_REQUIREMENTS
    )
    case_ids = [case.get("id") if isinstance(case, dict) else None for case in cases]
    if (
        len(case_ids) != len(contract)
        or len(set(case_ids)) != len(case_ids)
        or set(case_ids) != set(contract)
    ):
        return False, "wp10.1-probe-evidence-scenarios"
    for case in cases:
        case_id = case["id"]
        requirement = contract[case_id]
        if not _case_common_is_valid(case, requirement=requirement):
            return False, "wp10.1-probe-evidence-case"
        valid = (
            _wp10_1_migration_case_is_valid(case)
            if step.step_id.endswith("migration")
            else _wp10_1_runtime_case_is_valid(case)
        )
        if not valid:
            return False, "wp10.1-probe-evidence-result"
    return True, "ok"


def child_evidence_ok(step: Step, evidence_dir: Path) -> tuple[bool, str]:
    if not step.requires_probe_json:
        return True, "not-required"
    payload = load_json(probe_evidence_path(evidence_dir, step))
    if payload is None:
        return False, "missing-probe-evidence"
    if payload.get("status") != "passed":
        return False, f"probe-evidence-status:{payload.get('status')}"
    if step.step_id in {"WP10.1-migration", "WP10.1-runtime"}:
        expected_schema = (
            "wp10.1-migration-evidence.v1"
            if step.step_id.endswith("migration")
            else "wp10.1-runtime-evidence.v1"
        )
        if payload.get("schema") != expected_schema or payload.get("schema_version") != 1:
            return False, "wp10.1-probe-evidence-schema"
        summary = payload.get("summary")
        cases = payload.get("cases")
        source = payload.get("source")
        environment = payload.get("environment")
        requirements = payload.get("requirements")
        expected_requirements = (
            {"G10-05"} if step.step_id.endswith("migration") else {"G10-02", "G10-03", "G10-04"}
        )
        expected_probe_sha256 = hashlib.sha256((TOOLS_DIR / step.script).read_bytes()).hexdigest()
        expected_commit = current_source_commit()
        if (
            not isinstance(summary, dict)
            or not isinstance(cases, list)
            or not cases
            or not isinstance(source, dict)
            or expected_commit is None
            or source.get("commit") != expected_commit
            or GIT_COMMIT_RE.fullmatch(str(source.get("commit", ""))) is None
            or source.get("probe_sha256") != expected_probe_sha256
            or not isinstance(environment, dict)
            or not isinstance(environment.get("python"), str)
            or not environment["python"].strip()
            or not isinstance(environment.get("postgresql"), str)
            or not environment["postgresql"].strip()
            or not isinstance(requirements, list)
            or set(requirements) != expected_requirements
            or len(requirements) != len(expected_requirements)
            or not isinstance(payload.get("started_at"), str)
            or not payload["started_at"].strip()
            or not isinstance(payload.get("finished_at"), str)
            or not payload["finished_at"].strip()
        ):
            return False, "wp10.1-probe-evidence-shape"
        if (
            summary.get("total") != len(cases)
            or summary.get("passed") != len(cases)
            or summary.get("failed") != 0
            or summary.get("not_run") != 0
        ):
            return False, "wp10.1-probe-evidence-summary"
        valid, reason = _wp10_1_cases_are_valid(step, cases)
        if not valid:
            return False, reason
    if step.step_id == "WP10.5-runtime":
        matrix = load_json(matrix_evidence_path(evidence_dir))
        if matrix is None or matrix.get("status") != "passed":
            return False, "missing-or-failed-matrix-evidence"
    return True, "ok"


def step_passed(
    *,
    exit_code: int,
    stdout: str,
    step: Step,
    evidence_dir: Path,
) -> tuple[bool, str]:
    if exit_code != 0:
        return False, f"exit:{exit_code}"
    ok, reason = child_evidence_ok(step, evidence_dir)
    if not ok:
        return False, reason
    return stdout_contract_ok(step, stdout)


def not_run_record(step: Step, *, started: str, halted_after: str) -> dict[str, Any]:
    finished = utc_now()
    return {
        "schema": STEP_SCHEMA,
        "step_id": step.step_id,
        "group": step.group,
        "script": f"tools/{step.script}",
        "kind": step.kind,
        "command": [],
        "started_at": started,
        "finished_at": finished,
        "duration_seconds": 0,
        "exit_code": None,
        "status": NOT_RUN,
        "evidence_path": None,
        "probe_evidence_path": None,
        "detail": f"not_run: halted after {halted_after}",
    }


def _append_not_run(
    records: list[dict[str, Any]],
    evidence_dir: Path,
    remaining: tuple[Step, ...],
    *,
    halted_after: str,
    secrets: frozenset[str],
) -> None:
    del secrets
    for later in remaining:
        leftover = not_run_record(later, started=utc_now(), halted_after=halted_after)
        leftover_path = evidence_path(evidence_dir, later)
        write_json(leftover_path, leftover)
        leftover["evidence_path"] = str(leftover_path)
        records.append(leftover)


def execute_steps(
    evidence_dir: Path,
    *,
    runner: RunSubprocess = run_subprocess,
    stage_gate: StageGate | None = None,
    object_store_gate: ObjectStoreGate | None = None,
) -> dict[str, Any]:
    missing = discover_missing_scripts()
    started = utc_now()
    records: list[dict[str, Any]] = []
    status = "passed"
    failed_step: str | None = None
    secrets = secret_values()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    stages = stage_gate if stage_gate is not None else default_stage_gate()
    store_gate = object_store_gate if object_store_gate is not None else default_object_store_gate()
    store_report: dict[str, Any] = {"status": NOT_RUN}
    stage_reports: list[dict[str, Any]] = []
    previous_database_url = os.environ.get("UAP_DATABASE_URL")
    legacy_database_url = os.environ.get(LEGACY_DATABASE_ENV)
    if legacy_database_url:
        # The object-store guard still consumes the shared Settings contract;
        # actual probe/stage selection is handled by the three topology URLs.
        os.environ["UAP_DATABASE_URL"] = legacy_database_url

    def finish() -> dict[str, Any]:
        return _summary(
            status,
            started,
            failed_step,
            records,
            object_store=store_report,
            migration_stages=stage_reports,
        )

    if missing:
        status = "failed"
        failed_step = "discovery"
        records.append(
            {
                "step_id": "discovery",
                "command": ["<orchestrator>", "discover"],
                "started_at": started,
                "finished_at": utc_now(),
                "duration_seconds": 0,
                "exit_code": 2,
                "status": "failed",
                "evidence_path": None,
                "detail": "missing probe scripts: " + ", ".join(missing),
            }
        )
        _append_not_run(
            records,
            evidence_dir,
            FROZEN_STEPS,
            halted_after="discovery",
            secrets=secrets,
        )
        return finish()

    try:
        try:
            store_report = redact_document(store_gate.preflight(), secrets)
        except Exception as exc:
            store_report = {
                "status": "failed",
                "detail": redact_text(f"{type(exc).__name__}: {exc}", secrets),
            }
        if store_report.get("status") != "passed":
            status = "failed"
            failed_step = "object-store-preflight"
            _append_not_run(
                records,
                evidence_dir,
                FROZEN_STEPS,
                halted_after="object-store-preflight",
                secrets=secrets,
            )
        else:
            for index, step in enumerate(FROZEN_STEPS):
                step_started = utc_now()
                record: dict[str, Any] = {
                    "schema": STEP_SCHEMA,
                    "step_id": step.step_id,
                    "group": step.group,
                    "script": f"tools/{step.script}",
                    "kind": step.kind,
                    "command": [],
                    "started_at": step_started,
                    "finished_at": None,
                    "duration_seconds": None,
                    "exit_code": None,
                    "status": "running",
                    "evidence_path": None,
                    "probe_evidence_path": None,
                }
                records.append(record)
                try:
                    stage_report = redact_document(stages.ensure(step.step_id), secrets)
                    stage_reports.append(stage_report)
                    record["revision"] = stage_report.get("after") or stage_report.get("target")
                    if stage_report.get("status") != "passed":
                        raise ProbeConfigurationError(
                            str(stage_report.get("detail") or "revision mismatch")
                        )
                    clear_step_evidence(evidence_dir, step)
                    argv = os_argv(step, evidence_dir)
                    if argv_contains_secret(argv, secrets):
                        raise ProbeConfigurationError(
                            "refusing to spawn a child whose OS argv contains secrets"
                        )
                    record["command"] = redact_argv(argv, secrets)
                    completed = runner(argv, cwd=PLATFORM_DIR, env=child_env(step))
                    exit_code = int(completed.returncode)
                    output = completed.stdout or ""
                    record["exit_code"] = exit_code
                    record["output_excerpt"] = redact_text(output[-4000:], secrets)
                    ok, reason = step_passed(
                        exit_code=exit_code,
                        stdout=output,
                        step=step,
                        evidence_dir=evidence_dir,
                    )
                    record["status"] = "passed" if ok else "failed"
                    if not ok:
                        record["detail"] = reason
                except ProbeConfigurationError as exc:
                    record["exit_code"] = 2
                    record["status"] = "failed"
                    record["detail"] = str(exc)
                except Exception as exc:
                    record["exit_code"] = 1
                    record["status"] = "failed"
                    record["detail"] = redact_text(f"{type(exc).__name__}: {exc}", secrets)
                    record["traceback"] = redact_text(traceback.format_exc(), secrets)
                finished = utc_now()
                record["finished_at"] = finished
                start_dt = datetime.fromisoformat(step_started.replace("Z", "+00:00"))
                end_dt = datetime.fromisoformat(finished.replace("Z", "+00:00"))
                record["duration_seconds"] = max(0, int((end_dt - start_dt).total_seconds()))
                step_file = evidence_path(evidence_dir, step)
                write_json(step_file, record)
                if not step_file.is_file():
                    record["status"] = "failed"
                    record["detail"] = "failed-to-write-step-evidence"
                else:
                    record["evidence_path"] = str(step_file)
                probe_file = probe_evidence_path(evidence_dir, step)
                if probe_file.is_file():
                    record["probe_evidence_path"] = str(probe_file)
                if record["status"] != "passed" or record.get("evidence_path") is None:
                    record["status"] = "failed"
                    status = "failed"
                    failed_step = step.step_id
                    write_json(step_file, record)
                    _append_not_run(
                        records,
                        evidence_dir,
                        FROZEN_STEPS[index + 1 :],
                        halted_after=step.step_id,
                        secrets=secrets,
                    )
                    break
    finally:
        try:
            cleanup_report = redact_document(store_gate.cleanup(), secrets)
        except Exception as exc:
            cleanup_report = {
                "status": "failed",
                "detail": redact_text(f"{type(exc).__name__}: {exc}", secrets),
            }
        store_report = {**store_report, "cleanup": cleanup_report}
        if cleanup_report.get("status") not in {"passed", NOT_RUN} and status == "passed":
            status = "failed"
            failed_step = "object-store-cleanup"
        if previous_database_url is None:
            os.environ.pop("UAP_DATABASE_URL", None)
        else:
            os.environ["UAP_DATABASE_URL"] = previous_database_url

    return finish()


def _summary(
    status: str,
    started: str,
    failed_step: str | None,
    records: list[dict[str, Any]],
    *,
    object_store: dict[str, Any] | None = None,
    migration_stages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    counts = {
        "passed": sum(1 for item in records if item.get("status") == "passed"),
        "failed": sum(1 for item in records if item.get("status") == "failed"),
        "not_run": sum(1 for item in records if item.get("status") == NOT_RUN),
    }
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "status": status,
        "signed_sha": SIGNED_SHA,
        "start_sha": START_SHA,
        "migration_head": HEAD,
        "started_at": started,
        "finished_at": utc_now(),
        "failed_step": failed_step,
        "step_counts": counts,
        "steps": records,
        "database_topology": {
            "required_environment_variables": list(DATABASE_TOPOLOGY_ENVS),
            "legacy": LEGACY_DATABASE_ENV,
            "wp10_1": "UAP_WP10_1_DATABASE_URL",
            "wp10_2": "UAP_WP10_2_DATABASE_URL",
            "wp10_3": "UAP_WP10_3_DATABASE_URL",
            "wp10_4": "UAP_WP10_4_DATABASE_URL",
            "wp10_5": "UAP_WP10_5_DATABASE_URL",
            "regression": REGRESSION_DATABASE_ENV,
        },
    }
    if object_store is not None:
        payload["object_store"] = object_store
    if migration_stages is not None:
        payload["migration_stages"] = migration_stages
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fail-closed WP3 through WP10.5 runtime orchestrator"
    )
    parser.add_argument(
        "--evidence-dir", type=Path, help="directory outside the source boundary for JSON evidence"
    )
    parser.add_argument(
        "--exec-child",
        dest="exec_child",
        help="internal: run one frozen step in-process so DSN never appears in OS argv",
    )
    parser.add_argument(
        "--plan",
        "--list",
        dest="plan",
        action="store_true",
        help="print frozen step list; does not run probes and does not report passed",
    )
    return parser.parse_args(argv)


def run_exec_child(step_id: str, evidence_dir: Path) -> int:
    step = STEPS_BY_ID.get(step_id)
    if step is None:
        print(f"unknown step: {step_id}", file=sys.stderr)
        return 2
    script = TOOLS_DIR / step.script
    original_argv = sys.argv
    original_argv_values = list(original_argv)
    original_sys_path = sys.path
    original_sys_path_values = list(original_sys_path)
    try:
        sys.path.insert(0, str(TOOLS_DIR))
        sys.argv = [str(script), *extra_args(step, evidence_dir)]
        try:
            runpy.run_path(str(script), run_name="__main__")
        except SystemExit as exc:
            code = exc.code
            if code is None:
                return 0
            if isinstance(code, int):
                return code
            return 1
        return 0
    finally:
        original_sys_path[:] = original_sys_path_values
        sys.path = original_sys_path
        original_argv[:] = original_argv_values
        sys.argv = original_argv


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv if argv is None else [sys.argv[0], *argv])
    try:
        reject_cli_secrets(raw_argv)
        args = parse_args(None if argv is None else argv)
    except ProbeConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.plan:
        plan_payload = {
            "schema": SCHEMA,
            "status": "plan",
            "signed_sha": SIGNED_SHA,
            "start_sha": START_SHA,
            "migration_head": HEAD,
            "live": "NOT RUN / NOT PROVEN",
            "object_store_contract": {
                "required": "disposable-sidecar",
                "status": "plan",
                "shared_instance_rejected": True,
                "live": "NOT RUN / NOT PROVEN",
            },
            "runtime_advancement": [
                {"revision": "0019_manual_claims_binding", "steps": "WP3-WP9.6"},
                {"revision": "0020_wp10_publication_contract", "steps": "WP10.1-runtime"},
                {"revision": "0021_wp10_publisher_projection", "steps": "WP10.2-runtime"},
                {"revision": "0022_wp10_claim_search_projection", "steps": "WP10.3-runtime"},
                {"revision": "0023_wp10_api_read_indexes", "steps": "WP10.4-runtime"},
                {"revision": "0024_wp10_admin_replay", "steps": "WP10.5-runtime"},
            ],
            "steps": plan_steps(),
        }
        print(json.dumps(redact_document(plan_payload), ensure_ascii=False, indent=2))
        return 0
    if args.evidence_dir is None:
        message = (
            "--exec-child requires --evidence-dir"
            if args.exec_child
            else (
                "refusing to write evidence into the source tree; "
                "pass --evidence-dir outside the source boundary"
            )
        )
        print(message, file=sys.stderr)
        return 2
    try:
        evidence_dir = resolve_evidence_directory(
            args.evidence_dir,
            source_boundary=SOURCE_BOUNDARY,
        )
    except ProbeConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.exec_child:
        return run_exec_child(args.exec_child, evidence_dir)

    summary_path = evidence_dir / "wp10-runtime-evidence.json"
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "failed",
        "signed_sha": SIGNED_SHA,
        "start_sha": START_SHA,
        "migration_head": HEAD,
        "started_at": utc_now(),
        "finished_at": None,
        "failed_step": "orchestrator",
        "steps": [],
    }
    exit_code = 1
    try:
        payload = execute_steps(evidence_dir)
        exit_code = 0 if payload.get("status") == "passed" else 1
    except Exception as exc:
        payload["status"] = "failed"
        payload["failed_step"] = "orchestrator"
        payload["detail"] = redact_text(f"{type(exc).__name__}: {exc}")
        payload["traceback"] = redact_text(traceback.format_exc())
        payload["finished_at"] = utc_now()
        exit_code = 1
    finally:
        payload["finished_at"] = payload.get("finished_at") or utc_now()
        write_json(summary_path, payload)
        print(
            json.dumps(
                redact_document({"status": payload["status"], "evidence": str(summary_path)})
            )
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
