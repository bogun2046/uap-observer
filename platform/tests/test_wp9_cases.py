"""WP9.2 case client unit tests. No WP9.3 decision APIs."""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from psycopg.errors import UniqueViolation

from uap_platform.review import (
    ReviewSessionError,
    assign_review_case,
    close_review_case,
    open_review_case,
)
from uap_platform.review.canonical import (
    FROZEN_COMPACT_JSON,
    FROZEN_COMPACT_SHA256,
    FROZEN_HUGE_JSON,
    FROZEN_HUGE_SHA256,
    FROZEN_HUGE_SOURCE,
    FROZEN_NESTED_JSON,
    FROZEN_NESTED_SHA256,
    FROZEN_NUMBER_ARRAY_JSON,
    FROZEN_NUMBER_ARRAY_SHA256,
    FROZEN_NUMBER_ARRAY_SOURCE,
    FROZEN_NUMBER_JSON,
    FROZEN_NUMBER_SHA256,
    FROZEN_NUMBER_SOURCE,
    FROZEN_PRECISION_JSON,
    FROZEN_PRECISION_SHA256,
    FROZEN_PRECISION_SOURCE,
    FROZEN_WIDE_INT_JSON,
    FROZEN_WIDE_INT_SHA256,
    canonical_json,
    loads_canonical,
    payload_sha256,
    payload_sha256_text,
)
from uap_platform.review.errors import (
    KNOWLEDGE_RELATION_REVIEW_NOT_IN_WP9,
    REVIEW_CASE_ALREADY_OPEN,
    REVIEW_IDEMPOTENCY_PAYLOAD_CONFLICT,
    REVIEW_REQUEST_ID_MISSING,
    map_review_error,
)


def test_open_review_case_calls_definer() -> None:
    connection = MagicMock()
    cursor = MagicMock()
    case_id = uuid.uuid4()
    cursor.fetchone.return_value = (case_id,)
    connection.cursor.return_value.__enter__.return_value = cursor
    subject = uuid.uuid4()
    assert open_review_case(connection, "claim", subject, 0, "open a claim") == case_id
    sql = cursor.execute.call_args.args[0]
    assert "audit.open_review_case" in sql
    assert "p_actor_id" not in sql


def test_assign_and_close_call_definers() -> None:
    connection = MagicMock()
    cursor = MagicMock()
    case_id = uuid.uuid4()
    cursor.fetchone.return_value = (case_id,)
    connection.cursor.return_value.__enter__.return_value = cursor
    assert assign_review_case(connection, case_id, uuid.uuid4()) == case_id
    assert close_review_case(connection, case_id, "close after decide") == case_id
    executed = [call.args[0] for call in cursor.execute.call_args_list]
    assert any("assign_review_case" in sql for sql in executed)
    assert any("close_review_case" in sql for sql in executed)


def test_open_maps_already_open() -> None:
    connection = MagicMock()
    cursor = MagicMock()
    error = UniqueViolation("review_case_already_open")
    error.sqlstate = "23505"
    cursor.execute.side_effect = error
    connection.cursor.return_value.__enter__.return_value = cursor
    with pytest.raises(ReviewSessionError) as raised:
        open_review_case(connection, "claim", uuid.uuid4(), 0, "open a claim")
    assert raised.value.code in {
        REVIEW_CASE_ALREADY_OPEN,
        REVIEW_IDEMPOTENCY_PAYLOAD_CONFLICT,
    }
    assert raised.value.sqlstate == "23505"


def test_map_relation_and_request_id_codes() -> None:
    error = MagicMock()
    error.sqlstate = "22023"
    error.diag = MagicMock()
    error.diag.message_primary = KNOWLEDGE_RELATION_REVIEW_NOT_IN_WP9
    assert map_review_error(error).code == KNOWLEDGE_RELATION_REVIEW_NOT_IN_WP9
    error.diag.message_primary = REVIEW_REQUEST_ID_MISSING
    error.sqlstate = "42501"
    assert map_review_error(error).code == REVIEW_REQUEST_ID_MISSING
    error.diag.message_primary = REVIEW_IDEMPOTENCY_PAYLOAD_CONFLICT
    error.sqlstate = "23505"
    assert map_review_error(error).code == REVIEW_IDEMPOTENCY_PAYLOAD_CONFLICT
    error.diag.message_primary = REVIEW_CASE_ALREADY_OPEN
    assert map_review_error(error).code == REVIEW_CASE_ALREADY_OPEN


