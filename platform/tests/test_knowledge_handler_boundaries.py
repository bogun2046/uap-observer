from __future__ import annotations

from typing import Any

import pytest

from uap_platform.knowledge.contracts import SourceCandidate
from uap_platform.knowledge.handler import (
    _optional_int,
    _source_candidates_from_claims,
    _source_candidates_from_items,
)
from uap_platform.knowledge.payload import KnowledgePayloadError
from uap_platform.knowledge.reasons import KNOWLEDGE_PAYLOAD_MISMATCH


def test_source_candidates_map_ordinal_and_all_locator_axes() -> None:
    candidates = _source_candidates_from_items(
        [
            {"evidence": []},
            {
                "evidence": [
                    {
                        "locator_type": "pdf",
                        "start": "12",
                        "end": 28,
                        "page_start": "2",
                        "page_end": 3,
                    },
                    {
                        "locator_type": "video",
                        "start": 100,
                        "end": 200,
                        "time_start_ms": 1000,
                        "time_end_ms": "2000",
                    },
                ]
            },
        ]
    )

    assert candidates[0] == SourceCandidate(ordinal=0, locators=())
    assert candidates[1].ordinal == 1
    assert candidates[1].locators[0].page_start == 2
    assert candidates[1].locators[0].page_end == 3
    assert candidates[1].locators[1].time_start_ms == 1000
    assert candidates[1].locators[1].time_end_ms == 2000


@pytest.mark.parametrize(
    "items",
    [
        [{"evidence": "not-a-sequence-of-locators"}],
        [{"evidence": [None]}],
        [{"evidence": [{"locator_type": "unknown", "start": 1, "end": 2}]}],
    ],
)
def test_source_candidates_reject_invalid_evidence_shape(items: list[dict[str, Any]]) -> None:
    with pytest.raises(KnowledgePayloadError) as raised:
        _source_candidates_from_items(items)
    assert raised.value.code == KNOWLEDGE_PAYLOAD_MISMATCH


def test_source_candidates_reject_missing_or_non_integer_coordinates() -> None:
    for locator in (
        {"locator_type": "text", "end": 2},
        {"locator_type": "text", "start": "bad", "end": 2},
        {"locator_type": "text", "start": 1, "end": 2, "page_start": "bad"},
    ):
        with pytest.raises((KeyError, ValueError)):
            _source_candidates_from_items([{"evidence": [locator]}])


def test_claim_candidates_share_the_item_mapping_contract() -> None:
    candidates = _source_candidates_from_claims([{"evidence": []}, {"evidence": []}])
    assert [candidate.ordinal for candidate in candidates] == [0, 1]


def test_optional_int_preserves_missing_and_converts_present_values() -> None:
    assert _optional_int({}, "page_start") is None
    assert _optional_int({"page_start": "4"}, "page_start") == 4
    with pytest.raises(ValueError):
        _optional_int({"page_start": "four"}, "page_start")
