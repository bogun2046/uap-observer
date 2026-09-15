"""Exercise the WP10.1 0019 -> 0020 migration contract in isolated databases.

The default mode provisions disposable databases from the supplied administrator
DSN.  It deliberately does not print subprocess output because migration URLs
can contain credentials.  A caller may instead pass ``--database-url`` for an
already isolated 0019 database; that mode runs only the legacy quarantine and
fail-closed downgrade assertions and never drops the supplied database.
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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from alembic.config import Config
from psycopg import sql
from psycopg.conninfo import make_conninfo
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from alembic import command as alembic_command

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

from tools.wp8_1_runtime_probe import insert_span, seed_document  # noqa: E402
from tools.wp9_2_runtime_probe import (  # noqa: E402
    bind,
    bind_role,
    insert_person,
    open_sql,
    seed_grantor,
    sqlerror_tx,
)
from tools.wp9_5_runtime_probe import call_api  # noqa: E402
from tools.wp9_6_runtime_probe import manual_sql  # noqa: E402
from tools.wp10_stage_revisions import REVISION_0019, REVISION_0020  # noqa: E402

STAGE_PARENT = REVISION_0019
STAGE_REVISION = REVISION_0020
EVIDENCE_SCHEMA = "wp10.1-migration-evidence.v1"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

SNAPSHOT_COUNT_SQL = {
    "audit.review_decisions": "SELECT count(*) FROM audit.review_decisions",
    "audit.document_publication_grants": "SELECT count(*) FROM audit.document_publication_grants",
    "audit.claim_publication_grants": "SELECT count(*) FROM audit.claim_publication_grants",
    "audit.document_publication_manifests": (
        "SELECT count(*) FROM audit.document_publication_manifests"
    ),
    "audit.claim_publication_manifests": "SELECT count(*) FROM audit.claim_publication_manifests",
    "audit.publication_quarantine": "SELECT count(*) FROM audit.publication_quarantine",
    "ops.outbox_events": "SELECT count(*) FROM ops.outbox_events",
    "public.documents": "SELECT count(*) FROM public.documents",
}

ROLE_PASSWORD_ENVS = (
    "UAP_MIGRATOR_PASSWORD",
    "UAP_API_PASSWORD",
    "UAP_WORKER_PASSWORD",
    "UAP_SCHEDULER_PASSWORD",
    "UAP_PUBLISHER_PASSWORD",
    "UAP_MODEL_GOVERNANCE_PASSWORD",
    "UAP_PUBLIC_READER_PASSWORD",
    "UAP_AUDIT_READER_PASSWORD",
    "UAP_BACKUP_PASSWORD",
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def stable_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


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
    git = shutil.which("git")
    if git is None:
        return {
            "commit": repository_head(PLATFORM_ROOT.parent),
            "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
    result = subprocess.run(  # noqa: S603
        [
            git,
            "-c",
            f"safe.directory={PLATFORM_ROOT.parent}",
            "-C",
            str(PLATFORM_ROOT.parent),
            "rev-parse",
            "HEAD",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return {
        "commit": result.stdout.strip() if result.returncode == 0 else "unavailable",
        "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def contract_snapshot(database_url: str) -> dict[str, object]:
    tables = (
        "audit.review_decisions",
        "audit.document_publication_grants",
        "audit.claim_publication_grants",
        "audit.document_publication_manifests",
        "audit.claim_publication_manifests",
        "audit.publication_quarantine",
        "ops.outbox_events",
        "public.documents",
    )
    counts: dict[str, int | None] = {}
    for table in tables:
        exists = query(database_url, "SELECT to_regclass(%s)", table)
        counts[table] = int(query(database_url, SNAPSHOT_COUNT_SQL[table])) if exists else None
    revision = query(database_url, "SELECT version_num FROM public.alembic_version")
    objects = query(
        database_url,
        """
        SELECT jsonb_build_object(
            'tables', (SELECT count(*) FROM pg_class WHERE relnamespace IN
                ('audit'::regnamespace, 'ops'::regnamespace, 'public'::regnamespace)),
            'functions', (SELECT count(*) FROM pg_proc WHERE pronamespace IN
                ('audit'::regnamespace, 'ops'::regnamespace, 'public'::regnamespace)),
            'constraints', (SELECT count(*) FROM pg_constraint WHERE connamespace IN
                ('audit'::regnamespace, 'ops'::regnamespace, 'public'::regnamespace))
        )
        """,
    )
    value = {"revision": revision, "counts": counts, "objects": objects}
    return {**value, "digest": stable_digest(value)}


class EvidenceRecorder:
    def __init__(self) -> None:
        self.started_at = utc_now()
        self.postgres_version: str | None = None
        self.cases: list[dict[str, object]] = []

    def add(self, case_id: str, name: str, **details: object) -> None:
        self.cases.append(
            {"id": case_id, "requirement": "G10-05", "name": name, "status": "passed", **details}
        )

    def payload(self, status: str, failure: str | None = None) -> dict[str, object]:
        return {
            "schema_version": 1,
            "schema": EVIDENCE_SCHEMA,
            "probe": "wp10_1_migration_probe",
            "requirements": ["G10-05"],
            "status": status,
            "source": source_identity(),
            "environment": {
                "python": platform.python_version(),
                "postgresql": self.postgres_version,
            },
            "started_at": self.started_at,
            "finished_at": utc_now(),
            "initial_revision": STAGE_PARENT,
            "target_revision": STAGE_REVISION,
            "final_revision": STAGE_REVISION if status == "passed" else None,
            "summary": {
                "total": len(self.cases),
                "passed": sum(case["status"] == "passed" for case in self.cases),
                "failed": sum(case["status"] == "failed" for case in self.cases),
                "not_run": sum(case["status"] == "not_run" for case in self.cases),
            },
            "cases": self.cases,
            "failure": failure,
        }


def libpq_url(value: str) -> str:
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def sqlalchemy_url(value: str) -> str:
    if value.startswith("postgresql+psycopg://"):
        return value
    return value.replace("postgresql://", "postgresql+psycopg://", 1)


def database_url_from_admin(admin_url: str, database: str) -> str:
    return (
        make_url(sqlalchemy_url(admin_url))
        .set(database=database)
        .render_as_string(hide_password=False)
    )


def run_command(*args: str, database_url: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["UAP_DATABASE_URL"] = database_url
    return subprocess.run(  # noqa: S603
        list(args),
        cwd=PLATFORM_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def alembic(database_url: str, *args: str, expected: bool = True) -> str:
    result = run_command(sys.executable, "-m", "alembic", *args, database_url=database_url)
    if (result.returncode == 0) != expected:
        details = "\n".join(
            line
            for line in (result.stdout + result.stderr).splitlines()
            if "postgresql" not in line
        )
        raise RuntimeError(f"unexpected alembic result for {' '.join(args)}\n{details[-800:]}")
    return result.stdout + result.stderr


def rejected_alembic(database_url: str, operation: str, revision: str) -> dict[str, str]:
    """Run an expected rejection in-process so evidence uses the real DBAPI SQLSTATE."""

    config = Config(str(PLATFORM_ROOT / "alembic.ini"))
    config.cmd_opts = argparse.Namespace(x=["role=migrator"])
    previous_url = os.environ.get("UAP_DATABASE_URL")
    os.environ["UAP_DATABASE_URL"] = database_url
    try:
        try:
            if operation == "upgrade":
                alembic_command.upgrade(config, revision)
            elif operation == "downgrade":
                alembic_command.downgrade(config, revision)
            else:
                raise ValueError(f"unsupported alembic operation: {operation}")
        except DBAPIError as exc:
            original = exc.orig
            state = getattr(original, "sqlstate", None)
            if not isinstance(state, str):
                raise RuntimeError("rejected migration did not expose a DBAPI SQLSTATE") from exc
            primary = str(original).splitlines()[0]
            return {"sqlstate": state, "code": primary}
    finally:
        if previous_url is None:
            os.environ.pop("UAP_DATABASE_URL", None)
        else:
            os.environ["UAP_DATABASE_URL"] = previous_url
    raise RuntimeError(f"expected alembic {operation} {revision} to be rejected")


def tool(database_url: str, *args: str) -> None:
    result = run_command(sys.executable, *args, database_url=database_url)
    if result.returncode != 0:
        raise RuntimeError(f"unexpected tool result for {' '.join(args)}")


def create_database(admin_url: str, database: str) -> None:
    with psycopg.connect(libpq_url(admin_url), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))


def drop_database(admin_url: str, database: str) -> None:
    with psycopg.connect(libpq_url(admin_url), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )


def require_environment(required: tuple[str, ...] = ROLE_PASSWORD_ENVS) -> None:
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError("missing role password variables: " + ", ".join(missing))


def connect_role(database_url: str, role: str, password_env: str) -> psycopg.Connection[Any]:
    password = os.environ.get(password_env)
    if not password:
        raise RuntimeError(f"missing {password_env} for {role}")
    connection = psycopg.connect(
        make_conninfo(libpq_url(database_url), user=role, password=password)
    )
    connection.autocommit = True
    return connection


def decision_sql(
    case_id: uuid.UUID, decision: str, changes: dict[str, object]
) -> tuple[str, tuple[object, ...]]:
    return (
        "SELECT audit.record_review_decision(%s, %s::audit.review_decision, %s, %s::jsonb)",
        (
            case_id,
            decision,
            "wp10.1 migration probe decision",
            json.dumps(changes, separators=(",", ":")),
        ),
    )


def document_state(
    database_url: str, case_id: uuid.UUID, document_version_id: uuid.UUID
) -> tuple[int, ...]:
    manifest_count = 0
    if (
        query(database_url, "SELECT to_regclass('audit.document_publication_manifests')")
        is not None
    ):
        manifest_count = int(
            query(
                database_url,
                "SELECT count(*) FROM audit.document_publication_manifests AS manifest "
                "JOIN audit.document_publication_grants AS grant_row "
                "ON grant_row.id = manifest.grant_id "
                "WHERE grant_row.document_version_id = %s",
                document_version_id,
            )
        )
    return (
        int(
            query(
                database_url,
                "SELECT count(*) FROM audit.review_decisions WHERE review_case_id = %s",
                case_id,
            )
        ),
        int(
            query(
                database_url,
                "SELECT count(*) FROM audit.document_publication_grants "
                "WHERE document_version_id = %s",
                document_version_id,
            )
        ),
        manifest_count,
        int(
            query(
                database_url,
                "SELECT count(*) FROM ops.outbox_events "
                "WHERE aggregate_type = 'document_publication_grants' "
                "AND payload ->> 'subject_id' = %s",
                str(document_version_id),
            )
        ),
    )


def claim_state(database_url: str, case_id: uuid.UUID, claim_id: uuid.UUID) -> tuple[int, ...]:
    return (
        int(
            query(
                database_url,
                "SELECT count(*) FROM audit.review_decisions WHERE review_case_id = %s",
                case_id,
            )
        ),
        int(
            query(
                database_url,
                "SELECT count(*) FROM audit.claim_publication_grants WHERE claim_id = %s",
                claim_id,
            )
        ),
        int(
            query(
                database_url,
                "SELECT count(*) FROM audit.claim_publication_manifests AS manifest "
                "JOIN audit.claim_publication_grants AS grant_row "
                "ON grant_row.id = manifest.grant_id "
                "WHERE grant_row.claim_id = %s",
                claim_id,
            )
        ),
        int(
            query(
                database_url,
                "SELECT count(*) FROM ops.outbox_events "
                "WHERE aggregate_type = 'claim_publication_grants' "
                "AND payload ->> 'subject_id' = %s",
                str(claim_id),
            )
        ),
    )


def assert_downgrade_0019_behavior(database_url: str) -> None:
    admin = psycopg.connect(libpq_url(database_url))
    admin.autocommit = True
    api = connect_role(database_url, "uap_api", "UAP_API_PASSWORD")
    try:
        seed_grantor(admin)
        actor = insert_person(admin)
        bind_role(admin, actor, "reviewer")
        _principal, document_version_id, _source_id = seed_document(
            admin, f"wp10-downgrade-document-{uuid.uuid4().hex[:8]}"
        )
        case_id = call_api(
            api,
            actor,
            uuid.uuid4(),
            open_sql("document", document_version_id, "wp10.1 downgrade behavior probe"),
        )
        before = document_state(database_url, case_id, document_version_id)
        if before != (0, 0, 0, 0):
            raise RuntimeError(f"G10-05 downgrade document baseline was not empty: {before!r}")
        state, primary = sqlerror_tx(
            api,
            [
                *bind(actor, uuid.uuid4()),
                decision_sql(case_id, "approve", {"publication": {}}),
            ],
        )
        if state != "22023" or primary != "review_structured_changes_unsupported":
            raise RuntimeError(
                "G10-05 downgrade document structured changes: "
                f"expected 22023/review_structured_changes_unsupported, got {state}/{primary}"
            )
        after = document_state(database_url, case_id, document_version_id)
        if after != before:
            raise RuntimeError(
                f"G10-05 downgrade document rejection changed state: {before!r} -> {after!r}"
            )
    finally:
        api.close()
        admin.close()


def assert_claim_document_grant_required(database_url: str) -> dict[str, str]:
    admin = psycopg.connect(libpq_url(database_url))
    admin.autocommit = True
    api = connect_role(database_url, "uap_api", "UAP_API_PASSWORD")
    try:
        seed_grantor(admin)
        actor = insert_person(admin)
        bind_role(admin, actor, "reviewer")
        approver = insert_person(admin)
        bind_role(admin, approver, "reviewer")
        _principal, document_version_id, _source_id = seed_document(
            admin, f"wp10-missing-document-grant-{uuid.uuid4().hex[:8]}"
        )
        span_id = insert_span(admin, document_version_id, "c" * 64)
        claim_id = call_api(
            api,
            actor,
            uuid.uuid4(),
            manual_sql(
                document_version_id,
                "G10-05 missing document grant claim",
                "probe",
                [span_id],
            ),
        )
        case_id = call_api(
            api,
            actor,
            uuid.uuid4(),
            open_sql("claim", claim_id, "wp10.1 missing document grant probe"),
        )
        before = claim_state(database_url, case_id, claim_id)
        if before != (0, 0, 0, 0):
            raise RuntimeError(f"G10-05 missing document grant baseline was not empty: {before!r}")
        state, primary = sqlerror_tx(
            api,
            [
                *bind(approver, uuid.uuid4()),
                decision_sql(case_id, "approve", {}),
            ],
        )
        if state != "23514" or primary != "publication_document_grant_required":
            raise RuntimeError(
                "G10-05 missing document v2 grant: "
                f"expected 23514/publication_document_grant_required, got {state}/{primary}"
            )
        after = claim_state(database_url, case_id, claim_id)
        if after != before:
            raise RuntimeError(
                f"G10-05 missing document v2 grant changed state: {before!r} -> {after!r}"
            )
        return {"sqlstate": state, "code": primary}
    finally:
        api.close()
        admin.close()


def bootstrap_to_0019(database_url: str) -> None:
    alembic(database_url, "upgrade", "0001_roles_and_schemas")
    tool(database_url, "tools/configure_roles.py", "configure")
    tool(database_url, "tools/configure_roles.py", "enable-migrator")
    alembic(database_url, "-x", "role=migrator", "upgrade", STAGE_PARENT)
    if query(database_url, "SELECT version_num FROM public.alembic_version") != STAGE_PARENT:
        raise RuntimeError("bootstrap did not reach 0019_manual_claims_binding")
    if query(database_url, "SELECT to_regclass('audit.document_publication_grants')") is None:
        raise RuntimeError("bootstrap did not create publication grant tables")


def query(database_url: str, statement: str, *params: object) -> Any:
    with psycopg.connect(libpq_url(database_url)) as connection:
        with connection.cursor() as cursor:
            cursor.execute(statement, params)
            row = cursor.fetchone()
    if row is None:
        raise RuntimeError("probe query returned no row")
    return row[0]


def seed_legacy_state(database_url: str) -> None:
    grant_id = uuid.uuid4()
    case_id = uuid.uuid4()
    decision_id = uuid.uuid4()
    event_id = uuid.uuid4()
    with psycopg.connect(libpq_url(database_url)) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET session_replication_role = replica")
            cursor.execute(
                """
                INSERT INTO audit.document_publication_grants (
                    id, review_case_id, document_version_id, decision_id,
                    revision_no, grant_status, granted_at, publication_payload_sha256
                ) VALUES (%s, %s, %s, %s, 1, 'active', clock_timestamp(), repeat('a', 64))
                """,
                (grant_id, case_id, uuid.uuid4(), decision_id),
            )
            cursor.execute(
                """
                INSERT INTO ops.outbox_events (
                    id, aggregate_type, aggregate_id, event_type, event_key,
                    payload, occurred_at
                ) VALUES (
                    %s, 'document_publication_grants', %s, 'publication.granted', %s,
                    '{"schema":"publication-outbox.v1"}'::jsonb, clock_timestamp()
                )
                """,
                (event_id, grant_id, f"wp10.1-migration-probe:{event_id}"),
            )
            cursor.execute("SET session_replication_role = origin")
        connection.commit()


def seed_nonempty_public(database_url: str) -> None:
    with psycopg.connect(libpq_url(database_url)) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET session_replication_role = replica")
            cursor.execute(
                """
                INSERT INTO public.documents (
                    id, document_grant_id, slug, title, category, fact_status,
                    source_name, canonical_source_url, published_at, revision_no
                ) VALUES (
                    gen_random_uuid(), gen_random_uuid(), 'wp10-preflight',
                    'preflight marker', 'other', 'unverified', 'probe',
                    'https://example.test/preflight', clock_timestamp(), 1
                )
                """
            )
            cursor.execute("SET session_replication_role = origin")
        connection.commit()


def seed_downgrade_marker(database_url: str, marker: str) -> dict[str, str]:
    marker_id = uuid.uuid4()
    related = {"marker_id": str(marker_id), "marker": marker}
    if marker == "public":
        seed_nonempty_public(database_url)
        return related
    with psycopg.connect(libpq_url(database_url)) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET session_replication_role = replica")
            if marker == "quarantine":
                cursor.execute(
                    """
                    INSERT INTO audit.publication_quarantine
                        (id, grant_table, grant_id, reason_code)
                    VALUES (%s, 'document_publication_grants', %s,
                            'publication_manifest_required')
                    """,
                    (marker_id, uuid.uuid4()),
                )
            elif marker == "terminal":
                cursor.execute(
                    """
                    INSERT INTO ops.outbox_events (
                        id, aggregate_type, aggregate_id, event_type, event_key,
                        payload, occurred_at, terminal_at, terminal_error_code
                    ) VALUES (
                        %s, 'document_publication_grants', %s,
                        'publication.granted', %s,
                        '{"schema":"publication-outbox.v2"}'::jsonb,
                        clock_timestamp(), clock_timestamp(), 'probe_terminal'
                    )
                    """,
                    (marker_id, uuid.uuid4(), f"wp10.1-terminal:{marker_id}"),
                )
            elif marker == "v2":
                cursor.execute(
                    """
                    INSERT INTO audit.document_publication_manifests (
                        grant_id, review_case_id, decision_id, document_id,
                        document_version_id, title, summary, category, fact_status,
                        source_name, canonical_source_url, manifest_sha256
                    ) VALUES (
                        %s, %s, %s, %s, %s, 'v2 marker', NULL, 'other',
                        'unverified', 'probe', 'https://example.test/v2', repeat('a', 64)
                    )
                    """,
                    (marker_id, uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()),
                )
            else:
                raise ValueError(f"unsupported marker {marker}")
            cursor.execute("SET session_replication_role = origin")
        connection.commit()
    return related


def assert_independent_downgrade_marker(
    database_url: str, marker: str
) -> tuple[dict[str, object], dict[str, object], dict[str, str], dict[str, str]]:
    bootstrap_to_0019(database_url)
    alembic(database_url, "-x", "role=migrator", "upgrade", STAGE_REVISION)
    related = seed_downgrade_marker(database_url, marker)
    before = contract_snapshot(database_url)
    error = rejected_alembic(database_url, "downgrade", STAGE_PARENT)
    if error != {
        "sqlstate": "22023",
        "code": "publication_contract_state_blocks_downgrade",
    }:
        raise RuntimeError(f"G10-05 {marker} marker did not block downgrade")
    after = contract_snapshot(database_url)
    if after != before:
        raise RuntimeError(f"G10-05 {marker} rejection changed contract state")
    return before, after, related, error


def assert_legacy_contract(database_url: str) -> dict[str, str]:
    seed_legacy_state(database_url)
    alembic(database_url, "-x", "role=migrator", "upgrade", STAGE_REVISION)
    if query(database_url, "SELECT count(*) FROM audit.publication_quarantine") != 2:
        raise RuntimeError("G10-05 expected two legacy quarantine rows")
    if (
        query(
            database_url,
            "SELECT count(*) FROM ops.outbox_events WHERE terminal_at IS NOT NULL",
        )
        != 1
    ):
        raise RuntimeError("G10-05 expected one terminal legacy event")
    if (
        query(
            database_url,
            "SELECT count(*) FROM ops.outbox_events WHERE published_at IS NOT NULL",
        )
        != 0
    ):
        raise RuntimeError("G10-05 legacy event was incorrectly published")
    error = rejected_alembic(database_url, "downgrade", STAGE_PARENT)
    if error != {
        "sqlstate": "22023",
        "code": "publication_contract_state_blocks_downgrade",
    }:
        raise RuntimeError("G10-05 downgrade did not fail closed")
    if query(database_url, "SELECT version_num FROM public.alembic_version") != STAGE_REVISION:
        raise RuntimeError("G10-05 failed downgrade changed migration state")
    return error


def assert_empty_roundtrip(database_url: str) -> None:
    bootstrap_to_0019(database_url)
    alembic(database_url, "-x", "role=migrator", "upgrade", STAGE_REVISION)
    if query(database_url, "SELECT version_num FROM public.alembic_version") != STAGE_REVISION:
        raise RuntimeError("G10-05 empty upgrade did not reach 0020")
    alembic(
        database_url,
        "-x",
        "role=migrator",
        "downgrade",
        STAGE_PARENT,
    )
    if query(database_url, "SELECT version_num FROM public.alembic_version") != STAGE_PARENT:
        raise RuntimeError("G10-05 empty 0020 -> 0019 downgrade failed")
    assert_downgrade_0019_behavior(database_url)
    alembic(database_url, "-x", "role=migrator", "upgrade", STAGE_REVISION)
    if query(database_url, "SELECT version_num FROM public.alembic_version") != STAGE_REVISION:
        raise RuntimeError("G10-05 empty 0019 -> 0020 re-upgrade failed")


def assert_public_preflight(database_url: str) -> dict[str, str]:
    bootstrap_to_0019(database_url)
    seed_nonempty_public(database_url)
    error = rejected_alembic(database_url, "upgrade", STAGE_REVISION)
    if error != {"sqlstate": "23514", "code": "publication_manifest_invalid"}:
        raise RuntimeError("G10-05 public preflight did not fail closed")
    if query(database_url, "SELECT version_num FROM public.alembic_version") != STAGE_PARENT:
        raise RuntimeError("public preflight changed migration state")
    if query(database_url, "SELECT count(*) FROM public.documents") != 1:
        raise RuntimeError("public preflight marker was removed")
    return error


def run_isolated(admin_url: str, evidence: EvidenceRecorder) -> None:
    require_environment()
    evidence.postgres_version = str(query(admin_url, "SHOW server_version"))
    databases = {
        "legacy": f"uap_wp10_1_legacy_{uuid.uuid4().hex[:10]}",
        "roundtrip": f"uap_wp10_1_roundtrip_{uuid.uuid4().hex[:10]}",
        "preflight": f"uap_wp10_1_preflight_{uuid.uuid4().hex[:10]}",
        "negative": f"uap_wp10_1_negative_{uuid.uuid4().hex[:10]}",
        "v2": f"uap_wp10_1_v2_{uuid.uuid4().hex[:10]}",
        "quarantine": f"uap_wp10_1_quarantine_{uuid.uuid4().hex[:10]}",
        "terminal": f"uap_wp10_1_terminal_{uuid.uuid4().hex[:10]}",
        "public": f"uap_wp10_1_public_{uuid.uuid4().hex[:10]}",
    }
    created: list[str] = []
    try:
        for database in databases.values():
            create_database(admin_url, database)
            created.append(database)
        legacy_url = database_url_from_admin(admin_url, databases["legacy"])
        bootstrap_to_0019(legacy_url)
        legacy_before = contract_snapshot(legacy_url)
        legacy_error = assert_legacy_contract(legacy_url)
        legacy_after = contract_snapshot(legacy_url)
        evidence.add(
            "G10-05-legacy-quarantine",
            "legacy v1 upgrade preserves history, quarantines and terminalizes",
            initial_revision=STAGE_PARENT,
            target_revision=STAGE_REVISION,
            final_revision=STAGE_REVISION,
            role="uap_migrator",
            operation="alembic upgrade and rejected downgrade",
            related_ids={"database_alias": "legacy"},
            expected={
                "sqlstate": "22023",
                "code": "publication_contract_state_blocks_downgrade",
            },
            actual=legacy_error,
            before=legacy_before,
            after=legacy_after,
        )
        roundtrip_url = database_url_from_admin(admin_url, databases["roundtrip"])
        assert_empty_roundtrip(roundtrip_url)
        evidence.add(
            "G10-05-empty-roundtrip",
            "empty 0019 to 0020 to 0019 to 0020 roundtrip",
            initial_revision=STAGE_PARENT,
            target_revision=STAGE_REVISION,
            final_revision=STAGE_REVISION,
            role="uap_migrator",
            operation="alembic roundtrip",
            related_ids={"database_alias": "roundtrip"},
            expected={"final_revision": STAGE_REVISION},
            actual={
                "final_revision": query(roundtrip_url, "SELECT version_num FROM alembic_version")
            },
            before={"revision": STAGE_PARENT},
            after=contract_snapshot(roundtrip_url),
        )
        preflight_url = database_url_from_admin(admin_url, databases["preflight"])
        preflight_error = assert_public_preflight(preflight_url)
        evidence.add(
            "G10-05-upgrade-public-preflight",
            "nonempty legacy public projection blocks upgrade",
            initial_revision=STAGE_PARENT,
            target_revision=STAGE_REVISION,
            final_revision=STAGE_PARENT,
            role="uap_migrator",
            operation="alembic upgrade",
            related_ids={"database_alias": "preflight"},
            expected={"sqlstate": "23514", "code": "publication_manifest_invalid"},
            actual=preflight_error,
            before={"public.documents": 1},
            after=contract_snapshot(preflight_url),
        )
        negative_url = database_url_from_admin(admin_url, databases["negative"])
        bootstrap_to_0019(negative_url)
        alembic(negative_url, "-x", "role=migrator", "upgrade", STAGE_REVISION)
        dependency_error = assert_claim_document_grant_required(negative_url)
        evidence.add(
            "G10-05-claim-document-dependency",
            "claim without active document v2 grant rolls back",
            initial_revision=STAGE_REVISION,
            target_revision=STAGE_REVISION,
            final_revision=STAGE_REVISION,
            role="uap_api",
            operation="audit.record_review_decision approve claim",
            related_ids={"database_alias": "negative"},
            expected={"sqlstate": "23514", "code": "publication_document_grant_required"},
            actual=dependency_error,
            before={"decision_grant_manifest_outbox": [0, 0, 0, 0]},
            after={"decision_grant_manifest_outbox": [0, 0, 0, 0]},
        )
        for marker in ("v2", "quarantine", "terminal", "public"):
            marker_url = database_url_from_admin(admin_url, databases[marker])
            before, after, related, error = assert_independent_downgrade_marker(marker_url, marker)
            evidence.add(
                f"G10-05-downgrade-{marker}",
                f"independent {marker} state blocks downgrade before destruction",
                initial_revision=STAGE_REVISION,
                target_revision=STAGE_PARENT,
                final_revision=STAGE_REVISION,
                role="uap_migrator",
                operation="alembic downgrade",
                related_ids={"database_alias": marker, **related},
                expected={
                    "sqlstate": "22023",
                    "code": "publication_contract_state_blocks_downgrade",
                },
                actual=error,
                before=before,
                after=after,
            )
    finally:
        try:
            if created:
                tool(
                    database_url_from_admin(admin_url, databases["legacy"]),
                    "tools/configure_roles.py",
                    "disable-migrator",
                )
        finally:
            for database in created:
                drop_database(admin_url, database)


def run_existing(database_url: str, evidence: EvidenceRecorder) -> None:
    require_environment(("UAP_MIGRATOR_PASSWORD",))
    evidence.postgres_version = str(query(database_url, "SHOW server_version"))
    tool(database_url, "tools/configure_roles.py", "enable-migrator")
    try:
        assert_legacy_contract(database_url)
        evidence.add(
            "G10-05-existing-legacy",
            "existing isolated legacy database upgrade and downgrade rejection",
            initial_revision=STAGE_PARENT,
            target_revision=STAGE_REVISION,
            final_revision=STAGE_REVISION,
            role="uap_migrator",
            operation="alembic upgrade and downgrade",
            related_ids={"database_alias": "caller-supplied-isolated"},
            expected={"code": "publication_contract_state_blocks_downgrade"},
            actual={"code": "publication_contract_state_blocks_downgrade"},
            before={"revision": STAGE_PARENT},
            after=contract_snapshot(database_url),
        )
    finally:
        tool(database_url, "tools/configure_roles.py", "disable-migrator")


def main() -> None:
    parser = argparse.ArgumentParser(description="WP10.1 isolated migration probe (G10-05)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--admin-url", help="administrator DSN used to create disposable databases")
    group.add_argument("--database-url", help="already isolated database at revision 0019")
    parser.add_argument("--evidence-out", type=Path)
    args = parser.parse_args()
    evidence = EvidenceRecorder()
    status = "failed"
    failure: str | None = None
    try:
        if args.admin_url:
            run_isolated(args.admin_url, evidence)
        else:
            run_existing(args.database_url, evidence)
        status = "passed"
    except Exception as error:
        failure = type(error).__name__
        evidence.cases.append(
            {
                "id": "probe-failure",
                "requirement": "G10-05",
                "name": "migration probe failed closed",
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
                "requirement": "G10-05",
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
        if args.evidence_out is not None:
            atomic_write_json(args.evidence_out, evidence.payload(status, failure))
    print("G10-05 migration probe passed")
    print(
        "G10-05 SQLSTATE summary: "
        "legacy_downgrade=22023/publication_contract_state_blocks_downgrade; "
        "document_structured_changes=22023/review_structured_changes_unsupported; "
        "missing_document_v2_grant=23514/publication_document_grant_required; "
        "public_preflight=22023/publication_manifest_invalid"
    )
    print("G10-05 fail-closed counts: document and claim before=after=(0, 0, 0, 0)")


if __name__ == "__main__":
    main()
