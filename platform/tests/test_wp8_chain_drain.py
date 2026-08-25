"""G8-20: leftover WP8.1 resolve jobs must close through the knowledge lifecycle."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from psycopg.errors import (
    ForeignKeyViolation,
    InsufficientPrivilege,
    InvalidParameterValue,
    SerializationFailure,
)

from tools import wp8_3_runtime_probe as probe3
from tools import wp8_4_runtime_probe as probe4


def _claimed(
    analysis_id: uuid.UUID | None = None, job_type: str = "resolve_claims"
) -> tuple[Any, ...]:
    payload = {"analysis_result_id": str(analysis_id or uuid.uuid4())}
    return (uuid.uuid4(), uuid.uuid4(), job_type, payload, uuid.uuid4())


def _metrics() -> dict[str, Any]:
    return {
        "schema_version": "knowledge-attempt-metrics.v1",
        "input_candidates": 1,
        "materialized_candidates": 1,
        "input_locators": 2,
        "materialized_locators": 2,
        "rejected_candidates": 0,
        "rejected_locators": 0,
        "empty_valid_result": False,
        "rejected_by_code": {},
        "samples": [],
    }


def _bound_payload(
    *,
    analysis_id: uuid.UUID | None = None,
    result_type: str = "claim_extraction",
) -> dict[str, str]:
    analysis_id = analysis_id or uuid.uuid4()
    digest = "a" * 64
    return {
        "payload_schema_version": "knowledge.v2",
        "analysis_result_id": str(analysis_id),
        "analysis_result_sha256": digest,
        "analysis_schema_version": "ai.v1",
        "document_version_id": str(uuid.uuid4()),
        "result_type": result_type,
        "model_run_id": str(uuid.uuid4()),
        "input_sha256": digest,
        "extraction_anchor_status": "matched",
        "extraction_id": str(uuid.uuid4()),
    }


def _cursor_conn(*fetchones: object, rows: list[tuple[Any, ...]] | None = None) -> MagicMock:
    conn = MagicMock()
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn.cursor.return_value = cursor
    cursor.fetchone.side_effect = list(fetchones)
    cursor.fetchall.return_value = rows or []
    return conn


def test_close_claimed_resolve_job_uses_handler_success() -> None:
    conn = MagicMock()
    handler = MagicMock()
    handler.handle.return_value = "succeeded"
    claimed = _claimed()
    with patch.object(probe3, "_proven_complete_materialization", return_value=None):
        status = probe3._close_claimed_resolve_job(conn, claimed, handler=handler)
    assert status == "succeeded"
    handler.handle.assert_called_once()
    conn.cursor.assert_not_called()


def test_close_claimed_job_proven_replay_finishes_succeeded_without_handler() -> None:
    conn = MagicMock()
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn.cursor.return_value = cursor
    cursor.fetchone.return_value = ("succeeded",)
    handler = MagicMock()
    claimed = _claimed()
    metrics = _metrics()
    with patch.object(probe3, "_proven_complete_materialization", return_value=metrics):
        status = probe3._close_claimed_resolve_job(conn, claimed, handler=handler)
    assert status == "succeeded"
    handler.handle.assert_not_called()
    sql = str(cursor.execute.call_args.args[0])
    assert "ops.finish_knowledge_job" in sql
    assert "succeeded" in sql
    assert "terminal_failure" not in sql
    bound = cursor.execute.call_args.args[1]
    payload = getattr(bound[3], "obj", bound[3])
    assert payload["materialized_candidates"] == 1
    assert payload["materialized_locators"] == 2


def test_close_claimed_job_nosuchkey_with_rows_does_not_succeed() -> None:
    conn = MagicMock()
    handler = MagicMock()
    error = InvalidParameterValue("knowledge_payload_mismatch")
    error.sqlstate = "22023"
    handler.handle.side_effect = error
    with patch.object(probe3, "_proven_complete_materialization", return_value=None):
        with pytest.raises(InvalidParameterValue):
            probe3._close_claimed_resolve_job(conn, _claimed(), handler=handler)
    conn.rollback.assert_called()
    handler.handle.assert_called_once()


def test_close_claimed_job_python_nosuchkey_does_not_succeed() -> None:
    conn = MagicMock()
    handler = MagicMock()
    handler.handle.side_effect = RuntimeError("NoSuchKey")
    with patch.object(probe3, "_proven_complete_materialization", return_value=None):
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
    with patch.object(probe3, "_proven_complete_materialization", return_value=None):
        with pytest.raises(InsufficientPrivilege):
            probe3._close_claimed_resolve_job(conn, _claimed(), handler=handler)
    conn.rollback.assert_called()


def test_close_claimed_job_23503_with_rows_does_not_succeed() -> None:
    conn = MagicMock()
    handler = MagicMock()
    error = ForeignKeyViolation("foreign key")
    error.sqlstate = "23503"
    handler.handle.side_effect = error
    with patch.object(probe3, "_proven_complete_materialization", return_value=None):
        with pytest.raises(ForeignKeyViolation):
            probe3._close_claimed_resolve_job(conn, _claimed(), handler=handler)
    conn.rollback.assert_called()


def test_close_claimed_resolve_job_reraises_serialization_failure() -> None:
    conn = MagicMock()
    handler = MagicMock()
    handler.handle.side_effect = SerializationFailure("40001")
    with patch.object(probe3, "_proven_complete_materialization", return_value=None):
        with pytest.raises(SerializationFailure):
            probe3._close_claimed_resolve_job(conn, _claimed(), handler=handler)


def test_payload_binding_rejects_incomplete_payload() -> None:
    payload = {"analysis_result_id": str(uuid.uuid4())}
    assert probe3._payload_binding(payload, "claim_extraction") is None
    assert (
        probe3._proven_complete_materialization(
            MagicMock(), {"claims": [{"text": "x"}]}, job_type="resolve_claims"
        )
        is None
    )


def test_proven_complete_rejects_existing_rows_with_text_mismatch() -> None:
    payload = _bound_payload()
    analysis = {"claims": [{"claim": "hello", "evidence": [{"locator_type": "text"}]}]}
    conn = _cursor_conn((analysis,), (1,), rows=[(0, "supported", "b" * 64, 1)])
    assert (
        probe3._proven_complete_materialization(conn, payload, job_type="resolve_claims") is None
    )


def test_proven_complete_rejects_wp81_text_key_shape() -> None:
    payload = _bound_payload()
    analysis = {"claims": [{"text": "x", "evidence": [{"locator_type": "text"}]}]}
    conn = _cursor_conn((analysis,), (1,), rows=[(0, "x", "b" * 64, 1)])
    assert (
        probe3._proven_complete_materialization(conn, payload, job_type="resolve_claims") is None
    )


def test_proven_complete_accepts_matching_claim_rows() -> None:
    payload = _bound_payload()
    analysis = {
        "claims": [
            {"claim": "hello", "evidence": [{"locator_type": "text"}, {"locator_type": "text"}]}
        ]
    }
    conn = _cursor_conn((analysis,), (1,), rows=[(0, "hello", "b" * 64, 2)])
    metrics = probe3._proven_complete_materialization(
        conn, payload, job_type="resolve_claims"
    )
    assert metrics is not None
    assert metrics["materialized_candidates"] == 1
    assert metrics["materialized_locators"] == 2
    assert metrics["input_candidates"] == 1
    assert metrics["empty_valid_result"] is False


def test_close_claimed_entity_job_nosuchkey_with_rows_does_not_succeed() -> None:
    conn = MagicMock()
    handler = MagicMock()
    error = InvalidParameterValue("knowledge_payload_mismatch")
    error.sqlstate = "22023"
    handler.handle.side_effect = error
    claimed = _claimed(job_type="resolve_entities")
    with patch.object(probe4, "_proven_complete_materialization", return_value=None):
        with pytest.raises(InvalidParameterValue):
            probe4._close_claimed_resolve_job(conn, claimed, handler=handler)
    handler.handle.assert_called_once()


def test_close_claimed_entity_job_python_nosuchkey_does_not_succeed() -> None:
    conn = MagicMock()
    handler = MagicMock()
    handler.handle.side_effect = RuntimeError("NoSuchKey")
    claimed = _claimed(job_type="resolve_entities")
    with patch.object(probe4, "_proven_complete_materialization", return_value=None):
        with pytest.raises(RuntimeError, match="NoSuchKey"):
            probe4._close_claimed_resolve_job(conn, claimed, handler=handler)


def test_close_claimed_entity_job_42501_with_rows_does_not_succeed() -> None:
    conn = MagicMock()
    handler = MagicMock()
    error = InsufficientPrivilege("permission denied")
    error.sqlstate = "42501"
    handler.handle.side_effect = error
    claimed = _claimed(job_type="resolve_entities")
    with patch.object(probe4, "_proven_complete_materialization", return_value=None):
        with pytest.raises(InsufficientPrivilege):
            probe4._close_claimed_resolve_job(conn, claimed, handler=handler)


def test_close_claimed_entity_job_23503_with_rows_does_not_succeed() -> None:
    conn = MagicMock()
    handler = MagicMock()
    error = ForeignKeyViolation("foreign key")
    error.sqlstate = "23503"
    handler.handle.side_effect = error
    claimed = _claimed(job_type="resolve_entities")
    with patch.object(probe4, "_proven_complete_materialization", return_value=None):
        with pytest.raises(ForeignKeyViolation):
            probe4._close_claimed_resolve_job(conn, claimed, handler=handler)


def test_close_claimed_entity_job_proven_replay_finishes_succeeded_without_handler() -> None:
    conn = MagicMock()
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn.cursor.return_value = cursor
    cursor.fetchone.return_value = ("succeeded",)
    handler = MagicMock()
    claimed = _claimed(job_type="resolve_entities")
    metrics = _metrics()
    metrics["materialized_candidates"] = 1
    metrics["materialized_locators"] = 20
    metrics["input_locators"] = 20
    with patch.object(probe4, "_proven_complete_materialization", return_value=metrics):
        status = probe4._close_claimed_resolve_job(conn, claimed, handler=handler)
    assert status == "succeeded"
    handler.handle.assert_not_called()
    sql = str(cursor.execute.call_args.args[0])
    assert "succeeded" in sql
    assert "terminal_failure" not in sql


def test_proven_complete_accepts_matching_entity_rows() -> None:
    payload = _bound_payload(result_type="entity_extraction")
    analysis = {
        "entities": [
            {"name": "craft", "evidence": [{"locator_type": "text"}, {"locator_type": "text"}]}
        ]
    }
    conn = _cursor_conn((analysis,), (1,), rows=[(0, "craft", "object", 2)])
    metrics = probe4._proven_complete_materialization(
        conn, payload, job_type="resolve_entities"
    )
    assert metrics is not None
    assert metrics["materialized_candidates"] == 1
    assert metrics["materialized_locators"] == 2


def test_proven_complete_rejects_entity_rows_with_missing_evidence() -> None:
    payload = _bound_payload(result_type="entity_extraction")
    analysis = {
        "entities": [
            {"name": "craft", "evidence": [{"locator_type": "text"}, {"locator_type": "text"}]}
        ]
    }
    conn = _cursor_conn((analysis,), (1,), rows=[(0, "craft", "object", 1)])
    assert (
        probe4._proven_complete_materialization(conn, payload, job_type="resolve_entities")
        is None
    )
