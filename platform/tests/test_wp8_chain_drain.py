"""G8-20: leftover WP8.1 resolve jobs close only through the production handler."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from psycopg.errors import (
    ForeignKeyViolation,
    InsufficientPrivilege,
    InvalidParameterValue,
    SerializationFailure,
)

from tools import wp8_3_runtime_probe as probe3
from tools import wp8_4_runtime_probe as probe4

_PLATFORM = Path(__file__).resolve().parents[1]


def _claimed(
    analysis_id: uuid.UUID | None = None, job_type: str = "resolve_claims"
) -> tuple[Any, ...]:
    payload = {
        "analysis_result_id": str(analysis_id or uuid.uuid4()),
        "claim_fingerprint": "b" * 64,
        "entity_type": "object",
        "locator_ordinal": 0,
    }
    return (uuid.uuid4(), uuid.uuid4(), job_type, payload, uuid.uuid4())


def _assert_handler_only(
    probe: Any, claimed: tuple[Any, ...], handler: MagicMock, expected: str = "succeeded"
) -> None:
    conn = MagicMock()
    handler.handle.return_value = expected
    status = probe._close_claimed_resolve_job(conn, claimed, handler=handler)
    assert status == expected
    handler.handle.assert_called_once()
    conn.cursor.assert_not_called()


def test_close_claimed_resolve_job_uses_handler_success() -> None:
    _assert_handler_only(probe3, _claimed(), MagicMock())


def test_close_claimed_job_nosuchkey_with_rows_does_not_succeed() -> None:
    conn = MagicMock()
    handler = MagicMock()
    error = InvalidParameterValue("knowledge_payload_mismatch")
    error.sqlstate = "22023"
    handler.handle.side_effect = error
    with pytest.raises(InvalidParameterValue):
        probe3._close_claimed_resolve_job(conn, _claimed(), handler=handler)
    conn.rollback.assert_called()
    handler.handle.assert_called_once()


def test_close_claimed_job_python_nosuchkey_does_not_succeed() -> None:
    conn = MagicMock()
    handler = MagicMock()
    handler.handle.side_effect = RuntimeError("NoSuchKey")
    with pytest.raises(RuntimeError, match="NoSuchKey"):
        probe3._close_claimed_resolve_job(conn, _claimed(), handler=handler)
    handler.handle.assert_called_once()
    conn.cursor.assert_not_called()


def test_close_claimed_job_42501_with_rows_does_not_succeed() -> None:
    conn = MagicMock()
    handler = MagicMock()
    error = InsufficientPrivilege("permission denied")
    error.sqlstate = "42501"
    handler.handle.side_effect = error
    with pytest.raises(InsufficientPrivilege):
        probe3._close_claimed_resolve_job(conn, _claimed(), handler=handler)
    conn.rollback.assert_called()


def test_close_claimed_job_23503_with_rows_does_not_succeed() -> None:
    conn = MagicMock()
    handler = MagicMock()
    error = ForeignKeyViolation("foreign key")
    error.sqlstate = "23503"
    handler.handle.side_effect = error
    with pytest.raises(ForeignKeyViolation):
        probe3._close_claimed_resolve_job(conn, _claimed(), handler=handler)
    conn.rollback.assert_called()


def test_close_claimed_resolve_job_reraises_serialization_failure() -> None:
    conn = MagicMock()
    handler = MagicMock()
    handler.handle.side_effect = SerializationFailure("40001")
    with pytest.raises(SerializationFailure):
        probe3._close_claimed_resolve_job(conn, _claimed(), handler=handler)


def test_wrong_fingerprint_does_not_proven_succeed() -> None:
    claimed = _claimed()
    claimed[3]["claim_fingerprint"] = "c" * 64
    _assert_handler_only(probe3, claimed, MagicMock())


def test_wrong_entity_type_does_not_proven_succeed() -> None:
    claimed = _claimed(job_type="resolve_entities")
    claimed[3]["entity_type"] = "person"
    _assert_handler_only(probe4, claimed, MagicMock())


def test_wrong_locator_span_same_count_does_not_proven_succeed() -> None:
    claimed = _claimed()
    claimed[3]["evidence"] = [{"locator_type": "html", "start": 9, "end": 12}]
    _assert_handler_only(probe3, claimed, MagicMock())


def test_wrong_locator_ordinal_does_not_proven_succeed() -> None:
    claimed = _claimed()
    claimed[3]["locator_ordinal"] = 7
    _assert_handler_only(probe3, claimed, MagicMock())


def test_close_claimed_entity_job_nosuchkey_with_rows_does_not_succeed() -> None:
    conn = MagicMock()
    handler = MagicMock()
    error = InvalidParameterValue("knowledge_payload_mismatch")
    error.sqlstate = "22023"
    handler.handle.side_effect = error
    claimed = _claimed(job_type="resolve_entities")
    with pytest.raises(InvalidParameterValue):
        probe4._close_claimed_resolve_job(conn, claimed, handler=handler)
    handler.handle.assert_called_once()


def test_close_claimed_entity_job_python_nosuchkey_does_not_succeed() -> None:
    conn = MagicMock()
    handler = MagicMock()
    handler.handle.side_effect = RuntimeError("NoSuchKey")
    claimed = _claimed(job_type="resolve_entities")
    with pytest.raises(RuntimeError, match="NoSuchKey"):
        probe4._close_claimed_resolve_job(conn, claimed, handler=handler)


def test_close_claimed_entity_job_42501_with_rows_does_not_succeed() -> None:
    conn = MagicMock()
    handler = MagicMock()
    error = InsufficientPrivilege("permission denied")
    error.sqlstate = "42501"
    handler.handle.side_effect = error
    claimed = _claimed(job_type="resolve_entities")
    with pytest.raises(InsufficientPrivilege):
        probe4._close_claimed_resolve_job(conn, claimed, handler=handler)


def test_close_claimed_entity_job_23503_with_rows_does_not_succeed() -> None:
    conn = MagicMock()
    handler = MagicMock()
    error = ForeignKeyViolation("foreign key")
    error.sqlstate = "23503"
    handler.handle.side_effect = error
    claimed = _claimed(job_type="resolve_entities")
    with pytest.raises(ForeignKeyViolation):
        probe4._close_claimed_resolve_job(conn, claimed, handler=handler)


def test_no_pre_handler_succeeded_bypass_in_probes() -> None:
    for name in ("wp8_3_runtime_probe.py", "wp8_4_runtime_probe.py"):
        source = (_PLATFORM / "tools" / name).read_text(encoding="utf-8")
        assert "def _proven_complete_materialization" not in source
        assert "def _finish_existing_materialization" not in source
        assert "There is no pre-handler succeeded bypass." in source
        close = source[
            source.find("def _close_claimed_resolve_job") : source.find("def _claim_target_job")
        ]
        assert "active.handle(" in close
        assert "'succeeded'::ops.attempt_outcome" not in close
        assert "SELECT ops.finish_knowledge_job" not in close
