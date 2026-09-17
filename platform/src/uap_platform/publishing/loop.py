"""Dedicated non-HTTP Publisher loop for WP10.3."""

from __future__ import annotations

import argparse
import logging
import os
import uuid
from threading import Event

import psycopg

from .service import PublicationService, stable_failure_code

LOGGER = logging.getLogger(__name__)


class PublisherLoop:
    """Own commits around the dedicated SQL service calls."""

    def __init__(
        self,
        service: PublicationService,
        *,
        idle_seconds: float = 0.5,
    ) -> None:
        self._service = service
        self._connection = service.connection
        self._idle_seconds = idle_seconds

    @property
    def connection(self) -> psycopg.Connection[object]:
        """Expose the loop's transaction owner for orderly shutdown."""

        return self._connection

    def run_once(self) -> int:
        """Claim and process one batch; return the number of claimed events."""

        claims = self._service.claim()
        self._connection.commit()
        for claim in claims:
            try:
                result = self._service.apply(claim)
                self._connection.commit()
                LOGGER.info(
                    "publication event applied event_id=%s attempt_no=%d replayed=%s",
                    claim.event_id,
                    result.attempt_no,
                    result.replayed,
                )
            except Exception as error:
                self._connection.rollback()
                error_code = stable_failure_code(error)
                try:
                    failure = self._service.fail(
                        claim,
                        error_code=error_code,
                        terminal=error_code
                        in {
                            "publication_event_aggregate_mismatch",
                            "publication_event_schema_unsupported",
                            "publication_grant_missing",
                            "publication_manifest_invalid",
                            "publication_payload_hash_mismatch",
                            "publication_retry_exhausted",
                        },
                    )
                    self._connection.commit()
                except Exception as failure_error:
                    self._connection.rollback()
                    if stable_failure_code(failure_error) == "publication_lease_lost":
                        LOGGER.warning(
                            "publication lease expired before failure could be recorded "
                            "event_id=%s",
                            claim.event_id,
                        )
                        continue
                    raise
                LOGGER.warning(
                    "publication event failed event_id=%s attempt_no=%d outcome=%s",
                    claim.event_id,
                    failure.attempt_no,
                    failure.outcome,
                )
        return len(claims)

    def run_forever(self, stop: Event) -> None:
        """Poll until ``stop`` is set; no HTTP server or generic ack path."""

        while not stop.is_set():
            if self.run_once() == 0:
                stop.wait(self._idle_seconds)


def build_loop(database_url: str, *, dispatcher_id: str, lease_seconds: int) -> PublisherLoop:
    """Create the dedicated loop using only the Publisher database role."""

    connection = psycopg.connect(database_url)
    service = PublicationService(
        connection,
        dispatcher_id=dispatcher_id,
        lease_seconds=lease_seconds,
    )
    return PublisherLoop(service)


def main() -> None:
    """Run one or more dedicated Publisher polling iterations."""

    parser = argparse.ArgumentParser(description="Run the WP10.3 document/entity/claim Publisher")
    parser.add_argument("--dispatcher-id", default=f"publisher-{uuid.uuid4().hex[:12]}")
    parser.add_argument("--lease-seconds", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    database_url = os.environ.get("UAP_DATABASE_URL")
    if not database_url:
        parser.error("UAP_DATABASE_URL is required for the Publisher loop")
    loop = build_loop(
        database_url.replace("postgresql+psycopg://", "postgresql://", 1),
        dispatcher_id=args.dispatcher_id,
        lease_seconds=args.lease_seconds,
    )
    try:
        if args.once:
            loop.run_once()
        else:
            loop.run_forever(Event())
    finally:
        loop.connection.close()
