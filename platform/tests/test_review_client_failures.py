from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from psycopg.errors import InsufficientPrivilege

from uap_platform.review import cases, promotion
from uap_platform.review.errors import ReviewSessionError


class ScriptedCursor:
    def __init__(
        self, row: tuple[object, ...] | None = None, error: Exception | None = None
    ) -> None:
        self.row = row
        self.error = error

    def __enter__(self) -> ScriptedCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, _query: str, _params: object = None) -> None:
        if self.error is not None:
            raise self.error

    def fetchone(self) -> tuple[object, ...] | None:
        return self.row


class ScriptedConnection:
    def __init__(self, row: tuple[object, ...] | None = None, error: Exception | None = None):
        self.result = ScriptedCursor(row, error)

    def cursor(self) -> ScriptedCursor:
        return self.result


CASE_ID = uuid.UUID("00000000-0000-7000-8000-000000009401")
OTHER_ID = uuid.UUID("00000000-0000-7000-8000-000000009402")


@pytest.mark.parametrize(
    ("operation", "args"),
    [
        (cases.open_review_case, ("document", CASE_ID, 2, "open case")),
        (cases.assign_review_case, (CASE_ID, OTHER_ID)),
        (cases.close_review_case, (CASE_ID, "close case")),
        (promotion.select_analysis_result, (CASE_ID, "select")),
        (promotion.accept_entity_candidate, (CASE_ID, "accept")),
        (promotion.bind_entity_candidate, (CASE_ID, OTHER_ID, "bind")),
    ],
)
@pytest.mark.parametrize("row", [None, (None,)])
def test_review_clients_reject_missing_database_result(
    operation: object, args: tuple[object, ...], row: tuple[object, ...] | None
) -> None:
    with pytest.raises(ReviewSessionError) as raised:
        cast(Any, operation)(cast(Any, ScriptedConnection(row)), *args)
    assert raised.value.code == "review_unclassified"


@pytest.mark.parametrize(
    ("operation", "args"),
    [
        (cases.assign_review_case, (CASE_ID, OTHER_ID)),
        (cases.close_review_case, (CASE_ID, "close case")),
        (promotion.select_analysis_result, (CASE_ID, "select")),
        (promotion.accept_entity_candidate, (CASE_ID, "accept")),
        (promotion.bind_entity_candidate, (CASE_ID, OTHER_ID, "bind")),
    ],
)
def test_review_clients_map_database_permission_errors(
    operation: object, args: tuple[object, ...]
) -> None:
    with pytest.raises(ReviewSessionError) as raised:
        cast(Any, operation)(
            cast(Any, ScriptedConnection(error=InsufficientPrivilege("review_role_denied"))),
            *args,
        )
    assert raised.value.code == "review_session_role_denied"
    assert raised.value.sqlstate == "42501"
