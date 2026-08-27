"""Per-source request pacing, cooling, and health contracts."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .contracts import CollectionResult, FetchClassification


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    """Operational limits for one source, normally loaded from source config."""

    minimum_request_interval: timedelta = timedelta(0)
    cooldown: timedelta = timedelta(minutes=5)
    failure_threshold: int = 3

    def __post_init__(self) -> None:
        if self.minimum_request_interval < timedelta(0):
            raise ValueError("minimum request interval cannot be negative")
        if self.cooldown < timedelta(0):
            raise ValueError("cooldown cannot be negative")
        if self.failure_threshold < 1:
            raise ValueError("failure threshold must be positive")


@dataclass(frozen=True, slots=True)
class SourceHealth:
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    cooldown_until: datetime | None = None


class SourceCoolingDown(RuntimeError):
    """Raised when a source must not receive another request yet."""

    def __init__(self, source_id: object, until: datetime) -> None:
        self.source_id = source_id
        self.until = until
        super().__init__(f"source {source_id} is cooling down until {until.isoformat()}")


class SourceRateLimiter:
    """Thread-safe per-source pacing gate with injectable clock and sleeper."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep or time.sleep
        self._next_allowed: dict[object, datetime] = {}
        self._lock = threading.Lock()

    def wait(self, source_id: object, policy: SourcePolicy) -> datetime:
        """Block until the source interval permits a request and reserve its slot."""

        while True:
            now = self._clock()
            with self._lock:
                next_allowed = self._next_allowed.get(source_id)
                if next_allowed is None or next_allowed <= now:
                    self._next_allowed[source_id] = now + policy.minimum_request_interval
                    return now
            self._sleep(max(0.0, (next_allowed - now).total_seconds()))


class SourceHealthTracker:
    """In-process health view used by workers and deterministic policy tests."""

    def __init__(self) -> None:
        self._health: dict[object, SourceHealth] = {}
        self._lock = threading.Lock()

    def snapshot(self, source_id: object) -> SourceHealth:
        with self._lock:
            return self._health.get(source_id, SourceHealth())

    def require_available(self, source_id: object, now: datetime) -> None:
        health = self.snapshot(source_id)
        if health.cooldown_until is not None and health.cooldown_until > now:
            raise SourceCoolingDown(source_id, health.cooldown_until)

    def record(
        self,
        source_id: object,
        result: CollectionResult,
        finished_at: datetime,
        policy: SourcePolicy,
    ) -> SourceHealth:
        successful = result.classification in {
            FetchClassification.SUCCESS,
            FetchClassification.NOT_MODIFIED,
            FetchClassification.EMPTY,
        }
        with self._lock:
            previous = self._health.get(source_id, SourceHealth())
            if successful:
                current = SourceHealth(last_success_at=finished_at)
            else:
                failures = previous.consecutive_failures + 1
                cooldown_until = previous.cooldown_until
                if failures >= policy.failure_threshold:
                    cooldown_until = finished_at + policy.cooldown
                current = SourceHealth(
                    last_success_at=previous.last_success_at,
                    consecutive_failures=failures,
                    cooldown_until=cooldown_until,
                )
            self._health[source_id] = current
            return current
