"""WP9.1 runtime probe: G9-01-G9-05 on real uap_api / uap_worker logins."""

from __future__ import annotations

import os
import uuid
from typing import Any

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from uap_platform.config import load_settings

ROLE_PASSWORDS = {
    "uap_api": "UAP_API_PASSWORD",
    "uap_worker": "UAP_WORKER_PASSWORD",
    "uap_publisher": "UAP_PUBLISHER_PASSWORD",
    "uap_scheduler": "UAP_SCHEDULER_PASSWORD",
    "uap_public_reader": "UAP_PUBLIC_READER_PASSWORD",
    "uap_model_governance": "UAP_MODEL_GOVERNANCE_PASSWORD",
}

CURRENT_HEAD = "0017_selection_and_promotion"
EXPECTED_TABLE_COUNT = 50
GRANTOR_ID = uuid.UUID("00000000-0000-7000-8000-000000000901")


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


def insert_person(admin: psycopg.Connection[Any], *, active: bool = True) -> uuid.UUID:
    principal_id = uuid.uuid4()
    execute(
        admin,
        """
        INSERT INTO audit.principals (
            id, principal_type, issuer, subject, display_name, active
        ) VALUES (%s, 'person', %s, %s, %s, %s)
        """,
        principal_id,
        f"https://issuer.test/{principal_id}",
        str(principal_id),
        "review probe person",
        active,
    )
    return principal_id


def bind_role(
    admin: psycopg.Connection[Any],
    principal_id: uuid.UUID,
    role: str,
    *,
    scope_type: str = "global",
    scope_id: uuid.UUID | None = None,
) -> None:
    execute(
        admin,
        """
        INSERT INTO audit.role_bindings (
            id, principal_id, role, scope_type, scope_id, reason,
            granted_by, granted_at
        ) VALUES (
            %s, %s, %s::audit.application_role, %s, %s, 'wp9.1 probe',
            %s, clock_timestamp()
        )
        """,
        uuid.uuid4(),
        principal_id,
        role,
        scope_type,
        scope_id,
        GRANTOR_ID,
    )


def guc(principal_id: uuid.UUID) -> list[tuple[str, tuple[object, ...]]]:
    return [
        ("SELECT set_config('uap.principal_id', %s, true)", (str(principal_id),)),
    ]


def require_role(role: str) -> tuple[str, tuple[object, ...]]:
    return ("SELECT audit.require_active_role(%s::audit.application_role)", (role,))


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


def counts(admin: psycopg.Connection[Any]) -> tuple[int, int, int, int]:
    return (
        int(scalar(admin, "SELECT count(*) FROM audit.review_cases")),
        int(scalar(admin, "SELECT count(*) FROM audit.review_decisions")),
        int(scalar(admin, "SELECT count(*) FROM audit.role_bindings")),
        int(scalar(admin, "SELECT count(*) FROM audit.audit_events")),
    )


def g9_01(admin: psycopg.Connection[Any], api: psycopg.Connection[Any]) -> None:
    principal = insert_person(admin)
    bind_role(admin, principal, "reviewer")
    before = counts(admin)
    with api.transaction():
        with api.cursor() as cursor:
            cursor.execute("SELECT set_config('uap.principal_id', %s, true)", (str(principal),))
            cursor.execute(
                "SELECT audit.require_active_role(%s::audit.application_role)",
                ("reviewer",),
            )
            row = cursor.fetchone()
    require("g9-01 principal", row[0] if row else None, principal)
    require("g9-01 no writes", counts(admin), before)


def g9_02(admin: psycopg.Connection[Any], api: psycopg.Connection[Any]) -> None:
    missing_id = uuid.uuid4()
    inactive = insert_person(admin, active=False)
    bind_role(admin, inactive, "reviewer")
    cases: list[tuple[list[tuple[str, tuple[object, ...]]], str]] = [
        ([], "unset"),
        ([("SELECT set_config('uap.principal_id', %s, true)", ("",))], "empty"),
        ([("SELECT set_config('uap.principal_id', %s, true)", ("not-a-uuid",))], "invalid"),
        (guc(missing_id), "unknown"),
        (guc(inactive), "inactive"),
    ]
    for steps, label in cases:
        payload = [*steps, require_role("reviewer")]
        state, primary = sqlerror_tx(api, payload)
        require(f"g9-02 {label} sqlstate", state, "42501")
        require(f"g9-02 {label} code", primary, "review_principal_missing")


