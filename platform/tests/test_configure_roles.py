from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from tools import configure_roles
from tools.configure_roles import ROLE_PASSWORDS, required_passwords


def test_alembic_console_loads_membership_helpers_without_pythonpath(tmp_path: Path) -> None:
    platform = Path(__file__).resolve().parents[1]
    environ = dict(os.environ)
    environ.pop("PYTHONPATH", None)
    environ.update({
        "UAP_DATABASE_URL": "postgresql+psycopg://invalid:invalid@localhost/invalid",
        "UAP_S3_ENDPOINT": "127.0.0.1:8333",
        "UAP_S3_ACCESS_KEY": "offline-test",
        "UAP_S3_SECRET_KEY": "offline-test",
    })
    # Only the current interpreter's console script and this repository's config are executed.
    completed = subprocess.run(  # noqa: S603
        [sys.executable, str(Path(sys.executable).with_name("alembic")),
         "-c", str(platform / "alembic.ini"),
         "upgrade", "0001_roles_and_schemas", "--sql"],
        cwd=tmp_path, env=environ, text=True, capture_output=True, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "ALTER ROLE uap_migrator NOINHERIT" in completed.stdout


def test_alembic_import_paths_preserve_spaces(tmp_path: Path) -> None:
    from alembic.config import Config

    platform = Path(__file__).resolve().parents[1]
    copied = tmp_path / "platform with spaces"
    copied.mkdir()
    ini = copied / "alembic.ini"
    ini.write_text((platform / "alembic.ini").read_text())
    assert Config(str(ini)).get_prepend_sys_paths_list() == [
        str(copied / "src"), str(copied),
    ]


def test_required_passwords_maps_every_role() -> None:
    environ = {variable: f"secret-for-{role}" for role, variable in ROLE_PASSWORDS.items()}

    assert required_passwords(environ) == {role: f"secret-for-{role}" for role in ROLE_PASSWORDS}


def test_required_passwords_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="UAP_BACKUP_PASSWORD"):
        required_passwords({})


def test_database_bootstrap_probe_is_exposed_as_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(configure_roles, "database_bootstrapped", lambda: False)

    with pytest.raises(SystemExit) as error:
        configure_roles.main(["database-bootstrapped"])

    assert error.value.code == 3


def test_migrator_lifecycle_cli_dispatches_both_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states: list[bool] = []
    monkeypatch.setattr(configure_roles, "set_migrator_login", states.append)

    configure_roles.main(["enable-migrator"])
    configure_roles.main(["disable-migrator"])

    assert states == [True, False]


@pytest.mark.parametrize(
    "row",
    [None, (), (True, False, True, False), (False, True, True, False),
     (False, False, False, False), (False, False, True, True)],
)
def test_membership_verification_fails_closed(row: tuple[bool, ...] | None) -> None:
    with pytest.raises(RuntimeError, match="owner membership is not hardened"):
        configure_roles.verify_migrator_membership(row)


def test_membership_verification_accepts_explicit_elevation() -> None:
    configure_roles.verify_migrator_membership((False, False, True, False))


def test_membership_reconciliation_is_explicit_and_repeatable() -> None:
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (False, False, True, False)

    for _ in range(2):
        configure_roles.configure_migrator_membership(connection)

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    expected = [
        "ALTER ROLE uap_migrator NOINHERIT",
        "GRANT uap_owner TO uap_migrator WITH INHERIT FALSE, SET TRUE, ADMIN FALSE",
        configure_roles.MIGRATOR_MEMBERSHIP_QUERY,
    ]
    assert statements == expected * 2


def test_membership_reconciliation_verifies_result() -> None:
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value.fetchone.return_value = None
    with pytest.raises(RuntimeError, match="owner membership is not hardened"):
        configure_roles.configure_migrator_membership(connection)


