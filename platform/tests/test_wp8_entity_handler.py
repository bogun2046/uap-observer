"""G8-14/G8-16B fail-closed payload parsing and entity handler attempt closure."""

from __future__ import annotations

import inspect
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from psycopg.errors import RaiseException, SerializationFailure

from uap_platform.knowledge.handler import ResolveEntitiesHandler, _error_code_from_exception
from uap_platform.knowledge.job_types import (
    CLAIMABLE_JOB_TYPES,
    ENTITY_CLAIMABLE_JOB_TYPES,
    PRE_ENTITY_HANDLER_JOB_TYPES,
)
from uap_platform.knowledge.payload import KnowledgePayloadError, parse_knowledge_payload
from uap_platform.knowledge.reasons import (
    KNOWLEDGE_BUNDLE_MISMATCH,
    KNOWLEDGE_PAYLOAD_MISMATCH,
    KNOWLEDGE_SCHEMA_UNSUPPORTED,
)
from uap_platform.knowledge.worker import ResolveEntitiesWorker


def _valid_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "payload_schema_version": "knowledge.v2",
        "analysis_result_id": str(uuid.uuid4()),
        "analysis_result_sha256": "a" * 64,
        "analysis_schema_version": "ai.v1",
        "document_version_id": str(uuid.uuid4()),
        "result_type": "entity_extraction",
        "model_run_id": str(uuid.uuid4()),
        "input_sha256": "b" * 64,
        "extraction_anchor_status": "matched",
        "extraction_id": str(uuid.uuid4()),
    }
    payload.update(overrides)
    return payload


def test_parse_knowledge_payload_entity_result_type() -> None:
    parsed = parse_knowledge_payload(_valid_payload(), expected_result_type="entity_extraction")
    assert parsed.result_type == "entity_extraction"
    with pytest.raises(KnowledgePayloadError) as error:
        parse_knowledge_payload(_valid_payload())
    assert error.value.code == KNOWLEDGE_PAYLOAD_MISMATCH
    with pytest.raises(KnowledgePayloadError) as error:
        parse_knowledge_payload(
            _valid_payload(result_type="claim_extraction"),
            expected_result_type="entity_extraction",
        )
    assert error.value.code == KNOWLEDGE_PAYLOAD_MISMATCH


def test_parse_knowledge_payload_entity_schema() -> None:
    with pytest.raises(KnowledgePayloadError) as error:
        parse_knowledge_payload(
            _valid_payload(payload_schema_version="knowledge.v1"),
            expected_result_type="entity_extraction",
        )
    assert error.value.code == KNOWLEDGE_SCHEMA_UNSUPPORTED


def test_error_code_from_exception_extracts_frozen_token() -> None:
    assert _error_code_from_exception(RuntimeError("x knowledge_bundle_mismatch y")) == (
        KNOWLEDGE_BUNDLE_MISMATCH
    )


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


def _handler(conn: object) -> ResolveEntitiesHandler:
    return ResolveEntitiesHandler(conn, MagicMock())  # type: ignore[arg-type]


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
    handler = ResolveEntitiesHandler(conn, MagicMock())
    with pytest.raises(SerializationFailure):
        handler.handle(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), _valid_payload())
    finish_calls = [
        call for call in cursor.execute.call_args_list if "finish_knowledge_job" in str(call)
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
    handler = ResolveEntitiesHandler(conn, MagicMock())
    status = handler.handle(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), _valid_payload())
    assert status == "failed"
    assert any("finish_knowledge_job" in str(call) for call in cursor.execute.call_args_list)


def test_production_worker_activates_resolve_entities() -> None:
    inactive = ResolveEntitiesWorker(
        MagicMock(), MagicMock(), worker_id="pre", entities_handler_active=False
    )
    assert "resolve_entities" not in inactive.job_types
    assert inactive.job_types == PRE_ENTITY_HANDLER_JOB_TYPES
    assert inactive.job_types == CLAIMABLE_JOB_TYPES
    assert inactive.claim_job_types == ()
    production = ResolveEntitiesWorker(MagicMock(), MagicMock(), worker_id="prod")
    assert production.job_types == ENTITY_CLAIMABLE_JOB_TYPES
    assert "resolve_entities" in production.job_types
    assert production.claim_job_types == ("resolve_entities",)
    assert "resolve_relations" not in production.job_types
    for job_type in (*inactive.claim_job_types, *production.claim_job_types):
        assert job_type == "resolve_entities"


def test_worker_claim_one_none_and_dispatch_rejects_other_types() -> None:
    conn = MagicMock()
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn.cursor.return_value = cursor
    cursor.fetchone.return_value = None
    inactive = ResolveEntitiesWorker(
        conn, MagicMock(), worker_id="pre", entities_handler_active=False
    )
    assert inactive.claim_one() is None
    assert cursor.execute.call_count == 0
    assert conn.commit.call_count == 0
    worker = ResolveEntitiesWorker(conn, MagicMock(), worker_id="w")
    assert worker.claim_one() is None
    active_types = cursor.execute.call_args.args[1][1]
    assert "ops.claim_job" in str(cursor.execute.call_args.args[0])
    assert active_types == ["resolve_entities"]
    assert worker.run_once() is None
    with pytest.raises(KnowledgePayloadError):
        worker.dispatch((uuid.uuid4(), uuid.uuid4(), "resolve_claims", {}, uuid.uuid4()))
    with pytest.raises(KnowledgePayloadError):
        worker.dispatch((uuid.uuid4(),))
    with pytest.raises(KnowledgePayloadError):
        worker.dispatch(
            (uuid.uuid4(), uuid.uuid4(), "resolve_entities", "not-mapping", uuid.uuid4())
        )


def test_production_worker_from_settings_uses_active_default() -> None:
    assert "from_settings" in ResolveEntitiesWorker.__dict__
    assert ResolveEntitiesWorker.from_settings.__kwdefaults__ == {
        "entities_handler_active": True,
        "lease_seconds": 60,
    }


def test_handler_slice_comes_from_verified_object() -> None:
    source = inspect.getsource(ResolveEntitiesHandler._load_derived_text)
    assert "read_verified_object" in source
    assert "content_sha256" in source
    assert "byte_length" in source
    assert "fixture_extraction_text" not in source
    assert "materialize_entity_bundle" in inspect.getsource(ResolveEntitiesHandler)