def test_frozen_compact_canonical_sha256() -> None:
    assert canonical_json({"b": 2, "a": 1}) == FROZEN_COMPACT_JSON
    assert payload_sha256({"b": 2, "a": 1}) == FROZEN_COMPACT_SHA256
    assert payload_sha256({"a": 1, "b": 2}) == FROZEN_COMPACT_SHA256
    nested = {"z": [{"b": 2, "a": 1}, True, None], "m": {"d": "x y"}}
    assert canonical_json(nested) == FROZEN_NESTED_JSON
    assert payload_sha256(nested) == FROZEN_NESTED_SHA256
    assert FROZEN_COMPACT_SHA256 == (
        "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"
    )
    assert " " not in FROZEN_COMPACT_JSON
    assert "x y" in FROZEN_NESTED_JSON


def test_frozen_number_canonical_sha256() -> None:
    assert canonical_json(loads_canonical(FROZEN_NUMBER_SOURCE)) == FROZEN_NUMBER_JSON
    assert payload_sha256_text(FROZEN_NUMBER_SOURCE) == FROZEN_NUMBER_SHA256
    assert payload_sha256_text('{"e":100,"n":1,"small":0.0000001}') == FROZEN_NUMBER_SHA256
    assert payload_sha256({"e": 100}) == payload_sha256({"e": Decimal("1e2")})
    assert payload_sha256({"e": 100.0}) == payload_sha256({"e": 100})
    assert canonical_json(1) == canonical_json(1.0) == "1"
    assert canonical_json(0) == canonical_json(0.0) == "0"
    assert canonical_json(Decimal("-1.50")) == "-1.5"
    assert canonical_json(loads_canonical(FROZEN_NUMBER_ARRAY_SOURCE)) == (
        FROZEN_NUMBER_ARRAY_JSON
    )
    assert payload_sha256_text(FROZEN_NUMBER_ARRAY_SOURCE) == FROZEN_NUMBER_ARRAY_SHA256
    assert FROZEN_NUMBER_SHA256 == (
        "2c39cedbb91a51d5591b068931c00b4204cf539bed72ca2508566841726a5022"
    )
    assert FROZEN_NUMBER_JSON == '{"e":100,"n":1,"small":0.0000001}'


def test_frozen_huge_and_precision_canonical_sha256() -> None:
    assert canonical_json(loads_canonical(FROZEN_HUGE_SOURCE)) == FROZEN_HUGE_JSON
    assert len(FROZEN_HUGE_JSON) == 5001
    assert payload_sha256_text(FROZEN_HUGE_SOURCE) == FROZEN_HUGE_SHA256
    assert payload_sha256_text(FROZEN_HUGE_JSON) == FROZEN_HUGE_SHA256
    assert FROZEN_HUGE_SHA256 == (
        "d4e22924ae5b055f946dfeea48d109a17a5aa86b2edbc2340fcdb5361c19ed90"
    )
    assert canonical_json(loads_canonical(FROZEN_PRECISION_SOURCE)) == FROZEN_PRECISION_JSON
    assert payload_sha256_text(FROZEN_PRECISION_SOURCE) == FROZEN_PRECISION_SHA256
    assert payload_sha256_text(FROZEN_PRECISION_SOURCE + "00") == FROZEN_PRECISION_SHA256
    assert FROZEN_PRECISION_SHA256 == (
        "0bcebfcee59a792961cd281b05ce5d428b3962c99776cd0b0a23765721a311a6"
    )
    assert canonical_json(loads_canonical(FROZEN_WIDE_INT_JSON)) == FROZEN_WIDE_INT_JSON
    assert payload_sha256_text(FROZEN_WIDE_INT_JSON) == FROZEN_WIDE_INT_SHA256
    assert canonical_json(Decimal("1e5000")) == FROZEN_HUGE_JSON
    assert canonical_json(10**5000) == FROZEN_HUGE_JSON
    assert canonical_json(Decimal("-1e5000")) == "-" + FROZEN_HUGE_JSON
    assert "str(int(" not in Path(__file__).resolve().parents[1].joinpath(
        "src/uap_platform/review/canonical.py"
    ).read_text(encoding="utf-8")


def test_wp9_2_package_has_no_decision_functions() -> None:
    root = Path(__file__).resolve().parents[1] / "src/uap_platform/review"
    text = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    for token in (
        "record_review_decision",
        "select_analysis_result",
        "accept_entity_candidate",
        "apply_entity_merge",
        "create_manual_claim",
    ):
        assert token not in text
