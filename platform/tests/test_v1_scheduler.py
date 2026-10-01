from __future__ import annotations

import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from psycopg import Connection

from uap_platform.v1 import scheduler
from uap_platform.v1.config import V1Config, load_v1_config


class ScriptedCursor:
    def __init__(self, rows: list[tuple[object, ...] | None]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, object]] = []

    def __enter__(self) -> ScriptedCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: object = None) -> None:
        self.executed.append((query, params))

    def fetchone(self) -> tuple[object, ...] | None:
        return self.rows.pop(0)


class ScriptedConnection:
    def __init__(self, rows: list[tuple[object, ...] | None]) -> None:
        self.query = ScriptedCursor(rows)
        self.commits = 0

    def __enter__(self) -> ScriptedConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> ScriptedCursor:
        return self.query

    def commit(self) -> None:
        self.commits += 1


def test_schedule_window_uses_utc_bucket_without_mutating_observed_time() -> None:
    observed = datetime(2026, 9, 28, 12, 37, 49, 123456, tzinfo=UTC)

    assert scheduler.schedule_window(observed, 15) == datetime(
        2026, 9, 28, 12, 30, tzinfo=UTC
    )
    assert observed.minute == 37
    assert scheduler.schedule_window(observed, 60) == datetime(
        2026, 9, 28, 12, 0, tzinfo=UTC
    )


def test_enqueue_due_builds_idempotent_job_and_commits() -> None:
    config = load_v1_config()
    source = config.v1_1_source
    source_id = uuid.uuid4()
    config_version_id = uuid.uuid4()
    job_id = uuid.uuid4()
    connection = ScriptedConnection(
        [
            (source_id, config_version_id, {"fetch_url": source.fetch_url, "v1_1_enabled": True}),
            (job_id,),
        ]
    )
    observed = datetime(2026, 9, 28, 12, 37, tzinfo=UTC)

    queued = scheduler.enqueue_due(cast(Connection[Any], connection), config, observed)

    assert queued == (str(job_id),)
    assert connection.commits == 1
    assert len(connection.query.executed) == 2
    payload_json, key, run_after = cast(
        tuple[str, str, datetime], connection.query.executed[1][1]
    )
    payload = json.loads(payload_json)
    assert payload == {
        "source_id": str(source_id),
        "source_config_version_id": str(config_version_id),
        "source_type": source.source_type,
        "source_url": source.fetch_url,
        "run_key": key,
        "payload_schema_version": "v1.fetch.v1",
    }
    assert key == f"v1-fetch:{source_id}:{observed.replace(minute=30).isoformat()}"
    assert "0::smallint" in connection.query.executed[1][0]
    assert run_after == observed.replace(minute=30)


@pytest.mark.parametrize(
    "stored",
    [
        {"fetch_url": "https://wrong.example/feed.xml", "v1_1_enabled": True},
        {"fetch_url": "https://feed.example/rss.xml", "v1_1_enabled": False},
    ],
)
def test_enqueue_due_fails_closed_for_missing_or_unapproved_source(
    stored: dict[str, object],
) -> None:
    config = load_v1_config()
    connection = ScriptedConnection([(uuid.uuid4(), uuid.uuid4(), stored)])

    with pytest.raises(RuntimeError, match="stored source configuration"):
        scheduler.enqueue_due(
            cast(Connection[Any], connection), config, datetime(2026, 9, 28, tzinfo=UTC)
        )

    assert connection.commits == 0
    assert len(connection.query.executed) == 1


def test_enqueue_due_fails_when_source_is_not_bootstrapped() -> None:
    connection = ScriptedConnection([None])

    with pytest.raises(RuntimeError, match="not bootstrapped or is disabled"):
        scheduler.enqueue_due(
            cast(Connection[Any], connection),
            load_v1_config(),
            datetime(2026, 9, 28, tzinfo=UTC),
        )

    assert connection.commits == 0


def test_main_requires_credentialed_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UAP_V1_SCHEDULER_DATABASE_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["scheduler"])

    with pytest.raises(SystemExit, match="UAP_V1_SCHEDULER_DATABASE_URL is required"):
        scheduler.main()


def test_main_enqueues_at_explicit_time_and_emits_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config: V1Config = load_v1_config()
    source = config.v1_1_source
    job_id = uuid.uuid4()
    connection = ScriptedConnection(
        [
            (uuid.uuid4(), uuid.uuid4(), {"fetch_url": source.fetch_url, "v1_1_enabled": True}),
            (job_id,),
        ]
    )
    monkeypatch.setenv(
        "UAP_V1_SCHEDULER_DATABASE_URL", "postgresql+psycopg://scheduler@db/uap"
    )
    monkeypatch.setattr(sys, "argv", ["scheduler", "--at", "2026-09-28T12:37:00+00:00"])
    monkeypatch.setattr(scheduler, "load_v1_config", lambda _path: config)
    monkeypatch.setattr("uap_platform.v1.scheduler.psycopg.connect", lambda _dsn: connection)

    scheduler.main()

    output = json.loads(capsys.readouterr().out)
    assert output == {
        "enqueued_job_ids": [str(job_id)],
        "next_window_after": str(timedelta(minutes=config.schedule_interval_minutes)),
    }
