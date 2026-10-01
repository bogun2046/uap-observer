from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import cast
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from uap_platform.model_governance import ModelTaskType
from uap_platform.v1 import library as library_module
from uap_platform.v1.bootstrap import BOOTSTRAP_PRINCIPAL_ID
from uap_platform.v1.library import (
    MONTHLY_BUDGET_MICRO_CNY,
    MONTHLY_WARNING_MICRO_CNY,
    InternalLibrary,
    _month_bounds,
)

DOC = uuid.UUID("00000000-0000-7000-8000-000000001301")
VERSION = uuid.UUID("00000000-0000-7000-8000-000000001302")
PROMPT = uuid.UUID("00000000-0000-7000-8000-000000001303")
REQUEST = uuid.UUID("00000000-0000-7000-8000-000000001304")


def _db(
    *,
    fetchone: list[object] | None = None,
    fetchall: list[list[dict[str, object]]] | None = None,
) -> MagicMock:
    connection = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.side_effect = list(fetchone or [])
    cursor.fetchall.side_effect = list(fetchall or [])
    connection.cursor.return_value.__enter__.return_value = cursor
    connection.__enter__.return_value = connection
    connection.test_cursor = cursor
    return connection


def _library() -> InternalLibrary:
    return InternalLibrary(
        "postgresql+psycopg://reader@db/read",
        "postgresql+psycopg://worker@db/worker",
        "postgresql+psycopg://model@db/model",
        object(),  # type: ignore[arg-type]
    )


