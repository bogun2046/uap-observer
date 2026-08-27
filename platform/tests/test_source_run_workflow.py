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
        self.job_finished: tuple[object, ...] | None = None

    def start_source_run(
        self,
        source_id: uuid.UUID,
        job_id: uuid.UUID,
        run_key: str,
        started_at: datetime,
        source_config_version_id: uuid.UUID,
        attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
    ) -> uuid.UUID:
        self.started = (
            source_id,
            job_id,
            run_key,
            started_at,
            source_config_version_id,
            attempt_id,
            lease_token,
        )
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

    def finish_source_run_and_job(
        self,
        run_id: uuid.UUID,
        job_id: uuid.UUID,
        attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
        result: CollectionResult,
        finished_at: datetime,
    ) -> None:
        self.finished = (run_id, result, finished_at)
        self.job_finished = (job_id, attempt_id, lease_token, result)

    def fail_source_run_and_job(
        self,
        run_id: uuid.UUID,
        job_id: uuid.UUID,
        attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
        result: CollectionResult,
        finished_at: datetime,
    ) -> None:
        self.finished = (run_id, result, finished_at)
        self.job_finished = (job_id, attempt_id, lease_token, result)


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
    attempt_id = uuid.UUID("00000000-0000-7000-8000-000000000012")
    lease_token = uuid.UUID("00000000-0000-7000-8000-000000000013")
    config_id = uuid.UUID("00000000-0000-7000-8000-000000000014")
    result = RssSourceRunRunner(
        lambda _url, _headers: FetchResponse(
            200,
            b"<rss><channel><item><guid>x</guid><title>Story</title>"
            b"<link>https://example.test/story</link></item></channel></rss>",
        ),
        store,
        clock=lambda: next(moments),
    ).run(
        source_id,
        job_id,
        "source-run-1",
        "https://example.test/feed",
        attempt_id=attempt_id,
        lease_token=lease_token,
        source_config_version_id=config_id,
    )

    assert result.classification is FetchClassification.SUCCESS
    assert store.started == (
        source_id,
        job_id,
        "source-run-1",
        datetime(2026, 8, 16, 1, 0, tzinfo=UTC),
        config_id,
        attempt_id,
        lease_token,
    )
    assert store.persisted is not None
    assert store.finished is not None
    assert store.finished[0] == store.run_id
    assert store.finished[2] == datetime(2026, 8, 16, 1, 0, 2, tzinfo=UTC)
    assert store.job_finished is not None
    assert store.job_finished[:3] == (job_id, attempt_id, lease_token)


def test_runner_finishes_timeout_as_transient_failure() -> None:
    store = FakeStore()
    job_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    lease_token = uuid.uuid4()
    config_id = uuid.uuid4()
    result = RssSourceRunRunner(
        lambda _url, _headers: (_ for _ in ()).throw(TimeoutError("timed out")),
        store,
        clock=lambda: datetime(2026, 8, 16, tzinfo=UTC),
    ).run(
        uuid.uuid4(),
        job_id,
        "source-run-timeout",
        "https://example.test/feed",
        attempt_id=attempt_id,
        lease_token=lease_token,
        source_config_version_id=config_id,
    )

    assert result.classification is FetchClassification.TRANSIENT_FAILURE
    assert store.finished is not None
    finished_result = store.finished[1]
    assert isinstance(finished_result, CollectionResult)
    assert finished_result.error_code == "timeout"
    assert store.job_finished is not None
    assert store.job_finished[:3] == (job_id, attempt_id, lease_token)


def test_runner_records_persist_failure_before_reraising() -> None:
    store = FakeStore()
    store.raise_on_persist = True
    job_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    lease_token = uuid.uuid4()
    config_id = uuid.uuid4()

    try:
        RssSourceRunRunner(
            lambda _url, _headers: FetchResponse(
                200,
                b"<rss><channel><item><guid>x</guid><title>Story</title>"
                b"<link>https://example.test/story</link></item></channel></rss>",
            ),
            store,
            clock=lambda: datetime(2026, 8, 16, tzinfo=UTC),
        ).run(
            uuid.uuid4(),
            job_id,
            "source-run-persist-failure",
            "https://example.test/feed",
            attempt_id=attempt_id,
            lease_token=lease_token,
            source_config_version_id=config_id,
        )
    except RuntimeError as error:
        assert str(error) == "persist failed"
    else:
        raise AssertionError("persist failure was not re-raised")

    assert store.finished is not None
    failed_result = store.finished[1]
    assert isinstance(failed_result, CollectionResult)
    assert failed_result.error_code == "collector_error"
    assert store.job_finished is not None
    assert store.job_finished[:3] == (job_id, attempt_id, lease_token)
