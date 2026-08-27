from __future__ import annotations

import pytest

from tools import configure_roles
from tools.configure_roles import ROLE_PASSWORDS, required_passwords


def test_required_passwords_maps_every_role() -> None:
    environ = {variable: f"secret-for-{role}" for role, variable in ROLE_PASSWORDS.items()}

    assert required_passwords(environ) == {
        role: f"secret-for-{role}" for role in ROLE_PASSWORDS
    }


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