def test_month_bounds_uses_shanghai_month_and_handles_december() -> None:
    now = datetime(2026, 12, 31, 18, 30, tzinfo=UTC)

    start, end = _month_bounds(now)

    assert start == datetime(2027, 1, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert end == datetime(2027, 2, 1, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.mark.parametrize(
    ("row", "state"),
    [
        ({"extraction_outcome": "failed"}, "extraction_failed"),
        ({"extraction_outcome": "succeeded", "latest_model_status": "failed"}, "analysis_failed"),
        (
            {
                "extraction_outcome": "succeeded",
                "classification": {"relevance": {"decision": "irrelevant"}},
            },
            "not_relevant",
        ),
        (
            {
                "extraction_outcome": "succeeded",
                "summary": {"summary": "ready"},
                "claim_extraction": [1],
            },
            "analysis_ready",
        ),
        (
            {"extraction_outcome": "succeeded", "latest_model_status": "succeeded"},
            "analysis_partial",
        ),
        ({"extraction_outcome": "succeeded"}, "analysis_pending"),
    ],
)
def test_decorate_reports_internal_state_without_public_authorization(
    row: dict[str, object], state: str
) -> None:
    result = InternalLibrary._decorate(row)

    assert result["internal_state"] == state
    assert result["public_authorized"] is False
    assert result["claims"] == row.get("claim_extraction")
    assert result["entities"] == row.get("entity_extraction")
    if isinstance(row.get("summary"), dict):
        assert result["summary_text"] == row["summary"]["summary"]  # type: ignore[index]


def test_list_documents_caps_limit_normalizes_search_and_merges_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version_id = uuid.uuid4()
    document = {
        "document_id": DOC,
        "document_version_id": version_id,
        "extraction_outcome": "succeeded",
        "text_object_id": None,
    }
    read_db = _db(fetchall=[[document]])
    model_db = _db(
        fetchall=[
            [
                {
                    "document_version_id": version_id,
                    "result_type": "summary",
                    "result": {"summary": "Reviewed"},
                },
                {
                    "document_version_id": version_id,
                    "result_type": "claim_extraction",
                    "result": [{"claim": "A claim"}],
                },
            ],
            [],
        ]
    )
    connections = iter([read_db, model_db])
    monkeypatch.setattr(
        "uap_platform.v1.library.psycopg.connect", lambda *_args, **_kwargs: next(connections)
    )

    result = _library().list_documents(query="  public claim  ", limit=500)

    assert result["count"] == 1
    item = cast(list[dict[str, object]], result["items"])[0]
    assert item["summary_text"] == "Reviewed"
    assert item["claims"] == [{"claim": "A claim"}]
    assert item["internal_state"] == "analysis_ready"
    assert item["public_authorized"] is False
    query, params = read_db.test_cursor.execute.call_args.args
    assert "ILIKE" in query
    assert params == ("%public claim%", "%public claim%", "%public claim%", 100)


def test_list_documents_empty_does_not_open_model_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_db = _db(fetchall=[[]])
    monkeypatch.setattr(
        "uap_platform.v1.library.psycopg.connect", lambda *_args, **_kwargs: read_db
    )

    assert _library().list_documents(query="", limit=0) == {"items": [], "count": 0}
    assert read_db.cursor.call_count == 1
    assert read_db.test_cursor.execute.call_args.args[1] == (None, None, None, 1)


def test_get_document_reads_verified_source_text_and_returns_none_for_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = {
        "document_id": DOC,
        "document_version_id": VERSION,
        "extraction_outcome": "succeeded",
        "text_object_id": uuid.uuid4(),
    }
    read_db = _db(fetchone=[document])
    stored_db = _db(
        fetchone=[
            {
                "bucket_name": "derived",
                "object_key": "source",
                "content_sha256": "a" * 64,
                "byte_length": 12,
            }
        ]
    )
    model_db = _db(fetchall=[[], []])
    connections = iter([read_db, model_db, stored_db])
    monkeypatch.setattr(
        "uap_platform.v1.library.psycopg.connect", lambda *_args, **_kwargs: next(connections)
    )
    monkeypatch.setattr(
        library_module,
        "read_verified_object",
        lambda *_args: b"verified text",
    )

    result = _library().get_document(DOC)

    assert result is not None
    assert result["source_text"] == "verified text"
    assert result["public_authorized"] is False
    missing_db = _db(fetchone=[None])
    monkeypatch.setattr(
        "uap_platform.v1.library.psycopg.connect", lambda *_args, **_kwargs: missing_db
    )
    assert _library().get_document(uuid.uuid4()) is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b'{"uap_billing":{"cost_microunits":42}}', {"cost_microunits": 42}),
        (b'{"uap_billing":[]}', None),
        (b'{"other":true}', None),
        (b"not-json", None),
        (b"\xff", None),
    ],
)
def test_billing_for_object_parses_only_a_billing_object(
    monkeypatch: pytest.MonkeyPatch, payload: bytes, expected: object
) -> None:
    stored_db = _db(
        fetchone=[
            {
                "bucket_name": "model-io",
                "object_key": "response",
                "content_sha256": "b" * 64,
                "byte_length": len(payload),
            }
        ]
    )
    monkeypatch.setattr(
        "uap_platform.v1.library.psycopg.connect", lambda *_args, **_kwargs: stored_db
    )
    monkeypatch.setattr(library_module, "read_verified_object", lambda *_args: payload)

    instance = _library()
    assert instance._billing_for_object(None) is None
    assert instance._billing_for_object(uuid.uuid4()) == expected


def test_usage_aggregates_cost_and_budget_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    rows = [
        {
            "started_at": now,
            "provider": "deepseek",
            "model": "flash",
            "currency": "CNY",
            "input_tokens": 10,
            "output_tokens": 5,
            "cost_minor_units": 1,
            "response_object_id": uuid.uuid4(),
        },
        {
            "started_at": now,
            "provider": "deepseek",
            "model": "flash",
            "currency": "CNY",
            "input_tokens": 7,
            "output_tokens": 3,
            "cost_minor_units": 400,
            "response_object_id": None,
        },
    ]
    model_db = _db(fetchall=[rows])
    monkeypatch.setattr(
        "uap_platform.v1.library.psycopg.connect", lambda *_args, **_kwargs: model_db
    )
    instance = _library()
    costs = iter([{"cost_microunits": 17_000_000}, None])
    monkeypatch.setattr(instance, "_billing_for_object", lambda _object_id: next(costs))

    result = instance.usage()

    assert result["monthly_cost_microunits"] == 21_000_000
    assert result["monthly_budget_microunits"] == MONTHLY_BUDGET_MICRO_CNY
    assert result["monthly_warning_microunits"] == MONTHLY_WARNING_MICRO_CNY
    assert result["warning"] is True
    assert result["blocked"] is True
    daily = cast(list[dict[str, object]], result["daily"])
    assert len(daily) == 1
    assert daily[0]["call_count"] == 2
    assert daily[0]["input_tokens"] == 17
    assert daily[0]["output_tokens"] == 8
    assert daily[0]["cost_microunits"] == 21_000_000


