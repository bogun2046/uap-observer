"""Production workers that activate resolve_claims / resolve_entities after handlers exist."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, cast

from psycopg import Connection

from uap_platform.config import load_settings
from uap_platform.object_registry import ObjectClient
from uap_platform.object_store_init import build_client

from .handler import ResolveClaimsHandler, ResolveEntitiesHandler
from .job_types import claimable_job_types
from .payload import KnowledgePayloadError
from .reasons import KNOWLEDGE_PAYLOAD_MISMATCH, KNOWLEDGE_RELATION_TASK_NOT_IN_WP8

_CLAIMS_DISPATCHABLE = frozenset({"resolve_claims"})
_ENTITIES_DISPATCHABLE = frozenset({"resolve_entities"})
_RELATION_JOB_TYPE = "resolve_relations"


def finish_misclaimed_relation_job(
    connection: Connection[object],
    job_id: uuid.UUID,
    attempt_id: uuid.UUID,
    lease_token: uuid.UUID,
    job_type: str,
) -> str:
    """Close a mis-claimed resolve_relations job via ops.finish_job.

    ADR-0013 / G8-16C: no materialize, no finish_knowledge_job, never succeeded.
    """

    if job_type != _RELATION_JOB_TYPE:
        raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT ops.finish_job(
                %s, %s, %s, 'terminal_failure'::ops.attempt_outcome,
                NULL, %s, %s, NULL
            )
            """,
            (
                job_id,
                attempt_id,
                lease_token,
                KNOWLEDGE_RELATION_TASK_NOT_IN_WP8,
                KNOWLEDGE_RELATION_TASK_NOT_IN_WP8,
            ),
        )
        row = cast(tuple[Any, ...] | None, cursor.fetchone())
    connection.commit()
    return str(row[0]) if row else "dead"


class KnowledgeJobDispatcher:
    """Generic knowledge dispatcher. resolve_relations is fail-closed."""

    def __init__(
        self,
        connection: Connection[object],
        object_client: ObjectClient | None = None,
    ) -> None:
        self._connection = connection
        self._object_client = object_client

    def dispatch(self, claimed: tuple[Any, ...]) -> str:
        if len(claimed) < 5:
            raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH)
        job_type = str(claimed[2])
        if job_type == _RELATION_JOB_TYPE:
            return finish_misclaimed_relation_job(
                self._connection,
                uuid.UUID(str(claimed[0])),
                uuid.UUID(str(claimed[1])),
                uuid.UUID(str(claimed[4])),
                job_type,
            )
        if self._object_client is None:
            raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH)
        payload = claimed[3]
        if not isinstance(payload, Mapping):
            raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH)
        if job_type == "resolve_claims":
            handler: ResolveClaimsHandler | ResolveEntitiesHandler = ResolveClaimsHandler(
                self._connection, self._object_client
            )
        elif job_type == "resolve_entities":
            handler = ResolveEntitiesHandler(self._connection, self._object_client)
        else:
            raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH)
        return handler.handle(
            uuid.UUID(str(claimed[0])),
            uuid.UUID(str(claimed[1])),
            uuid.UUID(str(claimed[4])),
            payload,
        )


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
        self._activated_types = claimable_job_types(claims_handler_active=claims_handler_active)
        # ADR-0008: p_job_types must be a subset of handlers deployed here.
        # This consumer only dispatches resolve_claims; inactive it claims nothing.
        requested = ("resolve_claims",) if claims_handler_active else ()
        self._claim_job_types = tuple(
            job_type for job_type in requested if job_type in _CLAIMS_DISPATCHABLE
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
        """Platform-wide G8-16A claimable set. Sibling types stay undispatched here."""

        return self._activated_types

    @property
    def claim_job_types(self) -> tuple[str, ...]:
        """Exact array passed to ops.claim_job: only types this process can dispatch.

        Inactive Claims consumer: empty (never call claim_job with sibling types).
        After activation: only resolve_claims.
        """

        return self._claim_job_types

    def claim_one(self) -> tuple[Any, ...] | None:
        """Lease one job this process can dispatch. Empty type set claims nothing."""

        requested = self.claim_job_types
        if not requested:
            return None
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


class ResolveEntitiesWorker:
    """Claim and dispatch entity jobs. Production default includes resolve_entities."""

    def __init__(
        self,
        connection: Connection[object],
        object_client: ObjectClient,
        *,
        worker_id: str,
        entities_handler_active: bool = True,
        lease_seconds: int = 60,
    ) -> None:
        self._connection = connection
        self._object_client = object_client
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._activated_types = claimable_job_types(
            claims_handler_active=True,
            entities_handler_active=entities_handler_active,
        )
        # ADR-0008: p_job_types must be a subset of handlers deployed here.
        # This consumer only dispatches resolve_entities; inactive it claims nothing.
        requested = ("resolve_entities",) if entities_handler_active else ()
        self._claim_job_types = tuple(
            job_type for job_type in requested if job_type in _ENTITIES_DISPATCHABLE
        )

    @classmethod
    def from_settings(
        cls,
        connection: Connection[object],
        *,
        worker_id: str,
        entities_handler_active: bool = True,
        lease_seconds: int = 60,
    ) -> ResolveEntitiesWorker:
        """Production constructor: MinIO client from process settings, handler active."""

        return cls(
            connection,
            cast(ObjectClient, build_client(load_settings())),
            worker_id=worker_id,
            entities_handler_active=entities_handler_active,
            lease_seconds=lease_seconds,
        )

    @property
    def job_types(self) -> tuple[str, ...]:
        """Platform-wide G8-16B claimable set. Sibling types stay undispatched here."""

        return self._activated_types

    @property
    def claim_job_types(self) -> tuple[str, ...]:
        """Exact array passed to ops.claim_job: only types this process can dispatch.

        Inactive Entities consumer: empty (never call claim_job with sibling types).
        After activation: only resolve_entities.
        """

        return self._claim_job_types

    def claim_one(self) -> tuple[Any, ...] | None:
        """Lease one job this process can dispatch. Empty type set claims nothing."""

        requested = self.claim_job_types
        if not requested:
            return None
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
        """Route a claimed resolve_entities row to the production handler."""

        if len(claimed) < 5:
            raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH)
        job_type = str(claimed[2])
        if job_type != "resolve_entities":
            raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH)
        payload = claimed[3]
        if not isinstance(payload, Mapping):
            raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH)
        handler = ResolveEntitiesHandler(self._connection, self._object_client)
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
