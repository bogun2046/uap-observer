"""G8-13 fail-closed payload parsing and handler attempt closure without a live database."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from psycopg.errors import RaiseException, SerializationFailure

from uap_platform.knowledge.handler import (
    ResolveClaimsHandler,
    _error_code_from_exception,
    payload_from_claim,
)
from uap_platform.knowledge.job_types import CLAIMABLE_JOB_TYPES, PRE_CLAIM_HANDLER_JOB_TYPES
from uap_platform.knowledge.payload import KnowledgePayloadError, parse_knowledge_payload
from uap_platform.knowledge.reasons import (
    KNOWLEDGE_BUNDLE_MISMATCH,
    KNOWLEDGE_PAYLOAD_MISMATCH,
    KNOWLEDGE_SCHEMA_UNSUPPORTED,
)
from uap_platform.knowledge.worker import ResolveClaimsWorker


def _valid_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "payload_schema_version": "knowledge.v2",
        "analysis_result_id": str(uuid.uuid4()),
        "analysis_result_sha256": "a" * 64,
        "analysis_schema_version": "ai.v1",
        "document_version_id": str(uuid.uuid4()),
        "result_type": "claim_extraction",
        "model_run_id": str(uuid.uuid4()),
        "input_sha256": "b" * 64,
        "extraction_anchor_status": "matched",
        "extraction_id": str(uuid.uuid4()),
    }
    payload.update(overrides)
    return payload


def test_parse_knowledge_payload_missing_key() -> None:
    payload = _valid_payload()
    del payload["model_run_id"]
    with pytest.raises(KnowledgePayloadError) as error:
        parse_knowledge_payload(payload)
    assert error.value.code == KNOWLEDGE_PAYLOAD_MISMATCH


def test_parse_knowledge_payload_illegal_uuid() -> None:
    with pytest.raises(KnowledgePayloadError) as error:
        parse_knowledge_payload(_valid_payload(analysis_result_id="not-a-uuid"))
    assert error.value.code == KNOWLEDGE_PAYLOAD_MISMATCH


def test_parse_knowledge_payload_schema() -> None:
    with pytest.raises(KnowledgePayloadError) as error:
        parse_knowledge_payload(_valid_payload(payload_schema_version="knowledge.v1"))
    assert error.value.code == KNOWLEDGE_SCHEMA_UNSUPPORTED


def test_parse_knowledge_payload_illegal_anchor_pairing() -> None:
    with pytest.raises(KnowledgePayloadError) as error:
        parse_knowledge_payload(
            _valid_payload(extraction_anchor_status="matched", extraction_id=None)
        )
    assert error.value.code == KNOWLEDGE_PAYLOAD_MISMATCH


def test_parse_knowledge_payload_legal_missing_empty() -> None:
    parsed = parse_knowledge_payload(
        _valid_payload(extraction_anchor_status="missing", extraction_id=None)
    )
    assert parsed.anchor.extraction_id is None


def test_parse_knowledge_payload_rejects_non_mapping() -> None:
    with pytest.raises(KnowledgePayloadError) as error:
        parse_knowledge_payload(["not", "a", "mapping"])
    assert error.value.code == KNOWLEDGE_PAYLOAD_MISMATCH


def test_parse_knowledge_payload_rejects_wrong_result_type() -> None:
    with pytest.raises(KnowledgePayloadError) as error:
        parse_knowledge_payload(_valid_payload(result_type="entity_extraction"))
    assert error.value.code == KNOWLEDGE_PAYLOAD_MISMATCH


def test_parse_knowledge_payload_rejects_short_hash() -> None:
    with pytest.raises(KnowledgePayloadError) as error:
        parse_knowledge_payload(_valid_payload(input_sha256="abc"))
    assert error.value.code == KNOWLEDGE_PAYLOAD_MISMATCH


def test_payload_from_claim_rejects_non_object() -> None:
    with pytest.raises(KnowledgePayloadError):
        payload_from_claim((uuid.uuid4(), "resolve_claims", "knowledge.v2", "not-json"))


def test_payload_from_claim_reads_index_three() -> None:
    payload = _valid_payload()
    claimed = (
        uuid.uuid4(),
        uuid.uuid4(),
        "resolve_claims",
        payload,
        "knowledge.v2",
        "resolve-claims:x",
        1,
        uuid.uuid4(),
        None,
    )
    assert payload_from_claim(claimed) is payload


def test_error_code_from_exception_extracts_frozen_token() -> None:
    assert _error_code_from_exception(RuntimeError("x knowledge_bundle_mismatch y")) == (
        KNOWLEDGE_BUNDLE_MISMATCH
    )
    assert _error_code_from_exception(RuntimeError("nope")) == KNOWLEDGE_PAYLOAD_MISMATCH


class _Cursor:
    def __init__(self, owner: _Conn) -> None:
        self.owner = owner
        self.last_sql = ""

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        self.last_sql = sql
        self.owner.executed.append(sql)
        if self.owner.execute_hook is not None:
            self.owner.execute_hook(sql, params)

    def fetchone(self) -> tuple[Any, ...] | None:
        if "finish_knowledge_job" in self.last_sql:
            self.owner.finished = True
            return ("failed",)
        return None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []


class _Conn:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.rolled = 0
        self.committed = 0
        self.finished = False
        self.execute_hook: Any = None

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def rollback(self) -> None:
        self.rolled += 1

    def commit(self) -> None:
        self.committed += 1


def _handler(conn: object) -> ResolveClaimsHandler:
    return ResolveClaimsHandler(conn, MagicMock())  # type: ignore[arg-type]


def test_handler_missing_key_finishes_attempt() -> None:
    conn = _Conn()
    handler = _handler(conn)
    payload = _valid_payload()
    del payload["input_sha256"]
    status = handler.handle(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), payload)
    assert status == "failed"
    assert conn.finished is True
    assert conn.committed == 1
    assert any("finish_knowledge_job" in sql for sql in conn.executed)
    assert any("terminal_failure" in sql for sql in conn.executed)


def test_handler_illegal_uuid_finishes_attempt() -> None:
    conn = _Conn()
    handler = _handler(conn)
    status = handler.handle(
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        _valid_payload(document_version_id="zzzz"),
    )
    assert status == "failed"
    assert conn.finished is True


def test_handler_schema_unsupported_finishes_attempt() -> None:
    conn = _Conn()
    handler = _handler(conn)
    status = handler.handle(
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        _valid_payload(analysis_schema_version="ai.v0"),
    )
    assert status == "failed"
    assert conn.finished is True


def test_handler_analysis_mismatch_finishes_attempt() -> None:
    conn = _Conn()
    handler = _handler(conn)
    status = handler.handle(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), _valid_payload())
    assert status == "failed"
    assert conn.finished is True
    assert conn.rolled >= 1


def test_handler_unclassified_exception_finishes_attempt() -> None:
    conn = _Conn()

    def boom(sql: str, _params: object = None) -> None:
        if "finish_knowledge_job" in sql:
            return
        raise RuntimeError("unexpected mapper crash")

    conn.execute_hook = boom
    handler = _handler(conn)
    status = handler.handle(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), _valid_payload())
    assert status == "failed"
    assert conn.finished is True


def test_handler_40001_does_not_finish() -> None:
    conn = MagicMock()
    conn.rollback = MagicMock()
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn.cursor.return_value = cursor

    def explode(_sql: str, _params: object = None) -> None:
        error = SerializationFailure("resolution job lease is missing")
        error.sqlstate = "40001"
        raise error

    cursor.execute.side_effect = explode
    handler = ResolveClaimsHandler(conn, MagicMock())
    with pytest.raises(SerializationFailure):
        handler.handle(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), _valid_payload())
    finish_calls = [
        call
        for call in cursor.execute.call_args_list
        if "finish_knowledge_job" in str(call)
    ]
    assert finish_calls == []


def test_handler_deterministic_sql_before_savepoint_finishes() -> None:
    conn = MagicMock()
    conn.rollback = MagicMock()
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn.cursor.return_value = cursor
    cursor.fetchone.return_value = ("failed",)

    def explode(sql: str, _params: object = None) -> None:
        if "finish_knowledge_job" in sql:
            return
        error = RaiseException("knowledge_payload_mismatch")
        error.sqlstate = "22023"
        raise error

    cursor.execute.side_effect = explode
    handler = ResolveClaimsHandler(conn, MagicMock())
    status = handler.handle(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), _valid_payload())
    assert status == "failed"
    assert any("finish_knowledge_job" in str(call) for call in cursor.execute.call_args_list)


def test_claimable_types_never_include_entities() -> None:
    assert "resolve_entities" not in CLAIMABLE_JOB_TYPES
    assert "resolve_claims" not in PRE_CLAIM_HANDLER_JOB_TYPES
    assert "resolve_claims" in CLAIMABLE_JOB_TYPES


def test_production_worker_activates_resolve_claims() -> None:
    inactive = ResolveClaimsWorker(
        MagicMock(), MagicMock(), worker_id="pre", claims_handler_active=False
    )
    assert "resolve_claims" not in inactive.job_types
    assert inactive.job_types == PRE_CLAIM_HANDLER_JOB_TYPES
    assert inactive.claim_job_types == ()
    production = ResolveClaimsWorker(MagicMock(), MagicMock(), worker_id="prod")
    assert production.job_types == CLAIMABLE_JOB_TYPES
    assert "resolve_claims" in production.job_types
    assert production.claim_job_types == ("resolve_claims",)
    assert "resolve_entities" not in production.job_types
    assert "resolve_relations" not in production.job_types


def test_worker_claim_one_none_and_dispatch_rejects_other_types() -> None:
    conn = MagicMock()
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn.cursor.return_value = cursor
    cursor.fetchone.return_value = None
    inactive = ResolveClaimsWorker(
        conn, MagicMock(), worker_id="pre", claims_handler_active=False
    )
    assert inactive.claim_one() is None
    conn.cursor.assert_not_called()
    worker = ResolveClaimsWorker(conn, MagicMock(), worker_id="w")
    assert worker.claim_one() is None
    assert worker.run_once() is None
    with pytest.raises(KnowledgePayloadError):
        worker.dispatch((uuid.uuid4(), uuid.uuid4(), "fetch_source", {}, uuid.uuid4()))
    with pytest.raises(KnowledgePayloadError):
        worker.dispatch((uuid.uuid4(),))
    with pytest.raises(KnowledgePayloadError):
        worker.dispatch(
            (uuid.uuid4(), uuid.uuid4(), "resolve_claims", "not-mapping", uuid.uuid4())
        )
