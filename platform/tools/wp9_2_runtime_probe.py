"""WP9.2 runtime probe: G9-06-G9-09, G9-31, G9-34 on real uap_api logins."""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from uap_platform.config import load_settings

ROLE_PASSWORDS = {
    "uap_api": "UAP_API_PASSWORD",
    "uap_worker": "UAP_WORKER_PASSWORD",
}

CURRENT_HEAD = "0015_review_case_lifecycle"
EXPECTED_TABLE_COUNT = 50
GRANTOR_ID = uuid.UUID("00000000-0000-7000-8000-000000000901")
REASON = "open review case for wp9.2"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def admin_url() -> str:
    return load_settings().psycopg_database_url


def connect(role: str | None = None) -> psycopg.Connection[Any]:
    url = admin_url()
    if role is None:
        connection = psycopg.connect(url)
        connection.autocommit = True
        return connection
    password = os.environ.get(ROLE_PASSWORDS[role])
    if not password:
        raise RuntimeError(f"missing password for {role}")
    params = conninfo_to_dict(url)
    params.pop("user", None)
    params.pop("password", None)
    base = make_conninfo(**params)  # type: ignore[arg-type]
    connection = psycopg.connect(make_conninfo(base, user=role, password=password))
    connection.autocommit = True
    return connection


def scalar(connection: psycopg.Connection[Any], statement: str, *params: object) -> Any:
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("probe query returned no row")
    return row[0]


def execute(connection: psycopg.Connection[Any], statement: str, *params: object) -> None:
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


