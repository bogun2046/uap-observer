"""WP9.4 selection/promotion client unit tests. No WP9.5 merge or WP9.6 APIs."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock

from uap_platform.review import (
    accept_entity_candidate,
    bind_entity_candidate,
    select_analysis_result,
)
from uap_platform.review.errors import (
    REVIEW_BIND_TARGET_NOT_CANONICAL,
    REVIEW_CANDIDATE_EVIDENCE_MISSING,
    REVIEW_SELECTION_NOT_VALID,
    REVIEW_SELECTION_TYPE_UNSUPPORTED,
    map_review_error,
)


def test_select_analysis_result_calls_definer() -> None:
    connection = MagicMock()
    cursor = MagicMock()
    selection_id = uuid.uuid4()
    cursor.fetchone.return_value = (selection_id,)
    connection.cursor.return_value.__enter__.return_value = cursor
    analysis_id = uuid.uuid4()
    assert select_analysis_result(connection, analysis_id, "select this analysis") == selection_id
    sql = cursor.execute.call_args.args[0]
    assert "audit.select_analysis_result" in sql
    assert "p_actor_id" not in sql
    assert "enqueue_job" not in sql
    assert "apply_entity_merge" not in sql
    assert "create_manual_claim" not in sql


def test_accept_and_bind_call_definers() -> None:
    connection = MagicMock()
    cursor = MagicMock()
    entity_id = uuid.uuid4()
    cursor.fetchone.return_value = (entity_id,)
    connection.cursor.return_value.__enter__.return_value = cursor
    candidate_id = uuid.uuid4()
    assert accept_entity_candidate(connection, candidate_id, "accept this candidate") == entity_id
    bind_id = uuid.uuid4()
    assert (
        bind_entity_candidate(connection, candidate_id, bind_id, "bind this candidate") == entity_id
    )
    sql = " ".join(call.args[0] for call in cursor.execute.call_args_list)
    assert "audit.accept_entity_candidate" in sql
    assert "audit.bind_entity_candidate" in sql
    assert "core.merge_entities" not in sql
    assert "open_review_case" not in sql


def test_selection_codes_are_frozen() -> None:
    assert REVIEW_SELECTION_TYPE_UNSUPPORTED == "review_selection_type_unsupported"
    assert REVIEW_SELECTION_NOT_VALID == "review_selection_not_valid"
    assert REVIEW_CANDIDATE_EVIDENCE_MISSING == "review_candidate_evidence_missing"
    assert REVIEW_BIND_TARGET_NOT_CANONICAL == "review_bind_target_not_canonical"
    error = MagicMock()
    error.sqlstate = "22023"
    error.diag = MagicMock()
    error.diag.message_primary = REVIEW_SELECTION_NOT_VALID
    mapped = map_review_error(error)
    assert mapped.code == REVIEW_SELECTION_NOT_VALID
    assert mapped.sqlstate == "22023"


def test_wp9_4_package_has_no_later_stage_functions() -> None:
    promotion = (
        Path(__file__).resolve().parents[1] / "src/uap_platform/review/promotion.py"
    ).read_text(encoding="utf-8")
    for token in (
        "apply_entity_merge",
        "apply_entity_merge_reverse",
        "create_manual_claim",
        "bind_claim_subject",
        "publish_document",
    ):
        assert token not in promotion
    package = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (Path(__file__).resolve().parents[1] / "src/uap_platform/review").glob("*.py")
        if path.name not in {"claims.py", "__init__.py"}
    )
    for token in ("create_manual_claim", "bind_claim_subject", "publish_document"):
        assert token not in package
