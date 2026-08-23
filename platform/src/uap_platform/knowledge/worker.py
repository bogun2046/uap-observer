"""Production worker consumer that activates resolve_claims after the handler exists."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, cast

from psycopg import Connection

from uap_platform.config import load_settings
from uap_platform.object_registry import ObjectClient
from uap_platform.object_store_init import build_client

from .handler import ResolveClaimsHandler
from .job_types import claimable_job_types
from .payload import KnowledgePayloadError
from .reasons import KNOWLEDGE_PAYLOAD_MISMATCH

_DISPATCHABLE_JOB_TYPES = frozenset({"resolve_claims"})


class ResolveClaimsWorker:
    """Claim and dispatch knowledge jobs. Production default includes resolve_claims."""

    def __init__(
        self,
        connection: Connection[object],
        object_client: ObjectClient,
        *,
        worker_id: str,
        claims_handler_active: bool = True,
        lease_seconds: int = 60,
    ) -> None:
        self._connection = connection
        self._object_client = object_client
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._activated_types = claimable_job_types(
            claims_handler_active=claims_handler_active
        )
        self._claim_job_types = tuple(
            job_type
            for job_type in self._activated_types
            if job_type in _DISPATCHABLE_JOB_TYPES
        )

    @classmethod
    def from_settings(
        cls,
        connection: Connection[object],
        *,
        worker_id: str,
        claims_handler_active: bool = True,
        lease_seconds: int = 60,
    ) -> ResolveClaimsWorker:
        """Production constructor: MinIO client from process settings, handler active."""

        return cls(
            connection,
            cast(ObjectClient, build_client(load_settings())),
            worker_id=worker_id,
            claims_handler_active=claims_handler_active,
            lease_seconds=lease_seconds,
        )

    @property
    def job_types(self) -> tuple[str, ...]:
        """Activated claimable set used by G8-16A. Sibling types stay undispatched here."""

        return self._activated_types

    @property
    def claim_job_types(self) -> tuple[str, ...]:
        """Exact array passed to ops.claim_job.

        Pre-handler: the full pre-claim set, so claim_job is invoked and cannot
        select resolve_claims. After activation: only resolve_claims, which this
        consumer can dispatch without stealing extract/analyze jobs.
        """

        if self._claim_job_types:
            return self._claim_job_types
        return self._activated_types

    def claim_one(self) -> tuple[Any, ...] | None:
        """Always call ops.claim_job with the activated G8-16A type array."""

        requested = self.claim_job_types
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT job_id, attempt_id, job_type, payload, lease_token
                  FROM ops.claim_job('worker', %s, %s::text[], %s)
                """,
                (self._worker_id, list(requested), self._lease_seconds),
            )
            row = cast(tuple[Any, ...] | None, cursor.fetchone())
        self._connection.commit()
        return None if row is None else tuple(row)

    def dispatch(self, claimed: tuple[Any, ...]) -> str:
        """Route a claimed resolve_claims row to the production handler."""

        if len(claimed) < 5:
            raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH)
        job_type = str(claimed[2])
        if job_type != "resolve_claims":
            raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH)
        payload = claimed[3]
        if not isinstance(payload, Mapping):
            raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH)
        handler = ResolveClaimsHandler(self._connection, self._object_client)
        return handler.handle(
            uuid.UUID(str(claimed[0])),
            uuid.UUID(str(claimed[1])),
            uuid.UUID(str(claimed[4])),
            payload,
        )

    def run_once(self) -> str | None:
        claimed = self.claim_one()
        if claimed is None:
            return None
        return self.dispatch(claimed)