def require(name: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise RuntimeError(f"{name}: expected {expected!r}, got {actual!r}")


def sqlerror_tx(
    connection: psycopg.Connection[Any],
    steps: list[tuple[str, tuple[object, ...]]],
) -> tuple[str, str]:
    try:
        with connection.transaction():
            with connection.cursor() as cursor:
                for statement, params in steps:
                    cursor.execute(statement, params)
    except psycopg.Error as error:
        primary = ""
        if error.diag is not None and error.diag.message_primary:
            primary = error.diag.message_primary
        return str(error.sqlstate), primary
    raise RuntimeError("probe accepted a forbidden statement")


def seed_grantor(admin: psycopg.Connection[Any]) -> None:
    execute(
        admin,
        """
        INSERT INTO audit.principals (
            id, principal_type, issuer, subject, display_name, active
        ) VALUES (%s, 'person', 'https://issuer.test/grantor', 'wp9-1-grantor', 'grantor', true)
        ON CONFLICT (id) DO NOTHING
        """,
        GRANTOR_ID,
    )


def insert_person(admin: psycopg.Connection[Any]) -> uuid.UUID:
    principal_id = uuid.uuid4()
    execute(
        admin,
        """
        INSERT INTO audit.principals (
            id, principal_type, issuer, subject, display_name, active
        ) VALUES (%s, 'person', %s, %s, %s, true)
        """,
        principal_id,
        f"https://issuer.test/{principal_id}",
        str(principal_id),
        "review probe person",
    )
    return principal_id


def bind_role(admin: psycopg.Connection[Any], principal_id: uuid.UUID, role: str) -> None:
    execute(
        admin,
        """
        INSERT INTO audit.role_bindings (
            id, principal_id, role, scope_type, scope_id, reason, granted_by, granted_at
        ) VALUES (
            %s, %s, %s::audit.application_role, 'global', NULL, 'wp9.2 probe',
            %s, clock_timestamp()
        )
        """,
        uuid.uuid4(),
        principal_id,
        role,
        GRANTOR_ID,
    )


def seed_subjects(
    admin: psycopg.Connection[Any], tag: str
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    now = datetime.now(UTC)
    principal_id = uuid.uuid4()
    source_id = uuid.uuid4()
    document_id = uuid.uuid4()
    document_version_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    artifact_version_id = uuid.uuid4()
    source_run_id = uuid.uuid4()
    stored_id = uuid.uuid4()
    config_id = uuid.uuid4()
    job_id = uuid.uuid4()
    claim_id = uuid.uuid4()
    entity_id = uuid.uuid4()
    raw_hash = sha256_text(f"raw-{tag}")
    execute(
        admin,
        """
        INSERT INTO audit.principals (id, principal_type, service_name, display_name)
        VALUES (%s, 'service', %s, 'WP9.2 probe')
        """,
        principal_id,
        f"wp9-2-{tag}",
    )
    execute(
        admin,
        """
        INSERT INTO ingest.sources (id, slug, name, source_type, homepage_url)
        VALUES (%s, %s, 'WP9.2 probe', 'web', 'https://example.test')
        """,
        source_id,
        f"wp9-2-{tag}",
    )
    execute(
        admin,
        """
        INSERT INTO ingest.source_config_versions (
            id, source_id, version_no, configuration, configuration_sha256,
            effective_from, changed_by, change_reason
        ) VALUES (%s, %s, 1, '{}'::jsonb, repeat('b', 64), %s, %s, 'wp9.2')
        """,
        config_id,
        source_id,
        now,
        principal_id,
    )
    execute(
        admin,
        """
        INSERT INTO ops.jobs (
            id, job_type, payload, payload_schema_version, idempotency_key,
            status, priority, available_at, max_attempts, timeout_seconds, completed_at
        ) VALUES (
            %s, 'analyze_document', '{}'::jsonb, 'model.v1', %s,
            'succeeded', 0, now(), 1, 30, now()
        )
        """,
        job_id,
        f"wp9-2-job-{tag}",
    )
    execute(
        admin,
        """
        INSERT INTO ingest.source_runs (
            id, source_id, source_config_version_id, job_id, run_key, outcome,
            started_at, finished_at
        ) VALUES (%s, %s, %s, %s, %s, 'succeeded', %s, %s)
        """,
        source_run_id,
        source_id,
        config_id,
        job_id,
        f"wp9-2-run-{tag}",
        now,
        now,
    )
    execute(
        admin,
        """
        INSERT INTO core.stored_objects (
            id, storage_domain, bucket_name, object_key, content_sha256,
            byte_length, media_type, verified_at
        ) VALUES (%s, 'raw', 'raw', %s, %s, 12, 'text/plain', %s)
        """,
        stored_id,
        f"raw/{tag}",
        raw_hash,
        now,
    )
    execute(
        admin,
        """
        INSERT INTO ingest.artifacts (
            id, source_id, canonical_locator, artifact_kind, first_seen_at, last_seen_at
        ) VALUES (%s, %s, %s, 'html', %s, %s)
        """,
        artifact_id,
        source_id,
        f"https://example.test/wp9/{tag}",
        now,
        now,
    )
    execute(
        admin,
        """
        INSERT INTO ingest.artifact_versions (
            id, artifact_id, source_run_id, stored_object_id, retrieved_at
        ) VALUES (%s, %s, %s, %s, %s)
        """,
        artifact_version_id,
        artifact_id,
        source_run_id,
        stored_id,
        now,
    )
    execute(
        admin,
        """
        INSERT INTO core.documents (
            id, source_id, source_item_key, canonical_url, document_kind,
            first_seen_at, last_seen_at
        ) VALUES (%s, %s, %s, %s, 'article', %s, %s)
        """,
        document_id,
        source_id,
        tag,
        f"https://example.test/wp9/{tag}",
        now,
        now,
    )
    execute(
        admin,
        """
        INSERT INTO core.document_versions (
            id, document_id, artifact_version_id, version_no, normalized_content_sha256
        ) VALUES (%s, %s, %s, 1, %s)
        """,
        document_version_id,
        document_id,
        artifact_version_id,
        sha256_text(f"doc-{tag}"),
    )
    claim_text = f"wp9.2 claim {tag}"
    execute(
        admin,
        """
        INSERT INTO core.claims (
            id, document_version_id, claim_text, claim_fingerprint, claim_type,
            assertion_status, created_by
        ) VALUES (%s, %s, %s, %s, 'observation', 'reported', %s)
        """,
        claim_id,
        document_version_id,
        claim_text,
        sha256_text(claim_text),
        principal_id,
    )
    execute(
        admin,
        """
        INSERT INTO core.entities (
            id, entity_type, canonical_name, status
        ) VALUES (%s, 'person', %s, 'active')
        """,
        entity_id,
        f"WP9.2 Entity {tag}",
    )
    return document_version_id, claim_id, entity_id


def bind(principal: uuid.UUID, request: uuid.UUID | None) -> list[tuple[str, tuple[object, ...]]]:
    steps: list[tuple[str, tuple[object, ...]]] = [
        ("SELECT set_config('uap.principal_id', %s, true)", (str(principal),)),
    ]
    if request is not None:
        steps.append(("SELECT set_config('uap.request_id', %s, true)", (str(request),)))
    return steps


def open_sql(
    case_type: str, subject: uuid.UUID, reason: str = REASON, priority: int = 0
) -> tuple[str, tuple[object, ...]]:
    return (
        "SELECT audit.open_review_case(%s::audit.review_case_type, %s, %s::smallint, %s)",
        (case_type, subject, priority, reason),
    )


def assign_sql(case_id: uuid.UUID, assignee: uuid.UUID) -> tuple[str, tuple[object, ...]]:
    return ("SELECT audit.assign_review_case(%s, %s)", (case_id, assignee))


def close_sql(
    case_id: uuid.UUID, reason: str = "close after a decision"
) -> tuple[str, tuple[object, ...]]:
    return ("SELECT audit.close_review_case(%s, %s)", (case_id, reason))


def run_open(
    api: psycopg.Connection[Any],
    principal: uuid.UUID,
    request: uuid.UUID,
    case_type: str,
    subject: uuid.UUID,
    reason: str = REASON,
) -> uuid.UUID:
    with api.transaction():
        with api.cursor() as cursor:
            cursor.execute("SELECT set_config('uap.principal_id', %s, true)", (str(principal),))
            cursor.execute("SELECT set_config('uap.request_id', %s, true)", (str(request),))
            cursor.execute(
                "SELECT audit.open_review_case(%s::audit.review_case_type, %s, 0::smallint, %s)",
                (case_type, subject, reason),
            )
            row = cursor.fetchone()
    if row is None or row[0] is None:
        raise RuntimeError("open_review_case returned no id")
    return uuid.UUID(str(row[0]))


def g9_06(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    principal: uuid.UUID,
    document_id: uuid.UUID,
    claim_id: uuid.UUID,
    entity_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    before_cases = int(scalar(admin, "SELECT count(*) FROM audit.review_cases"))
    before_events = int(scalar(admin, "SELECT count(*) FROM audit.audit_events"))
    opened: list[uuid.UUID] = []
    for case_type, subject in (
        ("document", document_id),
        ("claim", claim_id),
        ("entity", entity_id),
    ):
        opened.append(run_open(api, principal, uuid.uuid4(), case_type, subject))
    require(
        "g9-06 cases +3",
        int(scalar(admin, "SELECT count(*) FROM audit.review_cases")),
        before_cases + 3,
    )
    require(
        "g9-06 events +3",
        int(scalar(admin, "SELECT count(*) FROM audit.audit_events")),
        before_events + 3,
    )
    for case_id, case_type in zip(opened, ("document", "claim", "entity"), strict=True):
        status, opened_by = (
            scalar(admin, "SELECT status::text FROM audit.review_cases WHERE id=%s", case_id),
            scalar(admin, "SELECT opened_by FROM audit.review_cases WHERE id=%s", case_id),
        )
        require(f"g9-06 {case_type} status", status, "open")
        require(f"g9-06 {case_type} opened_by", opened_by, principal)
        key = scalar(
            admin,
            """
            SELECT event_key FROM audit.audit_events
             WHERE target_id=%s AND action='review.case.open'
            """,
            case_id,
        )
        require(f"g9-06 {case_type} key prefix", str(key).startswith("review.case.open:"), True)
    return opened[0], opened[1], opened[2]


def g9_07(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    principal: uuid.UUID,
    claim_id: uuid.UUID,
) -> None:
    before = int(
        scalar(
            admin,
            "SELECT count(*) FROM audit.review_cases WHERE claim_id=%s AND closed_at IS NULL",
            claim_id,
        )
    )
    require("g9-07 one open before", before, 1)
    state, primary = sqlerror_tx(
        api,
        [*bind(principal, uuid.uuid4()), open_sql("claim", claim_id)],
    )
    require("g9-07 sqlstate", state, "23505")
    require("g9-07 code", primary, "review_case_already_open")
    require(
        "g9-07 still one open",
        int(
            scalar(
                admin,
                "SELECT count(*) FROM audit.review_cases WHERE claim_id=%s AND closed_at IS NULL",
                claim_id,
            )
        ),
        1,
    )


def g9_08(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    principal: uuid.UUID,
) -> None:
    before = int(
        scalar(admin, "SELECT count(*) FROM audit.review_cases WHERE case_type='relation'")
    )
    state, primary = sqlerror_tx(
        api,
        [*bind(principal, uuid.uuid4()), open_sql("relation", uuid.uuid4())],
    )
    require("g9-08 sqlstate", state, "22023")
    require("g9-08 code", primary, "knowledge_relation_review_not_in_wp9")
    require(
        "g9-08 no relation rows",
        int(scalar(admin, "SELECT count(*) FROM audit.review_cases WHERE case_type='relation'")),
        before,
    )


def g9_09(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    principal: uuid.UUID,
    assignee: uuid.UUID,
    case_id: uuid.UUID,
) -> None:
    request = uuid.uuid4()
    with api.transaction():
        with api.cursor() as cursor:
            cursor.execute("SELECT set_config('uap.principal_id', %s, true)", (str(principal),))
            cursor.execute("SELECT set_config('uap.request_id', %s, true)", (str(request),))
            cursor.execute("SELECT audit.assign_review_case(%s, %s)", (case_id, assignee))
    status, assigned_to = (
        scalar(admin, "SELECT status::text FROM audit.review_cases WHERE id=%s", case_id),
        scalar(admin, "SELECT assigned_to FROM audit.review_cases WHERE id=%s", case_id),
    )
    require("g9-09 status", status, "assigned")
    require("g9-09 assignee", assigned_to, assignee)
    state, primary = sqlerror_tx(
        api,
        [*bind(principal, uuid.uuid4()), close_sql(case_id)],
    )
    require("g9-09 close sqlstate", state, "22023")
    require("g9-09 close code", primary, "review_case_not_decidable")


def g9_31(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    principal: uuid.UUID,
    claim_id: uuid.UUID,
) -> None:
    before = int(scalar(admin, "SELECT count(*) FROM audit.review_cases"))
    state, primary = sqlerror_tx(api, [*bind(principal, None), open_sql("claim", uuid.uuid4())])
    require("g9-31 sqlstate", state, "42501")
    require("g9-31 code", primary, "review_request_id_missing")
    require("g9-31 no case", int(scalar(admin, "SELECT count(*) FROM audit.review_cases")), before)
    _ = claim_id


def g9_34(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    principal: uuid.UUID,
    assignee: uuid.UUID,
    document_id: uuid.UUID,
    extra_document: uuid.UUID,
) -> None:
    request = uuid.uuid4()
    first = run_open(api, principal, request, "document", document_id, REASON)
    replay = run_open(api, principal, request, "document", document_id, REASON)
    require("g9-34 open replay", replay, first)
    require(
        "g9-34 one case for subject",
        int(
            scalar(
                admin,
                """
                SELECT count(*) FROM audit.review_cases
                 WHERE document_version_id=%s AND closed_at IS NULL
                """,
                document_id,
            )
        ),
        1,
    )
    events = int(
        scalar(
            admin,
            "SELECT count(*) FROM audit.audit_events WHERE event_key=%s",
            f"review.case.open:{request}",
        )
    )
    require("g9-34 one open event", events, 1)

    state, primary = sqlerror_tx(
        api,
        [*bind(principal, request), open_sql("document", document_id, "changed reason text")],
    )
    require("g9-34 reason conflict sqlstate", state, "23505")
    require("g9-34 reason conflict", primary, "review_idempotency_payload_conflict")

    state, primary = sqlerror_tx(
        api,
        [*bind(principal, request), open_sql("document", extra_document)],
    )
    require("g9-34 subject conflict sqlstate", state, "23505")
    require("g9-34 subject conflict", primary, "review_idempotency_payload_conflict")
    require(
        "g9-34 extra subject unopened",
        int(
            scalar(
                admin,
                "SELECT count(*) FROM audit.review_cases WHERE document_version_id=%s",
                extra_document,
            )
        ),
        0,
    )

    state, primary = sqlerror_tx(
        api,
        [*bind(principal, request), open_sql("claim", document_id)],
    )
    require("g9-34 case_type conflict sqlstate", state, "23505")
    require("g9-34 case_type conflict", primary, "review_idempotency_payload_conflict")

    assign_request = request  # same request_id, different operation
    with api.transaction():
        with api.cursor() as cursor:
            cursor.execute("SELECT set_config('uap.principal_id', %s, true)", (str(principal),))
            cursor.execute("SELECT set_config('uap.request_id', %s, true)", (str(assign_request),))
            cursor.execute("SELECT audit.assign_review_case(%s, %s)", (first, assignee))
            cursor.execute("SELECT audit.assign_review_case(%s, %s)", (first, assignee))
            row = cursor.fetchone()
    require("g9-34 assign replay", uuid.UUID(str(row[0])) if row else None, first)
    require(
        "g9-34 assign events",
        int(
            scalar(
                admin,
                "SELECT count(*) FROM audit.audit_events WHERE event_key=%s",
                f"review.case.assign:{assign_request}",
            )
        ),
        1,
    )
    other_case = run_open(api, principal, uuid.uuid4(), "document", extra_document)
    state, primary = sqlerror_tx(
        api,
        [*bind(principal, assign_request), assign_sql(other_case, assignee)],
    )
    require("g9-34 assign case_id conflict", primary, "review_idempotency_payload_conflict")
    other_reviewer = insert_person(admin)
    bind_role(admin, other_reviewer, "reviewer")
    state, primary = sqlerror_tx(
        api,
        [*bind(principal, assign_request), assign_sql(first, other_reviewer)],
    )
    require("g9-34 assign assignee conflict", primary, "review_idempotency_payload_conflict")
    require(
        "g9-34 assigned_to unchanged",
        scalar(admin, "SELECT assigned_to FROM audit.review_cases WHERE id=%s", first),
        assignee,
    )


def permissions(admin: psycopg.Connection[Any]) -> None:
    require(
        "api execute open",
        scalar(
            admin,
            """
            SELECT has_function_privilege(
                'uap_api',
                'audit.open_review_case(audit.review_case_type,uuid,smallint,text)',
                'EXECUTE'
            )
            """,
        ),
        True,
    )
    require(
        "api execute assign",
        scalar(
            admin,
            """
            SELECT has_function_privilege(
                'uap_api', 'audit.assign_review_case(uuid,uuid)', 'EXECUTE'
            )
            """,
        ),
        True,
    )
    require(
        "api execute close",
        scalar(
            admin,
            """
            SELECT has_function_privilege(
                'uap_api', 'audit.close_review_case(uuid,text)', 'EXECUTE'
            )
            """,
        ),
        True,
    )
    require(
        "worker no execute open",
        scalar(
            admin,
            """
            SELECT has_function_privilege(
                'uap_worker',
                'audit.open_review_case(audit.review_case_type,uuid,smallint,text)',
                'EXECUTE'
            )
            """,
        ),
        False,
    )
    require(
        "worker no execute assign",
        scalar(
            admin,
            """
            SELECT has_function_privilege(
                'uap_worker', 'audit.assign_review_case(uuid,uuid)', 'EXECUTE'
            )
            """,
        ),
        False,
    )
    require(
        "api cannot insert cases",
        scalar(admin, "SELECT has_table_privilege('uap_api', 'audit.review_cases', 'INSERT')"),
        False,
    )
    require(
        "merge still closed",
        scalar(
            admin,
            """
            SELECT has_function_privilege(
                'uap_api', 'core.merge_entities(uuid,uuid,uuid,text)', 'EXECUTE'
            )
            """,
        ),
        False,
    )


def main() -> None:
    admin = connect()
    api = connect("uap_api")
    try:
        head = scalar(admin, "SELECT version_num FROM public.alembic_version")
        require("alembic head", head, CURRENT_HEAD)
        tables = scalar(
            admin,
            """
            SELECT count(*) FROM pg_tables
             WHERE schemaname IN ('ingest','core','ops','audit','public')
               AND tablename <> 'alembic_version'
            """,
        )
        require("table count", int(tables), EXPECTED_TABLE_COUNT)
        seed_grantor(admin)
        permissions(admin)
        reviewer = insert_person(admin)
        bind_role(admin, reviewer, "reviewer")
        assignee = insert_person(admin)
        bind_role(admin, assignee, "reviewer")
        document_id, claim_id, entity_id = seed_subjects(admin, uuid.uuid4().hex[:8])
        extra_document, extra_claim, _extra_entity = seed_subjects(admin, uuid.uuid4().hex[:8])
        _doc_case, claim_case, entity_case = g9_06(
            admin, api, reviewer, document_id, claim_id, entity_id
        )
        g9_07(admin, api, reviewer, claim_id)
        g9_08(admin, api, reviewer)
        g9_09(admin, api, reviewer, assignee, entity_case)
        g9_31(admin, api, reviewer, extra_claim)
        extra_open = seed_subjects(admin, uuid.uuid4().hex[:8])[0]
        g9_34(admin, api, reviewer, assignee, extra_document, extra_open)
        _ = claim_case
    finally:
        api.close()
        admin.close()
    print("WP9.2 runtime probe passed: G9-06 G9-07 G9-08 G9-09 G9-31 G9-34")


if __name__ == "__main__":
    main()
