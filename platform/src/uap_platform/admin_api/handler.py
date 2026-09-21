"""Framework-independent HTTP routing for the frozen Admin API."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Literal, cast
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import psycopg
from pydantic import ValidationError

from .contracts import (
    AdoptEditorialRequest,
    AssignmentRequest,
    BindCandidateRequest,
    DecisionRequest,
    EditorialClaimMutationRequest,
    EditorialEntityMutationRequest,
    EditorialPatchRequest,
    EditorialRevisionRestoreRequest,
    EntityType,
    LifecycleRequest,
    ManualClaimRequest,
    MergeRequest,
    OpenCaseRequest,
    Problem,
    PublicationEventState,
    PublicationReviewRequest,
    ReanalysisRequest,
    ReasonRequest,
    ReviewCaseType,
    ReviewStatus,
    StrictModel,
)
from .cursor import CursorError
from .errors import AdminError
from .oidc import OidcValidator, TokenError
from .service import AdminQueryService

LOGGER = logging.getLogger(__name__)
_MAX_TARGET_LENGTH = 8192
_MAX_BODY_LENGTH = 1_000_000
_CLOSED_PATHS = frozenset(
    {
        "/admin/v1/grants",
        "/admin/v1/relations",
        "/v1/relations",
    }
)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class AdminApiApplication:
    def __init__(self, service: AdminQueryService, oidc: OidcValidator, issuer: str) -> None:
        self._service = service
        self._oidc = oidc
        self._issuer = issuer

    def handle(
        self,
        method: str,
        target: str,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:
        request_headers = headers or {}
        request_id = uuid4()
        try:
            if len(target) > _MAX_TARGET_LENGTH:
                raise AdminError("api_resource_not_found")
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or parsed.fragment:
                raise AdminError("api_request_invalid")
            path = parsed.path
            try:
                query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
            except ValueError as error:
                raise AdminError("api_request_invalid") from error
            if any(len(values) != 1 for values in query.values()):
                raise AdminError("api_request_invalid")
            if path == "/healthz":
                self._require_query(query, set())
                if method != "GET":
                    raise AdminError("api_resource_not_found")
                request_id = self._header_request_id(request_headers)
                return self._health(request_id)
            if path in _CLOSED_PATHS or (
                path.startswith("/admin/v1/relations/") and path.count("/") == 4
            ):
                request_id = self._header_request_id(request_headers)
                raise AdminError("api_capability_closed")
            if not path.startswith("/admin/v1/"):
                request_id = self._header_request_id(request_headers)
                raise AdminError("api_resource_not_found")
            if method == "PATCH" and not path.startswith("/admin/v1/documents/"):
                request_id = self._header_request_id(request_headers)
                raise AdminError("api_resource_not_found")
            request_id = self._read_request_id(method, request_headers)
            principal_id = self._authenticate(request_headers)
            if method == "GET":
                return self._dispatch_read(path, query, principal_id, request_id)
            if method in {"POST", "PUT", "PATCH"}:
                payload = self._parse_body(body)
                return self._dispatch_write(method, path, query, payload, principal_id, request_id)
            raise AdminError("api_resource_not_found")
        except CursorError:
            return self._problem(request_id, AdminError("api_cursor_invalid"))
        except TokenError as error:
            return self._problem(request_id, AdminError(error.code))
        except AdminError as problem:
            return self._problem(request_id, problem)
        except (psycopg.Error, TimeoutError):
            LOGGER.warning("admin API database dependency unavailable request_id=%s", request_id)
            return self._problem(request_id, AdminError("api_dependency_unavailable"))
        except Exception:
            LOGGER.exception(
                "admin API internal error request_id=%s code=api_internal_error", request_id
            )
            return self._problem(request_id, AdminError("api_internal_error"))

    def _dispatch_read(
        self,
        path: str,
        query: Mapping[str, list[str]],
        principal_id: UUID,
        request_id: UUID,
    ) -> HttpResponse:
        if path == "/admin/v1/documents/trash":
            self._require_query(query, {"limit", "cursor"})
            return self._success(
                request_id,
                self._service.list_trash_documents(
                    principal_id=principal_id,
                    limit=self._limit(query),
                    cursor=self._optional(query, "cursor"),
                ),
            )
        if path.startswith("/admin/v1/documents/") and path.endswith("/publication"):
            self._require_query(query, set())
            if path.count("/") != 5:
                raise AdminError("api_resource_not_found")
            return self._resource(
                request_id,
                self._service.get_publication_status(
                    principal_id=principal_id,
                    document_id=self._nested_uuid(path, -2),
                ),
            )
        if path.startswith("/admin/v1/documents/") and path.endswith("/publication-status"):
            self._require_query(query, set())
            if path.count("/") != 5:
                raise AdminError("api_resource_not_found")
            return self._resource(
                request_id,
                self._service.get_publication_status(
                    principal_id=principal_id,
                    document_id=self._nested_uuid(path, -2),
                ),
            )
        if path == "/admin/v1/documents":
            self._require_query(query, {"q", "limit", "cursor"})
            return self._success(
                request_id,
                self._service.list_documents(
                    principal_id=principal_id,
                    query=self._optional(query, "q"),
                    limit=self._limit(query),
                    cursor=self._optional(query, "cursor"),
                ),
            )
        if path.startswith("/admin/v1/documents/") and path.count("/") == 4:
            self._require_query(query, set())
            return self._resource(
                request_id,
                self._service.get_document_detail(principal_id, self._path_uuid(path)),
            )
        if path.startswith("/admin/v1/documents/") and path.endswith("/audit"):
            self._require_query(query, {"limit", "cursor"})
            parts = path.split("/")
            if len(parts) != 6:
                raise AdminError("api_resource_not_found")
            return self._success(
                request_id,
                self._service.list_document_audit(
                    principal_id=principal_id,
                    document_id=self._nested_uuid(path, -2),
                    limit=self._limit(query),
                    cursor=self._optional(query, "cursor"),
                ),
            )
        if path.startswith("/admin/v1/documents/") and path.endswith("/editorial/revisions"):
            self._require_query(query, {"limit", "cursor"})
            parts = path.split("/")
            if len(parts) != 7:
                raise AdminError("api_resource_not_found")
            return self._resource(
                request_id,
                self._service.list_editorial_revisions(
                    principal_id=principal_id,
                    document_id=self._nested_uuid(path, -3),
                    limit=self._limit(query),
                    cursor=self._optional(query, "cursor"),
                ),
            )
        if path.startswith("/admin/v1/documents/") and "/editorial/revisions/" in path:
            self._require_query(query, set())
            parts = path.split("/")
            if len(parts) != 8 or parts[-2] == "restore":
                raise AdminError("api_resource_not_found")
            return self._resource(
                request_id,
                self._service.get_editorial_revision(
                    principal_id=principal_id,
                    document_id=self._nested_uuid(path, -4),
                    revision_ref=parts[-1],
                ),
            )
        if path == "/admin/v1/review-cases":
            self._require_query(query, {"status", "case_type", "assigned_to", "limit", "cursor"})
            return self._success(
                request_id,
                self._service.list_review_cases(
                    principal_id=principal_id,
                    status=self._enum(query, "status", ReviewStatus),
                    case_type=self._enum(query, "case_type", ReviewCaseType),
                    assigned_to=self._optional_uuid(query, "assigned_to"),
                    limit=self._limit(query),
                    cursor=self._optional(query, "cursor"),
                ),
            )
        if path.startswith("/admin/v1/review-cases/") and path.count("/") == 4:
            self._require_query(query, set())
            value = self._service.get_review_case(principal_id, self._path_uuid(path))
            return self._resource(request_id, value)
        if path == "/admin/v1/analysis-results":
            self._require_query(
                query,
                {"document_version_id", "result_type", "validation_status", "limit", "cursor"},
            )
            return self._success(
                request_id,
                self._service.list_analysis_results(
                    principal_id=principal_id,
                    document_version_id=self._optional_uuid(query, "document_version_id"),
                    result_type=self._optional(query, "result_type"),
                    validation_status=self._optional(query, "validation_status"),
                    limit=self._limit(query),
                    cursor=self._optional(query, "cursor"),
                ),
            )
        if path == "/admin/v1/entity-candidates":
            self._require_query(query, {"status", "analysis_result_id", "limit", "cursor"})
            return self._success(
                request_id,
                self._service.list_entity_candidates(
                    principal_id=principal_id,
                    status=self._optional(query, "status"),
                    analysis_result_id=self._optional_uuid(query, "analysis_result_id"),
                    limit=self._limit(query),
                    cursor=self._optional(query, "cursor"),
                ),
            )
        if path == "/admin/v1/evidence-spans":
            self._require_query(query, {"document_version_id", "limit", "cursor"})
            document_version_id = self._optional_uuid(query, "document_version_id")
            if document_version_id is None:
                raise AdminError("api_request_invalid")
            return self._success(
                request_id,
                self._service.list_evidence_spans(
                    principal_id=principal_id,
                    document_version_id=document_version_id,
                    limit=self._limit(query),
                    cursor=self._optional(query, "cursor"),
                ),
            )
        if path == "/admin/v1/entities":
            self._require_query(query, {"q", "status", "type", "limit", "cursor"})
            return self._success(
                request_id,
                self._service.list_entities(
                    principal_id=principal_id,
                    query=self._optional(query, "q"),
                    status=self._optional(query, "status"),
                    entity_type=self._enum(query, "type", EntityType),
                    limit=self._limit(query),
                    cursor=self._optional(query, "cursor"),
                ),
            )
        if path == "/admin/v1/publication-events":
            self._require_query(query, {"state", "limit", "cursor"})
            return self._success(
                request_id,
                self._service.list_publication_events(
                    principal_id=principal_id,
                    state=self._enum(query, "state", PublicationEventState),
                    limit=self._limit(query),
                    cursor=self._optional(query, "cursor"),
                ),
            )
        raise AdminError("api_resource_not_found")

    def _dispatch_write(
        self,
        method: str,
        path: str,
        query: Mapping[str, list[str]],
        payload: dict[str, Any],
        principal_id: UUID,
        request_id: UUID,
    ) -> HttpResponse:
        self._require_query(query, set())
        parts = path.split("/")
        if path.startswith("/admin/v1/documents/") and "/editorial/claims" in path:
            if len(parts) == 7 and method == "POST":
                body = self._model(EditorialClaimMutationRequest, payload)
                return self._success(
                    request_id,
                    self._service.mutate_editorial_claim(
                        principal_id=principal_id,
                        request_id=request_id,
                        document_id=self._nested_uuid(path, -3),
                        operation="add",
                        request=body,
                    ),
                )
            if len(parts) == 9 and method == "PATCH":
                body = self._model(EditorialClaimMutationRequest, payload)
                try:
                    identifier: dict[str, Any] = {"claim_id": UUID(parts[-2])}
                except ValueError:
                    identifier = {"item_ordinal": int(parts[-2])}
                body = body.model_copy(update=identifier)
                return self._success(
                    request_id,
                    self._service.mutate_editorial_claim(
                        principal_id=principal_id,
                        request_id=request_id,
                        document_id=self._nested_uuid(path, -5),
                        operation="edit",
                        request=body,
                    ),
                )
            if (
                len(parts) == 9
                and method == "POST"
                and parts[-1] in {"remove", "restore", "evidence"}
            ):
                body = self._model(EditorialClaimMutationRequest, payload)
                try:
                    identifier = {"claim_id": UUID(parts[-2])}
                except ValueError:
                    identifier = {"item_ordinal": int(parts[-2])}
                body = body.model_copy(update=identifier)
                operation = cast(
                    Literal["remove", "restore", "remove_evidence"],
                    "remove_evidence" if parts[-1] == "evidence" else parts[-1],
                )
                return self._success(
                    request_id,
                    self._service.mutate_editorial_claim(
                        principal_id=principal_id,
                        request_id=request_id,
                        document_id=self._nested_uuid(path, -5),
                        operation=operation,
                        request=body,
                    ),
                )
        if path.startswith("/admin/v1/documents/") and "/editorial/entities" in path:
            if len(parts) == 7 and method == "POST":
                body = self._model(EditorialEntityMutationRequest, payload)
                return self._success(
                    request_id,
                    self._service.mutate_editorial_entity(
                        principal_id=principal_id,
                        request_id=request_id,
                        document_id=self._nested_uuid(path, -3),
                        operation="add",
                        request=body,
                    ),
                )
            if len(parts) == 9 and method == "PATCH":
                body = self._model(EditorialEntityMutationRequest, payload)
                try:
                    identifier = {"entity_id": UUID(parts[-2])}
                except ValueError:
                    identifier = {"item_ordinal": int(parts[-2])}
                body = body.model_copy(update=identifier)
                return self._success(
                    request_id,
                    self._service.mutate_editorial_entity(
                        principal_id=principal_id,
                        request_id=request_id,
                        document_id=self._nested_uuid(path, -5),
                        operation="edit",
                        request=body,
                    ),
                )
            if (
                len(parts) == 9
                and method == "POST"
                and parts[-1] in {"remove", "restore", "evidence"}
            ):
                body = self._model(EditorialEntityMutationRequest, payload)
                try:
                    identifier = {"entity_id": UUID(parts[-2])}
                except ValueError:
                    identifier = {"item_ordinal": int(parts[-2])}
                body = body.model_copy(update=identifier)
                operation = cast(
                    Literal["remove", "restore", "remove_evidence"],
                    "remove_evidence" if parts[-1] == "evidence" else parts[-1],
                )
                return self._success(
                    request_id,
                    self._service.mutate_editorial_entity(
                        principal_id=principal_id,
                        request_id=request_id,
                        document_id=self._nested_uuid(path, -5),
                        operation=operation,
                        request=body,
                    ),
                )
        if path.startswith("/admin/v1/documents/") and path.endswith("/editorial"):
            if method != "PATCH" or path.count("/") != 5:
                raise AdminError("api_resource_not_found")
            body = self._model(EditorialPatchRequest, payload)
            return self._success(
                request_id,
                self._service.save_editorial(
                    principal_id=principal_id,
                    request_id=request_id,
                    document_id=self._nested_uuid(path, -2),
                    request=body,
                ),
            )
        if path.startswith("/admin/v1/documents/") and path.endswith("/editorial/adopt"):
            if method != "POST" or path.count("/") != 6:
                raise AdminError("api_resource_not_found")
            body = self._model(AdoptEditorialRequest, payload)
            return self._success(
                request_id,
                self._service.adopt_editorial(
                    principal_id=principal_id,
                    request_id=request_id,
                    document_id=self._nested_uuid(path, -3),
                    request=body,
                ),
            )
        if (
            method == "POST"
            and path.startswith("/admin/v1/documents/")
            and path.endswith("/restore")
            and "/editorial/revisions/" in path
        ):
            parts = path.split("/")
            if len(parts) != 9:
                raise AdminError("api_resource_not_found")
            body = self._model(EditorialRevisionRestoreRequest, payload)
            return self._success(
                request_id,
                self._service.restore_editorial_revision(
                    principal_id=principal_id,
                    request_id=request_id,
                    document_id=self._nested_uuid(path, -5),
                    revision_ref=parts[-2],
                    request=body,
                ),
            )
        if path.startswith("/admin/v1/documents/") and path.endswith("/reanalyze"):
            if method != "POST" or path.count("/") != 5:
                raise AdminError("api_resource_not_found")
            body = self._model(ReanalysisRequest, payload)
            return self._success(
                request_id,
                self._service.request_editorial_reanalysis(
                    principal_id=principal_id,
                    request_id=request_id,
                    document_id=self._nested_uuid(path, -2),
                    request=body,
                ),
            )
        if path.startswith("/admin/v1/documents/") and path.endswith("/trash"):
            if method != "POST" or path.count("/") != 5:
                raise AdminError("api_resource_not_found")
            body = self._model(LifecycleRequest, payload)
            return self._success(
                request_id,
                self._service.trash_document(
                    principal_id=principal_id,
                    request_id=request_id,
                    document_id=self._nested_uuid(path, -2),
                    request=body,
                ),
            )
        if path.startswith("/admin/v1/documents/") and path.endswith("/restore"):
            if method != "POST" or path.count("/") != 5:
                raise AdminError("api_resource_not_found")
            body = self._model(LifecycleRequest, payload)
            return self._success(
                request_id,
                self._service.restore_document(
                    principal_id=principal_id,
                    request_id=request_id,
                    document_id=self._nested_uuid(path, -2),
                    request=body,
                ),
            )
        if method == "POST" and path == "/admin/v1/review-cases":
            if str(payload.get("case_type")) == "relation":
                raise AdminError("api_capability_closed")
            body = self._model(OpenCaseRequest, payload)
            result = self._service.open_case(
                principal_id=principal_id,
                request_id=request_id,
                case_type=str(body.case_type),
                subject_id=body.subject_id,
                priority=body.priority,
                reason=body.reason,
            )
            return self._success(request_id, result)
        if (
            method == "POST"
            and path.startswith("/admin/v1/documents/")
            and path.endswith("/publication-review")
        ):
            if path.count("/") != 5:
                raise AdminError("api_resource_not_found")
            body = self._model(PublicationReviewRequest, payload)
            result = self._service.submit_publication_review(
                principal_id=principal_id,
                request_id=request_id,
                document_id=self._nested_uuid(path, -2),
                request=body,
            )
            return self._success(request_id, result)
        if (
            method == "POST"
            and path.startswith("/admin/v1/documents/")
            and path.endswith("/publication/review")
        ):
            if path.count("/") != 6:
                raise AdminError("api_resource_not_found")
            body = self._model(PublicationReviewRequest, payload)
            result = self._service.submit_publication_review(
                principal_id=principal_id,
                request_id=request_id,
                document_id=self._nested_uuid(path, -3),
                request=body,
            )
            return self._success(request_id, result)
        if path.startswith("/admin/v1/review-cases/") and path.endswith("/assignment"):
            if method != "PUT" or path.count("/") != 5:
                raise AdminError("api_resource_not_found")
            body = self._model(AssignmentRequest, payload)
            result = self._service.assign_case(
                principal_id=principal_id,
                request_id=request_id,
                case_id=self._nested_uuid(path, -2),
                assignee_id=body.assignee_id,
            )
            return self._success(request_id, result)
        if path.startswith("/admin/v1/review-cases/") and path.endswith("/close"):
            if method != "POST" or path.count("/") != 5:
                raise AdminError("api_resource_not_found")
            body = self._model(ReasonRequest, payload)
            result = self._service.close_case(
                principal_id=principal_id,
                request_id=request_id,
                case_id=self._nested_uuid(path, -2),
                reason=body.reason,
            )
            return self._success(request_id, result)
        if path.startswith("/admin/v1/review-cases/") and path.endswith("/decisions"):
            if method != "POST" or path.count("/") != 5:
                raise AdminError("api_resource_not_found")
            body = self._model(DecisionRequest, payload)
            result = self._service.record_decision(
                principal_id=principal_id,
                request_id=request_id,
                case_id=self._nested_uuid(path, -2),
                decision=str(body.decision),
                reason=body.reason,
                structured_changes=dict(body.structured_changes),
            )
            return self._success(request_id, result)
        if path.startswith("/admin/v1/analysis-results/") and path.endswith("/selection"):
            if method != "POST" or path.count("/") != 5:
                raise AdminError("api_resource_not_found")
            body = self._model(ReasonRequest, payload)
            result = self._service.select_result(
                principal_id=principal_id,
                request_id=request_id,
                analysis_result_id=self._nested_uuid(path, -2),
                reason=body.reason,
            )
            return self._success(request_id, result)
        if path.startswith("/admin/v1/entity-candidates/") and path.endswith("/accept"):
            if method != "POST" or path.count("/") != 5:
                raise AdminError("api_resource_not_found")
            body = self._model(ReasonRequest, payload)
            result = self._service.accept_candidate(
                principal_id=principal_id,
                request_id=request_id,
                candidate_id=self._nested_uuid(path, -2),
                reason=body.reason,
            )
            return self._success(request_id, result)
        if path.startswith("/admin/v1/entity-candidates/") and path.endswith("/bind"):
            if method != "POST" or path.count("/") != 5:
                raise AdminError("api_resource_not_found")
            body = self._model(BindCandidateRequest, payload)
            result = self._service.bind_candidate(
                principal_id=principal_id,
                request_id=request_id,
                candidate_id=self._nested_uuid(path, -2),
                entity_id=body.entity_id,
                reason=body.reason,
            )
            return self._success(request_id, result)
        if method == "POST" and path == "/admin/v1/entities/merges":
            body = self._model(MergeRequest, payload)
            result = self._service.merge_entities(
                principal_id=principal_id,
                request_id=request_id,
                source_entity_id=body.source_entity_id,
                target_entity_id=body.target_entity_id,
                reason=body.reason,
            )
            return self._success(request_id, result)
        if path.startswith("/admin/v1/entities/merge-events/") and path.endswith("/reverse"):
            if method != "POST" or path.count("/") != 6:
                raise AdminError("api_resource_not_found")
            body = self._model(ReasonRequest, payload)
            result = self._service.reverse_merge(
                principal_id=principal_id,
                request_id=request_id,
                merge_event_id=self._nested_uuid(path, -2),
                reason=body.reason,
            )
            return self._success(request_id, result)
        if method == "POST" and path == "/admin/v1/claims/manual":
            body = self._model(ManualClaimRequest, payload)
            result = self._service.create_claim(
                principal_id=principal_id,
                request_id=request_id,
                document_version_id=body.document_version_id,
                claim_text=body.claim_text,
                claim_type=str(body.claim_type),
                assertion_status=str(body.assertion_status),
                attribution=body.attribution,
                span_ids=list(body.span_ids),
            )
            return self._success(request_id, result)
        if path.startswith("/admin/v1/publication-events/") and path.endswith("/replay"):
            if method != "POST" or path.count("/") != 5:
                raise AdminError("api_resource_not_found")
            body = self._model(ReasonRequest, payload)
            result = self._service.replay_publication_event(
                principal_id=principal_id,
                request_id=request_id,
                event_id=self._nested_uuid(path, -2),
                reason=body.reason,
            )
            return self._success(request_id, result)
        raise AdminError("api_resource_not_found")

    def _authenticate(self, headers: Mapping[str, str]) -> UUID:
        authorization = next(
            (value for name, value in headers.items() if name.lower() == "Authorization".lower()),
            None,
        )
        if authorization is None or not authorization:
            raise AdminError("api_auth_required")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "Bearer".lower() or not token or " " in token:
            raise AdminError("api_auth_required")
        try:
            subject = self._oidc.validate(token)
        except TokenError as error:
            raise AdminError(error.code) from error
        return self._service.resolve_principal(self._issuer, subject)

    def _read_request_id(self, method: str, headers: Mapping[str, str]) -> UUID:
        if method in {"POST", "PUT", "PATCH"}:
            raw = next(
                (value for name, value in headers.items() if name.lower() == "idempotency-key"),
                None,
            )
            if raw is None or raw == "":
                raise AdminError("review_request_id_missing")
            try:
                request_id = UUID(raw)
            except ValueError as error:
                raise AdminError("review_request_id_invalid") from error
            if str(request_id) != raw:
                raise AdminError("review_request_id_invalid")
            return request_id
        return self._header_request_id(headers)

    @staticmethod
    def _header_request_id(headers: Mapping[str, str]) -> UUID:
        value = next(
            (item for name, item in headers.items() if name.lower() == "x-request-id"),
            None,
        )
        if value is None:
            return uuid4()
        try:
            request_id = UUID(value)
        except ValueError as error:
            raise AdminError("api_request_invalid") from error
        if str(request_id) != value:
            raise AdminError("api_request_invalid")
        return request_id

    @staticmethod
    def _parse_body(body: bytes | None) -> dict[str, Any]:
        if body is None or body == b"":
            raise AdminError("api_request_invalid")
        if len(body) > _MAX_BODY_LENGTH:
            raise AdminError("api_request_invalid")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AdminError("api_request_invalid") from error
        if not isinstance(payload, dict):
            raise AdminError("api_request_invalid")
        return payload

    @staticmethod
    def _model(model: type[StrictModel], payload: dict[str, Any]) -> Any:
        try:
            return model.model_validate(payload)
        except ValidationError as error:
            raise AdminError("api_request_invalid") from error

    @staticmethod
    def _require_query(query: Mapping[str, list[str]], allowed: set[str]) -> None:
        if set(query) - allowed:
            raise AdminError("api_request_invalid")

    @staticmethod
    def _optional(query: Mapping[str, list[str]], name: str) -> str | None:
        values = query.get(name)
        if not values:
            return None
        if values[0] == "":
            raise AdminError("api_request_invalid")
        return values[0]

    def _limit(self, query: Mapping[str, list[str]]) -> int:
        value = self._optional(query, "limit")
        if value is None:
            return 20
        try:
            limit = int(value)
        except ValueError as error:
            raise AdminError("api_request_invalid") from error
        if str(limit) != value or not 1 <= limit <= 100:
            raise AdminError("api_request_invalid")
        return limit

    def _enum(self, query: Mapping[str, list[str]], name: str, enum: type) -> str | None:
        value = self._optional(query, name)
        if value is None:
            return None
        try:
            return str(enum(value))
        except ValueError as error:
            raise AdminError("api_request_invalid") from error

    def _optional_uuid(self, query: Mapping[str, list[str]], name: str) -> UUID | None:
        value = self._optional(query, name)
        if value is None:
            return None
        try:
            result = UUID(value)
        except ValueError as error:
            raise AdminError("api_request_invalid") from error
        if str(result) != value:
            raise AdminError("api_request_invalid")
        return result

    @staticmethod
    def _path_uuid(path: str) -> UUID:
        value = path.rsplit("/", 1)[-1]
        try:
            result = UUID(value)
        except ValueError as error:
            raise AdminError("api_resource_not_found") from error
        if str(result) != value:
            raise AdminError("api_resource_not_found")
        return result

    @staticmethod
    def _nested_uuid(path: str, index: int) -> UUID:
        parts = path.split("/")
        try:
            value = parts[index]
            result = UUID(value)
        except (IndexError, ValueError) as error:
            raise AdminError("api_resource_not_found") from error
        if str(result) != value:
            raise AdminError("api_resource_not_found")
        return result

    def _resource(self, request_id: UUID, value: StrictModel | None) -> HttpResponse:
        if value is None:
            return self._problem(request_id, AdminError("api_resource_not_found"))
        return self._success(request_id, value)

    @staticmethod
    def _success(request_id: UUID, value: StrictModel) -> HttpResponse:
        return HttpResponse(
            status=200,
            headers={"Content-Type": "application/json", "X-Request-ID": str(request_id)},
            body=value.model_dump_json().encode("utf-8"),
        )

    @staticmethod
    def _health(request_id: UUID) -> HttpResponse:
        return HttpResponse(
            status=200,
            headers={"Content-Type": "application/json", "X-Request-ID": str(request_id)},
            body=b'{"status":"ok"}',
        )

    @staticmethod
    def _problem(request_id: UUID, problem: AdminError) -> HttpResponse:
        title = HTTPStatus(problem.status).phrase
        body = Problem(
            type=f"/problems/{problem.code}",
            title=title,
            status=problem.status,
            code=problem.code,
            request_id=request_id,
            detail=problem.detail,
        )
        return HttpResponse(
            status=problem.status,
            headers={
                "Content-Type": "application/problem+json",
                "X-Request-ID": str(request_id),
            },
            body=json.dumps(body.model_dump(mode="json"), separators=(",", ":")).encode("utf-8"),
        )
