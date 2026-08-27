"""WP9.6 manual claim client unit tests. No WP10 APIs."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from psycopg.errors import CheckViolation

from uap_platform.review import create_manual_claim
from uap_platform.review.errors import (
    MANUAL_CLAIM_REQUIRES_SUPPORTS,
    REVIEW_AI_EVIDENCE_IMMUTABLE,
    ReviewSessionError,
    map_review_error,
)


def test_create_manual_claim_calls_definer() -> None:
    connection = MagicMock()
    cursor = MagicMock()
    claim_id = uuid.uuid4()
    cursor.fetchone.return_value = (claim_id,)
    connection.cursor.return_value.__enter__.return_value = cursor
    document = uuid.uuid4()
    span = uuid.uuid4()
    assert (
        create_manual_claim(
            connection,
            document,
            "a visible craft hovered",
            "observation",
            "reported",
            "witness",
            [span],
        )
        == claim_id
    )
    sql = cursor.execute.call_args.args[0]
    assert "audit.create_manual_claim" in sql
    assert "p_actor_id" not in sql
    params = cursor.execute.call_args.args[1]
    assert params[0] == document
    assert params[5] == [span]


def test_create_manual_claim_empty_row() -> None:
    connection = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    connection.cursor.return_value.__enter__.return_value = cursor
    with pytest.raises(ReviewSessionError) as raised:
        create_manual_claim(
            connection,
            uuid.uuid4(),
            "a visible craft hovered",
            "observation",
            "reported",
            None,
            [uuid.uuid4()],
        )
    assert raised.value.code == "review_unclassified"


def test_map_manual_supports_and_ai_immutable() -> None:
    error = MagicMock(spec=CheckViolation)
    error.sqlstate = "23514"
    error.diag = MagicMock()
    error.diag.message_primary = MANUAL_CLAIM_REQUIRES_SUPPORTS
    mapped = map_review_error(error)
    assert mapped.code == MANUAL_CLAIM_REQUIRES_SUPPORTS
    error.sqlstate = "22023"
    error.diag.message_primary = REVIEW_AI_EVIDENCE_IMMUTABLE
    mapped = map_review_error(error)
    assert mapped.code == REVIEW_AI_EVIDENCE_IMMUTABLE


def test_wp9_6_python_does_not_call_private_binders() -> None:
    root = Path(__file__).resolve().parents[1] / "src/uap_platform/review"
    text = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    assert "audit.create_manual_claim" in text
    for token in (
        "audit._apply_claim_subject_bind",
        "audit._replace_claim_evidence",
        "audit._retire_manual_claim_supports",
        "bind_claim_subject_entity",
        "core.create_manual_claim",
        "core.merge_entities",
    ):
        assert token not in text
    claims = (root / "claims.py").read_text(encoding="utf-8")
    assert "p_actor_id" not in claims
    assert "fingerprint" not in claims
