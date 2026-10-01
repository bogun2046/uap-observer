from __future__ import annotations

import uuid
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, cast

import pytest

from uap_platform.v1 import worker as worker_module
from uap_platform.v1.worker import V1Worker, _model_payload_for_governance, _string, _uuid

JOB_ID = uuid.UUID("00000000-0000-7000-8000-000000009301")
ATTEMPT_ID = uuid.UUID("00000000-0000-7000-8000-000000009302")
LEASE_TOKEN = uuid.UUID("00000000-0000-7000-8000-000000009303")


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, _sql: str, _params: object = None) -> None:
        return None

    def fetchone(self) -> tuple[object, ...] | None:
        return self.connection.row


class FakeConnection:
    def __init__(self, row: tuple[object, ...] | None = None) -> None:
        self.row = row
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _worker(row: tuple[object, ...] | None) -> V1Worker:
    return V1Worker(
        cast(Any, FakeConnection(row)),
        cast(Any, FakeConnection()),
        cast(Any, object()),
        cast(Any, object()),
    )


def _claim(job_type: str, payload: object) -> tuple[object, ...]:
    return JOB_ID, ATTEMPT_ID, job_type, payload, LEASE_TOKEN


def test_payload_field_helpers_validate_required_values() -> None:
    assert _uuid({"id": str(JOB_ID)}, "id") == JOB_ID
    assert _string({"name": " worker "}, "name") == " worker "
    for payload, key in (({}, "id"), ({"id": 1}, "id"), ({"id": "bad"}, "id")):
        with pytest.raises((ValueError, AttributeError)):
            _uuid(payload, key)
    for value in (None, "", "  ", 1):
        with pytest.raises(ValueError):
            _string({"name": value}, "name")


def test_governance_payload_removes_only_document_orchestration_id() -> None:
    plain: Mapping[str, object] = {"document_version_id": str(JOB_ID), "prompt": "summary"}
    assert _model_payload_for_governance(plain) is plain
    source = {
        "document_id": str(JOB_ID),
        "document_version_id": str(ATTEMPT_ID),
        "prompt": "summary",
        "unexpected": True,
    }
    assert _model_payload_for_governance(source) == {
        "document_version_id": str(ATTEMPT_ID),
        "prompt": "summary",
        "unexpected": True,
    }


def test_worker_claim_none_commits_and_returns_without_dispatch() -> None:
    connection = FakeConnection()
    instance = V1Worker(
        cast(Any, connection), cast(Any, FakeConnection()), cast(Any, object()), cast(Any, object())
    )
    assert instance.run_once() is None
    assert connection.commits == 1


@pytest.mark.parametrize(
    ("job_type", "payload", "method_name", "expected_label"),
    [
        (
            "fetch_source",
            {"payload_schema_version": "v1.article-fetch.v1"},
            "_fetch_document",
            "fetch_source",
        ),
        (
            "fetch_source",
            {"payload_schema_version": "v1.fetch.v1"},
            "_fetch",
            "fetch_source",
        ),
        ("extract_document", {}, "_extract", "extract_document"),
        ("analyze_document", {}, "_analyze", "analyze_document"),
    ],
)
def test_worker_dispatches_claimed_jobs_to_the_matching_handler(
    monkeypatch: pytest.MonkeyPatch,
    job_type: str,
    payload: object,
    method_name: str,
    expected_label: str,
) -> None:
    instance = _worker(_claim(job_type, payload))
    dispatched: list[tuple[object, ...]] = []
    monkeypatch.setattr(instance, method_name, lambda *args: dispatched.append(args))

    assert instance.run_once() == expected_label
    assert len(dispatched) == 1
    assert dispatched[0][:3] == (JOB_ID, ATTEMPT_ID, LEASE_TOKEN)
    assert dispatched[0][3] == payload


@pytest.mark.parametrize(
    ("job_type", "payload", "error_code"),
    [
        ("fetch_source", None, "invalid_v1_payload"),
        ("future_job", {}, "unsupported_v1_job"),
    ],
)
def test_worker_finishes_unusable_claims_fail_closed(
    monkeypatch: pytest.MonkeyPatch, job_type: str, payload: object, error_code: str
) -> None:
    instance = _worker(_claim(job_type, payload))
    finished: list[tuple[object, ...]] = []
    monkeypatch.setattr(instance, "_finish_invalid", lambda *args: finished.append(args))

    assert instance.run_once() == job_type
    assert finished == [(JOB_ID, ATTEMPT_ID, LEASE_TOKEN, error_code)]


def test_worker_main_requires_database_urls_and_model_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UAP_V1_WORKER_DATABASE_URL", raising=False)
    monkeypatch.delenv("UAP_V1_MODEL_DATABASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="are required"):
        worker_module.main()


def test_worker_main_rejects_unapproved_budget_before_opening_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnapprovedConfig:
        deepseek_monthly_budget_cny = Decimal("21.00")

    monkeypatch.setenv("UAP_V1_WORKER_DATABASE_URL", "postgresql://worker@db/uap")
    monkeypatch.setenv("UAP_V1_MODEL_DATABASE_URL", "postgresql://model@db/uap")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(
        "uap_platform.v1.worker.load_v1_config",
        lambda _path=None: UnapprovedConfig(),
    )

    with pytest.raises(SystemExit, match="unapproved DeepSeek budget"):
        worker_module.main()


def test_worker_fetch_rejects_inactive_source_type(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = _worker(None)
    finished: list[tuple[object, ...]] = []
    monkeypatch.setattr(instance, "_finish_invalid", lambda *args: finished.append(args))

    instance._fetch(
        JOB_ID,
        ATTEMPT_ID,
        LEASE_TOKEN,
        {"payload_schema_version": "v1.fetch.v1", "source_type": "web"},
    )

    assert finished == [(JOB_ID, ATTEMPT_ID, LEASE_TOKEN, "web_source_not_active_v1_1")]


def test_build_client_from_environment_wraps_worker_url_as_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import SecretStr

    received: list[object] = []
    sentinel = object()

    class FakeSettings:
        def __init__(self, *, database_url: SecretStr) -> None:
            received.append(database_url)

    def build_client(settings: object) -> object:
        received.append(settings)
        return sentinel

    monkeypatch.setattr("uap_platform.config.Settings", FakeSettings)
    monkeypatch.setattr(worker_module, "build_client", build_client)

    result = worker_module.build_client_from_environment("postgresql://worker@db/uap")

    assert result is sentinel
    assert isinstance(received[0], SecretStr)
    assert received[0].get_secret_value() == "postgresql://worker@db/uap"
    assert isinstance(received[1], FakeSettings)
