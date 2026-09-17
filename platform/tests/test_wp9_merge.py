"""WP9.5 authorized merge client unit tests. No WP9.6 APIs."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from psycopg.errors import InsufficientPrivilege

from uap_platform.review import apply_entity_merge, apply_entity_merge_reverse
from uap_platform.review.errors import (
    REVIEW_ROLE_DENIED,
    REVIEW_SUBJECT_NOT_ACTIVE,
    REVIEW_SUBJECT_NOT_CANONICAL,
    ReviewSessionError,
    map_review_error,
)


def test_apply_entity_merge_calls_definer() -> None:
    connection = MagicMock()
    cursor = MagicMock()
    event_id = uuid.uuid4()
    cursor.fetchone.return_value = (event_id,)
    connection.cursor.return_value.__enter__.return_value = cursor
    source = uuid.uuid4()
    target = uuid.uuid4()
    assert apply_entity_merge(connection, source, target, "authorize merge of entities") == event_id
    sql = cursor.execute.call_args.args[0]
    assert "audit.apply_entity_merge" in sql
    assert "p_actor_id" not in sql
    assert "p_merged_by" not in sql
    assert "GRANT EXECUTE ON FUNCTION core.merge_entities" not in sql
    assert "create_manual_claim" not in sql


def test_apply_entity_merge_reverse_calls_definer() -> None:
    connection = MagicMock()
    cursor = MagicMock()
    reverse_id = uuid.uuid4()
    cursor.fetchone.return_value = (reverse_id,)
    connection.cursor.return_value.__enter__.return_value = cursor
    merge_event_id = uuid.uuid4()
    assert (
        apply_entity_merge_reverse(connection, merge_event_id, "authorize reverse of merge")
        == reverse_id
    )
    sql = cursor.execute.call_args.args[0]
    assert "audit.apply_entity_merge_reverse" in sql
    assert "p_reversed_by" not in sql
    assert "core.reverse_entity_merge" not in sql
    assert "create_manual_claim" not in sql


def test_apply_entity_merge_maps_role_denied() -> None:
    connection = MagicMock()
    cursor = MagicMock()
    cursor.execute.side_effect = InsufficientPrivilege("review_role_denied")
    connection.cursor.return_value.__enter__.return_value = cursor
    with pytest.raises(ReviewSessionError) as raised:
        apply_entity_merge(connection, uuid.uuid4(), uuid.uuid4(), "authorize merge of entities")
    assert raised.value.code in {REVIEW_ROLE_DENIED, "review_session_role_denied"}
    assert raised.value.sqlstate in {"42501", ""}


def test_apply_entity_merge_reverse_maps_role_denied() -> None:
    connection = MagicMock()
    cursor = MagicMock()
    cursor.execute.side_effect = InsufficientPrivilege("review_role_denied")
    connection.cursor.return_value.__enter__.return_value = cursor
    with pytest.raises(ReviewSessionError) as raised:
        apply_entity_merge_reverse(connection, uuid.uuid4(), "authorize reverse of merge")
    assert raised.value.code in {REVIEW_ROLE_DENIED, "review_session_role_denied"}
    assert raised.value.sqlstate in {"42501", ""}


def test_apply_entity_merge_empty_row() -> None:
    connection = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    connection.cursor.return_value.__enter__.return_value = cursor
    with pytest.raises(ReviewSessionError) as raised:
        apply_entity_merge(connection, uuid.uuid4(), uuid.uuid4(), "authorize merge of entities")
    assert raised.value.code == "review_unclassified"
    assert raised.value.sqlstate == "XX000"


def test_apply_entity_merge_reverse_empty_row() -> None:
    connection = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = (None,)
    connection.cursor.return_value.__enter__.return_value = cursor
    with pytest.raises(ReviewSessionError) as raised:
        apply_entity_merge_reverse(connection, uuid.uuid4(), "authorize reverse of merge")
    assert raised.value.code == "review_unclassified"
    assert raised.value.sqlstate == "XX000"


def test_merge_codes_are_frozen() -> None:
    assert REVIEW_ROLE_DENIED == "review_role_denied"
    assert REVIEW_SUBJECT_NOT_ACTIVE == "review_subject_not_active"
    assert REVIEW_SUBJECT_NOT_CANONICAL == "review_subject_not_canonical"
    error = MagicMock()
    error.sqlstate = "42501"
    error.diag = MagicMock()
    error.diag.message_primary = REVIEW_ROLE_DENIED
    mapped = map_review_error(error)
    assert mapped.code == REVIEW_ROLE_DENIED
    assert mapped.sqlstate == "42501"


def test_wp9_5_package_has_no_later_stage_functions() -> None:
    root = Path(__file__).resolve().parents[1] / "src/uap_platform/review"
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in root.glob("*.py")
        if path.name not in {"claims.py", "__init__.py"}
    )
    for token in (
        "create_manual_claim",
        "bind_claim_subject",
        "_apply_claim_subject_bind",
        "publish_document",
    ):
        assert token not in text
    merge = (root / "merge.py").read_text(encoding="utf-8")
    assert "audit.apply_entity_merge" in merge
    assert "audit.apply_entity_merge_reverse" in merge
    assert "p_actor_id" not in merge
    package = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    assert "audit._apply_claim_subject_bind" not in package
    assert "bind_claim_subject_entity" not in package
