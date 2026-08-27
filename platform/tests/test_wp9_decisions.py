"""WP9.3 decision client unit tests. No WP9.4 selection/merge APIs."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock

from uap_platform.review import record_review_decision
from uap_platform.review.errors import (
    REVIEW_DECISION_NOT_ALLOWED,
    REVIEW_GRANT_NOT_ACTIVE,
    REVIEW_SELF_REVIEW_DENIED,
    REVIEW_STRUCTURED_CHANGES_UNSUPPORTED,
    map_review_error,
)


def test_record_review_decision_calls_definer() -> None:
    connection = MagicMock()
    cursor = MagicMock()
    decision_id = uuid.uuid4()
    cursor.fetchone.return_value = (decision_id,)
    connection.cursor.return_value.__enter__.return_value = cursor
    case_id = uuid.uuid4()
    assert (
        record_review_decision(connection, case_id, "approve", "approve this claim")
        == decision_id
    )
    sql = cursor.execute.call_args.args[0]
    assert "audit.record_review_decision" in sql
    assert "p_actor_id" not in sql
    assert "enqueue_job" not in sql
    assert "select_analysis_result" not in sql


def test_structured_changes_code_is_frozen() -> None:
    assert REVIEW_STRUCTURED_CHANGES_UNSUPPORTED == (
        "review_structured_changes_unsupported"
    )
    assert REVIEW_SELF_REVIEW_DENIED == "review_self_review_denied"
    assert REVIEW_GRANT_NOT_ACTIVE == "review_grant_not_active"
    error = MagicMock()
    error.sqlstate = "22023"
    error.diag = MagicMock()
    error.diag.message_primary = REVIEW_DECISION_NOT_ALLOWED
    mapped = map_review_error(error)
    assert mapped.code == REVIEW_DECISION_NOT_ALLOWED
    assert mapped.sqlstate == "22023"


def test_wp9_3_clients_do_not_open_selection_stage() -> None:
    root = Path(__file__).resolve().parents[1] / "src/uap_platform/review"
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in root.glob("*.py")
        if path.name in {"cases.py", "decisions.py", "session.py"}
    )
    for token in (
        "select_analysis_result",
        "accept_entity_candidate",
        "bind_entity_candidate",
        "apply_entity_merge",
        "create_manual_claim",
        "bind_claim_subject",
        "publish_document",
    ):
        assert token not in text
