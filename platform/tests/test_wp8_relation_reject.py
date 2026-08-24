from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

from uap_platform.knowledge.payload import KnowledgePayloadError
from uap_platform.knowledge.reasons import KNOWLEDGE_RELATION_TASK_NOT_IN_WP8
from uap_platform.knowledge.worker import (
    KnowledgeJobDispatcher,
    finish_misclaimed_relation_job,
)


def test_finish_misclaimed_relation_job_uses_finish_job_not_knowledge() -> None:
    conn = MagicMock()
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn.cursor.return_value = cursor
    cursor.fetchone.return_value = ("dead",)
    job_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    token = uuid.uuid4()
    status = finish_misclaimed_relation_job(
        conn, job_id, attempt_id, token, "resolve_relations"
    )
    assert status == "dead"
    sql = str(cursor.execute.call_args.args[0])
    assert "ops.finish_job" in sql
    assert "finish_knowledge_job" not in sql
    assert "materialize" not in sql
    bound = cursor.execute.call_args.args[1]
    assert KNOWLEDGE_RELATION_TASK_NOT_IN_WP8 in bound
    conn.commit.assert_called_once()


def test_finish_misclaimed_relation_job_rejects_other_types() -> None:
    with pytest.raises(KnowledgePayloadError):
        finish_misclaimed_relation_job(
            MagicMock(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "resolve_claims"
        )


def test_dispatcher_routes_relations_to_finish_job() -> None:
    conn = MagicMock()
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn.cursor.return_value = cursor
    cursor.fetchone.return_value = ("dead",)
    dispatcher = KnowledgeJobDispatcher(conn)
    claimed: tuple[Any, ...] = (
        uuid.uuid4(),
        uuid.uuid4(),
        "resolve_relations",
        {},
        uuid.uuid4(),
    )
    assert dispatcher.dispatch(claimed) == "dead"
    sql = str(cursor.execute.call_args.args[0])
    assert "ops.finish_job" in sql
    assert "finish_knowledge_job" not in sql


def test_dispatcher_rejects_short_claimed_row() -> None:
    dispatcher = KnowledgeJobDispatcher(MagicMock())
    with pytest.raises(KnowledgePayloadError):
        dispatcher.dispatch((uuid.uuid4(), uuid.uuid4(), "resolve_relations"))


def test_dispatcher_without_object_client_rejects_claims() -> None:
    dispatcher = KnowledgeJobDispatcher(MagicMock())
    claimed: tuple[Any, ...] = (
        uuid.uuid4(),
        uuid.uuid4(),
        "resolve_claims",
        {},
        uuid.uuid4(),
    )
    with pytest.raises(KnowledgePayloadError):
        dispatcher.dispatch(claimed)


def test_dispatcher_rejects_unknown_type() -> None:
    dispatcher = KnowledgeJobDispatcher(MagicMock(), object_client=MagicMock())
    claimed: tuple[Any, ...] = (
        uuid.uuid4(),
        uuid.uuid4(),
        "resolve_aliases",
        {},
        uuid.uuid4(),
    )
    with pytest.raises(KnowledgePayloadError):
        dispatcher.dispatch(claimed)
