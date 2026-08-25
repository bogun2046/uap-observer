"""WP9.1 review session unit tests: GUC client, error map, no later-stage APIs."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from psycopg.errors import InsufficientPrivilege, InvalidAuthorizationSpecification

from uap_platform.review import (
    ReviewSessionError,
    bind_review_session,
    map_review_error,
    require_active_role,
)
from uap_platform.review.errors import (
    FROZEN_REVIEW_CODES,
    REVIEW_PRINCIPAL_MISSING,
    REVIEW_ROLE_DENIED,
    REVIEW_SERVICE_PRINCIPAL_DENIED,
    REVIEW_SESSION_ROLE_DENIED,
)


def test_bind_review_session_uses_set_local_guc() -> None:
    connection = MagicMock()
    cursor = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    principal = uuid.uuid4()
    request = uuid.uuid4()
    bind_review_session(connection, principal, request)
    executed = [call.args[0] for call in cursor.execute.call_args_list]
    assert executed[0] == "SELECT set_config('uap.principal_id', %s, true)"
    assert executed[1] == "SELECT set_config('uap.request_id', %s, true)"
    assert cursor.execute.call_args_list[0].args[1] == (str(principal),)
    assert "p_actor_id" not in "".join(executed)
    assert "SET uap.principal_id" not in "".join(executed)


def test_bind_review_session_omits_request_id_when_absent() -> None:
    connection = MagicMock()
    cursor = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    bind_review_session(connection, uuid.uuid4())
    executed = [call.args[0] for call in cursor.execute.call_args_list]
    assert len(executed) == 1
    assert "uap.request_id" not in executed[0]


def test_require_active_role_returns_principal() -> None:
    principal = uuid.uuid4()
    connection = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = (principal,)
    connection.cursor.return_value.__enter__.return_value = cursor
    assert require_active_role(connection, "reviewer") == principal
    sql = cursor.execute.call_args.args[0]
    assert "audit.require_active_role" in sql
    assert cursor.execute.call_args.args[1] == ("reviewer",)


def test_require_active_role_maps_frozen_codes() -> None:
    connection = MagicMock()
    cursor = MagicMock()
    cursor.execute.side_effect = InsufficientPrivilege("review_principal_missing")
    connection.cursor.return_value.__enter__.return_value = cursor
    with pytest.raises(ReviewSessionError) as raised:
        require_active_role(connection, "reviewer")
    assert raised.value.sqlstate in {"42501", ""}
    assert raised.value.code in {
        REVIEW_PRINCIPAL_MISSING,
        REVIEW_SESSION_ROLE_DENIED,
    }


def test_require_active_role_empty_row() -> None:
    connection = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    connection.cursor.return_value.__enter__.return_value = cursor
    with pytest.raises(ReviewSessionError) as raised:
        require_active_role(connection, "reviewer")
    assert raised.value.code == REVIEW_PRINCIPAL_MISSING


def test_map_review_error_privilege_without_token() -> None:
    error = MagicMock(spec=InvalidAuthorizationSpecification)
    error.sqlstate = "42501"
    error.diag = MagicMock()
    error.diag.message_primary = "permission denied for function require_active_role"
    mapped = map_review_error(error)
    assert mapped.code == REVIEW_SESSION_ROLE_DENIED
    assert mapped.sqlstate == "42501"


def test_map_review_error_frozen_primary() -> None:
    error = MagicMock()
    error.sqlstate = ""
    error.diag = MagicMock()
    error.diag.message_primary = REVIEW_ROLE_DENIED
    mapped = map_review_error(error)
    assert mapped.code == REVIEW_ROLE_DENIED
    assert mapped.sqlstate == "42501"


def test_map_review_error_unclassified() -> None:
    error = MagicMock()
    error.sqlstate = "XX000"
    error.diag = MagicMock()
    error.diag.message_primary = "boom"
    mapped = map_review_error(error)
    assert mapped.code == "review_unclassified"
    assert mapped.sqlstate == "XX000"


def test_frozen_codes_cover_g9_01_to_05() -> None:
    assert REVIEW_PRINCIPAL_MISSING in FROZEN_REVIEW_CODES
    assert REVIEW_SERVICE_PRINCIPAL_DENIED in FROZEN_REVIEW_CODES
    assert REVIEW_SESSION_ROLE_DENIED in FROZEN_REVIEW_CODES
    assert REVIEW_ROLE_DENIED in FROZEN_REVIEW_CODES


def test_wp9_1_package_has_no_later_stage_functions() -> None:
    root = Path(__file__).resolve().parents[1] / "src/uap_platform/review"
    text = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    for token in (
        "open_review_case",
        "assign_review_case",
        "close_review_case",
        "record_review_decision",
        "select_analysis_result",
        "accept_entity_candidate",
        "apply_entity_merge",
        "create_manual_claim",
    ):
        assert token not in text
