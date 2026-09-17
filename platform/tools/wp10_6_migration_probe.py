"""Live G10-26 upgrade/downgrade matrix for the WP10.6-B gate.

The probe deliberately creates one disposable database per matrix variant.  It
uses the real Alembic revisions and real PostgreSQL guards; no result is
inferred from source text.  Database URLs and role passwords remain in the
process environment and are never written to evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url

from alembic import command

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "wp10.6-b-g10-26-evidence.v1"
EXPECTED_ERROR = "publication_contract_state_blocks_downgrade"
EXPECTED_SQLSTATE = "22023"

SCENARIO_IDS = (
    "empty_0019_0020_roundtrip",
    "empty_0020_0021_roundtrip",
    "empty_0021_0022_roundtrip",
    "empty_0022_0023_roundtrip",
    "empty_0023_0024_roundtrip",
    "legacy_v1_quarantine_blocks_0020_downgrade",
    "mixed_v1_v2_state_blocks_0020_downgrade",
    "grant_lifecycle_states_0020_matrix",
    "pending_publication_event_0021_roundtrip",
    "retry_publication_event_blocks_0021_downgrade",
    "terminal_publication_event_blocks_0021_downgrade",
    "delivery_attempt_blocks_0021_downgrade",
    "public_claim_projection_states_block_0022_downgrade",
    "public_document_entity_control_and_head_roundtrip",
)


@dataclass(frozen=True)
class MigrationResult:
    action: str
    target: str
    exit_code: int
    sqlstate: str | None = None
    error_code: str | None = None
    role: str | None = "migrator"
    started_at: str | None = None
    finished_at: str | None = None
    error_detail: str | None = None

    def as_dict(self) -> dict[str, object]:
        role_option = f" -x role={self.role}" if self.role else ""
        return {
            "command": f"python -m alembic{role_option} {self.action} {self.target}",
            "exit_code": self.exit_code,
            "sqlstate": self.sqlstate,
            "error_code": self.error_code,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error_detail": self.error_detail,
        }


def libpq_url(value: str) -> str:
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def database_url(admin_url: str, database: str) -> str:
    parsed = make_url(admin_url)
    if parsed.drivername == "postgresql":
        parsed = parsed.set(drivername="postgresql+psycopg")
    return parsed.set(database=database).render_as_string(hide_password=False)


def now() -> str:
    return datetime.now(UTC).isoformat()


def _error_details(error: BaseException) -> tuple[str | None, str | None]:
    current: BaseException | None = error
    text = str(error)
    sqlstate: str | None = None
    error_code: str | None = None
    while current is not None:
        diagnostic = getattr(current, "diag", None)
        sqlstate = sqlstate or getattr(current, "sqlstate", None)
        primary = getattr(diagnostic, "message_primary", None)
        if primary:
            text += f" {primary}"
        current = current.__cause__ or current.__context__
    for candidate in (
        EXPECTED_ERROR,
        "publication_rebuild_mismatch",
        "publication_manifest_invalid",
    ):
        if candidate in text:
            error_code = candidate
            break
    match = re.search(r"\b(\d{5})\b", text)
    return sqlstate or (match.group(1) if match else None), error_code


def _safe_error_detail(error: BaseException) -> str:
    original = getattr(error, "orig", error)
    diagnostic = getattr(original, "diag", None)
    detail = getattr(diagnostic, "message_primary", None) or type(original).__name__
    for name, value in os.environ.items():
        if value and any(token in name for token in ("PASSWORD", "SECRET", "DSN", "DATABASE_URL")):
            detail = str(detail).replace(value, "[REDACTED]")
    return str(detail)[:500]


def run_alembic(
    database: str, action: str, target: str, *, role: str | None = "migrator"
) -> MigrationResult:
    if action not in {"upgrade", "downgrade"}:
        raise ValueError("unsupported migration action")
    started_at = now()
    config = Config(str(PLATFORM_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PLATFORM_ROOT / "alembic"))
    if role is not None:
        config.cmd_opts = argparse.Namespace(x=[f"role={role}"])
    previous = os.environ.get("UAP_DATABASE_URL")
    os.environ["UAP_DATABASE_URL"] = database
    try:
        getattr(command, action)(config, target)
    except Exception as error:  # pragma: no cover - exercised by live PG
        sqlstate, error_code = _error_details(error)
        return MigrationResult(
            action,
            target,
            1,
            sqlstate,
            error_code,
            role,
            started_at,
            now(),
            _safe_error_detail(error),
        )
    finally:
        if previous is None:
            os.environ.pop("UAP_DATABASE_URL", None)
        else:
            os.environ["UAP_DATABASE_URL"] = previous
    return MigrationResult(action, target, 0, role=role, started_at=started_at, finished_at=now())


def create_database(admin_url: str, name: str) -> None:
    with psycopg.connect(libpq_url(admin_url), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def drop_database(admin_url: str, name: str) -> None:
    with psycopg.connect(libpq_url(admin_url), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


def set_migrator_login(admin_url: str, enabled: bool) -> None:
    with psycopg.connect(libpq_url(admin_url), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='uap_migrator')")
            row = cursor.fetchone()
            if row is None or not row[0]:
                return
            cursor.execute(
                sql.SQL("ALTER ROLE uap_migrator {}").format(
                    sql.SQL("LOGIN" if enabled else "NOLOGIN")
                )
            )


def scalar(connection: psycopg.Connection[Any], statement: str, *params: object) -> Any:
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("probe query returned no row")
    return row[0]


def bootstrap(admin_url: str, name: str) -> str:
    isolated = database_url(admin_url, name)
    result = run_alembic(isolated, "upgrade", "0001_roles_and_schemas", role=None)
    if result.exit_code:
        raise RuntimeError(f"bootstrap migration failed: {result.error_detail}")
    previous = os.environ.get("UAP_DATABASE_URL")
    os.environ["UAP_DATABASE_URL"] = isolated
    try:
        from tools.configure_roles import configure

        configure()
    finally:
        if previous is None:
            os.environ.pop("UAP_DATABASE_URL", None)
        else:
            os.environ["UAP_DATABASE_URL"] = previous
    set_migrator_login(admin_url, True)
    result = run_alembic(isolated, "upgrade", "0019_manual_claims_binding")
    set_migrator_login(admin_url, False)
    if result.exit_code:
        raise RuntimeError(f"0019 bootstrap migration failed: {result.error_detail}")
    return isolated


def migrate(database: str, admin_url: str, action: str, target: str) -> MigrationResult:
    set_migrator_login(admin_url, True)
    try:
        return run_alembic(database, action, target)
    finally:
        set_migrator_login(admin_url, False)


def _catalog_rows(connection: psycopg.Connection[Any], query: str) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(query)
        return [tuple(row) for row in cursor.fetchall()]


def snapshot(database: str) -> dict[str, object]:
    with psycopg.connect(libpq_url(database)) as connection:
        table_rows = _catalog_rows(
            connection,
            """
            SELECT n.nspname, c.relname, c.relkind
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname IN ('public','ingest','core','ops','audit')
               AND c.relkind IN ('r','p','v','m')
             ORDER BY 1,2,3
            """,
        )
        columns = _catalog_rows(
            connection,
            """
            SELECT table_schema, table_name, column_name,
                   data_type, udt_schema, udt_name, is_nullable
              FROM information_schema.columns
             WHERE table_schema IN ('public','ingest','core','ops','audit')
             ORDER BY 1,2,3
            """,
        )
        constraints = _catalog_rows(
            connection,
            """
            SELECT n.nspname, c.relname, con.conname, con.contype,
                   pg_get_constraintdef(con.oid, true)
              FROM pg_constraint con
              JOIN pg_class c ON c.oid = con.conrelid
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname IN ('public','ingest','core','ops','audit')
             ORDER BY 1,2,3
            """,
        )
        indexes = _catalog_rows(
            connection,
            """
            SELECT schemaname, tablename, indexname, indexdef
              FROM pg_indexes
             WHERE schemaname IN ('public','ingest','core','ops','audit')
             ORDER BY 1,2,3
            """,
        )
        routines = _catalog_rows(
            connection,
            """
            SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid),
                   pg_get_function_result(p.oid), md5(p.prosrc)
              FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname IN ('public','ingest','core','ops','audit')
             ORDER BY 1,2,3
            """,
        )
        privileges = _catalog_rows(
            connection,
            """
            SELECT table_schema, table_name, grantee, privilege_type
              FROM information_schema.role_table_grants
             WHERE table_schema IN ('public','ingest','core','ops','audit')
             ORDER BY 1,2,3,4
            """,
        )
        routine_privileges = _catalog_rows(
            connection,
            """
            SELECT routine_schema, routine_name, grantee, privilege_type
              FROM information_schema.role_routine_grants
             WHERE routine_schema IN ('public','ingest','core','ops','audit')
             ORDER BY 1,2,3,4
            """,
        )
        schema_privileges = _catalog_rows(
            connection,
            """
            SELECT namespace.nspname,
                   COALESCE(grantee.rolname, 'PUBLIC'),
                   privilege.privilege_type
              FROM pg_namespace AS namespace
              CROSS JOIN LATERAL aclexplode(
                  COALESCE(
                      namespace.nspacl,
                      acldefault('n', namespace.nspowner)
                  )
              ) AS privilege
              LEFT JOIN pg_roles AS grantee ON grantee.oid = privilege.grantee
             WHERE namespace.nspname IN ('public','ingest','core','ops','audit')
             ORDER BY 1,2,3
            """,
        )
        counts: dict[str, int] = {}
        for schema, table, _kind in table_rows:
            if schema == "public" and table == "alembic_version":
                continue
            statement = sql.SQL("SELECT count(*) FROM {}.{}").format(
                sql.Identifier(str(schema)), sql.Identifier(str(table))
            )
            counts[f"{schema}.{table}"] = int(scalar(connection, statement.as_string(connection)))
        revision = str(scalar(connection, "SELECT version_num FROM public.alembic_version"))
    value: dict[str, object] = {
        "revision": revision,
        "counts": counts,
        "tables": table_rows,
        "columns": columns,
        "constraints": constraints,
        "indexes": indexes,
        "routines": routines,
        "privileges": privileges,
        "routine_privileges": routine_privileges,
        "schema_privileges": schema_privileges,
    }
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")
    )
    value["digest"] = hashlib.sha256(canonical.encode()).hexdigest()
    return value


def _dict_rows(connection: psycopg.Connection[Any], query: str) -> list[dict[str, object]]:
    with connection.cursor() as cursor:
        cursor.execute(query)
        names = [column.name for column in cursor.description or ()]
        rows = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
    return sorted(rows, key=_canonical_json)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def acl_snapshot(database: str) -> dict[str, object]:
    """Capture the normalized and raw catalogs needed for the A/B/C/D/E ACL proof."""
    with psycopg.connect(libpq_url(database)) as connection:
        connection.execute("SET TIME ZONE 'UTC'")
        result: dict[str, object] = {
            "revision": scalar(connection, "SELECT version_num FROM public.alembic_version")
        }
        queries = {
            "relations": """
                SELECT n.nspname AS schema, c.relname AS name, c.relkind AS kind,
                       pg_get_userbyid(c.relowner) AS owner, c.relrowsecurity,
                       c.relforcerowsecurity, c.relreplident, c.reloptions
                  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname IN ('ingest','core','ops','audit','public')
                   AND c.relkind IN ('r','p','v','m','S','f')
            """,
            "relation_acl": """
                SELECT n.nspname AS schema, c.relname AS name, c.relkind AS kind,
                       pg_get_userbyid(c.relowner) AS owner,
                       pg_get_userbyid(a.grantor) AS grantor,
                       CASE WHEN a.grantee=0 THEN 'PUBLIC'
                            ELSE pg_get_userbyid(a.grantee) END AS grantee,
                       a.privilege_type, a.is_grantable
                  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                  CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,
                    acldefault(CASE WHEN c.relkind='S' THEN 'S'::"char" ELSE 'r'::"char" END,
                               c.relowner))) a
                 WHERE n.nspname IN ('ingest','core','ops','audit','public')
                   AND c.relkind IN ('r','p','v','m','S','f')
            """,
            "columns": """
                SELECT n.nspname AS schema, c.relname AS relation, a.attname AS name,
                       format_type(a.atttypid,a.atttypmod) AS type, a.attnotnull,
                       a.attidentity, a.attgenerated,
                       pg_get_expr(d.adbin,d.adrelid) AS default_expr
                  FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
                  JOIN pg_namespace n ON n.oid=c.relnamespace
                  LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
                 WHERE n.nspname IN ('ingest','core','ops','audit','public')
                   AND c.relkind IN ('r','p','v','m','f')
                   AND a.attnum>0 AND NOT a.attisdropped
            """,
            "column_acl": """
                SELECT n.nspname AS schema, c.relname AS relation, a.attname AS name,
                       pg_get_userbyid(x.grantor) AS grantor,
                       CASE WHEN x.grantee=0 THEN 'PUBLIC'
                            ELSE pg_get_userbyid(x.grantee) END AS grantee,
                       x.privilege_type, x.is_grantable
                  FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
                  JOIN pg_namespace n ON n.oid=c.relnamespace
                  CROSS JOIN LATERAL aclexplode(a.attacl) x
                 WHERE n.nspname IN ('ingest','core','ops','audit','public')
                   AND a.attnum>0 AND NOT a.attisdropped
            """,
            "schemas": """
                SELECT n.nspname AS name, pg_get_userbyid(n.nspowner) AS owner
                  FROM pg_namespace n
                 WHERE n.nspname IN ('ingest','core','ops','audit','public')
            """,
            "schema_acl": """
                SELECT n.nspname AS name, pg_get_userbyid(a.grantor) AS grantor,
                       CASE WHEN a.grantee=0 THEN 'PUBLIC'
                            ELSE pg_get_userbyid(a.grantee) END AS grantee,
                       a.privilege_type, a.is_grantable
                  FROM pg_namespace n CROSS JOIN LATERAL
                       aclexplode(COALESCE(n.nspacl,acldefault('n',n.nspowner))) a
                 WHERE n.nspname IN ('ingest','core','ops','audit','public')
            """,
            "default_acl": """
                SELECT pg_get_userbyid(d.defaclrole) AS owner,
                       COALESCE(n.nspname,'GLOBAL') AS schema,
                       d.defaclobjtype AS object_type, pg_get_userbyid(a.grantor) AS grantor,
                       CASE WHEN a.grantee=0 THEN 'PUBLIC'
                            ELSE pg_get_userbyid(a.grantee) END AS grantee,
                       a.privilege_type, a.is_grantable
                  FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid=d.defaclnamespace
                  CROSS JOIN LATERAL aclexplode(d.defaclacl) a
            """,
            "functions": """
                SELECT n.nspname AS schema, p.proname AS name,
                       pg_get_function_identity_arguments(p.oid) AS identity_arguments,
                       pg_get_function_result(p.oid) AS result,
                       pg_get_userbyid(p.proowner) AS owner, l.lanname AS language,
                       p.prokind, p.provolatile, p.proisstrict, p.prosecdef,
                       p.proleakproof, p.proparallel, p.proconfig,
                       pg_get_functiondef(p.oid) AS definition
                  FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                  JOIN pg_language l ON l.oid=p.prolang
                 WHERE n.nspname IN ('ingest','core','ops','audit','public')
                   AND p.prokind IN ('f','p')
            """,
            "function_acl": """
                SELECT n.nspname AS schema, p.proname AS name,
                       pg_get_function_identity_arguments(p.oid) AS identity_arguments,
                       pg_get_userbyid(p.proowner) AS owner,
                       pg_get_userbyid(a.grantor) AS grantor,
                       CASE WHEN a.grantee=0 THEN 'PUBLIC'
                            ELSE pg_get_userbyid(a.grantee) END AS grantee,
                       a.privilege_type, a.is_grantable
                  FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                  CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
                 WHERE n.nspname IN ('ingest','core','ops','audit','public')
            """,
            "constraints": """
                SELECT n.nspname AS schema, c.relname AS relation, k.conname AS name,
                       k.contype, k.condeferrable, k.condeferred, k.convalidated,
                       pg_get_constraintdef(k.oid,true) AS definition
                  FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
                  JOIN pg_namespace n ON n.oid=c.relnamespace
                 WHERE n.nspname IN ('ingest','core','ops','audit','public')
            """,
            "indexes": """
                SELECT n.nspname AS schema, c.relname AS relation, i.relname AS name,
                       x.indisvalid, x.indisready, x.indisunique, x.indisprimary,
                       pg_get_indexdef(i.oid) AS definition
                  FROM pg_index x JOIN pg_class c ON c.oid=x.indrelid
                  JOIN pg_class i ON i.oid=x.indexrelid
                  JOIN pg_namespace n ON n.oid=c.relnamespace
                 WHERE n.nspname IN ('ingest','core','ops','audit','public')
            """,
            "triggers": """
                SELECT n.nspname AS schema, c.relname AS relation, t.tgname AS name,
                       t.tgenabled, pg_get_triggerdef(t.oid,true) AS definition
                  FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
                  JOIN pg_namespace n ON n.oid=c.relnamespace
                 WHERE n.nspname IN ('ingest','core','ops','audit','public')
                   AND NOT t.tgisinternal
            """,
            "roles": """
                SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb,
                       rolcanlogin, rolreplication, rolbypassrls, rolconnlimit, rolconfig
                  FROM pg_roles WHERE starts_with(rolname,'uap_')
            """,
            "memberships": """
                SELECT pg_get_userbyid(roleid) AS role, pg_get_userbyid(member) AS member,
                       pg_get_userbyid(grantor) AS grantor, admin_option,
                       inherit_option, set_option
                  FROM pg_auth_members
                 WHERE starts_with(pg_get_userbyid(roleid),'uap_')
                    OR starts_with(pg_get_userbyid(member),'uap_')
            """,
            "effective_tables": """
                SELECT r.rolname AS role, n.nspname AS schema, c.relname AS name,
                       v.privilege, has_table_privilege(r.oid,c.oid,v.privilege) AS allowed,
                       has_table_privilege(
                           r.oid,c.oid,v.privilege||' WITH GRANT OPTION'
                       ) AS grantable
                  FROM pg_roles r CROSS JOIN pg_class c
                  JOIN pg_namespace n ON n.oid=c.relnamespace
                  CROSS JOIN (VALUES ('SELECT'),('INSERT'),('UPDATE'),('DELETE'),
                                     ('TRUNCATE'),('REFERENCES'),('TRIGGER')) v(privilege)
                 WHERE starts_with(r.rolname,'uap_')
                   AND n.nspname IN ('ingest','core','ops','audit','public')
                   AND c.relkind IN ('r','p','v','m','f')
            """,
            "effective_functions": """
                SELECT r.rolname AS role, n.nspname AS schema, p.proname AS name,
                       pg_get_function_identity_arguments(p.oid) AS identity_arguments,
                       has_function_privilege(r.oid,p.oid,'EXECUTE') AS allowed,
                       has_function_privilege(r.oid,p.oid,'EXECUTE WITH GRANT OPTION') AS grantable
                  FROM pg_roles r CROSS JOIN pg_proc p
                  JOIN pg_namespace n ON n.oid=p.pronamespace
                 WHERE starts_with(r.rolname,'uap_')
                   AND n.nspname IN ('ingest','core','ops','audit','public')
            """,
        }
        for name, query in queries.items():
            result[name] = _dict_rows(connection, query)
        data: list[dict[str, object]] = []
        relations = result["relations"]
        if not isinstance(relations, list):
            raise RuntimeError("relation catalog snapshot is not a list")
        for relation in relations:
            if not isinstance(relation, dict) or relation["kind"] not in {"r", "p"}:
                continue
            if relation["schema"] == "public" and relation["name"] == "alembic_version":
                continue
            statement = sql.SQL("SELECT to_jsonb(t) FROM {}.{} t").format(
                sql.Identifier(str(relation["schema"])), sql.Identifier(str(relation["name"]))
            )
            contents = [row[0] for row in connection.execute(statement).fetchall()]
            contents.sort(key=_canonical_json)
            data.append(
                {
                    "schema": relation["schema"],
                    "name": relation["name"],
                    "count": len(contents),
                    "row_content_sha256": _digest(contents),
                }
            )
        result["data"] = sorted(data, key=_canonical_json)
    result["digest"] = _digest(result)
    return result


def snapshot_difference(before: dict[str, object], after: dict[str, object]) -> dict[str, object]:
    differences: dict[str, object] = {}
    for key in sorted(before.keys() | after.keys()):
        if key in {"digest", "revision"} or before.get(key) == after.get(key):
            continue
        left_value = before.get(key)
        right_value = after.get(key)
        if isinstance(left_value, list) and isinstance(right_value, list):
            left = {_canonical_json(item): item for item in left_value}
            right = {_canonical_json(item): item for item in right_value}
            differences[key] = {
                "removed": [left[item] for item in sorted(left.keys() - right.keys())],
                "added": [right[item] for item in sorted(right.keys() - left.keys())],
            }
        else:
            differences[key] = {"before": left_value, "after": right_value}
    return differences


def _insert(connection: psycopg.Connection[Any], statement: str, *params: object) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL session_replication_role = replica")
        cursor.execute(statement, params)


def seed_v1(database: str, *, count: int = 1) -> None:
    with psycopg.connect(libpq_url(database)) as connection:
        for _ in range(count):
            grant_id = uuid.uuid4()
            _insert(
                connection,
                """
                INSERT INTO audit.document_publication_grants
                    (id, review_case_id, document_version_id, decision_id, revision_no,
                     grant_status, granted_at, publication_payload_sha256)
                VALUES (%s,%s,%s,%s,1,'active',clock_timestamp(),repeat('a',64))
                """,
                grant_id,
                uuid.uuid4(),
                uuid.uuid4(),
                uuid.uuid4(),
            )
            _insert(
                connection,
                """
                INSERT INTO ops.outbox_events
                    (id, aggregate_type, aggregate_id, event_type, event_key, payload, occurred_at)
                VALUES (%s,'document_publication_grants',%s,'publication.granted',%s,
                        '{"schema":"publication-outbox.v1","subject_type":"document"}'::jsonb,
                        clock_timestamp())
                """,
                uuid.uuid4(),
                grant_id,
                f"g10-26-v1-{uuid.uuid4()}",
            )
        connection.commit()


def seed_grant_lifecycle(database: str, status: str) -> None:
    with psycopg.connect(libpq_url(database)) as connection:
        grant_id = uuid.uuid4()
        withdrawn = status == "withdrawn"
        _insert(
            connection,
            """
            INSERT INTO audit.document_publication_grants
                (id, review_case_id, document_version_id, decision_id, revision_no,
                 grant_status, granted_at, withdrawn_by_decision_id, withdrawn_at,
                 publication_payload_sha256)
            VALUES (%s,%s,%s,%s,1,%s,clock_timestamp(),%s,
                    CASE WHEN %s THEN clock_timestamp() ELSE NULL END,repeat('b',64))
            """,
            grant_id,
            uuid.uuid4(),
            uuid.uuid4(),
            uuid.uuid4(),
            status,
            uuid.uuid4() if withdrawn else None,
            withdrawn,
        )
        connection.commit()


def seed_event(database: str, state: str) -> None:
    event_id = uuid.uuid4()
    with psycopg.connect(libpq_url(database)) as connection:
        if state == "pending":
            _insert(
                connection,
                """
                INSERT INTO ops.outbox_events
                    (id, aggregate_type, aggregate_id, event_type, event_key, payload,
                     occurred_at, available_at)
                VALUES (%s,'document_publication_grants',%s,'publication.granted',%s,
                        '{"schema":"publication-outbox.v2","subject_type":"document"}'::jsonb,
                        clock_timestamp(),clock_timestamp()+interval '60 seconds')
                """,
                event_id,
                uuid.uuid4(),
                f"g10-26-{state}-{event_id}",
            )
        elif state == "retry":
            _insert(
                connection,
                """
                INSERT INTO ops.outbox_events
                    (id, aggregate_type, aggregate_id, event_type, event_key, payload,
                     occurred_at, available_at, publish_attempts, last_error_code,
                     last_error_summary)
                VALUES (%s,'document_publication_grants',%s,'publication.granted',%s,
                        '{"schema":"publication-outbox.v2","subject_type":"document"}'::jsonb,
                        clock_timestamp(),clock_timestamp()+interval '60 seconds',1,
                        'publication_dependency_not_ready','publication dependency is not ready')
                """,
                event_id,
                uuid.uuid4(),
                f"g10-26-{state}-{event_id}",
            )
        else:
            _insert(
                connection,
                """
                INSERT INTO ops.outbox_events
                    (id, aggregate_type, aggregate_id, event_type, event_key, payload,
                     occurred_at, publish_attempts, terminal_at, terminal_error_code)
                VALUES (%s,'document_publication_grants',%s,'publication.granted',%s,
                        '{"schema":"publication-outbox.v2","subject_type":"document"}'::jsonb,
                        clock_timestamp(),1,clock_timestamp(),'publication_event_schema_unsupported')
                """,
                event_id,
                uuid.uuid4(),
                f"g10-26-{state}-{event_id}",
            )
        connection.commit()


def seed_attempt(database: str) -> None:
    event_id = uuid.uuid4()
    with psycopg.connect(libpq_url(database)) as connection:
        _insert(
            connection,
            """
            INSERT INTO ops.outbox_events
                (id, aggregate_type, aggregate_id, event_type, event_key, payload,
                 occurred_at, publish_attempts, lease_owner, lease_token, lease_expires_at)
            VALUES (%s,'document_publication_grants',%s,'publication.granted',%s,
                    '{"schema":"publication-outbox.v2","subject_type":"document"}'::jsonb,
                    clock_timestamp(),1,'g10-26-attempt',%s,clock_timestamp()+interval '60 seconds')
            """,
            event_id,
            uuid.uuid4(),
            f"g10-26-attempt-{event_id}",
            uuid.uuid4(),
        )
        _insert(
            connection,
            """
            INSERT INTO audit.publication_delivery_attempts
                (event_id, attempt_no, dispatcher, lease_token_hash, outcome, started_at)
            VALUES (%s,1,'g10-26-attempt',repeat('c',64),'running',clock_timestamp())
            """,
            event_id,
        )
        connection.commit()


PUBLIC_TABLES = {
    "documents": """
        INSERT INTO public.documents
            (id,document_grant_id,slug,title,category,fact_status,source_name,
             canonical_source_url,published_at,revision_no)
        VALUES (%s,%s,%s,'fixture','other','unverified','fixture',%s,clock_timestamp(),1)
    """,
    "entities": """
        INSERT INTO public.entities
            (id,entity_grant_id,slug,entity_type,name,published_at,revision_no)
        VALUES (%s,%s,%s,'person','fixture',clock_timestamp(),1)
    """,
    "claims": """
        INSERT INTO public.claims
            (id,document_id,claim_grant_id,ordinal,claim_text,claim_type,assertion_status,revision_no)
        VALUES (%s,%s,%s,0,'fixture','fact','unverified',1)
    """,
    "evidence": """
        INSERT INTO public.evidence
            (id,document_id,excerpt,locator_type,public_locator,locator_sha256,source_url)
        VALUES (%s,%s,'fixture','page','{}'::jsonb,repeat('d',64),'https://g10-26.invalid/evidence')
    """,
    "claim_evidence": """
        INSERT INTO public.claim_evidence (id,claim_id,evidence_id) VALUES (%s,%s,%s)
    """,
    "document_entities": """
        INSERT INTO public.document_entities
            (id,document_id,entity_id,basis_evidence_id,basis_claim_id)
        VALUES (%s,%s,%s,%s,%s)
    """,
    "search_documents": """
        INSERT INTO public.search_documents
            (document_id,search_vector,display_text,indexed_at)
        VALUES (%s,to_tsvector('simple','fixture'),'fixture',clock_timestamp())
    """,
}


def seed_public(database: str, kind: str) -> None:
    with psycopg.connect(libpq_url(database)) as connection:
        ids = [uuid.uuid4() for _ in range(5)]
        if kind == "documents":
            params: tuple[object, ...] = (
                ids[0],
                ids[1],
                f"d-g10-26-{ids[0].hex}",
                f"https://g10-26.invalid/doc/{ids[0].hex}",
            )
        elif kind == "entities":
            params = (ids[0], ids[1], f"e-g10-26-{ids[0].hex}")
        elif kind == "claims":
            params = (ids[0], ids[1], ids[2])
        elif kind == "evidence":
            params = (ids[0], ids[1])
        elif kind == "claim_evidence":
            params = (ids[0], ids[1], ids[2])
        elif kind == "document_entities":
            params = (ids[0], ids[1], ids[2], ids[3], ids[4])
        elif kind == "search_documents":
            params = (ids[0],)
        else:
            raise ValueError(f"unknown public fixture: {kind}")
        _insert(connection, PUBLIC_TABLES[kind], *params)
        connection.commit()


def seed_identity_or_rebuild(database: str, kind: str) -> None:
    with psycopg.connect(libpq_url(database)) as connection:
        internal_id = uuid.uuid4()
        public_id = uuid.uuid4()
        if kind == "claim_identity":
            _insert(
                connection,
                """INSERT INTO audit.claim_public_identities
                    (claim_id,document_id,public_id,display_ordinal)
                   VALUES (%s,%s,%s,0)""",
                internal_id,
                uuid.uuid4(),
                public_id,
            )
        elif kind == "evidence_identity":
            _insert(
                connection,
                """INSERT INTO audit.evidence_public_identities
                    (evidence_span_id,public_id) VALUES (%s,%s)""",
                internal_id,
                public_id,
            )
        elif kind == "rebuild_run":
            _insert(
                connection,
                """INSERT INTO audit.publication_rebuild_runs
                    (rebuild_id,input_digest,status,started_at)
                   VALUES (%s,repeat('e',64),'running',clock_timestamp())""",
                uuid.uuid4(),
            )
        else:
            raise ValueError(f"unknown identity fixture: {kind}")
        connection.commit()


def seed_control_document_entity(database: str) -> None:
    with psycopg.connect(libpq_url(database)) as connection:
        _insert(
            connection,
            """INSERT INTO public.documents
                (id,document_grant_id,slug,title,category,fact_status,source_name,
                 canonical_source_url,published_at,revision_no)
               VALUES (%s,%s,%s,'fixture','other','unverified','fixture',
                       %s,clock_timestamp(),1)""",
            uuid.uuid4(),
            uuid.uuid4(),
            f"d-g10-26-control-{uuid.uuid4().hex}",
            f"https://g10-26.invalid/control/{uuid.uuid4().hex}",
        )
        _insert(
            connection,
            """INSERT INTO public.entities
                (id,entity_grant_id,slug,entity_type,name,published_at,revision_no)
               VALUES (%s,%s,%s,'person','fixture',clock_timestamp(),1)""",
            uuid.uuid4(),
            uuid.uuid4(),
            f"e-g10-26-control-{uuid.uuid4().hex}",
        )
        connection.commit()


LEGACY_EXECUTE_PAIRS = (
    ("uap_api", "core.canonical_entity_id(uuid)"),
    ("uap_publisher", "core.canonical_entity_id(uuid)"),
    ("uap_publisher", "ops.claim_job(text,text,text[],integer)"),
    ("uap_publisher", "ops.classify_failure(smallint,text)"),
    (
        "uap_publisher",
        "ops.finish_job(uuid,uuid,uuid,ops.attempt_outcome,smallint,text,text,integer)",
    ),
    ("uap_publisher", "ops.claim_outbox(text,integer,integer)"),
    ("uap_publisher", "ops.ack_outbox(uuid,uuid)"),
    ("uap_publisher", "ops.release_outbox(uuid,uuid,text,text)"),
    ("uap_publisher", "ops.publish_outbox_failure(uuid,uuid,text,text,integer)"),
)
LEGACY_API_WRITABLE_TABLES = frozenset(
    {
        "core.stored_objects",
        "core.documents",
        "core.document_versions",
        "core.extractions",
    }
)


def _role_database_url(database: str, role: str) -> str:
    password_name = f"UAP_{role.removeprefix('uap_').upper()}_PASSWORD"
    password = os.environ.get(password_name)
    if not password:
        raise RuntimeError(
            f"required role password environment variable is missing: {password_name}"
        )
    parsed = make_url(database)
    return parsed.set(username=role, password=password).render_as_string(hide_password=False)


def real_role_permissions(database: str) -> dict[str, object]:
    execute_results: list[dict[str, object]] = []
    dml_results: list[dict[str, object]] = []
    for role, function in LEGACY_EXECUTE_PAIRS:
        with psycopg.connect(libpq_url(_role_database_url(database, role))) as connection:
            allowed = bool(
                scalar(
                    connection,
                    "SELECT has_function_privilege(current_user, %s, 'EXECUTE')",
                    function,
                )
            )
            execute_results.append({"role": role, "function": function, "allowed": allowed})
    with psycopg.connect(libpq_url(_role_database_url(database, "uap_api"))) as connection:
        tables = _dict_rows(
            connection,
            """
            SELECT n.nspname AS schema, c.relname AS name
              FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname IN ('core','ops') AND c.relkind IN ('r','p')
            """,
        )
        for table in tables:
            qualified = f"{table['schema']}.{table['name']}"
            for privilege in ("INSERT", "UPDATE"):
                allowed = bool(
                    scalar(
                        connection,
                        "SELECT has_table_privilege(current_user, %s, %s)",
                        qualified,
                        privilege,
                    )
                )
                dml_results.append({"table": qualified, "privilege": privilege, "allowed": allowed})
    return {"execute": execute_results, "api_table_dml": dml_results}


def _assert_legacy_permissions(result: dict[str, object]) -> None:
    execute = result["execute"]
    dml = result["api_table_dml"]
    if not isinstance(execute, list) or not all(item["allowed"] for item in execute):
        raise RuntimeError("native 0019 EXECUTE privileges were not restored")
    if not isinstance(dml, list):
        raise RuntimeError("native 0019 table privileges were not captured")
    for item in dml:
        expected = item["table"] in LEGACY_API_WRITABLE_TABLES
        if item["allowed"] is not expected:
            raise RuntimeError(f"native 0019 DML mismatch for {item['table']} {item['privilege']}")


def _assert_wp10_boundary(result: dict[str, object]) -> None:
    execute = result["execute"]
    dml = result["api_table_dml"]
    if not isinstance(execute, list) or any(item["allowed"] for item in execute):
        raise RuntimeError("0020 retained a legacy direct EXECUTE path")
    if not isinstance(dml, list) or any(item["allowed"] for item in dml):
        raise RuntimeError("0020 retained a legacy direct core/ops write path")


def _assert_native_0024_permissions(database: str) -> dict[str, object]:
    with psycopg.connect(libpq_url(_role_database_url(database, "uap_api"))) as connection:
        permissions: dict[str, object] = {
            table: bool(
                scalar(
                    connection,
                    "SELECT has_table_privilege(current_user, %s, 'SELECT')",
                    table,
                )
            )
            for table in (
                "core.analysis_results",
                "ops.model_runs",
                "ops.prompt_versions",
            )
        }
    if permissions != {
        "core.analysis_results": True,
        "ops.model_runs": False,
        "ops.prompt_versions": False,
    }:
        raise RuntimeError("native 0024 model/analysis SELECT boundary changed")
    return permissions


def run_acl_roundtrip(admin_url: str) -> dict[str, object]:
    """Build A/B/C/D/E and prove the exact migration boundary with real roles."""
    started_at = now()
    abcd_name = f"uap_g10_26_acl_{uuid.uuid4().hex[:12]}"
    e_name = f"uap_g10_26_acl_{uuid.uuid4().hex[:12]}"
    stages: dict[str, Any] = {}
    commands: list[dict[str, object]] = []
    try:
        create_database(admin_url, abcd_name)
        abcd = bootstrap(admin_url, abcd_name)
        stages["A_native_0019"] = {
            "snapshot": acl_snapshot(abcd),
            "real_roles": real_role_permissions(abcd),
        }
        _assert_legacy_permissions(stages["A_native_0019"]["real_roles"])

        result = migrate(abcd, admin_url, "upgrade", "0020_wp10_publication_contract")
        commands.append(result.as_dict())
        if result.exit_code:
            raise RuntimeError("A to B migration failed")
        stages["B_first_0020"] = {
            "snapshot": acl_snapshot(abcd),
            "real_roles": real_role_permissions(abcd),
        }
        _assert_wp10_boundary(stages["B_first_0020"]["real_roles"])

        result = migrate(abcd, admin_url, "downgrade", "0019_manual_claims_binding")
        commands.append(result.as_dict())
        if result.exit_code:
            raise RuntimeError("B to C migration failed")
        stages["C_downgraded_0019"] = {
            "snapshot": acl_snapshot(abcd),
            "real_roles": real_role_permissions(abcd),
        }
        _assert_legacy_permissions(stages["C_downgraded_0019"]["real_roles"])

        result = migrate(abcd, admin_url, "upgrade", "0020_wp10_publication_contract")
        commands.append(result.as_dict())
        if result.exit_code:
            raise RuntimeError("C to D migration failed")
        stages["D_second_0020"] = {
            "snapshot": acl_snapshot(abcd),
            "real_roles": real_role_permissions(abcd),
        }
        _assert_wp10_boundary(stages["D_second_0020"]["real_roles"])

        create_database(admin_url, e_name)
        native_0024 = bootstrap(admin_url, e_name)
        result = migrate(native_0024, admin_url, "upgrade", "0024_wp10_admin_replay")
        commands.append(result.as_dict())
        if result.exit_code:
            raise RuntimeError("native E migration failed")
        stages["E_native_0024"] = {
            "snapshot": acl_snapshot(native_0024),
            "analysis_model_select": _assert_native_0024_permissions(native_0024),
        }

        c_minus_a = snapshot_difference(
            stages["A_native_0019"]["snapshot"], stages["C_downgraded_0019"]["snapshot"]
        )
        d_minus_b = snapshot_difference(
            stages["B_first_0020"]["snapshot"], stages["D_second_0020"]["snapshot"]
        )
        if c_minus_a or d_minus_b:
            raise RuntimeError("A/C or B/D normalized catalog mismatch")
        return {
            "status": "passed",
            "started_at": started_at,
            "finished_at": now(),
            "commands": commands,
            "stages": stages,
            "C_minus_A": c_minus_a,
            "D_minus_B": d_minus_b,
        }
    except Exception as error:
        return {
            "status": "failed",
            "started_at": started_at,
            "finished_at": now(),
            "commands": commands,
            "stages": stages,
            "error": f"{type(error).__name__}: {error}",
        }
    finally:
        set_migrator_login(admin_url, False)
        drop_database(admin_url, abcd_name)
        drop_database(admin_url, e_name)


def _assert_failure(result: MigrationResult) -> None:
    if (result.exit_code, result.sqlstate, result.error_code) != (
        1,
        EXPECTED_SQLSTATE,
        EXPECTED_ERROR,
    ):
        raise RuntimeError(
            "downgrade did not return the frozen guard SQLSTATE and error code: "
            + _canonical_json(result.as_dict())
        )


def _roundtrip(database: str, admin_url: str, head: str, parent: str) -> dict[str, object]:
    before = snapshot(database)
    result = migrate(database, admin_url, "downgrade", parent)
    if result.exit_code:
        raise RuntimeError("allowed downgrade unexpectedly failed")
    mid = snapshot(database)
    if mid["revision"] != parent:
        raise RuntimeError("roundtrip downgrade reached the wrong revision")
    upgrade = migrate(database, admin_url, "upgrade", head)
    if upgrade.exit_code:
        raise RuntimeError("roundtrip upgrade failed")
    after = snapshot(database)
    if before["digest"] != after["digest"]:
        changed = [
            key
            for key in (
                "revision",
                "counts",
                "tables",
                "columns",
                "constraints",
                "indexes",
                "routines",
                "privileges",
                "routine_privileges",
                "schema_privileges",
            )
            if before[key] != after[key]
        ]
    return {
        "before": before,
        "after": after,
        "downgrade": result.as_dict(),
        "upgrade": upgrade.as_dict(),
        "consistent": before["digest"] == after["digest"],
        "changed_keys": changed if before["digest"] != after["digest"] else [],
    }


def _blocked(database: str, admin_url: str, parent: str) -> dict[str, object]:
    before = snapshot(database)
    result = migrate(database, admin_url, "downgrade", parent)
    _assert_failure(result)
    after = snapshot(database)
    if before["digest"] != after["digest"] or before["revision"] != after["revision"]:
        raise RuntimeError("blocked downgrade changed revision or schema/data/privilege digest")
    return {"before": before, "after": after, "downgrade": result.as_dict(), "unchanged": True}


def _prepare(admin_url: str, head: str) -> tuple[str, str]:
    name = f"uap_g10_26_{uuid.uuid4().hex[:14]}"
    create_database(admin_url, name)
    try:
        isolated = bootstrap(admin_url, name)
        if head != "0019_manual_claims_binding":
            result = migrate(isolated, admin_url, "upgrade", head)
            if result.exit_code:
                raise RuntimeError(f"upgrade to {head} failed")
        return name, isolated
    except Exception:
        drop_database(admin_url, name)
        raise


def _run_variant(
    admin_url: str,
    scenario_id: str,
    variant_id: str,
    head: str,
    fixture: str | None,
    blocked: bool,
    parent: str,
) -> dict[str, object]:
    # Legacy-grant fixtures must exist before 0020 creates quarantine rows;
    # all later fixtures are inserted after their target revision exists.
    bootstrap_head = (
        "0019_manual_claims_binding"
        if fixture in {"v1", "mixed", "active", "superseded", "withdrawn"}
        else head
    )
    name, isolated = _prepare(admin_url, bootstrap_head)
    started = now()
    try:
        if fixture == "v1":
            seed_v1(isolated)
        elif fixture == "mixed":
            seed_v1(isolated, count=2)
        elif fixture in {"active", "superseded", "withdrawn"}:
            seed_grant_lifecycle(isolated, fixture)
        if bootstrap_head != head:
            result = migrate(isolated, admin_url, "upgrade", head)
            if result.exit_code:
                raise RuntimeError(f"fixture upgrade to {head} failed")
        if fixture == "v1":
            pass
        elif fixture == "mixed":
            pass
        elif fixture in {"active", "superseded", "withdrawn"}:
            pass
        elif fixture in {"pending", "retry", "terminal"}:
            seed_event(isolated, fixture)
        elif fixture == "attempt":
            seed_attempt(isolated)
        elif fixture in PUBLIC_TABLES:
            seed_public(isolated, fixture)
        elif fixture in {"claim_identity", "evidence_identity", "rebuild_run"}:
            seed_identity_or_rebuild(isolated, fixture)
        elif fixture == "control":
            seed_control_document_entity(isolated)
        checks = (
            _blocked(isolated, admin_url, parent)
            if blocked
            else _roundtrip(isolated, admin_url, head, parent)
        )
        final = snapshot(isolated)
        variant_result = {
            "scenario_id": scenario_id,
            "variant_id": variant_id,
            "database": name,
            "started_at": started,
            "finished_at": now(),
            "status": "passed",
            "initial_revision": "0019_manual_claims_binding",
            "target_revision": head,
            "final_revision": final["revision"],
            "checks": checks,
            "residue_for_database": False,
        }
        variant_result["status"] = "passed" if checks.get("consistent", True) else "failed"
        return variant_result
    finally:
        set_migrator_login(admin_url, False)
        drop_database(admin_url, name)


def scenario_variants(scenario_id: str) -> tuple[tuple[str, str, str, bool, str], ...]:
    empty = {
        "empty_0019_0020_roundtrip": (
            ("empty", "0020_wp10_publication_contract", False, "0019_manual_claims_binding"),
        ),
        "empty_0020_0021_roundtrip": (
            ("empty", "0021_wp10_publisher_projection", False, "0020_wp10_publication_contract"),
        ),
        "empty_0021_0022_roundtrip": (
            ("empty", "0022_wp10_claim_search_projection", False, "0021_wp10_publisher_projection"),
        ),
        "empty_0022_0023_roundtrip": (
            ("empty", "0023_wp10_api_read_indexes", False, "0022_wp10_claim_search_projection"),
        ),
        "empty_0023_0024_roundtrip": (
            ("empty", "0024_wp10_admin_replay", False, "0023_wp10_api_read_indexes"),
        ),
        "legacy_v1_quarantine_blocks_0020_downgrade": (
            ("v1", "0020_wp10_publication_contract", True, "0019_manual_claims_binding"),
        ),
        "mixed_v1_v2_state_blocks_0020_downgrade": (
            ("mixed", "0020_wp10_publication_contract", True, "0019_manual_claims_binding"),
        ),
        "grant_lifecycle_states_0020_matrix": (
            ("active", "0020_wp10_publication_contract", True, "0019_manual_claims_binding"),
            ("superseded", "0020_wp10_publication_contract", False, "0019_manual_claims_binding"),
            ("withdrawn", "0020_wp10_publication_contract", False, "0019_manual_claims_binding"),
        ),
        "pending_publication_event_0021_roundtrip": (
            ("pending", "0021_wp10_publisher_projection", False, "0020_wp10_publication_contract"),
        ),
        "retry_publication_event_blocks_0021_downgrade": (
            ("retry", "0021_wp10_publisher_projection", True, "0020_wp10_publication_contract"),
        ),
        "terminal_publication_event_blocks_0021_downgrade": (
            ("terminal", "0021_wp10_publisher_projection", True, "0020_wp10_publication_contract"),
        ),
        "delivery_attempt_blocks_0021_downgrade": (
            ("attempt", "0021_wp10_publisher_projection", True, "0020_wp10_publication_contract"),
        ),
        "public_claim_projection_states_block_0022_downgrade": tuple(
            (kind, "0022_wp10_claim_search_projection", True, "0021_wp10_publisher_projection")
            for kind in (
                "claims",
                "evidence",
                "claim_evidence",
                "document_entities",
                "search_documents",
                "claim_identity",
                "evidence_identity",
                "rebuild_run",
            )
        ),
        "public_document_entity_control_and_head_roundtrip": (
            ("control", "0024_wp10_admin_replay", False, "0023_wp10_api_read_indexes"),
        ),
    }
    return tuple(
        (scenario_id, variant, head, blocked, parent)
        for variant, head, blocked, parent in empty[scenario_id]
    )


def _residue(admin_url: str) -> list[str]:
    with psycopg.connect(libpq_url(admin_url)) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE 'uap_g10_26_%' ORDER BY datname"
            )
            return [str(row[0]) for row in cursor.fetchall()]


def run(admin_url: str) -> dict[str, object]:
    checks: list[dict[str, object]] = []
    started = now()
    acl_roundtrip = run_acl_roundtrip(admin_url)
    failure: str | None = None
    if acl_roundtrip["status"] != "passed":
        failure = "A/B/C/D/E ACL roundtrip failed"
    for index, scenario_id in enumerate(SCENARIO_IDS):
        variants = scenario_variants(scenario_id)
        if failure is not None:
            checks.append({"scenario_id": scenario_id, "status": "not_run", "reason": failure})
            continue
        scenario_started = now()
        variant_results: list[dict[str, object]] = []
        try:
            for _scenario, variant, head, blocked, parent in variants:
                variant_result = _run_variant(
                    admin_url,
                    scenario_id,
                    variant,
                    head,
                    None if variant == "empty" else variant,
                    blocked,
                    parent,
                )
                variant_results.append(variant_result)
                if variant_result["status"] != "passed":
                    variant_checks = variant_result.get("checks")
                    changed_keys = (
                        variant_checks.get("changed_keys", [])
                        if isinstance(variant_checks, dict)
                        else []
                    )
                    raise RuntimeError(
                        "variant roundtrip failed: " + ",".join(str(key) for key in changed_keys)
                    )
            checks.append(
                {
                    "scenario_id": scenario_id,
                    "status": "passed",
                    "started_at": scenario_started,
                    "finished_at": now(),
                    "variants": variant_results,
                }
            )
        except Exception as error:
            failure = f"scenario {index + 1} failed: {type(error).__name__}"
            checks.append(
                {
                    "scenario_id": scenario_id,
                    "status": "failed",
                    "started_at": scenario_started,
                    "finished_at": now(),
                    "error": failure,
                    "error_detail": str(error),
                    "variants": variant_results,
                }
            )
    residue = _residue(admin_url)
    status = (
        "passed"
        if failure is None and not residue and all(item["status"] == "passed" for item in checks)
        else "failed"
    )
    return {
        "schema": SCHEMA,
        "status": status,
        "scenario_count": len(SCENARIO_IDS),
        "passed": sum(item["status"] == "passed" for item in checks),
        "failed": sum(item["status"] == "failed" for item in checks),
        "not_run": sum(item["status"] == "not_run" for item in checks),
        "started_at": started,
        "finished_at": now(),
        "checks": checks,
        "acl_roundtrip": acl_roundtrip,
        "temporary_databases_remaining": residue,
        "residue_count": len(residue),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-url", help="admin URL; prefer UAP_WP10_6_ADMIN_URL")
    parser.add_argument("--evidence-out", type=Path, required=True)
    args = parser.parse_args(argv)
    admin_url = args.admin_url or os.environ.get("UAP_WP10_6_ADMIN_URL")
    if not admin_url:
        raise SystemExit("UAP_WP10_6_ADMIN_URL is required")
    evidence = run(admin_url)
    args.evidence_out.parent.mkdir(parents=True, exist_ok=True)
    args.evidence_out.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
