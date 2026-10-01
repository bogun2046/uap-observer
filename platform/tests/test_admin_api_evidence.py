from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from typing import Any, cast

import pytest
from psycopg import Connection

from uap_platform.admin_api import evidence
from uap_platform.admin_api.errors import AdminError
from uap_platform.knowledge.contracts import (
    AnchorStatus,
    ExtractionAnchor,
    MappingClass,
    MappingReport,
)

DOC = uuid.UUID("00000000-0000-7000-8000-000000001101")
EXT = uuid.UUID("00000000-0000-7000-8000-000000001102")
RESULT = uuid.UUID("00000000-0000-7000-8000-000000001103")
INPUT_SHA = hashlib.sha256(b"editorial evidence input").hexdigest()
TEXT = "Evidence supports the claim."


class ScriptedCursor:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, object]] = []

    def __enter__(self) -> ScriptedCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: object = None) -> None:
        self.executed.append((query, params))

    def fetchone(self) -> object:
        return self.rows.pop(0)


class ScriptedConnection:
    def __init__(self, rows: list[object]) -> None:
        self.query = ScriptedCursor(rows)

    def cursor(self) -> ScriptedCursor:
        return self.query


def _rows(
    *,
    result: object = {"claims": [{"evidence": [{"locator_type": "text", "start": 0, "end": 8}]}]},
    validation_status: str = "valid",
    input_sha: str = INPUT_SHA,
    extraction_sha: str = INPUT_SHA,
    location_map: object = [],
    spans: object = None,
    tuple_rows: bool = False,
    extraction: object = "present",
) -> list[object]:
    analysis: object = (
        (result, validation_status, input_sha)
        if tuple_rows
        else {"result": result, "validation_status": validation_status, "input_sha256": input_sha}
    )
    extracted: object
    if extraction == "missing":
        extracted = None
    else:
        values = (EXT, extraction_sha, location_map, "derived", "key", INPUT_SHA, len(TEXT))
        extracted = (
            values
            if tuple_rows
            else {
                "id": EXT,
                "output_sha256": extraction_sha,
                "location_map": location_map,
                "bucket_name": "derived",
                "object_key": "key",
                "content_sha256": INPUT_SHA,
                "byte_length": len(TEXT),
            }
        )
    return [analysis, extracted, (spans if spans is not None else [uuid.uuid4()],)]


def _materialize(
    connection: ScriptedConnection,
    *,
    result_type: str = "claim_extraction",
    item_ordinal: int | None = None,
    object_client: object = object(),
) -> dict[int, list[uuid.UUID]]:
    return evidence.materialize_ai_evidence(
        cast(Connection[dict[str, object]], connection),
        cast(Any, object_client),
        analysis_result_id=RESULT,
        document_version_id=DOC,
        result_type=result_type,
        item_ordinal=item_ordinal,
    )


def test_materialize_evidence_fails_when_object_storage_is_unavailable() -> None:
    with pytest.raises(AdminError, match="api_dependency_unavailable"):
        evidence.materialize_ai_evidence(
            cast(Connection[dict[str, object]], ScriptedConnection([])),
            None,
            analysis_result_id=RESULT,
            document_version_id=DOC,
            result_type="claim_extraction",
        )


@pytest.mark.parametrize(
    ("rows", "code"),
    [
        (_rows(result=None), "editorial_ai_result_invalid"),
        (_rows(validation_status="pending"), "editorial_ai_result_invalid"),
        (_rows(result={"claims": "not-an-array"}), "editorial_ai_result_invalid"),
        (_rows(result={"claims": [None]}), "editorial_ai_result_invalid"),
        (
            _rows(result={"claims": [{"evidence": []}]}, extraction="missing"),
            "editorial_ai_item_not_found",
        ),
        (
            _rows(result={"claims": [{"evidence": []}]}, extraction="missing"),
            "editorial_evidence_extraction_mismatch",
        ),
    ],
)
def test_materialize_evidence_rejects_invalid_or_missing_input(
    rows: list[object], code: str
) -> None:
    connection = ScriptedConnection(rows)
    with pytest.raises(AdminError, match=code):
        _materialize(connection, item_ordinal=1 if code == "editorial_ai_item_not_found" else None)
    assert len(connection.query.executed) == 2


def test_materialize_evidence_rejects_extraction_hash_mismatch() -> None:
    with pytest.raises(AdminError, match="editorial_evidence_extraction_mismatch"):
        _materialize(ScriptedConnection(_rows(extraction_sha="d" * 64)))


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("storage checksum mismatch"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"),
        LookupError("missing"),
    ],
)
def test_materialize_evidence_fails_closed_when_object_cannot_be_verified(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    def broken_read(*_args: object) -> bytes:
        raise failure

    monkeypatch.setattr(evidence, "read_verified_object", broken_read)
    with pytest.raises(AdminError, match="editorial_evidence_extraction_mismatch"):
        _materialize(ScriptedConnection(_rows()))


def test_materialize_evidence_rejects_unmapped_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rejected = MappingReport(
        classification=MappingClass.TERMINAL_UNMAPPABLE,
        anchor=ExtractionAnchor(AnchorStatus.MATCHED, EXT),
        accepted_candidates=(),
        rejected_candidates=(),
    )
    monkeypatch.setattr(evidence, "read_verified_object", lambda *_args: TEXT.encode())
    monkeypatch.setattr(evidence, "map_knowledge_result", lambda **_kwargs: rejected)

    with pytest.raises(AdminError, match="editorial_evidence_provenance_mismatch"):
        _materialize(ScriptedConnection(_rows()))


@pytest.mark.parametrize(
    ("result_type", "items_key"),
    [("claim_extraction", "claims"), ("entity_extraction", "entities")],
)
@pytest.mark.parametrize("item_ordinal", [None, 1])
def test_materialize_evidence_persists_verified_spans_and_maps_ordinals(
    monkeypatch: pytest.MonkeyPatch,
    result_type: str,
    items_key: str,
    item_ordinal: int | None,
) -> None:
    span_ids = [uuid.uuid4(), uuid.uuid4()]
    items = [
        {"evidence": [{"locator_type": "text", "start": 0, "end": 8}]},
        {"evidence": [{"locator_type": "text", "start": 9, "end": len(TEXT)}]},
    ]
    rows = _rows(result={items_key: items}, spans=span_ids, tuple_rows=True, location_map={})
    connection = ScriptedConnection(rows)
    monkeypatch.setattr(evidence, "read_verified_object", lambda *_args: TEXT.encode())

    result = _materialize(
        connection, result_type=result_type, item_ordinal=item_ordinal
    )

    expected = {0: [span_ids[0]]} if item_ordinal is not None else {
        0: [span_ids[0]],
        1: [span_ids[1]],
    }
    assert result == expected
    query, params = connection.query.executed[2]
    assert "audit.materialize_editorial_evidence" in query
    assert isinstance(params, tuple)
    assert params[:3] == (DOC, EXT, INPUT_SHA)
    payload = cast(Any, params[3]).obj
    assert len(payload) == (1 if item_ordinal is not None else 2)
    assert payload[-1]["evidence_text"] == TEXT[9:]
    locator = cast(Mapping[str, object], payload[0]["locator"]["source_locator"])
    assert locator["locator_type"] == "text"
