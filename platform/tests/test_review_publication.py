from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from psycopg import Connection
from psycopg.errors import InsufficientPrivilege

from uap_platform.review.errors import ReviewSessionError
from uap_platform.review.publication import open_document_publication_review_case


class ScriptedCursor:
    def __init__(
        self, row: tuple[object, ...] | None = None, error: Exception | None = None
    ) -> None:
        self.row = row
        self.error = error
        self.statement = ""
        self.parameters: object = None

    def __enter__(self) -> ScriptedCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str, parameters: object = None) -> None:
        self.statement = statement
        self.parameters = parameters
        if self.error is not None:
            raise self.error

    def fetchone(self) -> tuple[object, ...] | None:
        return self.row


class ScriptedConnection:
    def __init__(self, cursor: ScriptedCursor) -> None:
        self.query = cursor

    def cursor(self) -> ScriptedCursor:
        return self.query


def _call(connection: ScriptedConnection) -> uuid.UUID:
    return open_document_publication_review_case(
        cast(Connection[Any], connection),
        uuid.UUID("00000000-0000-7000-8000-000000001201"),
        uuid.UUID("00000000-0000-7000-8000-000000001202"),
        3,
        "review editorial revision",
    )


def test_open_publication_review_case_calls_authoritative_database_function() -> None:
    expected = uuid.uuid4()
    cursor = ScriptedCursor((expected,))

    assert _call(ScriptedConnection(cursor)) == expected
    assert "audit.open_document_publication_review_case" in cursor.statement
    assert cursor.parameters == (
        uuid.UUID("00000000-0000-7000-8000-000000001201"),
        uuid.UUID("00000000-0000-7000-8000-000000001202"),
        3,
        "review editorial revision",
    )


@pytest.mark.parametrize("row", [None, (None,)])
def test_open_publication_review_case_rejects_empty_database_result(
    row: tuple[object, ...] | None,
) -> None:
    with pytest.raises(ReviewSessionError, match="review_unclassified"):
        _call(ScriptedConnection(ScriptedCursor(row)))


def test_open_publication_review_case_maps_database_denial() -> None:
    cursor = ScriptedCursor(error=InsufficientPrivilege("review_role_denied"))

    with pytest.raises(ReviewSessionError) as raised:
        _call(ScriptedConnection(cursor))

    assert raised.value.code == "review_session_role_denied"
    assert raised.value.sqlstate == "42501"