def g9_03(admin: psycopg.Connection[Any], api: psycopg.Connection[Any]) -> None:
    service_id = uuid.uuid4()
    execute(
        admin,
        """
        INSERT INTO audit.principals (
            id, principal_type, service_name, display_name, active
        ) VALUES (%s, 'service', %s, 'wp9 service', true)
        """,
        service_id,
        f"svc-{service_id}",
    )
    state, primary = sqlerror_tx(api, [*guc(service_id), require_role("reviewer")])
    require("g9-03 sqlstate", state, "42501")
    require("g9-03 code", primary, "review_service_principal_denied")


def g9_04(admin: psycopg.Connection[Any], api: psycopg.Connection[Any]) -> None:
    worker = connect("uap_worker")
    try:
        person = insert_person(admin)
        bind_role(admin, person, "reviewer")
        state, primary = sqlerror_tx(worker, [*guc(person), require_role("reviewer")])
        require("g9-04 worker sqlstate", state, "42501")
        require(
            "g9-04 worker denied",
            primary in {"review_session_role_denied"}
            or "permission denied" in primary
            or "must be owner" in primary,
            True,
        )
        require(
            "g9-04 worker no execute",
            scalar(
                admin,
                """
                SELECT has_function_privilege(
                    'uap_worker',
                    'audit.require_active_role(audit.application_role)',
                    'EXECUTE'
                )
                """,
            ),
            False,
        )
    finally:
        worker.close()
    unbound = insert_person(admin)
    state, primary = sqlerror_tx(api, [*guc(unbound), require_role("reviewer")])
    require("g9-04 unbound sqlstate", state, "42501")
    require("g9-04 unbound code", primary, "review_role_denied")


def g9_05(admin: psycopg.Connection[Any], api: psycopg.Connection[Any]) -> None:
    senior = insert_person(admin)
    bind_role(admin, senior, "senior_reviewer")
    with api.transaction():
        with api.cursor() as cursor:
            cursor.execute("SELECT set_config('uap.principal_id', %s, true)", (str(senior),))
            cursor.execute(
                "SELECT audit.require_active_role(%s::audit.application_role)",
                ("reviewer",),
            )
            as_reviewer = cursor.fetchone()
            cursor.execute(
                "SELECT audit.require_active_role(%s::audit.application_role)",
                ("senior_reviewer",),
            )
            as_senior = cursor.fetchone()
    require("g9-05 senior as reviewer", as_reviewer[0] if as_reviewer else None, senior)
    require("g9-05 senior as senior", as_senior[0] if as_senior else None, senior)

    reviewer = insert_person(admin)
    bind_role(admin, reviewer, "reviewer")
    state, primary = sqlerror_tx(api, [*guc(reviewer), require_role("senior_reviewer")])
    require("g9-05 reviewer-as-senior sqlstate", state, "42501")
    require("g9-05 reviewer-as-senior code", primary, "review_role_denied")

    admin_person = insert_person(admin)
    bind_role(admin, admin_person, "platform_admin")
    state, primary = sqlerror_tx(api, [*guc(admin_person), require_role("reviewer")])
    require("g9-05 platform_admin sqlstate", state, "42501")
    require("g9-05 platform_admin code", primary, "review_role_denied")


def permissions(admin: psycopg.Connection[Any]) -> None:
    require(
        "api execute require_active_role",
        scalar(
            admin,
            """
            SELECT has_function_privilege(
                'uap_api',
                'audit.require_active_role(audit.application_role)',
                'EXECUTE'
            )
            """,
        ),
        True,
    )
    require(
        "api cannot insert review_cases",
        scalar(
            admin,
            "SELECT has_table_privilege('uap_api', 'audit.review_cases', 'INSERT')",
        ),
        False,
    )
    require(
        "api cannot insert append via execute",
        scalar(
            admin,
            """
            SELECT has_function_privilege(
                'uap_api',
                'audit.append_audit_event(text,text,text,uuid,uuid,jsonb)',
                'EXECUTE'
            )
            """,
        ),
        False,
    )
    for role in (
        "uap_publisher",
        "uap_scheduler",
        "uap_public_reader",
        "uap_model_governance",
    ):
        require(
            f"{role} no execute",
            scalar(
                admin,
                """
                SELECT has_function_privilege(
                    %s,
                    'audit.require_active_role(audit.application_role)',
                    'EXECUTE'
                )
                """,
                role,
            ),
            False,
        )
    require(
        "merge still closed",
        scalar(
            admin,
            """
            SELECT has_function_privilege(
                'uap_api',
                'core.merge_entities(uuid,uuid,uuid,text)',
                'EXECUTE'
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
        g9_01(admin, api)
        g9_02(admin, api)
        g9_03(admin, api)
        g9_04(admin, api)
        g9_05(admin, api)
    finally:
        api.close()
        admin.close()
    print("WP9.1 runtime probe passed: G9-01 G9-02 G9-03 G9-04 G9-05")


if __name__ == "__main__":
    main()
