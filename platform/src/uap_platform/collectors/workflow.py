"""Source-run lifecycle orchestration for collectors."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol

from .contracts import CollectionResult, FetchClassification, FetchResponse, NormalizedItem
from .rss import RssCollector


class SourceRunStore(Protocol):
    def start_source_run(
        self, source_id: uuid.UUID, job_id: uuid.UUID, run_key: str, started_at: datetime
    ) -> uuid.UUID: ...

    def persist_items(
        self,
        source_id: uuid.UUID,
        source_run_id: uuid.UUID,
        items: tuple[NormalizedItem, ...],
        seen_at: datetime,
    ) -> int: ...

    def finish_source_run(
        self, run_id: uuid.UUID, result: CollectionResult, finished_at: datetime
    ) -> None: ...

    def fail_source_run(
        self, run_id: uuid.UUID, result: CollectionResult, finished_at: datetime
    ) -> None: ...


class RssSourceRunRunner:
    """Run an RSS collector while keeping its source-run lifecycle explicit."""

    def __init__(
        self,
        fetch: Callable[[str, Mapping[str, str]], FetchResponse],
        store: SourceRunStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._fetch = fetch
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    def run(
        self,
        source_id: uuid.UUID,
        job_id: uuid.UUID,
        run_key: str,
        source_url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> CollectionResult:
        started_at = self._clock()
        run_id = self._store.start_source_run(source_id, job_id, run_key, started_at)
        collector = RssCollector(
            self._fetch,
            lambda items: self._store.persist_items(
                source_id, run_id, items, self._clock()
            ),
        )
        try:
            result = collector.collect(
                source_url,
                etag=etag,
                last_modified=last_modified,
            )
        except Exception as error:
            failure = CollectionResult(
                classification=FetchClassification.TERMINAL_FAILURE,
                http_status=599,
                fetched_count=0,
                error_code="collector_error",
                error_summary=str(error),
            )
            self._store.fail_source_run(run_id, failure, self._clock())
            raise
        self._store.finish_source_run(run_id, result, self._clock())
        return result
