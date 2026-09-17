"""Framework-independent HTTP routing for the six frozen anonymous GETs."""

from __future__ import annotations

import json
import logging
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import psycopg

from .contracts import DocumentCategory, EntityType, FactStatus, Problem, StrictModel
from .cursor import CursorError
from .service import PublicQueryService

LOGGER = logging.getLogger(__name__)
_MAX_TARGET_LENGTH = 8192


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class RequestProblem(ValueError):
    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.detail = detail


class PublicApiApplication:
    def __init__(self, service: PublicQueryService) -> None:
        self._service = service

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None
    ) -> HttpResponse:
        request_id: UUID
        try:
            request_id = self._request_id(headers or {})
        except RequestProblem as problem:
            return self._problem(uuid4(), problem.status, problem.code, problem.detail)
        try:
            if method != "GET" or len(target) > _MAX_TARGET_LENGTH:
                raise RequestProblem(404, "api_resource_not_found", "Resource not found.")
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or parsed.fragment:
                raise RequestProblem(422, "api_request_invalid", "Request is invalid.")
            path = parsed.path
            try:
                query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
            except ValueError as error:
                raise RequestProblem(422, "api_request_invalid", "Request is invalid.") from error
            if any(len(values) != 1 for values in query.values()):
                raise RequestProblem(422, "api_request_invalid", "Request is invalid.")

            if path == "/healthz":
                self._require_query(query, set())
                return self._health(request_id)
            if path == "/v1/relations" or (
                path.startswith("/v1/relations/") and path.count("/") == 3
            ):
                self._require_query(query, set())
                raise RequestProblem(404, "api_capability_closed", "Capability is closed.")
            if path == "/v1/documents":
                return self._documents(request_id, query)
            if path == "/v1/entities":
                return self._entities(request_id, query)
            if path == "/v1/search":
                return self._search(request_id, query)
            if path.startswith("/v1/documents/") and path.count("/") == 3:
                self._require_query(query, set())
                return self._resource(request_id, self._service.get_document(self._path_uuid(path)))
            if path.startswith("/v1/claims/") and path.count("/") == 3:
                self._require_query(query, set())
                return self._resource(request_id, self._service.get_claim(self._path_uuid(path)))
            if path.startswith("/v1/entities/") and path.count("/") == 3:
                self._require_query(query, set())
                return self._resource(request_id, self._service.get_entity(self._path_uuid(path)))
            raise RequestProblem(404, "api_resource_not_found", "Resource not found.")
        except CursorError:
            return self._problem(request_id, 400, "api_cursor_invalid", "Cursor is invalid.")
        except RequestProblem as problem:
            return self._problem(request_id, problem.status, problem.code, problem.detail)
        except (psycopg.Error, TimeoutError):
            LOGGER.warning("public API database dependency unavailable request_id=%s", request_id)
            return self._problem(
                request_id,
                503,
                "api_dependency_unavailable",
                "Service dependency is unavailable.",
            )
        except Exception:
            LOGGER.error(
                "public API internal error request_id=%s code=api_internal_error", request_id
            )
            return self._problem(request_id, 500, "api_internal_error", None)

    def _documents(self, request_id: UUID, query: Mapping[str, list[str]]) -> HttpResponse:
        self._require_query(
            query, {"limit", "cursor", "category", "fact_status", "published_after"}
        )
        result = self._service.list_documents(
            limit=self._limit(query),
            cursor=self._optional(query, "cursor"),
            category=self._enum(query, "category", DocumentCategory),
            fact_status=self._enum(query, "fact_status", FactStatus),
            published_after=self._datetime(query, "published_after"),
        )
        return self._success(request_id, result)

    def _entities(self, request_id: UUID, query: Mapping[str, list[str]]) -> HttpResponse:
        self._require_query(query, {"limit", "cursor", "type"})
        result = self._service.list_entities(
            limit=self._limit(query),
            cursor=self._optional(query, "cursor"),
            entity_type=self._enum(query, "type", EntityType),
        )
        return self._success(request_id, result)

    def _search(self, request_id: UUID, query: Mapping[str, list[str]]) -> HttpResponse:
        self._require_query(query, {"q", "limit", "cursor", "category", "fact_status"})
        raw_query = self._optional(query, "q")
        if raw_query is None:
            raise RequestProblem(400, "api_request_invalid", "Search query is invalid.")
        normalized = unicodedata.normalize("NFKC", raw_query).strip()
        if not 2 <= len(normalized) <= 200:
            raise RequestProblem(400, "api_request_invalid", "Search query is invalid.")
        result = self._service.search(
            query=normalized,
            limit=self._limit(query),
            cursor=self._optional(query, "cursor"),
            category=self._enum(query, "category", DocumentCategory),
            fact_status=self._enum(query, "fact_status", FactStatus),
        )
        return self._success(request_id, result)

    @staticmethod
    def _request_id(headers: Mapping[str, str]) -> UUID:
        value = next(
            (value for name, value in headers.items() if name.lower() == "x-request-id"),
            None,
        )
        if value is None:
            return uuid4()
        try:
            request_id = UUID(value)
        except ValueError as error:
            raise RequestProblem(400, "api_request_invalid", "Request ID is invalid.") from error
        if str(request_id) != value:
            raise RequestProblem(400, "api_request_invalid", "Request ID is invalid.")
        return request_id

    @staticmethod
    def _require_query(query: Mapping[str, list[str]], allowed: set[str]) -> None:
        if set(query) - allowed:
            raise RequestProblem(422, "api_request_invalid", "Request is invalid.")

    @staticmethod
    def _optional(query: Mapping[str, list[str]], name: str) -> str | None:
        values = query.get(name)
        if not values:
            return None
        if values[0] == "":
            raise RequestProblem(422, "api_request_invalid", "Request is invalid.")
        return values[0]

    def _limit(self, query: Mapping[str, list[str]]) -> int:
        value = self._optional(query, "limit")
        if value is None:
            return 20
        try:
            limit = int(value)
        except ValueError as error:
            raise RequestProblem(422, "api_request_invalid", "Request is invalid.") from error
        if str(limit) != value or not 1 <= limit <= 100:
            raise RequestProblem(422, "api_request_invalid", "Request is invalid.")
        return limit

    def _enum(self, query: Mapping[str, list[str]], name: str, enum: type) -> str | None:
        value = self._optional(query, name)
        if value is None:
            return None
        try:
            return str(enum(value))
        except ValueError as error:
            raise RequestProblem(422, "api_request_invalid", "Request is invalid.") from error

    def _datetime(self, query: Mapping[str, list[str]], name: str) -> datetime | None:
        value = self._optional(query, name)
        if value is None:
            return None
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise RequestProblem(422, "api_request_invalid", "Request is invalid.") from error
        if result.tzinfo is None:
            raise RequestProblem(422, "api_request_invalid", "Request is invalid.")
        return result

    @staticmethod
    def _path_uuid(path: str) -> UUID:
        value = path.rsplit("/", 1)[-1]
        try:
            result = UUID(value)
        except ValueError as error:
            raise RequestProblem(404, "api_resource_not_found", "Resource not found.") from error
        if str(result) != value:
            raise RequestProblem(404, "api_resource_not_found", "Resource not found.")
        return result

    def _resource(self, request_id: UUID, value: StrictModel | None) -> HttpResponse:
        if value is None:
            return self._problem(request_id, 404, "api_resource_not_found", "Resource not found.")
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
    def _problem(request_id: UUID, status: int, code: str, detail: str | None) -> HttpResponse:
        title = HTTPStatus(status).phrase
        problem = Problem(
            type=f"/problems/{code}",
            title=title,
            status=status,
            code=code,
            request_id=request_id,
            detail=detail,
        )
        return HttpResponse(
            status=status,
            headers={
                "Content-Type": "application/problem+json",
                "X-Request-ID": str(request_id),
            },
            body=json.dumps(problem.model_dump(mode="json"), separators=(",", ":")).encode("utf-8"),
        )
