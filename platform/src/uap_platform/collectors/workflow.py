"""Source-run lifecycle orchestration for collectors."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol

from .contracts import CollectionResult, FetchClassification, FetchResponse, NormalizedItem
from .policy import SourceCoolingDown, SourceHealthTracker, SourcePolicy, SourceRateLimiter
from .rss import RssCollector


class SourceRunStore(Protocol):
    def start_source_run(
        self,
        source_id: uuid.UUID,
        job_id: uuid.UUID,
        run_key: str,
        started_at: datetime,
        source_config_version_id: uuid.UUID,
        attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
    ) -> uuid.UUID: ...

    def persist_items(
        self,
        source_id: uuid.UUID,
        source_run_id: uuid.UUID,
        items: tuple[NormalizedItem, ...],
        seen_at: datetime,
    ) -> int: ...

    def finish_source_run_and_job(
        self,
        run_id: uuid.UUID,
        job_id: uuid.UUID,
        attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
        result: CollectionResult,
        finished_at: datetime,
    ) -> None: ...

    def fail_source_run_and_job(
        self,
        run_id: uuid.UUID,
        job_id: uuid.UUID,
        attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
        result: CollectionResult,
        finished_at: datetime,
    ) -> None: ...


class RssSourceRunRunner:
    """Run an RSS collector while keeping its source-run lifecycle explicit."""

    def __init__(
        self,
        fetch: Callable[[str, Mapping[str, str]], FetchResponse],
        store: SourceRunStore,
        clock: Callable[[], datetime] | None = None,
        rate_limiter: SourceRateLimiter | None = None,
        health_tracker: SourceHealthTracker | None = None,
        source_policy: SourcePolicy | None = None,
    ) -> None:
        self._fetch = fetch
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._rate_limiter = rate_limiter
        self._health_tracker = health_tracker
        self._source_policy = source_policy or SourcePolicy()

    def run(
        self,
        source_id: uuid.UUID,
        job_id: uuid.UUID,
        run_key: str,
        source_url: str,
        *,
        attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
        etag: str | None = None,
        last_modified: str | None = None,
        source_config_version_id: uuid.UUID,
    ) -> CollectionResult:
        started_at = self._clock()
        run_id = self._store.start_source_run(
            source_id,
            job_id,
            run_key,
            started_at,
            source_config_version_id,
            attempt_id,
            lease_token,
        )
        collector = RssCollector(
            self._fetch,
            lambda items: self._store.persist_items(
                source_id, run_id, items, self._clock()
            ),
        )
        try:
            if self._health_tracker is not None:
                self._health_tracker.require_available(source_id, self._clock())
            reserve_request = getattr(self._store, "reserve_source_request", None)
            if callable(reserve_request):
                while True:
                    wait_seconds = reserve_request(source_id, self._source_policy, self._clock())
                    if wait_seconds <= 0:
                        break
                    time.sleep(wait_seconds)
            if self._rate_limiter is not None:
                self._rate_limiter.wait(source_id, self._source_policy)
            result = collector.collect(
                source_url,
                etag=etag,
                last_modified=last_modified,
            )
            if self._health_tracker is not None:
                self._health_tracker.record(
                    source_id, result, self._clock(), self._source_policy
                )
        except Exception as error:
            if isinstance(error, SourceCoolingDown):
                failure = CollectionResult(
                    classification=FetchClassification.TRANSIENT_FAILURE,
                    http_status=429,
                    fetched_count=0,
                    error_code="cooldown",
                    error_summary=str(error),
                )
                self._store.fail_source_run_and_job(
                    run_id,
                    job_id,
                    attempt_id,
                    lease_token,
                    failure,
                    self._clock(),
                )
                return failure
            failure = CollectionResult(
                classification=FetchClassification.TRANSIENT_FAILURE,
                http_status=599,
                fetched_count=0,
                error_code="collector_error",
                error_summary=str(error),
            )
            if self._health_tracker is not None:
                self._health_tracker.record(
                    source_id, failure, self._clock(), self._source_policy
                )
            self._store.fail_source_run_and_job(
                run_id,
                job_id,
                attempt_id,
                lease_token,
                failure,
                self._clock(),
            )
            raise
        self._store.finish_source_run_and_job(
            run_id,
            job_id,
            attempt_id,
            lease_token,
            result,
            self._clock(),
        )
        return result
