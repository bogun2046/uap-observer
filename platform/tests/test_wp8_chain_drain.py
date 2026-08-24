"""G8-20: leftover WP8.1 resolve jobs must close through the knowledge lifecycle."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from psycopg.errors import InvalidParameterValue, SerializationFailure

from tools import wp8_3_runtime_probe as probe3
from tools import wp8_4_runtime_probe as probe4


def _claimed(analysis_id: uuid.UUID | None = None) -> tuple[Any, ...]:
    payload = {"analysis_result_id": str(analysis_id or uuid.uuid4())}
    return (uuid.uuid4(), uuid.uuid4(), "resolve_claims", payload, uuid.uuid4())


def test_close_claimed_resolve_job_uses_handler_success() -> None:
    conn = MagicMock()
    handler = MagicMock()
    handler.handle.return_value = "succeeded"
    claimed = _claimed()
    status = probe3._close_claimed_resolve_job(conn, claimed, handler=handler)
    assert status == "succeeded"
    handler.handle.assert_called_once()
    conn.cursor.assert_not_called()


def test_close_claimed_resolve_job_finishes_succeeded_when_claims_exist() -> None:
    conn = MagicMock()
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn.cursor.return_value = cursor
    cursor.fetchone.return_value = ("succeeded",)
    handler = MagicMock()
    handler.handle.side_effect = InvalidParameterValue("knowledge_payload_mismatch")
    claimed = _claimed()
    with patch.object(probe3, "_knowledge_counts", return_value=(3, 4)):
        status = probe3._close_claimed_resolve_job(conn, claimed, handler=handler)
    assert status == "succeeded"
    sql = str(cursor.execute.call_args.args[0])
    assert "ops.finish_knowledge_job" in sql
    assert "succeeded" in sql
    assert "terminal_failure" not in sql
    bound = cursor.execute.call_args.args[1]
    metrics = bound[3]
    payload = getattr(metrics, "obj", metrics)
    assert payload["materialized_candidates"] == 3
    assert payload["materialized_locators"] == 4


def test_close_claimed_resolve_job_mismatch_when_no_domain_rows() -> None:
    conn = MagicMock()
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn.cursor.return_value = cursor
    cursor.fetchone.return_value = ("dead",)
    handler = MagicMock()
    handler.handle.side_effect = InvalidParameterValue("knowledge_payload_mismatch")
    claimed = _claimed()
    with patch.object(probe3, "_knowledge_counts", return_value=(0, 0)):
        status = probe3._close_claimed_resolve_job(conn, claimed, handler=handler)
    assert status == "dead"
    sql = str(cursor.execute.call_args.args[0])
    assert "ops.finish_knowledge_job" in sql
    assert "terminal_failure" in sql
    assert "knowledge_payload_mismatch" in str(cursor.execute.call_args.args[1])


def test_close_claimed_resolve_job_reraises_serialization_failure() -> None:
    conn = MagicMock()
    handler = MagicMock()
    handler.handle.side_effect = SerializationFailure("40001")
    with pytest.raises(SerializationFailure):
        probe3._close_claimed_resolve_job(conn, _claimed(), handler=handler)
    conn.cursor.assert_not_called()


def test_close_claimed_entity_job_finishes_succeeded_when_candidates_exist() -> None:
    conn = MagicMock()
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn.cursor.return_value = cursor
    cursor.fetchone.return_value = ("succeeded",)
    handler = MagicMock()
    handler.handle.side_effect = InvalidParameterValue("knowledge_payload_mismatch")
    claimed: tuple[Any, ...] = (
        uuid.uuid4(),
        uuid.uuid4(),
        "resolve_entities",
        {"analysis_result_id": str(uuid.uuid4())},
        uuid.uuid4(),
    )
    with patch.object(probe4, "_knowledge_counts", return_value=(1, 20)):
        status = probe4._close_claimed_resolve_job(conn, claimed, handler=handler)
    assert status == "succeeded"
    sql = str(cursor.execute.call_args.args[0])
    assert "succeeded" in sql
    assert "terminal_failure" not in sql