def test_configure_always_reconciles_before_passwords(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value.fetchone.side_effect = [
        (False, False, True, False), ("alembic_version",), ("uap_owner",),
        ("uap_owner", True, True, True, True, False, False, False, True),
    ]
    connect = MagicMock()
    connect.return_value.__enter__.return_value = connection
    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(configure_roles, "Settings", MagicMock())
    for variable in ROLE_PASSWORDS.values():
        monkeypatch.setenv(variable, "test-credential")

    configure_roles.configure()
    statements = connection.cursor.return_value.__enter__.return_value.execute.call_args_list
    assert statements[0].args[0] == "ALTER ROLE uap_migrator NOINHERIT"
    assert len(statements) == 9 + len(ROLE_PASSWORDS)


def test_migrator_preflight_runs_before_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    import runpy

    import sqlalchemy
    from alembic.script import ScriptDirectory

    from alembic import context

    context_mock = MagicMock()
    context_mock.config.config_file_name = None
    context_mock.config.get_section.return_value = {}
    context_mock.config.config_ini_section = "alembic"
    context_mock.get_x_argument.return_value = {"role": "migrator"}
    context_mock.is_offline_mode.return_value = False
    context_mock.get_context.return_value.get_current_heads.return_value = ()
    context_mock.get_revision_argument.return_value = "head"
    scripts = MagicMock()
    scripts.get_revision.return_value.revision = "0022_wp10_claim_search_projection"
    monkeypatch.setattr(ScriptDirectory, "from_config", lambda config: scripts)
    for name in (
        "config", "get_x_argument", "is_offline_mode", "configure",
        "begin_transaction", "run_migrations", "get_context", "get_revision_argument",
    ):
        monkeypatch.setattr(context, name, getattr(context_mock, name), raising=False)
    monkeypatch.setattr(configure_roles, "verify_migrator_membership",
                        configure_roles.verify_migrator_membership)
    monkeypatch.setattr("uap_platform.config.Settings", MagicMock())
    monkeypatch.setenv("UAP_MIGRATOR_PASSWORD", "test-credential")
    # Supply a valid URL without touching a real database.
    settings = MagicMock()
    settings.database_url.get_secret_value.return_value = "postgresql+psycopg://admin@db/uap"
    monkeypatch.setattr("uap_platform.config.Settings", lambda: settings)
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    monkeypatch.setattr(sqlalchemy, "engine_from_config", lambda *args, **kwargs: engine)
    path = Path(__file__).resolve().parents[1] / "alembic/env.py"

    for row in (None, (False, True, True, False), (False, False, False, False)):
        connection.execute.return_value.first.return_value = row
        with pytest.raises(RuntimeError, match="owner membership is not hardened"):
            runpy.run_path(str(path))
        context_mock.run_migrations.assert_not_called()

    for version_access in (None, ("admin", True, True, True, True, False, False, False, True),
                           ("uap_owner", True, True, True, True, True, False, False, True)):
        connection.execute.return_value.first.side_effect = [
            (False, False, True, False), version_access,
        ]
        with pytest.raises(RuntimeError, match="version-table privilege contract"):
            runpy.run_path(str(path))
        context_mock.run_migrations.assert_not_called()

    assert all(
        call.args[0] == "RESET ROLE" for call in connection.exec_driver_sql.call_args_list
    )
    for starting_revision, expected_arms in (
        ("0020_wp10_publication_contract", 1),
        ("0021_wp10_publisher_projection", 1),
        ("0022_wp10_claim_search_projection", 0),
        ("0036_v133_full_rebuild_publication_evidence_guard", 0),
    ):
        connection.exec_driver_sql.reset_mock()
        context_mock.run_migrations.reset_mock()
        context_mock.get_context.return_value.get_current_heads.return_value = (starting_revision,)
        connection.execute.return_value.first.side_effect = [
            (False, False, True, False), VERSION_ACCESS,
        ]

        def simulate_revision_callback(revision: str = starting_revision) -> None:
            if revision == "0020_wp10_publication_contract":
                callback = context_mock.configure.call_args.kwargs["on_version_apply"]
                step = MagicMock()
                step.is_upgrade = True
                ctx = MagicMock()
                ctx.bind = connection
                callback(ctx=ctx, step=step, heads={"0021_wp10_publisher_projection"}, run_args={})

        context_mock.run_migrations.side_effect = simulate_revision_callback
        namespace = runpy.run_path(str(path))
        context_mock.run_migrations.assert_called_once()
        role_statements = [call.args[0] for call in connection.exec_driver_sql.call_args_list]
        assert role_statements == ["SET ROLE uap_owner"] * expected_arms + ["RESET ROLE"]
        assert "pg_auth_members" in str(connection.execute.call_args_list[-2].args[0])
        assert "has_table_privilege" in str(connection.execute.call_args.args[0])

    # A downgrade arriving at 0021 must not arm owner entry into 0022.
    connection.exec_driver_sql.reset_mock()
    step = MagicMock()
    step.is_upgrade = False
    ctx = MagicMock()
    ctx.bind = connection
    namespace["_on_version_apply"](
        ctx=ctx, step=step, heads={"0021_wp10_publisher_projection"}, run_args={}
    )
    connection.exec_driver_sql.assert_not_called()

    # Migration errors are preserved and rollback precedes RESET ROLE.
    connection.reset_mock()
    connection.execute.return_value.first.side_effect = [
        (False, False, True, False), VERSION_ACCESS,
    ]
    connection.in_transaction.return_value = True
    context_mock.get_context.return_value.get_current_heads.return_value = (
        "0021_wp10_publisher_projection",
    )
    context_mock.run_migrations.side_effect = RuntimeError("injected migration failure")
    with pytest.raises(RuntimeError, match="injected migration failure"):
        runpy.run_path(str(path))
    calls = [call[0] for call in connection.mock_calls]
    role_calls = [call.args[0] for call in connection.exec_driver_sql.call_args_list]
    assert role_calls == ["SET ROLE uap_owner", "RESET ROLE"]
    assert calls.index("rollback") < len(calls) - 1 - calls[::-1].index("exec_driver_sql")


VERSION_ACCESS = ("uap_owner", True, True, True, True, False, False, False, True)


@pytest.mark.parametrize("index", range(9))
def test_version_access_verification_rejects_each_contract_violation(index: int) -> None:
    row: list[object] = list(VERSION_ACCESS)
    row[index] = "unexpected_owner" if index == 0 else not row[index]
    with pytest.raises(RuntimeError, match="version-table privilege contract is not hardened"):
        configure_roles.verify_alembic_version_access(row)


@pytest.mark.parametrize("row", [None, (), (None,)])
def test_version_access_verification_rejects_missing_table(row: tuple[object, ...] | None) -> None:
    with pytest.raises(RuntimeError, match="version-table privilege contract is not hardened"):
        configure_roles.verify_alembic_version_access(row)


def test_version_access_reconciliation_grants_only_bookkeeping_dml() -> None:
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = [("alembic_version",), ("uap_owner",), VERSION_ACCESS] * 2
    for _ in range(2):
        configure_roles.configure_alembic_version_access(connection)
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert statements[:6] == statements[6:]
    assert statements[2:5] == [
        "GRANT USAGE ON SCHEMA public TO uap_migrator",
        "REVOKE ALL PRIVILEGES ON TABLE public.alembic_version FROM uap_migrator",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.alembic_version TO uap_migrator",
    ]


@pytest.mark.parametrize("rows", [[None], [(None,)], [("alembic_version",), ("admin",)]])
def test_version_access_reconciliation_fails_before_grants(rows: list[object]) -> None:
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = rows
    with pytest.raises(RuntimeError, match=r"public\.alembic_version"):
        configure_roles.configure_alembic_version_access(connection)
    assert all(call.args[0].startswith("SELECT") for call in cursor.execute.call_args_list)


@pytest.fixture
def membership_test_url(monkeypatch: pytest.MonkeyPatch) -> str:
    """Real PostgreSQL tests require an explicitly named disposable database."""

    url = os.environ.get("UAP_MEMBERSHIP_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires disposable PostgreSQL membership test database")
    database_name = conninfo_to_dict(url).get("dbname")
    if not isinstance(database_name, str) or not database_name.startswith("membership_"):
        pytest.fail("membership integration tests require a membership_ disposable database")
    monkeypatch.setenv("UAP_DATABASE_URL", url.replace("postgresql://", "postgresql+psycopg://", 1))
    return url


def test_postgres_existing_repair_and_idempotency(membership_test_url: str) -> None:
    with psycopg.connect(membership_test_url) as connection:
        connection.execute(
            "GRANT uap_owner TO uap_migrator WITH INHERIT TRUE, SET TRUE, ADMIN FALSE"
        )
        connection.execute(
            "REVOKE ALL PRIVILEGES ON TABLE public.alembic_version FROM uap_migrator"
        )
    configure_roles.configure()
    with psycopg.connect(membership_test_url) as connection:
        before = connection.execute(configure_roles.ALEMBIC_VERSION_ACCESS_QUERY).fetchone()
        assert before == VERSION_ACCESS
        connection.execute("GRANT TRUNCATE ON TABLE public.alembic_version TO uap_migrator")
    configure_roles.configure()
    configure_roles.configure()
    with psycopg.connect(membership_test_url) as connection:
        assert connection.execute(configure_roles.ALEMBIC_VERSION_ACCESS_QUERY).fetchone() == before
        assert connection.execute(configure_roles.MIGRATOR_MEMBERSHIP_QUERY).fetchone() == (
            False, False, True, False
        )


def test_postgres_wrong_owner_fails_closed(membership_test_url: str) -> None:
    with psycopg.connect(membership_test_url) as connection:
        connection.execute("ALTER TABLE public.alembic_version OWNER TO CURRENT_USER")
        unexpected_owner = connection.execute("SELECT current_user").fetchone()
    try:
        with pytest.raises(RuntimeError, match="owner must be uap_owner"):
            configure_roles.configure()
        with psycopg.connect(membership_test_url) as connection:
            assert connection.execute(
                "SELECT pg_get_userbyid(relowner) FROM pg_class "
                "WHERE oid = 'public.alembic_version'::regclass"
            ).fetchone() == unexpected_owner
    finally:
        with psycopg.connect(membership_test_url) as connection:
            connection.execute("ALTER TABLE public.alembic_version OWNER TO uap_owner")
        configure_roles.configure()


def test_postgres_migrator_privileges_and_upgrade(membership_test_url: str) -> None:
    configure_roles.configure()
    configure_roles.set_migrator_login(True)
    try:
        url = make_conninfo(
            membership_test_url, user="uap_migrator", password=os.environ["UAP_MIGRATOR_PASSWORD"]
        )
        with psycopg.connect(url) as connection:
            assert connection.execute("SELECT session_user, current_user").fetchone() == (
                "uap_migrator", "uap_migrator"
            )
            assert connection.execute(configure_roles.ALEMBIC_VERSION_ACCESS_QUERY).fetchone() == (
                VERSION_ACCESS
            )
            for table in ("alembic_version", "search_documents"):
                assert connection.execute(
                    "SELECT has_table_privilege(current_user, %s, 'TRUNCATE')", (f"public.{table}",)
                ).fetchone() == (False,)
                with pytest.raises(psycopg.errors.InsufficientPrivilege), connection.transaction():
                    connection.execute(
                        psycopg.sql.SQL("TRUNCATE public.{}").format(psycopg.sql.Identifier(table))
                    )
            connection.execute("SET ROLE uap_owner")
            assert connection.execute("SELECT current_user").fetchone() == ("uap_owner",)
            assert connection.execute(
                "SELECT has_table_privilege(current_user, 'public.search_documents', 'TRUNCATE')"
            ).fetchone() == (True,)
            connection.execute("RESET ROLE")
            assert connection.execute("SELECT current_user").fetchone() == ("uap_migrator",)
        completed = subprocess.run(
            [sys.executable, "-m", "alembic", "-x", "role=migrator", "upgrade", "head"],
            cwd=Path(__file__).resolve().parents[1],
            check=False, capture_output=True, text=True, timeout=120,
        )
        assert completed.returncode == 0, completed.stderr
        with psycopg.connect(url) as connection:
            assert connection.execute("SELECT session_user, current_user").fetchone() == (
                "uap_migrator", "uap_migrator"
            )
    finally:
        configure_roles.set_migrator_login(False)
