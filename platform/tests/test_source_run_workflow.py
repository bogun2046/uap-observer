from __future__ import annotations

import uuid
from datetime import UTC, datetime

from uap_platform.collectors import (
    CollectionResult,
    FetchClassification,
    FetchResponse,
    NormalizedItem,
    RssSourceRunRunner,
)


class FakeStore:
    def __init__(self) -> None:
        self.run_id = uuid.UUID("00000000-0000-7000-8000-000000000001")
        self.raise_on_persist = False
        self.started: tuple[object, ...] | None = None
        self.persisted: tuple[object, ...] | None = None
        self.finished: tuple[object, ...] | None = None

    def start_source_run(
        self,
        source_id: uuid.UUID,
        job_id: uuid.UUID,
        run_key: str,
        started_at: datetime,
        source_config_version_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        self.started = (source_id, job_id, run_key, started_at, source_config_version_id)
        return self.run_id

    def persist_items(
        self,
        source_id: uuid.UUID,
        source_run_id: uuid.UUID,
        items: tuple[NormalizedItem, ...],
        seen_at: datetime,
    ) -> int:
        if self.raise_on_persist:
            raise RuntimeError("persist failed")
        self.persisted = (source_id, source_run_id, items, seen_at)
        return len(items)

    def finish_source_run(
        self, run_id: uuid.UUID, result: CollectionResult, finished_at: datetime
    ) -> None:
        self.finished = (run_id, result, finished_at)

    def fail_source_run(
        self, run_id: uuid.UUID, result: CollectionResult, finished_at: datetime
    ) -> None:
        self.finished = (run_id, result, finished_at)


def test_runner_records_start_persist_and_finish_in_order() -> None:
    source_id = uuid.UUID("00000000-0000-7000-8000-000000000010")
    job_id = uuid.UUID("00000000-0000-7000-8000-000000000011")
    moments = iter(
        (
            datetime(2026, 8, 16, 1, 0, tzinfo=UTC),
            datetime(2026, 8, 16, 1, 0, 1, tzinfo=UTC),
            datetime(2026, 8, 16, 1, 0, 2, tzinfo=UTC),
        )
    )
    store = FakeStore()
    result = RssSourceRunRunner(
        lambda _url, _headers: FetchResponse(
            200,
            b"<rss><channel><item><guid>x</guid><title>Story</title>"
            b"<link>https://example.test/story</link></item></channel></rss>",
        ),
        store,
        clock=lambda: next(moments),
    ).run(source_id, job_id, "source-run-1", "https://example.test/feed")

    assert result.classification is FetchClassification.SUCCESS
    assert store.started == (
        source_id,
        job_id,
        "source-run-1",
        datetime(2026, 8, 16, 1, 0, tzinfo=UTC),
        None,
    )
    assert store.persisted is not None
    assert store.finished is not None
    assert store.finished[0] == store.run_id
    assert store.finished[2] == datetime(2026, 8, 16, 1, 0, 2, tzinfo=UTC)


def test_runner_finishes_timeout_as_transient_failure() -> None:
    store = FakeStore()
    result = RssSourceRunRunner(
        lambda _url, _headers: (_ for _ in ()).throw(TimeoutError("timed out")),
        store,
        clock=lambda: datetime(2026, 8, 16, tzinfo=UTC),
    ).run(uuid.uuid4(), uuid.uuid4(), "source-run-timeout", "https://example.test/feed")

    assert result.classification is FetchClassification.TRANSIENT_FAILURE
    assert store.finished is not None
    finished_result = store.finished[1]
    assert isinstance(finished_result, CollectionResult)
    assert finished_result.error_code == "timeout"


def test_runner_records_persist_failure_before_reraising() -> None:
    store = FakeStore()
    store.raise_on_persist = True

    try:
        RssSourceRunRunner(
            lambda _url, _headers: FetchResponse(
                200,
                b"<rss><channel><item><guid>x</guid><title>Story</title>"
                b"<link>https://example.test/story</link></item></channel></rss>",
            ),
            store,
            clock=lambda: datetime(2026, 8, 16, tzinfo=UTC),
        ).run(uuid.uuid4(), uuid.uuid4(), "source-run-persist-failure", "https://example.test/feed")
    except RuntimeError as error:
        assert str(error) == "persist failed"
    else:
        raise AssertionError("persist failure was not re-raised")

    assert store.finished is not None
    failed_result = store.finished[1]
    assert isinstance(failed_result, CollectionResult)
    assert failed_result.error_code == "collector_error"