def test_request_reanalysis_validates_budget_and_queues_audited_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_conn = _db(fetchone=[(VERSION,)])
    model_conn = _db(fetchone=[(1,), (PROMPT,)])
    enqueue_conn = _db(fetchone=[("job-1",)])
    connections = iter([missing_conn, model_conn, enqueue_conn])
    calls: list[str] = []

    def connect(dsn: str, *_args: object, **_kwargs: object) -> MagicMock:
        calls.append(dsn)
        return next(connections)

    monkeypatch.setattr("uap_platform.v1.library.psycopg.connect", connect)
    monkeypatch.setattr(
        _library(),
        "usage",
        lambda: {"blocked": False, "monthly_cost_microunits": 300},
    )
    instance = _library()
    monkeypatch.setattr(
        instance,
        "usage",
        lambda: {"blocked": False, "monthly_cost_microunits": 300},
    )

    result = instance.request_reanalysis(DOC, ModelTaskType.SUMMARY, " retry ", REQUEST)

    assert result == {"job_id": "job-1", "request_id": str(REQUEST), "status": "queued"}
    assert calls == [
        "postgresql://worker@db/worker",
        "postgresql://model@db/model",
        "postgresql://worker@db/worker",
    ]
    assert enqueue_conn.commit.call_count == 1
    audit_params = enqueue_conn.test_cursor.execute.call_args.args[1]
    metadata = json.loads(audit_params[6])
    assert metadata == {
        "task_type": "summary",
        "reason": "retry",
        "job_id": "job-1",
        "budget_cost_microunits_before": 300,
    }
    assert audit_params[2] == BOOTSTRAP_PRINCIPAL_ID


def test_request_reanalysis_rejects_invalid_reason_translation_and_blocked_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = _library()
    with pytest.raises(ValueError, match="reason must contain"):
        instance.request_reanalysis(DOC, ModelTaskType.SUMMARY, "  ", REQUEST)
    with pytest.raises(ValueError, match="translation is not part"):
        instance.request_reanalysis(DOC, ModelTaskType.TRANSLATION, "retry", REQUEST)
    monkeypatch.setattr(
        instance,
        "usage",
        lambda: {"blocked": True, "monthly_cost_microunits": MONTHLY_BUDGET_MICRO_CNY},
    )
    with pytest.raises(RuntimeError, match="monthly_model_budget_exhausted"):
        instance.request_reanalysis(DOC, ModelTaskType.SUMMARY, "retry", REQUEST)


@pytest.mark.parametrize(
    ("document_row", "call_count", "prompt_row", "error"),
    [
        (None, None, None, "document_not_ready"),
        ((VERSION,), (12,), None, "article_model_call_budget_exhausted"),
        ((VERSION,), (1,), None, "active_prompt_missing"),
    ],
)
def test_request_reanalysis_rejects_missing_document_budget_or_prompt(
    monkeypatch: pytest.MonkeyPatch,
    document_row: object,
    call_count: object,
    prompt_row: object,
    error: str,
) -> None:
    instance = _library()
    monkeypatch.setattr(
        instance,
        "usage",
        lambda: {"blocked": False, "monthly_cost_microunits": 0},
    )
    worker_db = _db(fetchone=[document_row])
    model_db = _db(fetchone=[call_count, prompt_row])
    connections = iter([worker_db, model_db])
    monkeypatch.setattr(
        "uap_platform.v1.library.psycopg.connect", lambda *_args, **_kwargs: next(connections)
    )

    with pytest.raises((LookupError, RuntimeError), match=error):
        instance.request_reanalysis(DOC, ModelTaskType.SUMMARY, "retry", REQUEST)
