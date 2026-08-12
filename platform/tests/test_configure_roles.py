from __future__ import annotations

import pytest

from tools.configure_roles import ROLE_PASSWORDS, required_passwords


def test_required_passwords_maps_every_role() -> None:
    environ = {variable: f"secret-for-{role}" for role, variable in ROLE_PASSWORDS.items()}

    assert required_passwords(environ) == {
        role: f"secret-for-{role}" for role in ROLE_PASSWORDS
    }


def test_required_passwords_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="UAP_BACKUP_PASSWORD"):
        required_passwords({})
