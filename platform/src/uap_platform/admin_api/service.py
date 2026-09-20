"""Admin reads over sanitized review state and writes through frozen public wrappers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.errors import Error as PsycopgError
from psycopg.rows import RowFactory, tuple_row

from uap_platform.object_registry import ObjectClient, read_verified_object
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
    AdoptEditorialRequest,
    AnalysisResultPage,
    AnalysisResultSummary,
    AuditHistoryEvent,
    AuditHistoryPage,
    DocumentDetail,
    DocumentListPage,
    DocumentListSummary,
    EditorialClaimMutationRequest,
    EditorialContent,
    EditorialEntityMutationRequest,
    EditorialPatchRequest,
    EditorialRevisionDetail,
    EditorialRevisionPage,
    EditorialRevisionRestoreRequest,
    EditorialRevisionSummary,
    EntityCandidatePage,
    EntityCandidateSummary,
    EntityType,
    EvidenceSpanPage,
    EvidenceSpanSummary,
    GrantStatus,
    LifecycleRequest,
    ProjectionState,
    PublicationEventPage,
    PublicationEventState,
    PublicationEventSummary,
    PublicationState,
    ReanalysisRequest,
    ReviewCaseDetail,
    ReviewCasePage,
    ReviewCaseSummary,
    ReviewDecision,
    ReviewDecisionSummary,
    TrashDocumentPage,
    TrashDocumentSummary,
    WriteResult,
)
from .cursor import CursorCodec, CursorError, filters_digest
from .errors import AdminError, map_database_error
from .evidence import materialize_ai_evidence
from .pool import AdminApiPool

CASE_SORT = "priority_desc,opened_at_asc,id_asc"
CREATED_SORT = "created_at_asc,id_asc"
OCCURRED_SORT = "occurred_at_asc,id_asc"
REVISION_SORT = "revision_no_desc,id_desc"
REVIEWER_ROLE = "reviewer"
SENIOR_ROLE = "senior_reviewer"
OPERATOR_ROLE = "data_operator"
EDITORIAL_ROLE = "editorial_admin"


class AdminQueryService:
    def __init__(
        self,
        pool: AdminApiPool,
        cursor_codec: CursorCodec,
        object_client: ObjectClient | None = None,
    ) -> None:
        self._pool = pool
        self._cursor = cursor_codec
        self._object_client = object_client

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

    def list_documents(
        self,
        *,
        principal_id: UUID,
        query: str | None,
        limit: int,
        cursor: str | None,
    ) -> DocumentListPage:
        """Return the normal Internal Library projection, excluding trash."""

        clean_query = query.strip() if query else None
        filters = {"q": clean_query}
        digest = filters_digest(filters)
        last_created, last_id = self._created_cursor(cursor, "documents", digest)
        search = f"%{clean_query}%" if clean_query else None
        with self._pool.read_transaction(principal_id) as connection:
            self._require_role(connection, EDITORIAL_ROLE)
            with connection.cursor() as db_cursor:
                db_cursor.execute(
                    """
                    SELECT d.id AS document_id, dv.id AS document_version_id,
                           coalesce(editorial.content ->> 'title', dv.original_title) AS title,
                           d.canonical_url, d.first_seen_at, dv.source_published_at,
                           d.deleted_at, d.deleted_by, d.delete_reason,
                           s.id AS source_id, s.slug AS source_slug, s.name AS source_name,
                           e.outcome AS extraction_outcome,
                           coalesce(ai.valid_count, 0) AS valid_count,
                           coalesce(ai.failed_count, 0) AS failed_count,
                           coalesce(ai.latest_at, d.first_seen_at) AS latest_ai_at,
                           editorial.revision_no AS editorial_revision_no,
                           editorial.created_at AS editorial_created_at
                      FROM core.documents AS d
                      JOIN ingest.sources AS s ON s.id = d.source_id
                      JOIN LATERAL (
                          SELECT item.* FROM core.document_versions AS item
                           WHERE item.document_id = d.id
                           ORDER BY item.version_no DESC, item.id DESC LIMIT 1
                      ) AS dv ON true
                      LEFT JOIN LATERAL (
                          SELECT item.outcome FROM core.extractions AS item
                           WHERE item.document_version_id = dv.id
                           ORDER BY item.created_at DESC, item.id DESC LIMIT 1
                      ) AS e ON true
                      LEFT JOIN LATERAL (
                          SELECT item.revision_no, item.content, item.created_at
                            FROM core.editorial_revisions AS item
                           WHERE item.document_version_id = dv.id
                           ORDER BY item.revision_no DESC, item.id DESC LIMIT 1
                      ) AS editorial ON true
                      LEFT JOIN LATERAL (
                          SELECT count(*) FILTER (
                                     WHERE item.validation_status = 'valid'::core.validation_status
                                 ) AS valid_count,
                                 count(*) FILTER (
                                     WHERE item.validation_status =
                                           'invalid'::core.validation_status
                                 ) AS failed_count,
                                 max(item.created_at) AS latest_at
                            FROM core.analysis_results AS item
                           WHERE item.document_version_id = dv.id
                      ) AS ai ON true
                     WHERE d.deleted_at IS NULL
                       AND (%s::text IS NULL
                            OR dv.original_title ILIKE %s
                            OR s.name ILIKE %s
                            OR coalesce(editorial.content ->> 'title', '') ILIKE %s)
                       AND (%s::timestamptz IS NULL
                            OR (d.first_seen_at, d.id) > (%s, %s::uuid))
                     ORDER BY d.first_seen_at ASC, d.id ASC
                     LIMIT %s
                    """,
                    (
                        search,
                        search,
                        search,
                        search,
                        last_created,
                        last_created,
                        last_id,
                        limit + 1,
                    ),
                )
                rows = db_cursor.fetchall()
        items: list[DocumentListSummary] = []
        for row in rows[:limit]:
            valid_count = int(cast(int, row["valid_count"]))
            failed_count = int(cast(int, row["failed_count"]))
            outcome = str(row["extraction_outcome"]) if row["extraction_outcome"] else None
            if outcome != "succeeded":
                internal_state = "extraction_failed"
            elif failed_count:
                internal_state = "analysis_failed"
            elif valid_count >= 3:
                internal_state = "analysis_ready"
            elif valid_count:
                internal_state = "analysis_partial"
            else:
                internal_state = "analysis_pending"
            editorial_created = cast(datetime | None, row["editorial_created_at"])
            latest_ai = cast(datetime | None, row["latest_ai_at"])
            items.append(
                DocumentListSummary(
                    document_id=cast(UUID, row["document_id"]),
                    document_version_id=cast(UUID, row["document_version_id"]),
                    title=cast(str | None, row["title"]),
                    source={
                        "id": row["source_id"],
                        "slug": row["source_slug"],
                        "name": row["source_name"],
                    },
                    canonical_url=cast(str | None, row["canonical_url"]),
                    source_published_at=cast(datetime | None, row["source_published_at"]),
                    internal_state=internal_state,
                    lifecycle={
                        "trashed": False,
                        "deleted_at": row["deleted_at"],
                        "deleted_by": row["deleted_by"],
                        "reason": row["delete_reason"],
                    },
                    indicators={
                        "has_editorial": row["editorial_revision_no"] is not None,
                        "newer_ai_result_available": (
                            editorial_created is not None
                            and latest_ai is not None
                            and latest_ai > editorial_created
                        ),
                        "trashed": False,
                        "reanalyze_allowed": True,
                    },
                )
            )
        next_cursor = None
        if len(rows) > limit and items:
            tail = rows[limit - 1]
            next_cursor = self._cursor.encode(
                resource="documents",
                sort=CREATED_SORT,
                last=[self._iso(tail["first_seen_at"]), str(tail["document_id"])],
                filters_sha256=digest,
            )
        return DocumentListPage(items=items, next_cursor=next_cursor)

    def get_document_detail(self, principal_id: UUID, document_id: UUID) -> DocumentDetail | None:
        """Return a source/AI/editorial projection without collapsing provenance."""

        with self._pool.read_transaction(principal_id) as connection:
            self._require_role(connection, EDITORIAL_ROLE)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT d.id AS document_id, dv.id AS document_version_id,
                           d.canonical_url, d.deleted_at, d.deleted_by, d.delete_reason,
                           s.id AS source_id, s.slug AS source_slug, s.name AS source_name,
                           dv.original_title, dv.source_published_at, dv.language_code,
                           dv.normalized_content_sha256, e.id AS extraction_id,
                           e.outcome AS extraction_outcome, e.error_code AS extraction_error_code,
                           e.title AS extracted_title, e.author AS extracted_author,
                           e.source_date AS extracted_source_date, e.text_object_id,
                           e.output_sha256 AS extraction_output_sha256,
                           stored.bucket_name AS text_bucket_name,
                           stored.object_key AS text_object_key,
                           stored.content_sha256 AS text_content_sha256,
                           stored.byte_length AS text_byte_length
                      FROM core.documents AS d
                      JOIN ingest.sources AS s ON s.id = d.source_id
                      JOIN LATERAL (
                          SELECT item.* FROM core.document_versions AS item
                           WHERE item.document_id = d.id
                           ORDER BY item.version_no DESC, item.id DESC LIMIT 1
                      ) AS dv ON true
                      LEFT JOIN LATERAL (
                          SELECT item.* FROM core.extractions AS item
                           WHERE item.document_version_id = dv.id
                           ORDER BY item.created_at DESC, item.id DESC LIMIT 1
                      ) AS e ON true
                      LEFT JOIN core.stored_objects AS stored ON stored.id = e.text_object_id
                     WHERE d.id = %s
                    """,
                    (document_id,),
                )
                row = cursor.fetchone()
            if row is None:
                return None
            version_id = UUID(str(row["document_version_id"]))
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT result.id, result.result_type, result.result,
                           result.schema_version, result.validation_status, result.created_at,
                           run.id AS model_run_id, run.provider, run.model, run.input_sha256,
                           run.status AS model_status, run.input_tokens, run.output_tokens,
                           run.cost_minor_units, run.currency, run.error_code,
                           run.started_at, run.finished_at,
                           prompt.id AS prompt_version_id, prompt.version AS prompt_version,
                           prompt.content_sha256 AS prompt_hash
                      FROM core.analysis_results AS result
                      LEFT JOIN ops.model_runs AS run
                        ON run.id = result.model_run_id
                       AND run.document_version_id = result.document_version_id
                      LEFT JOIN ops.prompt_versions AS prompt
                        ON prompt.id = run.prompt_version_id
                     WHERE result.document_version_id = %s
                     ORDER BY result.result_type, result.created_at DESC, result.id DESC
                    """,
                    (version_id,),
                )
                analysis_rows = cursor.fetchall()
                cursor.execute(
                    """
                    SELECT revision.id, revision.revision_no, revision.operation,
                           revision.base_revision_no, revision.content,
                           revision.source_map, revision.adopted_from,
                           revision.created_by, revision.created_at,
                           (
                               SELECT body.content
                                 FROM core.editorial_revisions AS body
                                WHERE body.document_version_id = revision.document_version_id
                                  AND body.content IS NOT NULL
                                ORDER BY body.revision_no DESC, body.id DESC
                                LIMIT 1
                           ) AS latest_content
                      FROM core.editorial_revisions AS revision
                     WHERE revision.document_version_id = %s
                     ORDER BY revision.revision_no DESC, revision.id DESC
                     LIMIT 1
                    """,
                    (version_id,),
                )
                revision = cursor.fetchone()

        source_text = None
        if self._object_client is not None and row["text_object_id"] is not None:
            if (
                row["text_bucket_name"] is not None
                and row["text_object_key"] is not None
                and row["text_content_sha256"] is not None
                and row["text_byte_length"] is not None
            ):
                try:
                    source_text = read_verified_object(
                        self._object_client,
                        str(row["text_bucket_name"]),
                        str(row["text_object_key"]),
                        str(row["text_content_sha256"]),
                        int(cast(int, row["text_byte_length"])),
                    ).decode("utf-8", errors="replace")
                except Exception as error:
                    raise AdminError("api_dependency_unavailable") from error

        ai_results: dict[str, Any] = {}
        latest_ai_at: datetime | None = None
        for item in analysis_rows:
            result_type = str(item["result_type"])
            if result_type in ai_results:
                continue
            created_at = cast(datetime, item["created_at"])
            latest_ai_at = max(latest_ai_at, created_at) if latest_ai_at else created_at
            ai_results[result_type] = {
                "analysis_result_id": item["id"],
                "result": item["result"],
                "schema_version": item["schema_version"],
                "validation_status": item["validation_status"],
                "created_at": created_at,
                "model_run": {
                    "id": item["model_run_id"],
                    "provider": item["provider"],
                    "model": item["model"],
                    "input_sha256": item["input_sha256"],
                    "status": item["model_status"],
                    "input_tokens": item["input_tokens"],
                    "output_tokens": item["output_tokens"],
                    "cost_minor_units": item["cost_minor_units"],
                    "currency": item["currency"],
                    "error_code": item["error_code"],
                    "started_at": item["started_at"],
                    "finished_at": item["finished_at"],
                    "prompt_version_id": item["prompt_version_id"],
                    "prompt_version": item["prompt_version"],
                    "prompt_hash": item["prompt_hash"],
                },
            }

        extraction_outcome = str(row["extraction_outcome"]) if row["extraction_outcome"] else None
        classification_payload = ai_results.get("classification", {}).get("result")
        classification_relevance = (
            classification_payload.get("relevance")
            if isinstance(classification_payload, Mapping)
            else None
        )
        if extraction_outcome != "succeeded":
            internal_state = "extraction_failed"
        elif any(
            value.get("model_run", {}).get("status") in {"failed", "invalid"}
            for value in ai_results.values()
        ):
            internal_state = "analysis_failed"
        elif (
            isinstance(classification_relevance, Mapping)
            and classification_relevance.get("decision") == "irrelevant"
        ):
            internal_state = "not_relevant"
        elif {"summary", "claim_extraction", "entity_extraction"}.issubset(ai_results):
            internal_state = "analysis_ready"
        elif ai_results:
            internal_state = "analysis_partial"
        else:
            internal_state = "analysis_pending"

        revision_row = cast(Mapping[str, Any], revision) if revision is not None else None
        editorial = None
        if revision_row is not None:
            editorial = {
                "revision": EditorialRevisionSummary(
                    id=cast(UUID, revision_row["id"]),
                    document_version_id=version_id,
                    revision_no=cast(int, revision_row["revision_no"]),
                    operation=cast(
                        Literal["save", "adopt", "trash", "restore", "restore_revision"],
                        revision_row["operation"],
                    ),
                    base_revision_no=cast(int, revision_row["base_revision_no"]),
                    created_by=cast(UUID, revision_row["created_by"]),
                    created_at=cast(datetime, revision_row["created_at"]),
                    source_map=cast(dict[str, Any], revision_row["source_map"]),
                    adopted_from=cast(dict[str, Any], revision_row["adopted_from"]),
                ),
                "content": (
                    None
                    if revision_row["content"] is None and revision_row["latest_content"] is None
                    else cast(
                        dict[str, Any],
                        revision_row["content"]
                        if revision_row["content"] is not None
                        else revision_row["latest_content"],
                    )
                ),
            }
        trashed = row["deleted_at"] is not None
        return DocumentDetail(
            document_id=cast(UUID, row["document_id"]),
            document_version_id=version_id,
            source={"id": row["source_id"], "slug": row["source_slug"], "name": row["source_name"]},
            canonical_url=cast(str | None, row["canonical_url"]),
            internal_state=internal_state,
            lifecycle={
                "trashed": trashed,
                "deleted_at": row["deleted_at"],
                "deleted_by": row["deleted_by"],
                "reason": row["delete_reason"],
            },
            raw={
                "original_title": row["original_title"],
                "language_code": row["language_code"],
                "source_published_at": row["source_published_at"],
                "normalized_content_sha256": row["normalized_content_sha256"],
                "source_text": source_text,
                "text_object_id": row["text_object_id"],
                "extraction": {
                    "id": row["extraction_id"],
                    "outcome": row["extraction_outcome"],
                    "error_code": row["extraction_error_code"],
                    "title": row["extracted_title"],
                    "author": row["extracted_author"],
                    "source_date": row["extracted_source_date"],
                    "output_sha256": row["extraction_output_sha256"],
                },
            },
            ai_results=ai_results,
            editorial=editorial,
            indicators={
                "has_editorial": editorial is not None,
                "newer_ai_result_available": (
                    editorial is not None
                    and latest_ai_at is not None
                    and revision_row is not None
                    and latest_ai_at > cast(datetime, revision_row["created_at"])
                ),
                "trashed": trashed,
                "reanalyze_allowed": not trashed,
            },
        )

    def list_trash_documents(
        self, *, principal_id: UUID, limit: int, cursor: str | None
    ) -> TrashDocumentPage:
        with self._pool.read_transaction(principal_id) as connection:
            self._require_role(connection, EDITORIAL_ROLE)
            digest = filters_digest({"resource": "trash"})
            last_deleted, last_id = self._created_cursor(cursor, "trash-documents", digest)
            with connection.cursor() as db_cursor:
                db_cursor.execute(
                    """
                    SELECT d.id AS document_id, dv.id AS document_version_id,
                           coalesce(editorial.content ->> 'title', dv.original_title) AS title,
                           d.deleted_at, d.deleted_by, d.delete_reason,
                           s.id AS source_id, s.slug AS source_slug, s.name AS source_name,
                           coalesce(editorial.revision_no, 0) AS revision_no
                      FROM core.documents AS d
                      JOIN ingest.sources AS s ON s.id = d.source_id
                      JOIN LATERAL (
                          SELECT item.* FROM core.document_versions AS item
                           WHERE item.document_id = d.id
                           ORDER BY item.version_no DESC, item.id DESC LIMIT 1
                      ) AS dv ON true
                      LEFT JOIN LATERAL (
                          SELECT item.revision_no, item.content
                            FROM core.editorial_revisions AS item
                           WHERE item.document_version_id = dv.id
                           ORDER BY item.revision_no DESC, item.id DESC LIMIT 1
                      ) AS editorial ON true
                     WHERE d.deleted_at IS NOT NULL
                       AND (%s::timestamptz IS NULL OR (d.deleted_at, d.id) > (%s, %s::uuid))
                     GROUP BY d.id, dv.id, dv.original_title, editorial.content,
                              editorial.revision_no, d.deleted_at, d.deleted_by,
                              d.delete_reason, s.id, s.slug, s.name
                     ORDER BY d.deleted_at ASC, d.id ASC
                     LIMIT %s
                    """,
                    (last_deleted, last_deleted, last_id, limit + 1),
                )
                rows = db_cursor.fetchall()
        items = [
            TrashDocumentSummary(
                document_id=cast(UUID, row["document_id"]),
                document_version_id=cast(UUID, row["document_version_id"]),
                title=cast(str | None, row["title"]),
                source={
                    "id": row["source_id"],
                    "slug": row["source_slug"],
                    "name": row["source_name"],
                },
                trashed_at=cast(datetime, row["deleted_at"]),
                trashed_by=cast(UUID, row["deleted_by"]),
                reason=cast(str | None, row["delete_reason"]),
                revision_no=cast(int, row["revision_no"]),
            )
            for row in rows[:limit]
        ]
        next_cursor = None
        if len(rows) > limit and items:
            tail = rows[limit - 1]
            next_cursor = self._cursor.encode(
                resource="trash-documents",
                sort=CREATED_SORT,
                last=[self._iso(tail["deleted_at"]), str(tail["document_id"])],
                filters_sha256=digest,
            )
        return TrashDocumentPage(items=items, next_cursor=next_cursor)

    def list_document_audit(
        self, *, principal_id: UUID, document_id: UUID, limit: int, cursor: str | None
    ) -> AuditHistoryPage:
        with self._pool.read_transaction(principal_id) as connection:
            self._require_role(connection, EDITORIAL_ROLE)
            digest = filters_digest({"document_id": str(document_id)})
            if cursor:
                last = self._cursor.decode(
                    cursor, resource="document-audit", sort=OCCURRED_SORT, filters_sha256=digest
                )
                if len(last) != 2:
                    raise CursorError("cursor is invalid")
                try:
                    last_occurred = datetime.fromisoformat(str(last[0]))
                    last_id = UUID(str(last[1]))
                except (TypeError, ValueError) as error:
                    raise CursorError("cursor is invalid") from error
            else:
                last_occurred, last_id = None, None
            with connection.cursor() as db_cursor:
                db_cursor.execute(
                    """
                    SELECT id, event_key, action, actor_id, occurred_at, request_id,
                           target_id, metadata
                      FROM audit.audit_events
                     WHERE target_type = 'document' AND target_id = %s
                       AND (%s::timestamptz IS NULL OR (occurred_at, id) > (%s, %s::uuid))
                     ORDER BY occurred_at ASC, id ASC
                     LIMIT %s
                    """,
                    (document_id, last_occurred, last_occurred, last_id, limit + 1),
                )
                rows = db_cursor.fetchall()
        items = [AuditHistoryEvent(**cast(dict[str, Any], dict(row))) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit and items:
            tail = rows[limit - 1]
            next_cursor = self._cursor.encode(
                resource="document-audit",
                sort=OCCURRED_SORT,
                last=[self._iso(tail["occurred_at"]), str(tail["id"])],
                filters_sha256=digest,
            )
        return AuditHistoryPage(items=items, next_cursor=next_cursor)

    def list_editorial_revisions(
        self,
        *,
        principal_id: UUID,
        document_id: UUID,
        limit: int,
        cursor: str | None,
    ) -> EditorialRevisionPage | None:
        """List immutable editorial history for the document's current version."""

        last_revision, last_id = self._revision_cursor(
            cursor, filters_digest({"document_id": str(document_id)})
        )
        with self._pool.read_transaction(principal_id) as connection:
            self._require_role(connection, EDITORIAL_ROLE)
            with connection.cursor() as db_cursor:
                db_cursor.execute(
                    """
                    SELECT version.id AS document_version_id,
                           revision.id, revision.revision_no, revision.operation,
                           revision.base_revision_no, revision.content,
                           revision.source_map, revision.adopted_from,
                           revision.created_by, revision.created_at,
                           (revision.revision_no = current.current_revision_no) AS is_current,
                           audit_event.metadata ->> 'reason' AS audit_reason
                      FROM core.documents AS document
                      JOIN LATERAL (
                          SELECT item.id
                            FROM core.document_versions AS item
                           WHERE item.document_id = document.id
                           ORDER BY item.version_no DESC, item.id DESC
                           LIMIT 1
                      ) AS version ON true
                      JOIN LATERAL (
                          SELECT max(item.revision_no) AS current_revision_no
                            FROM core.editorial_revisions AS item
                           WHERE item.document_version_id = version.id
                      ) AS current ON true
                      JOIN core.editorial_revisions AS revision
                        ON revision.document_version_id = version.id
                      LEFT JOIN LATERAL (
                          SELECT event.metadata
                            FROM audit.audit_events AS event
                           WHERE event.target_type = 'document'
                             AND event.target_id = document.id
                             AND event.metadata ->> 'revision_no' = revision.revision_no::text
                           ORDER BY event.occurred_at DESC, event.id DESC
                           LIMIT 1
                      ) AS audit_event ON true
                     WHERE document.id = %s
                       AND (
                            %s::integer IS NULL
                            OR revision.revision_no < %s::integer
                            OR (revision.revision_no = %s::integer AND revision.id < %s::uuid)
                       )
                     ORDER BY revision.revision_no DESC, revision.id DESC
                     LIMIT %s
                    """,
                    (document_id, last_revision, last_revision, last_revision, last_id, limit + 1),
                )
                rows = db_cursor.fetchall()
        if not rows:
            # Distinguish an empty history from an unknown document.
            with self._pool.read_transaction(principal_id) as connection:
                self._require_role(connection, EDITORIAL_ROLE)
                with connection.cursor() as db_cursor:
                    db_cursor.execute("SELECT 1 FROM core.documents WHERE id = %s", (document_id,))
                    if db_cursor.fetchone() is None:
                        return None
        items = [
            self._editorial_revision_summary(cast(Mapping[str, Any], row)) for row in rows[:limit]
        ]
        next_cursor = None
        if len(rows) > limit and items:
            tail = cast(Mapping[str, Any], rows[limit - 1])
            next_cursor = self._cursor.encode(
                resource="editorial-revisions",
                sort=REVISION_SORT,
                last=[int(tail["revision_no"]), str(tail["id"])],
                filters_sha256=filters_digest({"document_id": str(document_id)}),
            )
        return EditorialRevisionPage(items=items, next_cursor=next_cursor)

    def get_editorial_revision(
        self, *, principal_id: UUID, document_id: UUID, revision_ref: str
    ) -> EditorialRevisionDetail | None:
        revision_no: int | None = None
        revision_id: UUID | None = None
        try:
            revision_no = int(revision_ref)
        except ValueError:
            try:
                revision_id = UUID(revision_ref)
            except ValueError as error:
                raise AdminError("api_request_invalid") from error
        with self._pool.read_transaction(principal_id) as connection:
            self._require_role(connection, EDITORIAL_ROLE)
            with connection.cursor() as db_cursor:
                db_cursor.execute(
                    """
                    SELECT version.id AS document_version_id,
                           revision.id, revision.revision_no, revision.operation,
                           revision.base_revision_no, revision.content,
                           revision.source_map, revision.adopted_from,
                           revision.created_by, revision.created_at,
                           (revision.revision_no = current.current_revision_no) AS is_current,
                           audit_event.metadata ->> 'reason' AS audit_reason
                      FROM core.documents AS document
                      JOIN LATERAL (
                          SELECT item.id
                            FROM core.document_versions AS item
                           WHERE item.document_id = document.id
                           ORDER BY item.version_no DESC, item.id DESC LIMIT 1
                      ) AS version ON true
                      JOIN LATERAL (
                          SELECT max(item.revision_no) AS current_revision_no
                            FROM core.editorial_revisions AS item
                           WHERE item.document_version_id = version.id
                      ) AS current ON true
                      JOIN core.editorial_revisions AS revision
                        ON revision.document_version_id = version.id
                      LEFT JOIN LATERAL (
                          SELECT event.metadata
                            FROM audit.audit_events AS event
                           WHERE event.target_type = 'document'
                             AND event.target_id = document.id
                             AND event.metadata ->> 'revision_no' = revision.revision_no::text
                           ORDER BY event.occurred_at DESC, event.id DESC
                           LIMIT 1
                      ) AS audit_event ON true
                     WHERE document.id = %s
                       AND ((%s::integer IS NOT NULL AND revision.revision_no = %s::integer)
                            OR (%s::uuid IS NOT NULL AND revision.id = %s::uuid))
                    """,
                    (document_id, revision_no, revision_no, revision_id, revision_id),
                )
                row = db_cursor.fetchone()
        if row is None:
            return None
        summary = self._editorial_revision_summary(cast(Mapping[str, Any], row))
        content = row["content"]
        return EditorialRevisionDetail(
            **summary.model_dump(),
            content=(EditorialContent.model_validate(content) if content is not None else None),
        )

    def restore_editorial_revision(
        self,
        *,
        principal_id: UUID,
        request_id: UUID,
        document_id: UUID,
        revision_ref: str,
        request: EditorialRevisionRestoreRequest,
    ) -> WriteResult:
        source_revision_id: UUID | None = None
        try:
            source_revision_id = UUID(revision_ref)
            source_revision_no: int | None = None
        except ValueError:
            try:
                source_revision_no = int(revision_ref)
            except ValueError as error:
                raise AdminError("api_request_invalid") from error

        def _call(connection: Connection[dict[str, object]]) -> UUID:
            self._require_role(connection, EDITORIAL_ROLE)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT revision.id
                      FROM core.documents AS document
                      JOIN LATERAL (
                          SELECT item.id
                            FROM core.document_versions AS item
                           WHERE item.document_id = document.id
                           ORDER BY item.version_no DESC, item.id DESC LIMIT 1
                      ) AS version ON true
                      JOIN core.editorial_revisions AS revision
                        ON revision.document_version_id = version.id
                     WHERE document.id = %s
                       AND ((%s::integer IS NOT NULL AND revision.revision_no = %s::integer)
                            OR (%s::uuid IS NOT NULL AND revision.id = %s::uuid))
                       AND revision.content IS NOT NULL
                    """,
                    (
                        document_id,
                        source_revision_no,
                        source_revision_no,
                        source_revision_id,
                        source_revision_id,
                    ),
                )
                source = cursor.fetchone()
                if source is None:
                    raise AdminError("editorial_revision_not_found")
                cursor.execute(
                    """
                    SELECT audit.restore_editorial_revision(%s, %s, %s, %s)
                    """,
                    (
                        document_id,
                        source["id"],
                        request.expected_revision,
                        request.reason,
                    ),
                )
                row = cursor.fetchone()
            if row is None:
                raise AdminError("api_internal_error")
            return self._scalar_uuid(row, "restore_editorial_revision")

        return self._write_editorial(principal_id, request_id, "editorial.revision_restore", _call)

    def _load_editorial_context(
        self, connection: Connection[dict[str, object]], document_id: UUID, version_id: UUID
    ) -> tuple[UUID, int, dict[str, Any], bool]:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT document.id, document.deleted_at, version.original_title
                  FROM core.documents AS document
                  JOIN core.document_versions AS version ON version.document_id = document.id
                 WHERE document.id = %s AND version.id = %s
                """,
                (document_id, version_id),
            )
            document = cursor.fetchone()
            if document is None:
                raise AdminError("editorial_document_version_mismatch")
            cursor.execute(
                """
                SELECT coalesce(max(revision_no), 0) AS revision_no
                  FROM core.editorial_revisions
                 WHERE document_version_id = %s
                """,
                (version_id,),
            )
            revision = cursor.fetchone()
            cursor.execute(
                """
                SELECT content
                  FROM core.editorial_revisions
                 WHERE document_version_id = %s AND content IS NOT NULL
                 ORDER BY revision_no DESC, id DESC
                 LIMIT 1
                """,
                (version_id,),
            )
            content_row = cursor.fetchone()
        revision_row = cast(Mapping[str, Any], revision) if revision is not None else None
        content_map = cast(Mapping[str, Any], content_row) if content_row is not None else None
        content = (
            cast(dict[str, Any], content_map["content"])
            if content_map is not None and content_map["content"] is not None
            else {
                "title": document["original_title"] or "Untitled document",
                "summary": None,
                "bullets": [],
                "category": "other",
                "labels": [],
                "claims": [],
                "entities": [],
            }
        )
        return (
            UUID(str(document["id"])),
            int(revision_row["revision_no"]) if revision_row is not None else 0,
            dict(content),
            document["deleted_at"] is not None,
        )

    @staticmethod
    def _validate_editorial_content(content: Mapping[str, object]) -> EditorialContent:
        try:
            return EditorialContent.model_validate(content)
        except Exception as error:
            raise AdminError("api_request_invalid") from error

    def save_editorial(
        self,
        *,
        principal_id: UUID,
        request_id: UUID,
        document_id: UUID,
        request: EditorialPatchRequest,
    ) -> WriteResult:
        def _call(connection: Connection[dict[str, object]]) -> UUID:
            self._require_role(connection, EDITORIAL_ROLE)
            _, current_revision, current, trashed = self._load_editorial_context(
                connection, document_id, request.document_version_id
            )
            if trashed:
                raise AdminError("editorial_document_trashed")
            if current_revision != request.expected_revision:
                raise AdminError("editorial_revision_conflict")
            current.update(request.changes())
            validated = self._validate_editorial_content(current)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT audit.save_editorial_revision(%s, %s, %s::jsonb, %s::jsonb, %s::jsonb)",
                    (
                        request.document_version_id,
                        request.expected_revision,
                        json.dumps(validated.model_dump(mode="json"), sort_keys=True),
                        json.dumps({field: {"source": "editorial"} for field in request.changes()}),
                        json.dumps({}),
                    ),
                )
                row = cursor.fetchone()
            if row is None:
                raise AdminError("api_internal_error")
            return self._scalar_uuid(row, "save_editorial_revision")

        return self._write_editorial(
            principal_id,
            request_id,
            "editorial.save",
            _call,
        )

    def _validate_evidence_ids(
        self, connection: Connection[dict[str, object]], version_id: UUID, ids: list[UUID]
    ) -> None:
        if not ids:
            return
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, document_version_id, extraction_id
                  FROM core.evidence_spans
                 WHERE id = ANY(%s::uuid[])
                """,
                (ids,),
            )
            rows = cursor.fetchall()
        found = {UUID(str(row["id"])) for row in rows}
        if len(found) != len(set(ids)):
            raise AdminError("editorial_evidence_not_found")
        for row in rows:
            if UUID(str(row["document_version_id"])) != version_id:
                raise AdminError("editorial_evidence_version_mismatch")
            if row["extraction_id"] is None:
                raise AdminError("editorial_evidence_extraction_mismatch")

    def mutate_editorial_claim(
        self,
        *,
        principal_id: UUID,
        request_id: UUID,
        document_id: UUID,
        operation: Literal["add", "edit", "remove", "restore", "remove_evidence"],
        request: EditorialClaimMutationRequest,
    ) -> WriteResult:
        def _call(connection: Connection[dict[str, object]]) -> UUID:
            self._require_role(connection, EDITORIAL_ROLE)
            _, revision, current, trashed = self._load_editorial_context(
                connection, document_id, request.document_version_id
            )
            if trashed:
                raise AdminError("editorial_document_trashed")
            if revision != request.expected_revision:
                raise AdminError("editorial_revision_conflict")
            claims = list(current.get("claims") or [])
            target_id = request.claim_id or (request.claim.claim_id if request.claim else None)
            index = next(
                (
                    i
                    for i, item in enumerate(claims)
                    if target_id and item.get("claim_id") == str(target_id)
                ),
                None,
            )
            if index is None and request.item_ordinal is not None:
                index = request.item_ordinal if request.item_ordinal < len(claims) else None
            if operation == "add":
                if request.claim is None:
                    raise AdminError("editorial_evidence_request_invalid")
                item = request.claim.model_dump(mode="json")
                item["claim_id"] = item.get("claim_id") or str(uuid4())
                if request.evidence_span_ids is not None:
                    item["evidence_span_ids"] = [str(value) for value in request.evidence_span_ids]
                self._validate_evidence_ids(
                    connection,
                    request.document_version_id,
                    [UUID(str(value)) for value in item.get("evidence_span_ids", [])],
                )
                claims.append(item)
                action = "editorial.claim.added"
            else:
                if index is None:
                    raise AdminError("editorial_ai_item_not_found")
                item = dict(claims[index])
                if operation == "edit":
                    if request.claim is None:
                        raise AdminError("editorial_evidence_request_invalid")
                    item.update(request.claim.model_dump(mode="json"))
                    item["claim_id"] = item.get("claim_id") or (
                        str(target_id) if target_id else str(uuid4())
                    )
                    action = "editorial.claim.edited"
                elif operation == "remove":
                    item["state"] = "removed"
                    action = "editorial.claim.removed"
                elif operation == "restore":
                    item["state"] = "active"
                    action = "editorial.claim.restored"
                else:
                    item["evidence_span_ids"] = []
                    action = "editorial.claim.evidence_removed"
                if request.evidence_span_ids is not None:
                    item["evidence_span_ids"] = [str(value) for value in request.evidence_span_ids]
                self._validate_evidence_ids(
                    connection,
                    request.document_version_id,
                    [UUID(str(value)) for value in item.get("evidence_span_ids", [])],
                )
                claims[index] = item
            current["claims"] = claims
            validated = self._validate_editorial_content(current)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT audit.save_editorial_revision(%s, %s, %s::jsonb, %s::jsonb, %s::jsonb)",
                    (
                        request.document_version_id,
                        request.expected_revision,
                        json.dumps(validated.model_dump(mode="json"), sort_keys=True),
                        json.dumps(
                            {"claims": {"source": "editorial", "action": action}}, sort_keys=True
                        ),
                        json.dumps(
                            {"action": action, "item_id": item.get("claim_id")}, sort_keys=True
                        ),
                    ),
                )
                row = cursor.fetchone()
            if row is None:
                raise AdminError("api_internal_error")
            return self._scalar_uuid(row, "save_editorial_revision")

        return self._write_editorial(
            principal_id, request_id, f"editorial.claim.{operation}", _call
        )

    def mutate_editorial_entity(
        self,
        *,
        principal_id: UUID,
        request_id: UUID,
        document_id: UUID,
        operation: Literal["add", "edit", "remove", "restore", "remove_evidence"],
        request: EditorialEntityMutationRequest,
    ) -> WriteResult:
        def _call(connection: Connection[dict[str, object]]) -> UUID:
            self._require_role(connection, EDITORIAL_ROLE)
            _, revision, current, trashed = self._load_editorial_context(
                connection, document_id, request.document_version_id
            )
            if trashed:
                raise AdminError("editorial_document_trashed")
            if revision != request.expected_revision:
                raise AdminError("editorial_revision_conflict")
            entities = list(current.get("entities") or [])
            target_id = request.entity_id or (request.entity.entity_id if request.entity else None)
            index = next(
                (
                    i
                    for i, item in enumerate(entities)
                    if target_id and item.get("entity_id") == str(target_id)
                ),
                None,
            )
            if index is None and request.item_ordinal is not None:
                index = request.item_ordinal if request.item_ordinal < len(entities) else None
            if operation == "add":
                if request.entity is None:
                    raise AdminError("editorial_evidence_request_invalid")
                item = request.entity.model_dump(mode="json")
                item["entity_id"] = item.get("entity_id") or str(uuid4())
                if request.evidence_span_ids is not None:
                    item["evidence_span_ids"] = [str(value) for value in request.evidence_span_ids]
                self._validate_evidence_ids(
                    connection,
                    request.document_version_id,
                    [UUID(str(value)) for value in item.get("evidence_span_ids", [])],
                )
                entities.append(item)
                action = "editorial.entity.added"
            else:
                if index is None:
                    raise AdminError("editorial_ai_item_not_found")
                item = dict(entities[index])
                if operation == "edit":
                    if request.entity is None:
                        raise AdminError("editorial_evidence_request_invalid")
                    item.update(request.entity.model_dump(mode="json"))
                    item["entity_id"] = item.get("entity_id") or (
                        str(target_id) if target_id else str(uuid4())
                    )
                    action = "editorial.entity.edited"
                elif operation == "remove":
                    item["state"] = "removed"
                    action = "editorial.entity.removed"
                elif operation == "restore":
                    item["state"] = "active"
                    action = "editorial.entity.restored"
                else:
                    item["evidence_span_ids"] = []
                    action = "editorial.entity.evidence_removed"
                if request.evidence_span_ids is not None:
                    item["evidence_span_ids"] = [str(value) for value in request.evidence_span_ids]
                self._validate_evidence_ids(
                    connection,
                    request.document_version_id,
                    [UUID(str(value)) for value in item.get("evidence_span_ids", [])],
                )
                entities[index] = item
            current["entities"] = entities
            validated = self._validate_editorial_content(current)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT audit.save_editorial_revision(%s, %s, %s::jsonb, %s::jsonb, %s::jsonb)",
                    (
                        request.document_version_id,
                        request.expected_revision,
                        json.dumps(validated.model_dump(mode="json"), sort_keys=True),
                        json.dumps(
                            {"entities": {"source": "editorial", "action": action}}, sort_keys=True
                        ),
                        json.dumps(
                            {"action": action, "item_id": item.get("entity_id")}, sort_keys=True
                        ),
                    ),
                )
                row = cursor.fetchone()
            if row is None:
                raise AdminError("api_internal_error")
            return self._scalar_uuid(row, "save_editorial_revision")

        return self._write_editorial(
            principal_id, request_id, f"editorial.entity.{operation}", _call
        )

    def adopt_editorial(
        self,
        *,
        principal_id: UUID,
        request_id: UUID,
        document_id: UUID,
        request: AdoptEditorialRequest,
    ) -> WriteResult:
        def _call(connection: Connection[dict[str, object]]) -> UUID:
            self._require_role(connection, EDITORIAL_ROLE)
            _, current_revision, current, trashed = self._load_editorial_context(
                connection, document_id, request.document_version_id
            )
            if trashed:
                raise AdminError("editorial_document_trashed")
            if current_revision != request.expected_revision:
                raise AdminError("editorial_revision_conflict")
            if len(set(request.fields)) != len(request.fields):
                raise AdminError("editorial_adopt_field_invalid")
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT document_version_id, result_type, validation_status, result
                      FROM core.analysis_results
                     WHERE id = %s
                    """,
                    (request.source_analysis_result_id,),
                )
                result = cursor.fetchone()
            if result is None:
                raise AdminError("editorial_ai_result_invalid")
            if UUID(str(result["document_version_id"])) != request.document_version_id:
                raise AdminError("editorial_ai_result_foreign")
            if str(result["validation_status"]) != "valid":
                raise AdminError("editorial_ai_result_invalid")
            result_payload = result["result"]
            if not isinstance(result_payload, Mapping):
                raise AdminError("editorial_ai_result_invalid")
            task_type = str(result["result_type"])
            task_fields = {
                "summary": {"summary", "bullets"},
                "classification": {"category", "labels"},
                "claim_extraction": {"claims"},
                "entity_extraction": {"entities"},
            }.get(task_type, set())
            if not set(request.fields).issubset(task_fields):
                raise AdminError("editorial_adopt_field_invalid")
            changes: dict[str, object] = {}
            for field in request.fields:
                if field in request.values:
                    changes[field] = request.values[field]
                elif field == "category":
                    changes[field] = result_payload.get("suggested_document_category")
                elif field in result_payload:
                    changes[field] = result_payload[field]
                else:
                    raise AdminError("editorial_adopt_field_invalid")
            if task_type in {"claim_extraction", "entity_extraction"} and (
                "claims" in request.fields or "entities" in request.fields
            ):
                resolved = materialize_ai_evidence(
                    connection,
                    self._object_client,
                    analysis_result_id=request.source_analysis_result_id,
                    document_version_id=request.document_version_id,
                    result_type=task_type,
                    item_ordinal=request.item_ordinal,
                )
                source_items = result_payload.get(
                    "claims" if task_type == "claim_extraction" else "entities"
                )
                if not isinstance(source_items, Sequence) or isinstance(source_items, (str, bytes)):
                    raise AdminError("editorial_ai_result_invalid")
                adopted_items: list[dict[str, object]] = []
                for ordinal, raw_item in enumerate(source_items):
                    if request.item_ordinal is not None and ordinal != request.item_ordinal:
                        continue
                    if not isinstance(raw_item, Mapping):
                        raise AdminError("editorial_ai_result_invalid")
                    item = dict(raw_item)
                    item.pop("evidence", None)
                    if task_type == "claim_extraction":
                        item = {
                            "claim_id": str(uuid4()),
                            "claim": item.get("claim", ""),
                            "source_statement": item.get("source_statement", ""),
                            "speaker": item.get("speaker"),
                            "claim_type": item.get("claim_type", "other"),
                            "assertion_status": item.get("assertion_status", "unverified"),
                            "evidence_span_ids": [
                                str(value)
                                for value in resolved.get(
                                    0 if request.item_ordinal is not None else ordinal, []
                                )
                            ],
                            "state": "active",
                        }
                    else:
                        item = {
                            "entity_id": str(uuid4()),
                            "name": item.get("name", ""),
                            "entity_type": item.get("entity_type", "concept"),
                            "aliases": item.get("aliases", []),
                            "evidence_span_ids": [
                                str(value)
                                for value in resolved.get(
                                    0 if request.item_ordinal is not None else ordinal, []
                                )
                            ],
                            "state": "active",
                        }
                    adopted_items.append(item)
                changes["claims" if task_type == "claim_extraction" else "entities"] = adopted_items
            current.update(changes)
            validated = self._validate_editorial_content(current)
            source_map = {
                field: {
                    "source": "ai",
                    "analysis_result_id": str(request.source_analysis_result_id),
                }
                for field in request.fields
            }
            adopted_from: dict[str, Any] = {
                "analysis_result_id": str(request.source_analysis_result_id),
                "result_type": task_type,
                "fields": list(request.fields),
            }
            if task_type in {"claim_extraction", "entity_extraction"} and (
                "claims" in request.fields or "entities" in request.fields
            ):
                if request.item_ordinal is not None:
                    adopted_from["item_ordinal"] = request.item_ordinal
                evidence_ids: list[str] = []
                evidence_items = cast(
                    Sequence[object], changes.get("claims") or changes.get("entities") or []
                )
                adopted_from["item_ordinals"] = [
                    request.item_ordinal if request.item_ordinal is not None else index
                    for index, _item in enumerate(evidence_items)
                ]
                for evidence_item in evidence_items:
                    if isinstance(evidence_item, Mapping):
                        evidence_ids.extend(
                            str(value) for value in evidence_item.get("evidence_span_ids", [])
                        )
                adopted_from["resolved_evidence_span_ids"] = evidence_ids
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT audit.adopt_editorial_suggestion("
                    "%s, %s, %s::jsonb, %s::jsonb, %s::jsonb)",
                    (
                        request.document_version_id,
                        request.expected_revision,
                        json.dumps(validated.model_dump(mode="json"), sort_keys=True),
                        json.dumps(source_map, sort_keys=True),
                        json.dumps(adopted_from, sort_keys=True),
                    ),
                )
                row = cursor.fetchone()
            if row is None:
                raise AdminError("api_internal_error")
            return self._scalar_uuid(row, "adopt_editorial_suggestion")

        return self._write_editorial(principal_id, request_id, "editorial.adopt", _call)

    def request_editorial_reanalysis(
        self,
        *,
        principal_id: UUID,
        request_id: UUID,
        document_id: UUID,
        request: ReanalysisRequest,
    ) -> WriteResult:
        def _call(connection: Connection[dict[str, object]]) -> UUID:
            self._require_role(connection, EDITORIAL_ROLE)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT audit.request_editorial_reanalysis(%s, %s, %s, %s)",
                    (document_id, request.document_version_id, request.task_type, request.reason),
                )
                row = cursor.fetchone()
            if row is None:
                raise AdminError("api_internal_error")
            return self._scalar_uuid(row, "request_editorial_reanalysis")

        return self._write_editorial(principal_id, request_id, "reanalysis.request", _call)

    def trash_document(
        self,
        *,
        principal_id: UUID,
        request_id: UUID,
        document_id: UUID,
        request: LifecycleRequest,
    ) -> WriteResult:
        def _call(connection: Connection[dict[str, object]]) -> UUID:
            self._require_role(connection, EDITORIAL_ROLE)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT audit.trash_document(%s, %s, %s)",
                    (document_id, request.expected_revision, request.reason),
                )
                row = cursor.fetchone()
            if row is None:
                raise AdminError("api_internal_error")
            return self._scalar_uuid(row, "trash_document")

        return self._write_editorial(principal_id, request_id, "document.trash", _call)

    def restore_document(
        self,
        *,
        principal_id: UUID,
        request_id: UUID,
        document_id: UUID,
        request: LifecycleRequest,
    ) -> WriteResult:
        def _call(connection: Connection[dict[str, object]]) -> UUID:
            self._require_role(connection, EDITORIAL_ROLE)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT audit.restore_document(%s, %s, %s)",
                    (document_id, request.expected_revision, request.reason),
                )
                row = cursor.fetchone()
            if row is None:
                raise AdminError("api_internal_error")
            return self._scalar_uuid(row, "restore_document")

        return self._write_editorial(principal_id, request_id, "document.restore", _call)

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

    def _write_editorial(
        self,
        principal_id: UUID,
        request_id: UUID,
        operation: str,
        call: Callable[[Connection[dict[str, object]]], UUID],
    ) -> WriteResult:
        with self._pool.write_transaction(principal_id, request_id) as connection:
            try:
                result_id = call(connection)
            except ReviewSessionError as error:
                raise AdminError(error.code) from error
            except PsycopgError as error:
                raise map_database_error(error) from error
            return WriteResult(
                operation=operation,
                resource_id=result_id,
                request_id=request_id,
                publication=None,
            )

    @staticmethod
    def _scalar_uuid(row: object, column: str) -> UUID:
        if isinstance(row, Mapping):
            value = row.get(column)
        else:
            value = cast(Sequence[Any], row)[0]
        if value is None:
            raise AdminError("api_internal_error")
        return UUID(str(value))

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

    def _revision_cursor(self, cursor: str | None, digest: str) -> tuple[int | None, UUID | None]:
        if not cursor:
            return None, None
        last = self._cursor.decode(
            cursor, resource="editorial-revisions", sort=REVISION_SORT, filters_sha256=digest
        )
        if len(last) != 2:
            raise CursorError("cursor is invalid")
        try:
            revision_no = int(last[0])
            revision_id = UUID(str(last[1]))
        except (TypeError, ValueError) as error:
            raise CursorError("cursor is invalid") from error
        if revision_no < 1 or str(revision_id) != str(last[1]):
            raise CursorError("cursor is invalid")
        return revision_no, revision_id

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
    def _editorial_revision_summary(row: Mapping[str, Any]) -> EditorialRevisionSummary:
        content = row.get("content")
        adopted_from = dict(row.get("adopted_from") or {})
        source_map = dict(row.get("source_map") or {})
        source_id_raw = adopted_from.get("source_revision_id") or source_map.get(
            "source_revision_id"
        )
        source_id: UUID | None = None
        if source_id_raw:
            try:
                source_id = UUID(str(source_id_raw))
            except ValueError:
                source_id = None
        source_no_raw = adopted_from.get("source_revision_no") or source_map.get(
            "source_revision_no"
        )
        source_no = int(source_no_raw) if source_no_raw is not None else None
        reason = (
            row.get("reason")
            or row.get("audit_reason")
            or adopted_from.get("reason")
            or source_map.get("reason")
        )
        digest = None
        if content is not None:
            digest = hashlib.sha256(
                json.dumps(
                    content, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode("utf-8")
            ).hexdigest()
        return EditorialRevisionSummary(
            id=UUID(str(row["id"])),
            document_version_id=UUID(str(row["document_version_id"])),
            revision_no=int(row["revision_no"]),
            operation=cast(
                Literal["save", "adopt", "trash", "restore", "restore_revision"],
                str(row["operation"]),
            ),
            base_revision_no=int(row["base_revision_no"]),
            created_by=UUID(str(row["created_by"])),
            created_at=row["created_at"],
            source_map=source_map,
            adopted_from=adopted_from,
            is_current=bool(row.get("is_current", False)),
            content_digest=digest,
            reason=str(reason) if reason is not None else None,
            source_revision_id=source_id,
            source_revision_no=source_no,
        )

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
