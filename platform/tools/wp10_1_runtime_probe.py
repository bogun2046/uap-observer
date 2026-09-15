"""WP10.1 runtime probe for G10-03 and G10-04 on real role connections.

G10-05 migration-state checks are run by the isolated migration harness because
they require starting from 0019 with legacy v1 rows before applying 0020.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_PLATFORM_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLATFORM_ROOT / "src"
for _path in (str(_PLATFORM_ROOT), str(_SRC)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import psycopg  # noqa: E402
from psycopg.conninfo import make_conninfo  # noqa: E402

from tools.wp8_1_runtime_probe import (  # noqa: E402
    insert_analysis,
    insert_model_run,
    insert_span,
    insert_supported_ai_claim,
    seed_document,
    sha256_text,
)
from tools.wp9_2_runtime_probe import (  # noqa: E402
    bind,
    bind_role,
    connect,
    execute,
    insert_person,
    open_sql,
    require,
    scalar,
    seed_grantor,
    sqlerror_tx,
)
from tools.wp9_5_runtime_probe import call_api  # noqa: E402
from tools.wp9_6_runtime_probe import manual_sql  # noqa: E402
from uap_platform.admin_api.contracts import (  # noqa: E402
    AssertionStatus,
    ClaimType,
    EntityType,
    ReviewCaseType,
    ReviewStatus,
)
from uap_platform.public_api.contracts import (  # noqa: E402
    DocumentCategory,
    FactStatus,
    LocatorType,
)

CURRENT_HEAD = "0020_wp10_publication_contract"
REASON = "wp10.1 runtime acceptance decision"
API_PASSWORD_ENV = "UAP_API_PASSWORD"  # noqa: S105
PUBLISHER_PASSWORD_ENV = "UAP_PUBLISHER_PASSWORD"  # noqa: S105
BOTTOM_MERGE = "core.merge_entities(uuid,uuid,uuid,text)"
BOTTOM_REVERSE = "core.reverse_entity_merge(uuid,uuid,text)"
REVIEW_DECISION = "audit.record_review_decision(uuid,audit.review_decision,text,jsonb)"
EVIDENCE_SCHEMA = "wp10.1-runtime-evidence.v1"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

SNAPSHOT_COUNT_SQL = {
    "audit.review_decisions": "SELECT count(*) FROM audit.review_decisions",
    "audit.document_publication_grants": "SELECT count(*) FROM audit.document_publication_grants",
    "audit.claim_publication_grants": "SELECT count(*) FROM audit.claim_publication_grants",
    "audit.entity_publication_grants": "SELECT count(*) FROM audit.entity_publication_grants",
    "audit.document_publication_manifests": (
        "SELECT count(*) FROM audit.document_publication_manifests"
    ),
    "audit.claim_publication_manifests": "SELECT count(*) FROM audit.claim_publication_manifests",
    "audit.claim_publication_manifest_evidence": (
        "SELECT count(*) FROM audit.claim_publication_manifest_evidence"
    ),
    "audit.entity_publication_manifests": "SELECT count(*) FROM audit.entity_publication_manifests",
    "audit.publication_quarantine": "SELECT count(*) FROM audit.publication_quarantine",
    "audit.audit_events": "SELECT count(*) FROM audit.audit_events",
    "ops.outbox_events": "SELECT count(*) FROM ops.outbox_events",
    "public.documents": "SELECT count(*) FROM public.documents",
}

LEGACY_ENUM_CASES: tuple[tuple[str, str, Any], ...] = (
    ("document_version", "audit.review_case_type", ReviewCaseType),
    ("candidate", "audit.review_status", ReviewStatus),
    ("in_review", "audit.review_status", ReviewStatus),
    ("place", "core.entity_type", EntityType),
    ("document", "core.entity_type", EntityType),
    ("topic", "core.entity_type", EntityType),
    ("characters", "core.locator_type", LocatorType),
    ("page", "core.locator_type", LocatorType),
    ("source_action", "core.claim_type", ClaimType),
    ("official_record", "core.assertion_status", AssertionStatus),
)

ENUM_CAST_SQL = {
    "audit.review_case_type": "SELECT %s::audit.review_case_type",
    "audit.review_status": "SELECT %s::audit.review_status",
    "core.entity_type": "SELECT %s::core.entity_type",
    "core.locator_type": "SELECT %s::core.locator_type",
    "core.claim_type": "SELECT %s::core.claim_type",
    "core.assertion_status": "SELECT %s::core.assertion_status",
}

ENUM_RANGE_SQL = {
    "audit.review_case_type": (
        "SELECT array_agg(value::text ORDER BY ordinality) "
        "FROM unnest(enum_range(NULL::audit.review_case_type)) "
        "WITH ORDINALITY AS item(value, ordinality)"
    ),
    "audit.review_status": (
        "SELECT array_agg(value::text ORDER BY ordinality) "
        "FROM unnest(enum_range(NULL::audit.review_status)) "
        "WITH ORDINALITY AS item(value, ordinality)"
    ),
    "core.entity_type": (
        "SELECT array_agg(value::text ORDER BY ordinality) "
        "FROM unnest(enum_range(NULL::core.entity_type)) "
        "WITH ORDINALITY AS item(value, ordinality)"
    ),
    "core.locator_type": (
        "SELECT array_agg(value::text ORDER BY ordinality) "
        "FROM unnest(enum_range(NULL::core.locator_type)) "
        "WITH ORDINALITY AS item(value, ordinality)"
    ),
    "core.claim_type": (
        "SELECT array_agg(value::text ORDER BY ordinality) "
        "FROM unnest(enum_range(NULL::core.claim_type)) "
        "WITH ORDINALITY AS item(value, ordinality)"
    ),
    "core.assertion_status": (
        "SELECT array_agg(value::text ORDER BY ordinality) "
        "FROM unnest(enum_range(NULL::core.assertion_status)) "
        "WITH ORDINALITY AS item(value, ordinality)"
    ),
    "public.document_category": (
        "SELECT array_agg(value::text ORDER BY ordinality) "
        "FROM unnest(enum_range(NULL::public.document_category)) "
        "WITH ORDINALITY AS item(value, ordinality)"
    ),
    "public.fact_status": (
        "SELECT array_agg(value::text ORDER BY ordinality) "
        "FROM unnest(enum_range(NULL::public.fact_status)) "
        "WITH ORDINALITY AS item(value, ordinality)"
    ),
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def repository_head(repository: Path) -> str:
    marker = repository / ".git"
    if marker.is_file():
        text = marker.read_text(encoding="utf-8").strip()
        if not text.startswith("gitdir: "):
            return "unavailable"
        git_dir = (repository / text.removeprefix("gitdir: ")).resolve()
    else:
        git_dir = marker
    head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    if COMMIT_RE.fullmatch(head):
        return head
    if not head.startswith("ref: "):
        return "unavailable"
    reference = head.removeprefix("ref: ")
    loose = git_dir / reference
    if loose.is_file():
        value = loose.read_text(encoding="utf-8").strip()
        return value if COMMIT_RE.fullmatch(value) else "unavailable"
    packed = git_dir / "packed-refs"
    if packed.is_file():
        suffix = f" {reference}"
        for line in packed.read_text(encoding="utf-8").splitlines():
            if line.endswith(suffix):
                value = line.split(" ", 1)[0]
                return value if COMMIT_RE.fullmatch(value) else "unavailable"
    return "unavailable"


def source_identity() -> dict[str, str]:
    source = Path(__file__).read_bytes()
    git = shutil.which("git")
    if git is None:
        return {
            "commit": repository_head(_PLATFORM_ROOT.parent),
            "probe_sha256": hashlib.sha256(source).hexdigest(),
        }
    result = subprocess.run(  # noqa: S603
        [
            git,
            "-c",
            f"safe.directory={_PLATFORM_ROOT.parent}",
            "-C",
            str(_PLATFORM_ROOT.parent),
            "rev-parse",
            "HEAD",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return {
        "commit": result.stdout.strip() if result.returncode == 0 else "unavailable",
        "probe_sha256": hashlib.sha256(source).hexdigest(),
    }


def stable_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def database_snapshot(admin: psycopg.Connection[Any]) -> dict[str, object]:
    counts = {
        table: int(scalar(admin, statement)) for table, statement in SNAPSHOT_COUNT_SQL.items()
    }
    return {"counts": counts, "digest": stable_digest(counts)}


class EvidenceRecorder:
    def __init__(self, initial_revision: str) -> None:
        self.started_at = utc_now()
        self.initial_revision = initial_revision
        self.cases: list[dict[str, object]] = []

    def add(self, case_id: str, requirement: str, name: str, **details: object) -> None:
        self.cases.append(
            {
                "id": case_id,
                "requirement": requirement,
                "name": name,
                "status": "passed",
                **details,
            }
        )

    def payload(
        self,
        *,
        status: str,
        final_revision: str | None,
        postgres_version: str | None,
        failure: str | None = None,
    ) -> dict[str, object]:
        passed = sum(case["status"] == "passed" for case in self.cases)
        failed = sum(case["status"] == "failed" for case in self.cases)
        not_run = sum(case["status"] == "not_run" for case in self.cases)
        return {
            "schema_version": 1,
            "schema": EVIDENCE_SCHEMA,
            "probe": "wp10_1_runtime_probe",
            "requirements": ["G10-02", "G10-03", "G10-04"],
            "status": status,
            "source": source_identity(),
            "environment": {
                "python": platform.python_version(),
                "postgresql": postgres_version,
            },
            "started_at": self.started_at,
            "finished_at": utc_now(),
            "initial_revision": self.initial_revision,
            "target_revision": CURRENT_HEAD,
            "final_revision": final_revision,
            "summary": {
                "total": len(self.cases),
                "passed": passed,
                "failed": failed,
                "not_run": not_run,
            },
            "cases": self.cases,
            "failure": failure,
        }


def connect_role(url: str, role: str, password_env: str) -> psycopg.Connection[Any]:
    password = os.environ.get(password_env)
    if not password:
        raise RuntimeError(f"missing {password_env} for {role}")
    connection = psycopg.connect(make_conninfo(url, user=role, password=password))
    connection.autocommit = True
    return connection


def decide_sql(
    case_id: uuid.UUID,
    decision: str,
    changes: Mapping[str, object],
) -> tuple[str, tuple[object, ...]]:
    return (
        "SELECT audit.record_review_decision(%s, %s::audit.review_decision, %s, %s::jsonb)",
        (case_id, decision, REASON, json.dumps(changes, separators=(",", ":"))),
    )


def has_execute(admin: psycopg.Connection[Any], role: str, signature: str) -> bool:
    return bool(scalar(admin, "SELECT has_function_privilege(%s, %s, 'EXECUTE')", role, signature))


def one(connection: psycopg.Connection[Any], statement: str, *params: object) -> tuple[Any, ...]:
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("probe query returned no row")
    return tuple(row)


def expect_denied(
    connection: psycopg.Connection[Any], name: str, statement: str, params: tuple[object, ...] = ()
) -> tuple[str | None, str | None]:
    state, primary = sqlerror_tx(connection, [(statement, params)])
    require(name, state, "42501")
    return state, primary


def g10_02_enums(admin: psycopg.Connection[Any], evidence: EvidenceRecorder) -> None:
    before = database_snapshot(admin)
    for index, (legacy, database_type, api_enum) in enumerate(LEGACY_ENUM_CASES, start=1):
        state, primary = sqlerror_tx(admin, [(ENUM_CAST_SQL[database_type], (legacy,))])
        require(f"legacy DB enum rejects {legacy}", state, "22P02")
        api_exception: str | None = None
        try:
            api_enum(legacy)
        except ValueError as error:
            api_exception = type(error).__name__
        require(f"OpenAPI enum rejects {legacy}", api_exception, "ValueError")
        after = database_snapshot(admin)
        require(f"legacy enum {legacy} leaves database unchanged", after, before)
        evidence.add(
            f"G10-02-legacy-{index:02d}",
            "G10-02",
            f"legacy value {legacy} rejected",
            role="uap_owner",
            operation=f"enum cast {database_type} and OpenAPI DTO validation",
            related_ids={},
            expected={
                "database_sqlstate": "22P02",
                "api_exception": "ValueError",
                "http_status": None,
                "request_id": None,
            },
            actual={
                "database_sqlstate": state,
                "database_error": primary,
                "api_exception": api_exception,
                "http_status": None,
                "request_id": None,
            },
            before=before,
            after=after,
            api_boundary="frozen OpenAPI enum DTO; no HTTP input exists for every enum",
        )

    registered: tuple[tuple[str, Any], ...] = (
        ("audit.review_case_type", ReviewCaseType),
        ("audit.review_status", ReviewStatus),
        ("core.entity_type", EntityType),
        ("core.locator_type", LocatorType),
        ("core.claim_type", ClaimType),
        ("core.assertion_status", AssertionStatus),
        ("public.document_category", DocumentCategory),
        ("public.fact_status", FactStatus),
    )
    documented_db_only_values = {"audit.review_case_type": {"relation"}}
    for index, (database_type, api_enum) in enumerate(registered, start=1):
        database_values = one(admin, ENUM_RANGE_SQL[database_type])[0]
        api_values = [item.value for item in api_enum]
        database_value_set = set(database_values)
        api_value_set = set(api_values)
        require(
            f"registered API enum {database_type} is accepted by database",
            api_value_set <= database_value_set,
            True,
        )
        require(
            f"registered DB-only enum values {database_type}",
            database_value_set - api_value_set,
            documented_db_only_values.get(database_type, set()),
        )
        evidence.add(
            f"G10-02-positive-{index:02d}",
            "G10-02",
            f"registered enum {database_type} matches API",
            role="uap_owner",
            operation=f"enum_range {database_type}",
            related_ids={},
            expected={
                "api_values_accepted_by_database": api_values,
                "documented_db_only_values": sorted(
                    documented_db_only_values.get(database_type, set())
                ),
            },
            actual={
                "database_values": database_values,
                "api_values": api_values,
                "database_only_values": sorted(database_value_set - api_value_set),
            },
            before=before,
            after=database_snapshot(admin),
        )


def timezone_independent_payload(
    admin: psycopg.Connection[Any],
    grant_id: uuid.UUID,
    decision_id: uuid.UUID,
    document_version_id: uuid.UUID,
    publication: Mapping[str, object],
) -> None:
    payloads: list[dict[str, object]] = []
    timezone_sql = {
        "Asia/Shanghai": "SET LOCAL TIME ZONE 'Asia/Shanghai'",
        "UTC": "SET LOCAL TIME ZONE 'UTC'",
    }
    for timezone in ("Asia/Shanghai", "UTC"):
        with admin.transaction():
            with admin.cursor() as cursor:
                cursor.execute(timezone_sql[timezone])
                cursor.execute(
                    """
                    SELECT audit._document_publication_payload(
                        %s, %s, %s, 1, %s::jsonb
                    )
                    """,
                    (grant_id, decision_id, document_version_id, json.dumps(publication)),
                )
                row = cursor.fetchone()
        if row is None or not isinstance(row[0], dict):
            raise RuntimeError("timezone payload probe returned no JSON object")
        payloads.append(row[0])
    require("timezone-independent canonical payload", payloads[0], payloads[1])
    require(
        "canonical source timestamp is UTC",
        payloads[0]["source_published_at"],
        "2024-01-02T03:04:05.123456Z",
    )


def legacy_revise_resolves_quarantine(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    actor: uuid.UUID,
) -> dict[str, str]:
    _principal, document_version_id, _source_id = seed_document(
        admin, f"wp10-legacy-revise-{uuid.uuid4().hex[:8]}"
    )
    case_id = uuid.uuid4()
    previous_decision_id = uuid.uuid4()
    legacy_grant_id = uuid.uuid4()
    legacy_event_id = uuid.uuid4()
    grant_quarantine_id = uuid.uuid4()
    event_quarantine_id = uuid.uuid4()
    publication = {
        "publication": {
            "title": "WP10.1 legacy revision",
            "summary": "A valid v2 replacement for a quarantined grant.",
            "category": "official_report",
            "fact_status": "source_reported",
            "summary_analysis_result_id": None,
        }
    }
    now = datetime.now(UTC)
    execute(
        admin,
        """
        INSERT INTO audit.review_cases (
            id, document_version_id, case_type, status, priority,
            opened_by, opened_at
        ) VALUES (%s, %s, 'document', 'approved', 0, %s, %s)
        """,
        case_id,
        document_version_id,
        actor,
        now,
    )
    execute(
        admin,
        """
        INSERT INTO audit.review_decisions (
            id, review_case_id, sequence_no, decision, reason,
            structured_changes, decided_by, decided_at
        ) VALUES (%s, %s, 1, 'approve', %s, %s::jsonb, %s, %s)
        """,
        previous_decision_id,
        case_id,
        REASON,
        json.dumps(publication, separators=(",", ":")),
        actor,
        now,
    )
    execute(
        admin,
        """
        INSERT INTO audit.document_publication_grants (
            id, review_case_id, document_version_id, decision_id,
            revision_no, grant_status, granted_at, publication_payload_sha256
        ) VALUES (%s, %s, %s, %s, 1, 'active', %s, repeat('a', 64))
        """,
        legacy_grant_id,
        case_id,
        document_version_id,
        previous_decision_id,
        now,
    )
    execute(
        admin,
        """
        INSERT INTO ops.outbox_events (
            id, aggregate_type, aggregate_id, event_type, event_key,
            payload, occurred_at
        ) VALUES (%s, 'document_publication_grants', %s, 'publication.granted', %s,
                  '{"schema":"publication-outbox.v1"}'::jsonb, %s)
        """,
        legacy_event_id,
        legacy_grant_id,
        f"wp10-legacy-revise:{legacy_event_id}",
        now,
    )
    execute(
        admin,
        """
        INSERT INTO audit.publication_quarantine (
            id, grant_table, grant_id, reason_code
        ) VALUES (%s, 'document_publication_grants', %s, 'publication_manifest_required')
        """,
        grant_quarantine_id,
        legacy_grant_id,
    )
    execute(
        admin,
        """
        INSERT INTO audit.publication_quarantine (id, event_id, reason_code)
        VALUES (%s, %s, 'publication_manifest_required')
        """,
        event_quarantine_id,
        legacy_event_id,
    )
    call_api(api, actor, uuid.uuid4(), decide_sql(case_id, "revise", publication))
    require(
        "legacy grant superseded by v2 revise",
        scalar(
            admin,
            "SELECT grant_status::text FROM audit.document_publication_grants WHERE id = %s",
            legacy_grant_id,
        ),
        "superseded",
    )
    require(
        "legacy quarantine rows resolved by v2 revise",
        scalar(
            admin,
            """
            SELECT count(*) FROM audit.publication_quarantine
             WHERE resolves_quarantine_id IN (%s, %s)
               AND resolution = 'resolved_by_v2_revision'
            """,
            grant_quarantine_id,
            event_quarantine_id,
        ),
        2,
    )
    require(
        "legacy revise creates v2 manifest",
        scalar(
            admin,
            """
            SELECT count(*) FROM audit.document_publication_manifests AS manifest
             JOIN audit.document_publication_grants AS grant_row
               ON grant_row.id = manifest.grant_id
            WHERE grant_row.review_case_id = %s AND grant_row.revision_no = 2
            """,
            case_id,
        ),
        1,
    )
    return {
        "review_case_id": str(case_id),
        "legacy_grant_id": str(legacy_grant_id),
        "legacy_event_id": str(legacy_event_id),
        "grant_quarantine_id": str(grant_quarantine_id),
        "event_quarantine_id": str(event_quarantine_id),
    }


def g10_03_permissions(
    admin: psycopg.Connection[Any],
    role_connections: Mapping[str, psycopg.Connection[Any]],
    evidence: EvidenceRecorder,
) -> None:
    api = role_connections["uap_api"]
    publisher = role_connections["uap_publisher"]
    before = database_snapshot(admin)
    denied_statements = {
        "core.documents": {
            "INSERT": "INSERT INTO core.documents DEFAULT VALUES",
            "UPDATE": "UPDATE core.documents SET source_item_key = source_item_key WHERE false",
            "DELETE": "DELETE FROM core.documents WHERE false",
        },
        "core.claims": {
            "INSERT": "INSERT INTO core.claims DEFAULT VALUES",
            "UPDATE": "UPDATE core.claims SET claim_text = claim_text WHERE false",
            "DELETE": "DELETE FROM core.claims WHERE false",
        },
        "core.entities": {
            "INSERT": "INSERT INTO core.entities DEFAULT VALUES",
            "UPDATE": "UPDATE core.entities SET canonical_name = canonical_name WHERE false",
            "DELETE": "DELETE FROM core.entities WHERE false",
        },
        "ops.outbox_events": {
            "INSERT": "INSERT INTO ops.outbox_events DEFAULT VALUES",
            "UPDATE": "UPDATE ops.outbox_events SET event_key = event_key WHERE false",
            "DELETE": "DELETE FROM ops.outbox_events WHERE false",
        },
        "audit.review_cases": {
            "INSERT": "INSERT INTO audit.review_cases DEFAULT VALUES",
            "UPDATE": "UPDATE audit.review_cases SET priority = priority WHERE false",
            "DELETE": "DELETE FROM audit.review_cases WHERE false",
        },
    }
    case_no = 0
    for table in (
        "core.documents",
        "core.claims",
        "core.entities",
        "ops.outbox_events",
        "audit.review_cases",
    ):
        for privilege in ("INSERT", "UPDATE", "DELETE"):
            case_no += 1
            require(
                f"uap_api no {privilege} {table}",
                scalar(admin, "SELECT has_table_privilege('uap_api', %s, %s)", table, privilege),
                False,
            )
            state, primary = expect_denied(
                api,
                f"uap_api direct {privilege} {table}",
                denied_statements[table][privilege],
            )
            after = database_snapshot(admin)
            require(f"uap_api denied {privilege} {table} is atomic", after, before)
            evidence.add(
                f"G10-03-api-dml-{case_no:02d}",
                "G10-03",
                f"uap_api {privilege} {table} denied",
                role="uap_api",
                operation=f"{privilege} {table}",
                related_ids={},
                expected={"sqlstate": "42501"},
                actual={"sqlstate": state, "error": primary},
                before=before,
                after=after,
            )
    for privilege in ("INSERT", "UPDATE", "DELETE"):
        require(
            f"uap_publisher no {privilege} public.documents",
            scalar(
                admin,
                "SELECT has_table_privilege('uap_publisher', 'public.documents', %s)",
                privilege,
            ),
            False,
        )

    for index, (privilege, statement) in enumerate(
        (
            ("INSERT", "INSERT INTO public.documents DEFAULT VALUES"),
            ("UPDATE", "UPDATE public.documents SET title = title WHERE false"),
            ("DELETE", "DELETE FROM public.documents WHERE false"),
        ),
        start=1,
    ):
        state, primary = expect_denied(
            publisher, f"uap_publisher direct public {privilege}", statement
        )
        after = database_snapshot(admin)
        require(f"publisher denied public {privilege} is atomic", after, before)
        evidence.add(
            f"G10-03-publisher-dml-{index:02d}",
            "G10-03",
            f"uap_publisher {privilege} public.documents denied",
            role="uap_publisher",
            operation=f"{privilege} public.documents",
            related_ids={},
            expected={"sqlstate": "42501"},
            actual={"sqlstate": state, "error": primary},
            before=before,
            after=after,
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
        require(f"{role} no bottom merge", has_execute(admin, role, BOTTOM_MERGE), False)
        require(f"{role} no bottom reverse", has_execute(admin, role, BOTTOM_REVERSE), False)
        connection = role_connections[role]
        for suffix, statement in (
            (
                "merge",
                "SELECT core.merge_entities(NULL::uuid, NULL::uuid, NULL::uuid, 'probe'::text)",
            ),
            (
                "reverse",
                "SELECT core.reverse_entity_merge(NULL::uuid, NULL::uuid, 'probe'::text)",
            ),
        ):
            state, primary = expect_denied(connection, f"{role} bottom {suffix}", statement)
            evidence.add(
                f"G10-03-{role}-{suffix}",
                "G10-03",
                f"{role} cannot execute bottom {suffix}",
                role=role,
                operation=statement.split("(", 1)[0].removeprefix("SELECT "),
                related_ids={},
                expected={"sqlstate": "42501"},
                actual={"sqlstate": state, "error": primary},
                before=before,
                after=database_snapshot(admin),
            )
    require(
        "uap_publisher no review decision",
        has_execute(admin, "uap_publisher", REVIEW_DECISION),
        False,
    )
    for role in ("uap_worker", "uap_publisher", "uap_public_reader"):
        state, primary = expect_denied(
            role_connections[role],
            f"{role} review decision",
            "SELECT audit.record_review_decision("
            "NULL::uuid, NULL::audit.review_decision, NULL::text, NULL::jsonb)",
        )
        evidence.add(
            f"G10-03-{role}-review",
            "G10-03",
            f"{role} cannot execute review decision",
            role=role,
            operation="audit.record_review_decision",
            related_ids={},
            expected={"sqlstate": "42501"},
            actual={"sqlstate": state, "error": primary},
            before=before,
            after=database_snapshot(admin),
        )
    for signature in (
        "ops.ack_outbox(uuid,uuid)",
        "ops.claim_outbox(text,integer,integer)",
        "ops.publish_outbox_failure(uuid,uuid,text,text,integer)",
    ):
        require(
            f"uap_publisher no generic {signature}",
            has_execute(admin, "uap_publisher", signature),
            False,
        )


def g10_04_manifests(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    evidence: EvidenceRecorder,
) -> None:
    seed_grantor(admin)
    actor = insert_person(admin)
    bind_role(admin, actor, "reviewer")
    second_reviewer = insert_person(admin)
    bind_role(admin, second_reviewer, "reviewer")
    _principal, document_version_id, _source_id = seed_document(
        admin, f"wp10-document-{uuid.uuid4().hex[:8]}"
    )
    execute(
        admin,
        "UPDATE core.document_versions SET source_published_at = %s WHERE id = %s",
        datetime(2024, 1, 2, 3, 4, 5, 123456, tzinfo=UTC),
        document_version_id,
    )

    document_case = call_api(
        api,
        actor,
        uuid.uuid4(),
        open_sql("document", document_version_id, REASON),
    )
    publication = {
        "publication": {
            "title": "WP10.1 runtime document",
            "summary": "A stable publication snapshot.",
            "category": "official_report",
            "fact_status": "source_reported",
            "summary_analysis_result_id": None,
        }
    }
    document_before = database_snapshot(admin)
    decision_id = call_api(
        api, actor, uuid.uuid4(), decide_sql(document_case, "approve", publication)
    )
    grant_id, grant_sha = one(
        admin,
        (
            "SELECT id, publication_payload_sha256 "
            "FROM audit.document_publication_grants WHERE decision_id = %s"
        ),
        decision_id,
    )
    manifest = scalar(
        admin,
        (
            "SELECT jsonb_build_object('title', title, 'summary', summary, "
            "'category', category::text, 'fact_status', fact_status::text, "
            "'source_name', source_name, 'canonical_source_url', canonical_source_url) "
            "FROM audit.document_publication_manifests WHERE grant_id = %s"
        ),
        grant_id,
    )
    manifest_sha = scalar(
        admin,
        "SELECT manifest_sha256 FROM audit.document_publication_manifests WHERE grant_id = %s",
        grant_id,
    )
    event_schema, event_sha = one(
        admin,
        (
            "SELECT payload ->> 'schema', payload ->> 'payload_sha256' "
            "FROM ops.outbox_events "
            "WHERE aggregate_id = %s AND event_type = 'publication.granted'"
        ),
        grant_id,
    )
    require("document grant hash", grant_sha, manifest_sha)
    require("document outbox schema", event_schema, "publication-outbox.v2")
    require("document outbox hash", event_sha, manifest_sha)
    require("document title snapshot", manifest["title"], publication["publication"]["title"])
    require(
        "document audit event",
        scalar(
            admin,
            (
                "SELECT count(*) FROM audit.audit_events "
                "WHERE target_id = %s AND action = 'review.decision'"
            ),
            decision_id,
        ),
        1,
    )
    document_after = database_snapshot(admin)
    evidence.add(
        "G10-04-document-approve",
        "G10-04",
        "document approve atomically creates v2 publication records",
        role="uap_api",
        operation="audit.record_review_decision approve document",
        related_ids={
            "review_case_id": str(document_case),
            "decision_id": str(decision_id),
            "grant_id": str(grant_id),
            "document_version_id": str(document_version_id),
        },
        expected={"manifest_hash_equals_grant_and_outbox": True},
        actual={
            "grant_sha256": grant_sha,
            "manifest_sha256": manifest_sha,
            "outbox_sha256": event_sha,
            "outbox_schema": event_schema,
        },
        before=document_before,
        after=document_after,
    )
    timezone_independent_payload(admin, grant_id, decision_id, document_version_id, publication)
    revise_before = database_snapshot(admin)
    revise_ids = legacy_revise_resolves_quarantine(admin, api, actor)
    evidence.add(
        "G10-04-document-revise",
        "G10-04",
        "document revise creates v2 manifest and resolves quarantine",
        role="uap_api",
        operation="audit.record_review_decision revise document",
        related_ids=revise_ids,
        expected={"quarantine_resolutions": 2},
        actual={"quarantine_resolutions": 2},
        before=revise_before,
        after=database_snapshot(admin),
    )

    _principal, claim_document_version_id, _source_id = seed_document(
        admin, f"wp10-claim-{uuid.uuid4().hex[:8]}"
    )
    claim_document_case = call_api(
        api,
        actor,
        uuid.uuid4(),
        open_sql("document", claim_document_version_id, REASON),
    )
    claim_document_publication = {
        "publication": {
            "title": "WP10.1 claim source",
            "summary": "A source document for the claim manifest.",
            "category": "official_report",
            "fact_status": "source_reported",
            "summary_analysis_result_id": None,
        }
    }
    call_api(
        api,
        actor,
        uuid.uuid4(),
        decide_sql(claim_document_case, "approve", claim_document_publication),
    )
    span_id = insert_span(admin, claim_document_version_id, "b" * 64)
    claim_id = call_api(
        api,
        actor,
        uuid.uuid4(),
        manual_sql(claim_document_version_id, "A runtime-supported claim", "witness", [span_id]),
    )
    claim_case = call_api(api, actor, uuid.uuid4(), open_sql("claim", claim_id, REASON))
    claim_before = database_snapshot(admin)
    claim_decision = call_api(
        api, second_reviewer, uuid.uuid4(), decide_sql(claim_case, "approve", {})
    )
    claim_grant_id, claim_sha = one(
        admin,
        (
            "SELECT id, publication_payload_sha256 "
            "FROM audit.claim_publication_grants WHERE decision_id = %s"
        ),
        claim_decision,
    )
    claim_manifest_sha, evidence_count = one(
        admin,
        (
            "SELECT manifest.manifest_sha256, "
            "(SELECT count(*) FROM audit.claim_publication_manifest_evidence AS evidence "
            "WHERE evidence.grant_id = manifest.grant_id) "
            "FROM audit.claim_publication_manifests AS manifest "
            "WHERE manifest.grant_id = %s"
        ),
        claim_grant_id,
    )
    require("claim manifest hash", claim_sha, claim_manifest_sha)
    require("claim manifest evidence", evidence_count, 1)
    evidence.add(
        "G10-04-claim-post-change",
        "G10-04",
        "claim manifest captures post-change state",
        role="uap_api",
        operation="audit.record_review_decision approve claim",
        related_ids={
            "review_case_id": str(claim_case),
            "decision_id": str(claim_decision),
            "grant_id": str(claim_grant_id),
            "claim_id": str(claim_id),
            "document_version_id": str(claim_document_version_id),
            "evidence_span_id": str(span_id),
        },
        expected={"evidence_count": 1, "manifest_hash_equals_grant": True},
        actual={
            "evidence_count": evidence_count,
            "grant_sha256": claim_sha,
            "manifest_sha256": claim_manifest_sha,
        },
        before=claim_before,
        after=database_snapshot(admin),
    )

    append_only_before = database_snapshot(admin)
    state, primary = sqlerror_tx(
        admin,
        [
            (
                "UPDATE audit.claim_publication_manifests "
                "SET claim_text = claim_text WHERE grant_id = %s",
                (claim_grant_id,),
            )
        ],
    )
    require("manifest append-only sqlstate", state, "55000")
    require("manifest append-only error", primary, "publication contract history is immutable")
    append_only_after = database_snapshot(admin)
    require("manifest append-only rejection is atomic", append_only_after, append_only_before)
    evidence.add(
        "G10-04-manifest-append-only",
        "G10-04",
        "published manifest history rejects mutation",
        role="uap_owner",
        operation="UPDATE audit.claim_publication_manifests",
        related_ids={"grant_id": str(claim_grant_id)},
        expected={"sqlstate": "55000", "code": "publication contract history is immutable"},
        actual={"sqlstate": state, "code": primary},
        before=append_only_before,
        after=append_only_after,
    )

    entity_id = uuid.uuid4()
    execute(
        admin,
        (
            "INSERT INTO core.entities (id, entity_type, canonical_name, status) "
            "VALUES (%s, 'person', %s, 'active')"
        ),
        entity_id,
        f"WP10 runtime entity {entity_id}",
    )
    entity_case = call_api(api, actor, uuid.uuid4(), open_sql("entity", entity_id, REASON))
    entity_before = database_snapshot(admin)
    entity_decision = call_api(
        api, second_reviewer, uuid.uuid4(), decide_sql(entity_case, "approve", {})
    )
    entity_grant_id, entity_sha = one(
        admin,
        (
            "SELECT id, publication_payload_sha256 "
            "FROM audit.entity_publication_grants WHERE decision_id = %s"
        ),
        entity_decision,
    )
    entity_manifest_sha = scalar(
        admin,
        "SELECT manifest_sha256 FROM audit.entity_publication_manifests WHERE grant_id = %s",
        entity_grant_id,
    )
    require("entity manifest hash", entity_sha, entity_manifest_sha)
    evidence.add(
        "G10-04-entity-post-change",
        "G10-04",
        "entity manifest captures post-change state",
        role="uap_api",
        operation="audit.record_review_decision approve entity",
        related_ids={
            "review_case_id": str(entity_case),
            "decision_id": str(entity_decision),
            "grant_id": str(entity_grant_id),
            "entity_id": str(entity_id),
        },
        expected={"manifest_hash_equals_grant": True},
        actual={"grant_sha256": entity_sha, "manifest_sha256": entity_manifest_sha},
        before=entity_before,
        after=database_snapshot(admin),
    )

    invalid_before = database_snapshot(admin)
    state, primary = sqlerror_tx(
        api,
        [
            *bind(actor, uuid.uuid4()),
            decide_sql(
                document_case,
                "revise",
                {"publication": {**publication["publication"], "unknown": "reject"}},
            ),
        ],
    )
    require("unknown publication field sqlstate", state, "22023")
    require("unknown publication field error", primary, "publication_manifest_invalid")
    invalid_after = database_snapshot(admin)
    require("unknown publication field leaves all state unchanged", invalid_after, invalid_before)
    evidence.add(
        "G10-04-invalid-unknown-field",
        "G10-04",
        "unknown publication field rejected atomically",
        role="uap_api",
        operation="audit.record_review_decision revise document",
        related_ids={"review_case_id": str(document_case)},
        expected={"sqlstate": "22023", "code": "publication_manifest_invalid"},
        actual={"sqlstate": state, "code": primary},
        before=invalid_before,
        after=invalid_after,
    )

    invalid_variants = (
        (
            "bad-basis",
            {
                "publication": {
                    **publication["publication"],
                    "summary_analysis_result_id": str(uuid.uuid4()),
                }
            },
            "22023",
            "publication_manifest_invalid",
        ),
        (
            "missing-title",
            {
                "publication": {
                    key: value
                    for key, value in publication["publication"].items()
                    if key != "title"
                }
            },
            "22023",
            "publication_manifest_invalid",
        ),
    )
    for suffix, changes, expected_state, expected_code in invalid_variants:
        before = database_snapshot(admin)
        state, primary = sqlerror_tx(
            api,
            [*bind(actor, uuid.uuid4()), decide_sql(document_case, "revise", changes)],
        )
        require(f"{suffix} sqlstate", state, expected_state)
        require(f"{suffix} code", primary, expected_code)
        after = database_snapshot(admin)
        require(f"{suffix} leaves all state unchanged", after, before)
        evidence.add(
            f"G10-04-invalid-{suffix}",
            "G10-04",
            f"{suffix} rejected atomically",
            role="uap_api",
            operation="audit.record_review_decision revise document",
            related_ids={"review_case_id": str(document_case)},
            expected={"sqlstate": expected_state, "code": expected_code},
            actual={"sqlstate": state, "code": primary},
            before=before,
            after=after,
        )

    _principal, no_url_version_id, _source_id = seed_document(
        admin, f"wp10-no-url-{uuid.uuid4().hex[:8]}"
    )
    no_url_document_id = scalar(
        admin,
        "SELECT document_id FROM core.document_versions WHERE id = %s",
        no_url_version_id,
    )
    execute(
        admin,
        "UPDATE core.documents SET canonical_url = NULL WHERE id = %s",
        no_url_document_id,
    )
    no_url_case = call_api(
        api,
        actor,
        uuid.uuid4(),
        open_sql("document", no_url_version_id, REASON),
    )
    before = database_snapshot(admin)
    state, primary = sqlerror_tx(
        api,
        [*bind(actor, uuid.uuid4()), decide_sql(no_url_case, "approve", publication)],
    )
    require("missing source URL sqlstate", state, "22023")
    require("missing source URL code", primary, "publication_source_url_missing")
    after = database_snapshot(admin)
    require("missing source URL rejection is atomic", after, before)
    evidence.add(
        "G10-04-invalid-source-url",
        "G10-04",
        "document without canonical source URL is rejected atomically",
        role="uap_api",
        operation="audit.record_review_decision approve document",
        related_ids={
            "review_case_id": str(no_url_case),
            "document_id": str(no_url_document_id),
            "document_version_id": str(no_url_version_id),
        },
        expected={"sqlstate": "22023", "code": "publication_source_url_missing"},
        actual={"sqlstate": state, "code": primary},
        before=before,
        after=after,
    )

    for suffix, remove_evidence in (("no-evidence", True), ("long-excerpt", False)):
        _principal, negative_version_id, _source_id = seed_document(
            admin, f"wp10-{suffix}-{uuid.uuid4().hex[:8]}"
        )
        source_case = call_api(
            api,
            actor,
            uuid.uuid4(),
            open_sql("document", negative_version_id, REASON),
        )
        call_api(
            api,
            actor,
            uuid.uuid4(),
            decide_sql(source_case, "approve", claim_document_publication),
        )
        negative_span_id = insert_span(admin, negative_version_id, "c" * 64)
        tag = f"wp10-{suffix}-{uuid.uuid4().hex[:8]}"
        model_run_id = insert_model_run(
            admin,
            document_version_id=negative_version_id,
            task_type="claim_extraction",
            input_sha256=sha256_text(tag),
            tag=tag,
        )
        analysis_id = insert_analysis(
            admin,
            model_run_id=model_run_id,
            document_version_id=negative_version_id,
            result_type="claim_extraction",
            result={"claims": []},
        )
        negative_claim_id, _evidence_id = insert_supported_ai_claim(
            admin,
            analysis_id,
            negative_version_id,
            0,
            f"Claim for {suffix} rejection",
            sha256_text(f"claim-{suffix}"),
            negative_span_id,
        )
        if remove_evidence:
            with admin.transaction():
                with admin.cursor() as cursor:
                    cursor.execute("SET LOCAL session_replication_role = replica")
                    cursor.execute(
                        "DELETE FROM core.claim_evidence WHERE claim_id = %s",
                        (negative_claim_id,),
                    )
            expected_state = "23514"
            expected_code = "publication_evidence_required"
        else:
            execute(
                admin,
                "UPDATE core.evidence_spans SET evidence_text = %s WHERE id = %s",
                "x" * 2001,
                negative_span_id,
            )
            expected_state = "22023"
            expected_code = "publication_evidence_excerpt_too_long"
        negative_case = call_api(
            api,
            actor,
            uuid.uuid4(),
            open_sql("claim", negative_claim_id, REASON),
        )
        before = database_snapshot(admin)
        state, primary = sqlerror_tx(
            api,
            [
                *bind(second_reviewer, uuid.uuid4()),
                decide_sql(negative_case, "approve", {}),
            ],
        )
        require(f"{suffix} sqlstate", state, expected_state)
        require(f"{suffix} code", primary, expected_code)
        after = database_snapshot(admin)
        require(f"{suffix} rejection is atomic", after, before)
        evidence.add(
            f"G10-04-invalid-{suffix}",
            "G10-04",
            f"claim {suffix} is rejected atomically",
            role="uap_api",
            operation="audit.record_review_decision approve claim",
            related_ids={
                "review_case_id": str(negative_case),
                "claim_id": str(negative_claim_id),
                "document_version_id": str(negative_version_id),
                "evidence_span_id": str(negative_span_id),
            },
            expected={"sqlstate": expected_state, "code": expected_code},
            actual={"sqlstate": state, "code": primary},
            before=before,
            after=after,
            fixture=(
                "missing evidence state installed by owner with triggers disabled"
                if remove_evidence
                else "valid claim with overlong evidence excerpt"
            ),
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", help="libpq URL for the temporary database")
    parser.add_argument("--evidence-out", type=Path)
    args = parser.parse_args()
    if args.database_url:
        os.environ["UAP_DATABASE_URL"] = args.database_url
    base_url = os.environ["UAP_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    admin = connect()
    initial_revision = str(scalar(admin, "SELECT version_num FROM alembic_version"))
    evidence = EvidenceRecorder(initial_revision)
    role_passwords = {
        "uap_api": API_PASSWORD_ENV,
        "uap_worker": "UAP_WORKER_PASSWORD",
        "uap_scheduler": "UAP_SCHEDULER_PASSWORD",
        "uap_publisher": PUBLISHER_PASSWORD_ENV,
        "uap_model_governance": "UAP_MODEL_GOVERNANCE_PASSWORD",
        "uap_public_reader": "UAP_PUBLIC_READER_PASSWORD",
        "uap_audit_reader": "UAP_AUDIT_READER_PASSWORD",
        "uap_backup": "UAP_BACKUP_PASSWORD",
    }
    role_connections: dict[str, psycopg.Connection[Any]] = {}
    postgres_version: str | None = None
    status = "failed"
    failure: str | None = None
    try:
        role_connections = {
            role: connect_role(base_url, role, password_env)
            for role, password_env in role_passwords.items()
        }
        postgres_version = str(scalar(admin, "SHOW server_version"))
        require(
            "migration head", scalar(admin, "SELECT version_num FROM alembic_version"), CURRENT_HEAD
        )
        g10_02_enums(admin, evidence)
        g10_03_permissions(admin, role_connections, evidence)
        g10_04_manifests(admin, role_connections["uap_api"], evidence)
        status = "passed"
    except Exception as error:
        failure = type(error).__name__
        evidence.cases.append(
            {
                "id": "probe-failure",
                "requirement": "G10-02/G10-03/G10-04",
                "name": "runtime probe failed closed",
                "status": "failed",
                "error_type": type(error).__name__,
                "expected": {},
                "actual": {},
                "before": {},
                "after": {},
                "related_ids": {},
            }
        )
        evidence.cases.append(
            {
                "id": "remaining-cases",
                "requirement": "G10-02/G10-03/G10-04",
                "name": "cases after failure",
                "status": "not_run",
                "expected": {},
                "actual": {},
                "before": {},
                "after": {},
                "related_ids": {},
            }
        )
        raise
    finally:
        final_revision: str | None = None
        try:
            final_revision = str(scalar(admin, "SELECT version_num FROM alembic_version"))
        except Exception:
            final_revision = None
        if args.evidence_out is not None:
            atomic_write_json(
                args.evidence_out,
                evidence.payload(
                    status=status,
                    final_revision=final_revision,
                    postgres_version=postgres_version,
                    failure=failure,
                ),
            )
        for connection in role_connections.values():
            connection.close()
        admin.close()
    print("WP10.1 runtime validation passed: G10-02 G10-03 G10-04")


if __name__ == "__main__":
    main()
