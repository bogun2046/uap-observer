"""Thin Publisher client for the WP10.3 owner-controlled SQL protocol.

The service deliberately does not commit.  ``PublisherLoop`` owns the
transaction boundary so claiming, applying, and failing an event have an
explicit commit/rollback decision at the process boundary.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from psycopg import Connection

_RETRYABLE_CODES = frozenset(
    {
        "publication_dependency_not_ready",
        "publication_database_unavailable",
        "publication_lease_lost",
    }
)
_TERMINAL_CODES = frozenset(
    {
        "publication_event_aggregate_mismatch",
        "publication_event_schema_unsupported",
        "publication_grant_missing",
        "publication_manifest_invalid",
        "publication_payload_hash_mismatch",
    }
)
_SUMMARY_BY_CODE = {
    "publication_dependency_not_ready": "publication dependency is not ready",
    "publication_database_unavailable": "publication database dependency failed",
    "publication_event_aggregate_mismatch": "publication event aggregate is invalid",
    "publication_event_schema_unsupported": "publication event schema is unsupported",
    "publication_grant_missing": "publication grant is missing",
    "publication_lease_lost": "publication lease is no longer valid",
    "publication_manifest_invalid": "publication manifest is invalid",
    "publication_payload_hash_mismatch": "publication payload integrity check failed",
    "publication_retry_exhausted": "publication retry limit was exhausted",
}


@dataclass(frozen=True)
class PublicationClaim:
    """A leased event returned by the dedicated claim function."""

    event_id: uuid.UUID
    causation_job_id: uuid.UUID | None
    aggregate_type: str
    aggregate_id: uuid.UUID
    event_type: str
    event_key: str
    payload: dict[str, Any]
    attempt_no: int
    lease_token: uuid.UUID
    lease_expires_at: datetime


@dataclass(frozen=True)
class PublicationApplyResult:
    """The redacted result of an atomic projection plus outbox ack."""

    event_id: uuid.UUID
    published_at: datetime
    projection_digest: str
    attempt_no: int
    replayed: bool


@dataclass(frozen=True)
class PublicationFailureResult:
    """The redacted result of retry or terminal failure handling."""

    event_id: uuid.UUID
    attempt_no: int
    outcome: str
    available_at: datetime | None
    terminal_at: datetime | None
    replayed: bool


def stable_failure_code(error: BaseException) -> str:
    """Map a database exception to a registered, non-sensitive code."""

    diagnostic = getattr(error, "diag", None)
    primary = getattr(diagnostic, "message_primary", None)
    if isinstance(primary, str):
        if primary in _RETRYABLE_CODES or primary in _TERMINAL_CODES:
            return primary
        if primary == "publication_retry_exhausted":
            return primary
    sqlstate = getattr(error, "sqlstate", None)
    if sqlstate in {"40001", "40P01", "08000", "08003", "08006"}:
        return "publication_database_unavailable"
    return "publication_database_unavailable"


def stable_failure_summary(error_code: str) -> str:
    """Return the fixed sanitized summary registered for ``error_code``."""

    return _SUMMARY_BY_CODE.get(error_code, "publication database dependency failed")


class PublicationService:
    """Execute only the three dedicated WP10 Publisher SQL functions.

    No method performs direct DML and no method commits.  The latter is
    intentional: the dedicated loop controls lease release and rollback.
    """

    def __init__(
        self,
        connection: Connection[object],
        *,
        dispatcher_id: str,
        lease_seconds: int = 60,
        batch_limit: int = 10,
    ) -> None:
        self._connection = connection
        self.dispatcher_id = dispatcher_id
        self.lease_seconds = lease_seconds
        self.batch_limit = batch_limit

    @property
    def connection(self) -> Connection[object]:
        """Expose the transaction owner without exposing any extra SQL path."""

        return self._connection

    def claim(self) -> list[PublicationClaim]:
        """Lease a batch of document/entity/claim v2 events."""

        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT event_id, causation_job_id, aggregate_type, aggregate_id,
                       event_type, event_key, payload, attempt_no,
                       lease_token, lease_expires_at
                  FROM ops.claim_publication_outbox(%s, %s, %s)
                """,
                (self.dispatcher_id, self.lease_seconds, self.batch_limit),
            )
            rows = cursor.fetchall()
        return [self._claim(cast(tuple[Any, ...], row)) for row in rows]

    def apply(self, claim: PublicationClaim) -> PublicationApplyResult:
        """Atomically apply one projection and acknowledge its event."""

        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM ops.apply_publication_event(%s, %s)",
                (claim.event_id, claim.lease_token),
            )
            row = cursor.fetchone()
        if row is None:
            raise RuntimeError("publication apply returned no result")
        apply_row = cast(tuple[Any, ...], row)
        return PublicationApplyResult(
            event_id=cast(uuid.UUID, apply_row[0]),
            published_at=cast(datetime, apply_row[1]),
            projection_digest=str(apply_row[2]),
            attempt_no=int(apply_row[3]),
            replayed=bool(apply_row[4]),
        )

    def fail(
        self,
        claim: PublicationClaim,
        *,
        error_code: str,
        retry_delay_seconds: int = 0,
        terminal: bool = False,
    ) -> PublicationFailureResult:
        """Record a sanitized retryable or terminal outcome for one lease."""

        summary = stable_failure_summary(error_code)
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                  FROM ops.fail_publication_event(
                      %s, %s, %s, %s, %s, %s
                  )
                """,
                (
                    claim.event_id,
                    claim.lease_token,
                    error_code,
                    summary,
                    retry_delay_seconds,
                    terminal,
                ),
            )
            row = cursor.fetchone()
        if row is None:
            raise RuntimeError("publication failure returned no result")
        fail_row = cast(tuple[Any, ...], row)
        return PublicationFailureResult(
            event_id=cast(uuid.UUID, fail_row[0]),
            attempt_no=int(fail_row[1]),
            outcome=str(fail_row[2]),
            available_at=cast(datetime | None, fail_row[3]),
            terminal_at=cast(datetime | None, fail_row[4]),
            replayed=bool(fail_row[5]),
        )

    @staticmethod
    def _claim(row: tuple[Any, ...]) -> PublicationClaim:
        if len(row) != 10 or not isinstance(row[6], dict):
            raise RuntimeError("publication claim returned an invalid shape")
        return PublicationClaim(
            event_id=cast(uuid.UUID, row[0]),
            causation_job_id=cast(uuid.UUID | None, row[1]),
            aggregate_type=str(row[2]),
            aggregate_id=cast(uuid.UUID, row[3]),
            event_type=str(row[4]),
            event_key=str(row[5]),
            payload=cast(dict[str, Any], row[6]),
            attempt_no=int(row[7]),
            lease_token=cast(uuid.UUID, row[8]),
            lease_expires_at=cast(datetime, row[9]),
        )
