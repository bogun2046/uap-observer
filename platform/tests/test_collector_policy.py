from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from uap_platform.collectors import (
    CollectionResult,
    FetchClassification,
    SourceCoolingDown,
    SourceHealthTracker,
    SourcePolicy,
    SourceRateLimiter,
)


def test_rate_limiter_enforces_per_source_interval() -> None:
    current = [datetime(2026, 8, 17, tzinfo=UTC)]
    sleeps: list[float] = []

    def clock() -> datetime:
        return current[0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        current[0] += timedelta(seconds=seconds)

    limiter = SourceRateLimiter(clock=clock, sleep=sleep)
    policy = SourcePolicy(minimum_request_interval=timedelta(seconds=5))

    limiter.wait("source-a", policy)
    limiter.wait("source-a", policy)

    assert sleeps == [5.0]


def test_health_tracker_cools_after_threshold_and_recovers_on_success() -> None:
    tracker = SourceHealthTracker()
    policy = SourcePolicy(cooldown=timedelta(seconds=30), failure_threshold=2)
    failed = CollectionResult(FetchClassification.TRANSIENT_FAILURE, 503, 1)
    first = datetime(2026, 8, 17, tzinfo=UTC)

    tracker.record("source-a", failed, first, policy)
    health = tracker.record("source-a", failed, first + timedelta(seconds=1), policy)

    assert health.consecutive_failures == 2
    assert health.cooldown_until == first + timedelta(seconds=31)
    with pytest.raises(SourceCoolingDown):
        tracker.require_available("source-a", first + timedelta(seconds=2))

    recovered = tracker.record(
        "source-a",
        CollectionResult(FetchClassification.SUCCESS, 200, 1),
        first + timedelta(seconds=40),
        policy,
    )
    assert recovered.consecutive_failures == 0
    assert recovered.cooldown_until is None
