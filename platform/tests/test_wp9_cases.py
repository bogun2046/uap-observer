"""WP9.2 case client unit tests. No WP9.3 decision APIs."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from psycopg.errors import UniqueViolation

from uap_platform.review import (
    ReviewSessionError,
    assign_review_case,
    close_review_case,
    open_review_case,
)
from uap_platform.review.errors import (
    KNOWLEDGE_RELATION_REVIEW_NOT_IN_WP9,
    REVIEW_CASE_ALREADY_OPEN,
    REVIEW_IDEMPOTENCY_PAYLOAD_CONFLICT,
    REVIEW_REQUEST_ID_MISSING,
    map_review_error,
)


def test_open_review_case_calls_definer() -> None:
    connection = MagicMock()
    cursor = MagicMock()
    case_id = uuid.uuid4()
    cursor.fetchone.return_value = (case_id,)
    connection.cursor.return_value.__enter__.return_value = cursor
    subject = uuid.uuid4()
    assert open_review_case(connection, "claim", subject, 0, "open a claim") == case_id
    sql = cursor.execute.call_args.args[0]
    assert "audit.open_review_case" in sql
    assert "p_actor_id" not in sql


def test_assign_and_close_call_definers() -> None:
    connection = MagicMock()
    cursor = MagicMock()
    case_id = uuid.uuid4()
    cursor.fetchone.return_value = (case_id,)
    connection.cursor.return_value.__enter__.return_value = cursor
    assert assign_review_case(connection, case_id, uuid.uuid4()) == case_id
    assert close_review_case(connection, case_id, "close after decide") == case_id
    executed = [call.args[0] for call in cursor.execute.call_args_list]
    assert any("assign_review_case" in sql for sql in executed)
    assert any("close_review_case" in sql for sql in executed)


def test_open_maps_already_open() -> None:
    connection = MagicMock()
    cursor = MagicMock()
    error = UniqueViolation("review_case_already_open")
    error.sqlstate = "23505"
    cursor.execute.side_effect = error
    connection.cursor.return_value.__enter__.return_value = cursor
    with pytest.raises(ReviewSessionError) as raised:
        open_review_case(connection, "claim", uuid.uuid4(), 0, "open a claim")
    assert raised.value.code in {
        REVIEW_CASE_ALREADY_OPEN,
        REVIEW_IDEMPOTENCY_PAYLOAD_CONFLICT,
    }
    assert raised.value.sqlstate == "23505"


def test_map_relation_and_request_id_codes() -> None:
    error = MagicMock()
    error.sqlstate = "22023"
    error.diag = MagicMock()
    error.diag.message_primary = KNOWLEDGE_RELATION_REVIEW_NOT_IN_WP9
    assert map_review_error(error).code == KNOWLEDGE_RELATION_REVIEW_NOT_IN_WP9
    error.diag.message_primary = REVIEW_REQUEST_ID_MISSING
    error.sqlstate = "42501"
    assert map_review_error(error).code == REVIEW_REQUEST_ID_MISSING
    error.diag.message_primary = REVIEW_IDEMPOTENCY_PAYLOAD_CONFLICT
    error.sqlstate = "23505"
    assert map_review_error(error).code == REVIEW_IDEMPOTENCY_PAYLOAD_CONFLICT
    error.diag.message_primary = REVIEW_CASE_ALREADY_OPEN
    assert map_review_error(error).code == REVIEW_CASE_ALREADY_OPEN


def test_wp9_2_package_has_no_decision_functions() -> None:
    root = Path(__file__).resolve().parents[1] / "src/uap_platform/review"
    text = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    for token in (
        "record_review_decision",
        "select_analysis_result",
        "accept_entity_candidate",
        "apply_entity_merge",
        "create_manual_claim",
    ):
        assert token not in text
