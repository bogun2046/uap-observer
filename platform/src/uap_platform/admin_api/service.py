"""Admin reads over sanitized review state and writes through frozen public wrappers."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from psycopg import Connection
from psycopg.errors import Error as PsycopgError
from psycopg.rows import RowFactory, tuple_row

from uap_platform.review.cases import assign_review_case, close_review_case, open_review_case
from uap_platform.review.claims import create_manual_claim
from uap_platform.review.decisions import record_review_decision
from uap_platform.review.errors import ReviewSessionError
from uap_platform.review.merge import apply_entity_merge, apply_entity_merge_reverse
from uap_platform.review.promotion import (
    accept_entity_candidate,
    bind_entity_candidate,
    select_analysis_result,
)
from uap_platform.review.session import require_active_role

from .contracts import (
    AdminEntityPage,
    AdminEntitySummary,
    AnalysisResultPage,
    AnalysisResultSummary,
    EntityCandidatePage,
    EntityCandidateSummary,
    EntityType,
    EvidenceSpanPage,
    EvidenceSpanSummary,
    GrantStatus,
    ProjectionState,
    PublicationEventPage,
    PublicationEventState,
    PublicationEventSummary,
    PublicationState,
    ReviewCaseDetail,
    ReviewCasePage,
    ReviewCaseSummary,
    ReviewDecision,
    ReviewDecisionSummary,
    WriteResult,
)
from .cursor import CursorCodec, CursorError, filters_digest
from .errors import AdminError, map_database_error
from .pool import AdminApiPool

CASE_SORT = "priority_desc,opened_at_asc,id_asc"
CREATED_SORT = "created_at_asc,id_asc"
OCCURRED_SORT = "occurred_at_asc,id_asc"
REVIEWER_ROLE = "reviewer"
SENIOR_ROLE = "senior_reviewer"
OPERATOR_ROLE = "data_operator"


class AdminQueryService:
    def __init__(self, pool: AdminApiPool, cursor_codec: CursorCodec) -> None:
        self._pool = pool
        self._cursor = cursor_codec

    def list_review_cases(
        self,
        *,
        principal_id: UUID,
        status: str | None,
        case_type: str | None,
        assigned_to: UUID | None,
        limit: int,
        cursor: str | None,
    ) -> ReviewCasePage:
        filters = {
            "assigned_to": str(assigned_to) if assigned_to else None,
            "case_type": case_type,
            "status": status,
        }
        digest = filters_digest(filters)
        last_priority, last_opened, last_id = self._case_cursor(cursor, digest)
        with self._pool.read_transaction(principal_id) as connection:
            self._require_role(connection, REVIEWER_ROLE)
            with connection.cursor() as db_cursor:
                db_cursor.execute(
                    """
                    SELECT review_case.id, review_case.case_type, review_case.status,
                           review_case.priority, review_case.assigned_to, review_case.opened_by,
                           review_case.opened_at, review_case.closed_at,
                           COALESCE(
                               review_case.document_version_id, review_case.claim_id,
                               review_case.entity_id, review_case.relation_id
                           ) AS subject_id
                      FROM audit.review_cases AS review_case
                     WHERE (%s::audit.review_status IS NULL
                            OR review_case.status = %s::audit.review_status)
                       AND (%s::audit.review_case_type IS NULL
                            OR review_case.case_type = %s::audit.review_case_type)
                       AND (%s::uuid IS NULL OR review_case.assigned_to = %s::uuid)
                       AND (
                            %s::smallint IS NULL
                            OR review_case.priority < %s::smallint
                            OR (
                                review_case.priority = %s::smallint
                                AND review_case.opened_at > %s::timestamptz
                            )
                            OR (
                                review_case.priority = %s::smallint
                                AND review_case.opened_at = %s::timestamptz
                                AND review_case.id > %s::uuid
                            )
                       )
                     ORDER BY review_case.priority DESC, review_case.opened_at ASC,
                              review_case.id ASC
                     LIMIT %s
                    """,
                    (
                        status,
                        status,
                        case_type,
                        case_type,
                        assigned_to,
                        assigned_to,
                        last_priority,
                        last_priority,
                        last_priority,
                        last_opened,
                        last_priority,
                        last_opened,
                        last_id,
                        limit + 1,
                    ),
                )
                rows = db_cursor.fetchall()
            items = [self._case_summary(row) for row in rows[:limit]]
            next_cursor = None
            if len(rows) > limit and items:
                tail = rows[limit - 1]
                next_cursor = self._cursor.encode(
                    resource="review-cases",
                    sort=CASE_SORT,
                    last=[
                        int(cast(int, tail["priority"])),
                        self._iso(tail["opened_at"]),
                        str(tail["id"]),
                    ],
                    filters_sha256=digest,
                )
            return ReviewCasePage(items=items, next_cursor=next_cursor)

    def get_review_case(self, principal_id: UUID, case_id: UUID) -> ReviewCaseDetail | None:
        with self._pool.read_transaction(principal_id) as connection:
            self._require_role(connection, REVIEWER_ROLE)
            with connection.cursor() as db_cursor:
                db_cursor.execute(
                    """
                    SELECT review_case.id, review_case.case_type, review_case.status,
                           review_case.priority, review_case.assigned_to, review_case.opened_by,
                           review_case.opened_at, review_case.closed_at,
                           COALESCE(
                               review_case.document_version_id, review_case.claim_id,
                               review_case.entity_id, review_case.relation_id
                           ) AS subject_id
                      FROM audit.review_cases AS review_case
                     WHERE review_case.id = %s
                    """,
                    (case_id,),
                )
                row = db_cursor.fetchone()
            if row is None:
                return None
            with connection.cursor() as db_cursor:
                db_cursor.execute(
                    """
                    SELECT decision.id, decision.sequence_no, decision.decision, decision.reason,
                           decision.decided_by, decision.decided_at
                      FROM audit.review_decisions AS decision
                     WHERE decision.review_case_id = %s
                     ORDER BY decision.sequence_no ASC, decision.id ASC
                    """,
                    (case_id,),
                )
                decisions = [
                    ReviewDecisionSummary(
                        id=cast(UUID, item["id"]),
                        sequence_no=cast(int, item["sequence_no"]),
                        decision=cast(ReviewDecision, item["decision"]),
                        reason=str(item["reason"]),
                        decided_by=cast(UUID, item["decided_by"]),
                        decided_at=cast(datetime, item["decided_at"]),
                    )
                    for item in db_cursor.fetchall()
                ]
            publication = self._publication_for_case(connection, case_id)
            summary = self._case_summary(row)
            return ReviewCaseDetail(
                **summary.model_dump(), decisions=decisions, publication=publication
            )

    def list_analysis_results(
        self,
        *,
        principal_id: UUID,
        document_version_id: UUID | None,
        result_type: str | None,
        validation_status: str | None,
        limit: int,
        cursor: str | None,
    ) -> AnalysisResultPage:
        filters = {
            "document_version_id": str(document_version_id) if document_version_id else None,
            "result_type": result_type,
            "validation_status": validation_status,
        }
        digest = filters_digest(filters)
        last_created, last_id = self._created_cursor(cursor, "analysis-results", digest)
        with self._pool.read_transaction(principal_id) as connection:
            self._require_role(connection, REVIEWER_ROLE)
            with connection.cursor() as db_cursor:
                db_cursor.execute(
                    """
                    SELECT result.id, result.document_version_id, result.result_type,
                           result.schema_version, result.validation_status, result.created_at
                      FROM core.analysis_results AS result
                     WHERE (%s::uuid IS NULL OR result.document_version_id = %s::uuid)
                       AND (%s::text IS NULL OR result.result_type = %s::ops.model_task_type)
                       AND (%s::text IS NULL
                            OR result.validation_status = %s::core.validation_status)
                       AND (
                            %s::timestamptz IS NULL
                            OR (result.created_at, result.id) > (%s::timestamptz, %s::uuid)
                       )
                     ORDER BY result.created_at ASC, result.id ASC
                     LIMIT %s
                    """,
                    (
                        document_version_id,
                        document_version_id,
                        result_type,
                        result_type,
                        validation_status,
                        validation_status,
                        last_created,
                        last_created,
                        last_id,
                        limit + 1,
                    ),
                )
                rows = db_cursor.fetchall()
            items = [
                AnalysisResultSummary(
                    id=cast(UUID, row["id"]),
                    document_version_id=cast(UUID, row["document_version_id"]),
                    result_type=str(row["result_type"]),
                    schema_version=str(row["schema_version"]),
                    validation_status=str(row["validation_status"]),
                    created_at=cast(datetime, row["created_at"]),
                )
                for row in rows[:limit]
            ]
            next_cursor = self._next_created_cursor("analysis-results", digest, rows, limit, items)
            return AnalysisResultPage(items=items, next_cursor=next_cursor)

    def list_entity_candidates(
        self,
        *,
        principal_id: UUID,
        status: str | None,
        analysis_result_id: UUID | None,
        limit: int,
        cursor: str | None,
    ) -> EntityCandidatePage:
        filters = {
            "analysis_result_id": str(analysis_result_id) if analysis_result_id else None,
            "status": status,
        }
        digest = filters_digest(filters)
        last_created, last_id = self._created_cursor(cursor, "entity-candidates", digest)
        with self._pool.read_transaction(principal_id) as connection:
            self._require_role(connection, REVIEWER_ROLE)
            with connection.cursor() as db_cursor:
                db_cursor.execute(
                    """
                    SELECT candidate.id, candidate.analysis_result_id,
                           candidate.document_version_id, candidate.ordinal,
                           candidate.proposed_entity_type, candidate.proposed_name,
                           candidate.status, candidate.created_at
                      FROM core.entity_candidates AS candidate
                     WHERE (%s::text IS NULL OR candidate.status = %s::core.candidate_status)
                       AND (%s::uuid IS NULL OR candidate.analysis_result_id = %s::uuid)
                       AND (
                            %s::timestamptz IS NULL
                            OR (candidate.created_at, candidate.id) > (%s::timestamptz, %s::uuid)
                       )
                     ORDER BY candidate.created_at ASC, candidate.id ASC
                     LIMIT %s
                    """,
                    (
                        status,
                        status,
                        analysis_result_id,
                        analysis_result_id,
                        last_created,
                        last_created,
                        last_id,
                        limit + 1,
                    ),
                )
                rows = db_cursor.fetchall()
            items = [
                EntityCandidateSummary(
                    id=cast(UUID, row["id"]),
                    analysis_result_id=cast(UUID, row["analysis_result_id"]),
                    document_version_id=cast(UUID, row["document_version_id"]),
                    ordinal=cast(int, row["ordinal"]),
                    proposed_entity_type=cast(EntityType, row["proposed_entity_type"]),
                    proposed_name=str(row["proposed_name"]),
                    status=str(row["status"]),
                    created_at=cast(datetime, row["created_at"]),
                )
                for row in rows[:limit]
            ]
            next_cursor = self._next_created_cursor("entity-candidates", digest, rows, limit, items)
            return EntityCandidatePage(items=items, next_cursor=next_cursor)

    def list_evidence_spans(
        self,
        *,
        principal_id: UUID,
        document_version_id: UUID,
        limit: int,
        cursor: str | None,
    ) -> EvidenceSpanPage:
        filters = {"document_version_id": str(document_version_id)}
        digest = filters_digest(filters)
        last_created, last_id = self._created_cursor(cursor, "evidence-spans", digest)
        with self._pool.read_transaction(principal_id) as connection:
            self._require_role(connection, REVIEWER_ROLE)
            with connection.cursor() as db_cursor:
                db_cursor.execute(
                    """
                    SELECT span.id, span.document_version_id, span.locator_type,
                           span.char_start, span.char_end, span.page_start, span.page_end,
                           span.time_start_ms, span.time_end_ms, span.locator, span.created_at
                      FROM core.evidence_spans AS span
                     WHERE span.document_version_id = %s
                       AND (
                            %s::timestamptz IS NULL
                            OR (span.created_at, span.id) > (%s::timestamptz, %s::uuid)
                       )
                     ORDER BY span.created_at ASC, span.id ASC
                     LIMIT %s
                    """,
                    (
                        document_version_id,
                        last_created,
                        last_created,
                        last_id,
                        limit + 1,
                    ),
                )
                rows = db_cursor.fetchall()
            items = [
                EvidenceSpanSummary(
                    id=cast(UUID, row["id"]),
                    document_version_id=cast(UUID, row["document_version_id"]),
                    locator_type=str(row["locator_type"]),
                    char_start=cast(int | None, row["char_start"]),
                    char_end=cast(int | None, row["char_end"]),
                    page_start=cast(int | None, row["page_start"]),
                    page_end=cast(int | None, row["page_end"]),
                    time_start_ms=cast(int | None, row["time_start_ms"]),
                    time_end_ms=cast(int | None, row["time_end_ms"]),
                    locator=cast(dict[str, object], row["locator"]),
                    created_at=cast(datetime, row["created_at"]),
                )
                for row in rows[:limit]
            ]
            next_cursor = self._next_created_cursor("evidence-spans", digest, rows, limit, items)
            return EvidenceSpanPage(items=items, next_cursor=next_cursor)

    def list_entities(
        self,
        *,
        principal_id: UUID,
        query: str | None,
        status: str | None,
        entity_type: str | None,
        limit: int,
        cursor: str | None,
    ) -> AdminEntityPage:
        filters = {"q": query, "status": status, "type": entity_type}
        digest = filters_digest(filters)
        last_created, last_id = self._created_cursor(cursor, "entities", digest)
        with self._pool.read_transaction(principal_id) as connection:
            self._require_role(connection, REVIEWER_ROLE)
            with connection.cursor() as db_cursor:
                db_cursor.execute(
                    """
                    SELECT entity.id, entity.entity_type, entity.canonical_name,
                           entity.description, entity.country_code, entity.status,
                           entity.created_at
                      FROM core.entities AS entity
                     WHERE (%s::text IS NULL
                            OR position(lower(%s) IN lower(entity.canonical_name)) > 0)
                       AND (%s::text IS NULL OR entity.status = %s::core.entity_status)
                       AND (%s::text IS NULL OR entity.entity_type = %s::core.entity_type)
                       AND (
                            %s::timestamptz IS NULL
                            OR (entity.created_at, entity.id) > (%s::timestamptz, %s::uuid)
                       )
                     ORDER BY entity.created_at ASC, entity.id ASC
                     LIMIT %s
                    """,
                    (
                        query,
                        query,
                        status,
                        status,
                        entity_type,
                        entity_type,
                        last_created,
                        last_created,
                        last_id,
                        limit + 1,
                    ),
                )
                rows = db_cursor.fetchall()
            items = [
                AdminEntitySummary(
                    id=cast(UUID, row["id"]),
                    type=cast(EntityType, row["entity_type"]),
                    name=str(row["canonical_name"]),
                    description=cast(str | None, row["description"]),
                    country_code=cast(str | None, row["country_code"]),
                    status=str(row["status"]),
                    created_at=cast(datetime, row["created_at"]),
                )
                for row in rows[:limit]
            ]
            next_cursor = self._next_created_cursor("entities", digest, rows, limit, items)
            return AdminEntityPage(items=items, next_cursor=next_cursor)

    def list_publication_events(
        self,
        *,
        principal_id: UUID,
        state: str | None,
        limit: int,
        cursor: str | None,
    ) -> PublicationEventPage:
        filters = {"state": state}
        digest = filters_digest(filters)
        last_occurred, last_id = self._occurred_cursor(cursor, digest)
        with self._pool.read_transaction(principal_id) as connection:
            self._require_any_role(connection, (SENIOR_ROLE, OPERATOR_ROLE))
            with connection.cursor() as db_cursor:
                db_cursor.execute(
                    """
                    SELECT event.id, event.event_type, event.aggregate_type, event.aggregate_id,
                           event.occurred_at, event.publish_attempts, event.terminal_at,
                           event.terminal_error_code, event.last_error_code,
                           event.available_at, event.published_at
                      FROM ops.outbox_events AS event
                     WHERE event.event_type LIKE 'publication.%%'
                       AND event.published_at IS NULL
                       AND (
                            (%s::text IS NULL AND (event.terminal_at IS NOT NULL
                                 OR event.available_at > clock_timestamp()))
                            OR (%s::text = 'terminal' AND event.terminal_at IS NOT NULL)
                            OR (
                                %s::text = 'retry_wait'
                                AND event.terminal_at IS NULL
                                AND event.available_at > clock_timestamp()
                            )
                       )
                       AND (
                            %s::timestamptz IS NULL
                            OR (event.occurred_at, event.id) > (%s::timestamptz, %s::uuid)
                       )
                     ORDER BY event.occurred_at ASC, event.id ASC
                     LIMIT %s
                    """,
                    (
                        state,
                        state,
                        state,
                        last_occurred,
                        last_occurred,
                        last_id,
                        limit + 1,
                    ),
                )
                rows = db_cursor.fetchall()
            items = [self._publication_event(row) for row in rows[:limit]]
            next_cursor = None
            if len(rows) > limit and items:
                tail = rows[limit - 1]
                next_cursor = self._cursor.encode(
                    resource="publication-events",
                    sort=OCCURRED_SORT,
                    last=[self._iso(tail["occurred_at"]), str(tail["id"])],
                    filters_sha256=digest,
                )
            return PublicationEventPage(items=items, next_cursor=next_cursor)

    def open_case(
        self,
        *,
        principal_id: UUID,
        request_id: UUID,
        case_type: str,
        subject_id: UUID,
        priority: int,
        reason: str,
    ) -> WriteResult:
        if case_type == "relation":
            raise AdminError("api_capability_closed")
        return self._write(
            principal_id,
            request_id,
            "review.case.open",
            lambda connection: open_review_case(
                connection, case_type, subject_id, priority, reason
            ),
        )

    def assign_case(
        self, *, principal_id: UUID, request_id: UUID, case_id: UUID, assignee_id: UUID
    ) -> WriteResult:
        return self._write(
            principal_id,
            request_id,
            "review.case.assign",
            lambda connection: assign_review_case(connection, case_id, assignee_id),
            resource_id=case_id,
        )

    def close_case(
        self, *, principal_id: UUID, request_id: UUID, case_id: UUID, reason: str
    ) -> WriteResult:
        return self._write(
            principal_id,
            request_id,
            "review.case.close",
            lambda connection: close_review_case(connection, case_id, reason),
            resource_id=case_id,
        )

    def record_decision(
        self,
        *,
        principal_id: UUID,
        request_id: UUID,
        case_id: UUID,
        decision: str,
        reason: str,
        structured_changes: dict[str, Any],
    ) -> WriteResult:
        return self._write(
            principal_id,
            request_id,
            "review.decision",
            lambda connection: record_review_decision(
                connection, case_id, decision, reason, structured_changes
            ),
            publication_case_id=case_id,
        )

    def select_result(
        self, *, principal_id: UUID, request_id: UUID, analysis_result_id: UUID, reason: str
    ) -> WriteResult:
        return self._write(
            principal_id,
            request_id,
            "review.selection",
            lambda connection: select_analysis_result(connection, analysis_result_id, reason),
        )

    def accept_candidate(
        self, *, principal_id: UUID, request_id: UUID, candidate_id: UUID, reason: str
    ) -> WriteResult:
        return self._write(
            principal_id,
            request_id,
            "review.candidate.accept",
            lambda connection: accept_entity_candidate(connection, candidate_id, reason),
        )

    def bind_candidate(
        self,
        *,
        principal_id: UUID,
        request_id: UUID,
        candidate_id: UUID,
        entity_id: UUID,
        reason: str,
    ) -> WriteResult:
        return self._write(
            principal_id,
            request_id,
            "review.candidate.bind",
            lambda connection: bind_entity_candidate(connection, candidate_id, entity_id, reason),
            resource_id=entity_id,
        )

    def merge_entities(
        self,
        *,
        principal_id: UUID,
        request_id: UUID,
        source_entity_id: UUID,
        target_entity_id: UUID,
        reason: str,
    ) -> WriteResult:
        return self._write(
            principal_id,
            request_id,
            "review.entity.merge",
            lambda connection: apply_entity_merge(
                connection, source_entity_id, target_entity_id, reason
            ),
        )

    def reverse_merge(
        self, *, principal_id: UUID, request_id: UUID, merge_event_id: UUID, reason: str
    ) -> WriteResult:
        return self._write(
            principal_id,
            request_id,
            "review.entity.merge_reverse",
            lambda connection: apply_entity_merge_reverse(connection, merge_event_id, reason),
        )

    def create_claim(
        self,
        *,
        principal_id: UUID,
        request_id: UUID,
        document_version_id: UUID,
        claim_text: str,
        claim_type: str,
        assertion_status: str,
        attribution: str | None,
        span_ids: list[UUID],
    ) -> WriteResult:
        return self._write(
            principal_id,
            request_id,
            "review.claim.manual",
            lambda connection: create_manual_claim(
                connection,
                document_version_id,
                claim_text,
                claim_type,
                assertion_status,
                attribution,
                span_ids,
            ),
        )

    def replay_publication_event(
        self, *, principal_id: UUID, request_id: UUID, event_id: UUID, reason: str
    ) -> WriteResult:
        def _call(connection: Connection[dict[str, object]]) -> UUID:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT audit.requeue_publication_event(%s, %s)",
                        (event_id, reason),
                    )
                    row = cursor.fetchone()
            except PsycopgError as error:
                raise map_database_error(error) from error
            if row is None:
                raise AdminError("api_internal_error")
            value = row[0] if not isinstance(row, Mapping) else row.get("requeue_publication_event")
            if value is None:
                raise AdminError("api_internal_error")
            return UUID(str(value))

        return self._write(
            principal_id,
            request_id,
            "publication.replay",
            _call,
            resource_id=event_id,
        )

    def resolve_principal(self, issuer: str, subject: str) -> UUID:
        try:
            with self._pool.lookup_transaction() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT principal.id, principal.active, principal.principal_type
                          FROM audit.principals AS principal
                         WHERE principal.issuer = %s AND principal.subject = %s
                        """,
                        (issuer, subject),
                    )
                    row = cursor.fetchone()
        except PsycopgError as error:
            raise map_database_error(error) from error
        if row is None or row["active"] is not True or str(row["principal_type"]) != "person":
            raise AdminError("api_principal_not_provisioned")
        return UUID(str(row["id"]))

    def _write(
        self,
        principal_id: UUID,
        request_id: UUID,
        operation: str,
        call: Callable[[Connection[dict[str, object]]], UUID],
        *,
        resource_id: UUID | None = None,
        publication_case_id: UUID | None = None,
    ) -> WriteResult:
        with self._pool.write_transaction(principal_id, request_id) as connection:
            try:
                with self._tuple_rows(connection):
                    result_id = call(connection)
            except ReviewSessionError as error:
                raise AdminError(error.code) from error
            except PsycopgError as error:
                raise map_database_error(error) from error
            resolved = resource_id if resource_id is not None else result_id
            publication = None
            if publication_case_id is not None:
                publication = self._publication_for_case(connection, publication_case_id)
            return WriteResult(
                operation=operation,
                resource_id=resolved,
                request_id=request_id,
                publication=publication,
            )

    @staticmethod
    @contextmanager
    def _tuple_rows(
        connection: Connection[dict[str, object]],
    ) -> Iterator[Connection[dict[str, object]]]:
        original: RowFactory[dict[str, object]] = connection.row_factory
        connection.row_factory = cast(RowFactory[dict[str, object]], tuple_row)
        try:
            yield connection
        finally:
            connection.row_factory = original

    def _require_role(self, connection: Connection[dict[str, object]], role: str) -> UUID:
        try:
            with self._tuple_rows(connection):
                return require_active_role(connection, role)
        except ReviewSessionError as error:
            raise AdminError(error.code) from error
        except PsycopgError as error:
            raise map_database_error(error) from error

    def _require_any_role(
        self, connection: Connection[dict[str, object]], roles: tuple[str, ...]
    ) -> UUID:
        last: AdminError | None = None
        for role in roles:
            with connection.cursor() as cursor:
                cursor.execute("SAVEPOINT uap_admin_role_probe")
            try:
                with self._tuple_rows(connection):
                    principal_id = require_active_role(connection, role)
            except ReviewSessionError as error:
                with connection.cursor() as cursor:
                    cursor.execute("ROLLBACK TO SAVEPOINT uap_admin_role_probe")
                    cursor.execute("RELEASE SAVEPOINT uap_admin_role_probe")
                if error.code != "review_role_denied":
                    raise AdminError(error.code) from error
                last = AdminError(error.code)
                continue
            except PsycopgError as error:
                with connection.cursor() as cursor:
                    cursor.execute("ROLLBACK TO SAVEPOINT uap_admin_role_probe")
                    cursor.execute("RELEASE SAVEPOINT uap_admin_role_probe")
                raise map_database_error(error) from error
            with connection.cursor() as cursor:
                cursor.execute("RELEASE SAVEPOINT uap_admin_role_probe")
            return principal_id
        raise last or AdminError("review_role_denied")

    def _case_cursor(
        self, cursor: str | None, digest: str
    ) -> tuple[int | None, datetime | None, UUID | None]:
        if not cursor:
            return None, None, None
        last = self._cursor.decode(
            cursor, resource="review-cases", sort=CASE_SORT, filters_sha256=digest
        )
        if len(last) != 3:
            raise CursorError("cursor is invalid")
        try:
            priority = int(last[0])
            opened_at = datetime.fromisoformat(str(last[1]))
            case_id = UUID(str(last[2]))
        except (TypeError, ValueError) as error:
            raise CursorError("cursor is invalid") from error
        if opened_at.tzinfo is None or str(case_id) != str(last[2]):
            raise CursorError("cursor is invalid")
        return priority, opened_at, case_id

    def _created_cursor(
        self, cursor: str | None, resource: str, digest: str
    ) -> tuple[datetime | None, UUID | None]:
        if not cursor:
            return None, None
        last = self._cursor.decode(
            cursor, resource=resource, sort=CREATED_SORT, filters_sha256=digest
        )
        if len(last) != 2:
            raise CursorError("cursor is invalid")
        try:
            created_at = datetime.fromisoformat(str(last[0]))
            item_id = UUID(str(last[1]))
        except (TypeError, ValueError) as error:
            raise CursorError("cursor is invalid") from error
        if created_at.tzinfo is None or str(item_id) != str(last[1]):
            raise CursorError("cursor is invalid")
        return created_at, item_id

    def _occurred_cursor(
        self, cursor: str | None, digest: str
    ) -> tuple[datetime | None, UUID | None]:
        if not cursor:
            return None, None
        last = self._cursor.decode(
            cursor,
            resource="publication-events",
            sort=OCCURRED_SORT,
            filters_sha256=digest,
        )
        if len(last) != 2:
            raise CursorError("cursor is invalid")
        try:
            occurred_at = datetime.fromisoformat(str(last[0]))
            item_id = UUID(str(last[1]))
        except (TypeError, ValueError) as error:
            raise CursorError("cursor is invalid") from error
        if occurred_at.tzinfo is None or str(item_id) != str(last[1]):
            raise CursorError("cursor is invalid")
        return occurred_at, item_id

    def _next_created_cursor(
        self,
        resource: str,
        digest: str,
        rows: Sequence[Mapping[str, object]],
        limit: int,
        items: list[Any],
    ) -> str | None:
        if len(rows) <= limit or not items:
            return None
        tail = rows[limit - 1]
        return self._cursor.encode(
            resource=resource,
            sort=CREATED_SORT,
            last=[self._iso(tail["created_at"]), str(tail["id"])],
            filters_sha256=digest,
        )

    @staticmethod
    def _iso(value: object) -> str:
        if not isinstance(value, datetime):
            raise CursorError("cursor is invalid")
        return value.isoformat()

    @staticmethod
    def _case_summary(row: Mapping[str, Any]) -> ReviewCaseSummary:
        return ReviewCaseSummary(
            id=row["id"],
            case_type=row["case_type"],
            status=row["status"],
            priority=row["priority"],
            subject_id=row["subject_id"],
            assigned_to=row["assigned_to"],
            opened_by=row["opened_by"],
            opened_at=row["opened_at"],
            closed_at=row["closed_at"],
        )

    @staticmethod
    def _publication_event(row: Mapping[str, Any]) -> PublicationEventSummary:
        terminal = row["terminal_at"] is not None
        state = PublicationEventState.TERMINAL if terminal else PublicationEventState.RETRY_WAIT
        code = row["terminal_error_code"] or row["last_error_code"]
        summary = None
        if code:
            summary = "Publication delivery is waiting or terminal."
        return PublicationEventSummary(
            id=row["id"],
            event_type=row["event_type"],
            aggregate_type=row["aggregate_type"],
            aggregate_id=row["aggregate_id"],
            occurred_at=row["occurred_at"],
            publish_attempts=row["publish_attempts"],
            state=state,
            error_code=code,
            error_summary=summary,
        )

    def _publication_for_case(
        self, connection: Connection[dict[str, object]], case_id: UUID
    ) -> PublicationState | None:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT grant_id, grant_status, revision_no, outbox_event_id, projection_state
                  FROM (
                    SELECT document_grant.id AS grant_id,
                           document_grant.grant_status,
                           document_grant.revision_no,
                           (
                               SELECT event.id
                                 FROM ops.outbox_events AS event
                                WHERE event.payload ->> 'grant_id'
                              = lower(document_grant.id::text)
                                  AND event.event_type LIKE 'publication.%%'
                                ORDER BY event.occurred_at DESC, event.id DESC
                                LIMIT 1
                           ) AS outbox_event_id,
                           CASE
                               WHEN document_grant.grant_status = 'withdrawn'
                                   THEN 'withdrawn'
                               WHEN EXISTS (
                                   SELECT 1 FROM audit.publication_quarantine AS quarantine
                                    WHERE quarantine.grant_id = document_grant.id
                                      AND quarantine.resolved_at IS NULL
                               ) THEN 'blocked'
                               WHEN EXISTS (
                                   SELECT 1 FROM public.documents AS document
                                    WHERE document.document_grant_id = document_grant.id
                               ) THEN 'visible'
                               WHEN EXISTS (
                                   SELECT 1 FROM ops.outbox_events AS event
                                    WHERE event.payload ->> 'grant_id'
                              = lower(document_grant.id::text)
                                      AND event.terminal_at IS NOT NULL
                                      AND event.published_at IS NULL
                               ) THEN 'blocked'
                               ELSE 'pending'
                           END AS projection_state
                      FROM audit.document_publication_grants AS document_grant
                     WHERE document_grant.review_case_id = %s
                     UNION ALL
                    SELECT claim_grant.id, claim_grant.grant_status, claim_grant.revision_no,
                           (
                               SELECT event.id FROM ops.outbox_events AS event
                                WHERE event.payload ->> 'grant_id' = lower(claim_grant.id::text)
                                  AND event.event_type LIKE 'publication.%%'
                                ORDER BY event.occurred_at DESC, event.id DESC
                                LIMIT 1
                           ),
                           CASE
                               WHEN claim_grant.grant_status = 'withdrawn' THEN 'withdrawn'
                               WHEN EXISTS (
                                   SELECT 1 FROM public.claims AS claim
                                    WHERE claim.claim_grant_id = claim_grant.id
                               ) THEN 'visible'
                               WHEN EXISTS (
                                   SELECT 1 FROM ops.outbox_events AS event
                                    WHERE event.payload ->> 'grant_id' = lower(claim_grant.id::text)
                                      AND event.terminal_at IS NOT NULL
                                      AND event.published_at IS NULL
                               ) THEN 'blocked'
                               ELSE 'pending'
                           END
                      FROM audit.claim_publication_grants AS claim_grant
                     WHERE claim_grant.review_case_id = %s
                     UNION ALL
                    SELECT entity_grant.id, entity_grant.grant_status, entity_grant.revision_no,
                           (
                               SELECT event.id FROM ops.outbox_events AS event
                                WHERE event.payload ->> 'grant_id'
                              = lower(entity_grant.id::text)
                                  AND event.event_type LIKE 'publication.%%'
                                ORDER BY event.occurred_at DESC, event.id DESC
                                LIMIT 1
                           ),
                           CASE
                               WHEN entity_grant.grant_status = 'withdrawn' THEN 'withdrawn'
                               WHEN EXISTS (
                                   SELECT 1 FROM public.entities AS entity
                                    WHERE entity.entity_grant_id = entity_grant.id
                               ) THEN 'visible'
                               WHEN EXISTS (
                                   SELECT 1 FROM ops.outbox_events AS event
                                    WHERE event.payload ->> 'grant_id'
                              = lower(entity_grant.id::text)
                                      AND event.terminal_at IS NOT NULL
                                      AND event.published_at IS NULL
                               ) THEN 'blocked'
                               ELSE 'pending'
                           END
                      FROM audit.entity_publication_grants AS entity_grant
                     WHERE entity_grant.review_case_id = %s
                  ) AS publication
                 ORDER BY revision_no DESC, grant_id DESC
                 LIMIT 1
                """,
                (case_id, case_id, case_id),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return PublicationState(
            grant_id=cast(UUID, row["grant_id"]),
            revision=cast(int, row["revision_no"]),
            grant_status=GrantStatus(str(row["grant_status"])),
            outbox_event_id=cast(UUID | None, row["outbox_event_id"]),
            projection_state=ProjectionState(str(row["projection_state"])),
        )
