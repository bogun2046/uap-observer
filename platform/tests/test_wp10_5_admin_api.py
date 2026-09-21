"""WP10.5 Admin API static contract, routing, and isolation tests."""

from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from queue import LifoQueue
from threading import Barrier, Lock
from typing import cast
from unittest.mock import patch
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import Connection
from psycopg.pq import TransactionStatus
from psycopg.rows import tuple_row
from pydantic import ValidationError

from tools import validate_wp10_5
from uap_platform.admin_api.config import load_admin_api_settings
from uap_platform.admin_api.contracts import (
    OpenCaseRequest,
    Problem,
    PublicationReviewRequest,
    WriteResult,
)
from uap_platform.admin_api.cursor import CursorCodec
from uap_platform.admin_api.errors import AdminError, map_database_error
from uap_platform.admin_api.handler import AdminApiApplication
from uap_platform.admin_api.oidc import OidcValidator, TokenError
from uap_platform.admin_api.pool import AdminApiPool
from uap_platform.admin_api.service import AdminQueryService


def platform_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_wp10_5_migration_is_linear_and_replay_only() -> None:
    migration = (platform_root() / "alembic/versions/0024_wp10_admin_replay.py").read_text(
        encoding="utf-8"
    )
    assert 'revision = "0024_wp10_admin_replay"' in migration
    assert 'down_revision = "0023_wp10_api_read_indexes"' in migration
    assert "CREATE FUNCTION audit.requeue_publication_event" in migration
    assert "GRANT EXECUTE ON FUNCTION audit.requeue_publication_event" in migration
    assert "CREATE TABLE" not in migration
    assert "core.merge_entities" not in migration
    assert "core.reverse_entity_merge" not in migration


def test_wp10_5_static_contract() -> None:
    assert [
        item.name for item in validate_wp10_5.evaluate(platform_root()) if not item.passed
    ] == []


def test_wp10_5_accepts_only_reviewed_implementation_ticket_versions() -> None:
    current = dict(validate_wp10_5.FROZEN_DOC_HASHES)
    current["implementation-ticket.md"] = validate_wp10_5.WP106_D_IMPLEMENTATION_TICKET_SHA256
    assert validate_wp10_5.frozen_docs_are_valid(current)

    historical = dict(validate_wp10_5.FROZEN_DOC_HASHES)
    assert validate_wp10_5.frozen_docs_are_valid(historical)

    unapproved = {**current, "implementation-ticket.md": "0" * 64}
    assert not validate_wp10_5.frozen_docs_are_valid(unapproved)

    changed_contract = {**current, "api-contract.md": "0" * 64}
    assert not validate_wp10_5.frozen_docs_are_valid(changed_contract)


class FakeService:
    def resolve_principal(self, issuer: str, subject: str) -> UUID:
        if subject == "unknown":
            from uap_platform.admin_api.errors import AdminError

            raise AdminError("api_principal_not_provisioned")
        return UUID("00000000-0000-4000-8000-000000000001")

    def list_review_cases(self, **kwargs: object) -> object:
        from uap_platform.admin_api.contracts import ReviewCasePage

        return ReviewCasePage(items=[], next_cursor=None)

    def open_case(self, **kwargs: object) -> WriteResult:
        return WriteResult(
            operation="review.case.open",
            resource_id=UUID("00000000-0000-4000-8000-000000000002"),
            request_id=kwargs["request_id"],  # type: ignore[arg-type]
            publication=None,
        )


class FakeOidc:
    def validate(self, token: str) -> str:
        if token == "bad":
            raise TokenError("api_token_invalid")
        return token


def _app() -> AdminApiApplication:
    return AdminApiApplication(FakeService(), FakeOidc(), "https://issuer.test/wp10.5")  # type: ignore[arg-type]


def test_strict_admin_dto_and_write_result() -> None:
    schema = WriteResult.model_json_schema()
    assert schema["additionalProperties"] is False
    problem_schema = Problem.model_json_schema()
    assert problem_schema["additionalProperties"] is False
    request = OpenCaseRequest.model_validate(
        {
            "case_type": "document",
            "subject_id": "00000000-0000-4000-8000-000000000003",
            "reason": "open a document case",
        }
    )
    assert request.priority == 0
    with pytest.raises(ValidationError):
        OpenCaseRequest.model_validate(
            {
                "case_type": "document",
                "subject_id": "00000000-0000-4000-8000-000000000003",
                "reason": "open a document case",
                "unknown": True,
            }
        )


def test_admin_config_requires_uap_api_and_rejects_privileged_dsn() -> None:
    jwks = json.dumps({"keys": [{"kty": "RSA", "n": "sA", "e": "AQAB"}]})
    base = {
        "UAP_ADMIN_DATABASE_URL": "postgresql://uap_api:secret@db/uap",
        "UAP_ADMIN_CURSOR_SECRET": "s" * 32,
        "UAP_ADMIN_OIDC_ISSUER": "https://issuer.test/wp10.5",
        "UAP_ADMIN_OIDC_AUDIENCE": "uap-admin",
        "UAP_ADMIN_OIDC_JWKS": jwks,
    }
    settings = load_admin_api_settings(base)
    assert settings.safe_summary()["port"] == 8082
    assert "uap_api:secret" not in repr(settings)
    with pytest.raises(ValueError, match="privileged"):
        load_admin_api_settings({**base, "UAP_DATABASE_URL": "postgresql://owner/db"})
    with pytest.raises(ValueError, match="privileged"):
        load_admin_api_settings({**base, "UAP_PUBLIC_DATABASE_URL": "postgresql://reader/db"})
    with pytest.raises(ValueError, match="admin API"):
        load_admin_api_settings(
            {**base, "UAP_ADMIN_DATABASE_URL": "postgresql://uap_public_reader:secret@db/uap"}
        )
    with pytest.raises(ValueError, match="admin API"):
        load_admin_api_settings(
            {**base, "UAP_ADMIN_DATABASE_URL": "postgresql://uap_publisher:secret@db/uap"}
        )


def test_http_auth_cookie_not_bypass_and_request_id() -> None:
    app = _app()
    missing = app.handle("GET", "/admin/v1/review-cases")
    assert missing.status == 401
    assert json.loads(missing.body)["code"] == "api_auth_required"
    cookie = app.handle(
        "GET",
        "/admin/v1/review-cases",
        {"Cookie": "session=admin", "X-Request-ID": "00000000-0000-4000-8000-000000000099"},
    )
    assert cookie.status == 401
    assert json.loads(cookie.body)["code"] == "api_auth_required"
    assert cookie.headers["X-Request-ID"] == "00000000-0000-4000-8000-000000000099"
    invalid = app.handle("GET", "/admin/v1/review-cases", {"Authorization": "Bearer bad"})
    assert invalid.status == 401
    assert json.loads(invalid.body)["code"] == "api_token_invalid"
    unknown = app.handle("GET", "/admin/v1/review-cases", {"Authorization": "Bearer unknown"})
    assert unknown.status == 403
    assert json.loads(unknown.body)["code"] == "api_principal_not_provisioned"
    basic = app.handle("GET", "/admin/v1/review-cases", {"Authorization": "Basic abc"})
    assert basic.status == 401
    health = app.handle("GET", "/healthz")
    assert health.status == 200
    assert json.loads(health.body) == {"status": "ok"}


def test_write_idempotency_key_and_success_is_200() -> None:
    app = _app()
    missing = app.handle(
        "POST",
        "/admin/v1/review-cases",
        {"Authorization": "Bearer reviewer-subject"},
        json.dumps(
            {
                "case_type": "document",
                "subject_id": "00000000-0000-4000-8000-000000000003",
                "reason": "open a document case",
            }
        ).encode(),
    )
    assert missing.status == 400
    assert json.loads(missing.body)["code"] == "review_request_id_missing"
    invalid = app.handle(
        "POST",
        "/admin/v1/review-cases",
        {
            "Authorization": "Bearer reviewer-subject",
            "Idempotency-Key": "not-a-uuid",
        },
        b"{}",
    )
    assert invalid.status == 400
    assert json.loads(invalid.body)["code"] == "review_request_id_invalid"
    request_id = "00000000-0000-4000-8000-000000000044"
    created = app.handle(
        "POST",
        "/admin/v1/review-cases",
        {
            "Authorization": "Bearer reviewer-subject",
            "Idempotency-Key": request_id,
        },
        json.dumps(
            {
                "case_type": "document",
                "subject_id": "00000000-0000-4000-8000-000000000003",
                "reason": "open a document case",
            }
        ).encode(),
    )
    assert created.status == 200
    body = json.loads(created.body)
    assert body["operation"] == "review.case.open"
    assert body["request_id"] == request_id
    assert created.headers["X-Request-ID"] == request_id
    assert created.headers["Content-Type"] == "application/json"


def test_relation_case_closed_before_schema() -> None:
    app = _app()
    response = app.handle(
        "POST",
        "/admin/v1/review-cases",
        {
            "Authorization": "Bearer reviewer-subject",
            "Idempotency-Key": str(uuid4()),
        },
        json.dumps(
            {
                "case_type": "relation",
                "subject_id": "00000000-0000-4000-8000-000000000003",
                "reason": "open a relation case",
            }
        ).encode(),
    )
    assert response.status == 404
    assert json.loads(response.body)["code"] == "api_capability_closed"
    grants = app.handle("POST", "/admin/v1/grants", {"Authorization": "Bearer reviewer-subject"})
    assert json.loads(grants.body)["code"] in {"api_capability_closed", "review_request_id_missing"}
    closed = app.handle("GET", "/admin/v1/grants")
    assert json.loads(closed.body)["code"] == "api_capability_closed"


def test_problem_shape_is_sanitized() -> None:
    app = _app()
    response = app.handle(
        "GET", "/admin/v1/review-cases?unknown=1", {"Authorization": "Bearer sub"}
    )
    body = json.loads(response.body)
    assert set(body) == {"type", "title", "status", "code", "request_id", "detail"}
    assert body["type"] == f"/problems/{body['code']}"
    assert not any(token in response.body.decode() for token in ("SQLSTATE", "audit.", "psycopg"))


def test_service_sql_has_no_bottom_or_private_writes() -> None:
    service = (platform_root() / "src/uap_platform/admin_api/service.py").read_text(
        encoding="utf-8"
    )
    handler = (platform_root() / "src/uap_platform/admin_api/handler.py").read_text(
        encoding="utf-8"
    )
    combined = service + handler
    assert "INSERT INTO" not in combined
    assert "UPDATE " not in combined
    assert "DELETE FROM" not in combined
    assert "core.merge_entities" not in combined
    assert "core.reverse_entity_merge" not in combined
    assert "enqueue_publication_outbox" not in combined
    assert "audit._apply_" not in combined
    assert "ops._apply_" not in combined
    assert "audit.requeue_publication_event" in service
    assert ' f"""' not in service


def test_unknown_database_message_is_internal() -> None:
    class FakeDiag:
        message_primary = "relation review_cases does not exist"

    class FakeError(Exception):
        diag = FakeDiag()
        sqlstate = "42P01"

    mapped = map_database_error(FakeError())  # type: ignore[arg-type]
    assert mapped.code == "api_internal_error"
    assert mapped.status == 500
    assert mapped.detail is None


def test_pool_slot_reservation_is_atomic_and_failure_releases_slot() -> None:
    pool = object.__new__(AdminApiPool)
    pool._dsn = "postgresql://uap_api@localhost/uap"
    pool._max_size = 2
    pool._created = 0
    pool._lock = Lock()
    pool._closed = False
    pool._connections = LifoQueue()
    barrier = Barrier(32)

    def reserve() -> bool:
        barrier.wait()
        return pool._reserve_slot()

    with ThreadPoolExecutor(max_workers=32) as executor:
        results = list(executor.map(lambda _item: reserve(), range(32)))
    assert sum(results) == 2
    assert pool._created == 2

    pool._created = 1
    with patch("uap_platform.admin_api.pool.psycopg.connect", side_effect=RuntimeError("connect")):
        with pytest.raises(RuntimeError, match="connect"):
            pool._open_reserved()
    assert pool._created == 0


def test_pool_successive_lookups_leave_connection_idle() -> None:
    class FakeInfo:
        def __init__(self) -> None:
            self.transaction_status = TransactionStatus.IDLE

    class FakeCursor:
        def __init__(self, connection: FakeConnection) -> None:
            self._connection = connection
            self._sql = ""

        def execute(self, sql: str, params: object = None) -> None:
            self._sql = str(sql)
            if self._sql.startswith("SET TRANSACTION"):
                if self._connection.info.transaction_status != TransactionStatus.IDLE:
                    raise psycopg.ProgrammingError(
                        "SET TRANSACTION must be called before any query"
                    )
                self._connection.info.transaction_status = TransactionStatus.INTRANS
                return
            self._connection.info.transaction_status = TransactionStatus.INTRANS

        def fetchone(self) -> dict[str, object] | None:
            if "uap.principal_id" in self._sql:
                return {"principal": None}
            if "uap.request_id" in self._sql:
                return {"request": None}
            return None

        def __enter__(self) -> FakeCursor:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    class FakeConnection:
        def __init__(self) -> None:
            self.info = FakeInfo()
            self.closed = False

        def cursor(self) -> FakeCursor:
            return FakeCursor(self)

        def commit(self) -> None:
            self.info.transaction_status = TransactionStatus.IDLE

        def rollback(self) -> None:
            self.info.transaction_status = TransactionStatus.IDLE

        def close(self) -> None:
            self.closed = True

    pool = object.__new__(AdminApiPool)
    pool._dsn = "postgresql://uap_api@localhost/uap"
    pool._max_size = 1
    pool._created = 1
    pool._lock = Lock()
    pool._closed = False
    pool._connections = LifoQueue()
    connection = FakeConnection()
    pool._connections.put(cast(Connection[dict[str, object]], connection))

    for _attempt in range(2):
        with pool.lookup_transaction() as checked_out:
            with checked_out.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        assert connection.info.transaction_status == TransactionStatus.IDLE
        assert not connection.closed
        assert pool._connections.qsize() == 1


class AdminScriptedCursor:
    def __init__(self, owner: AdminScriptedConnection) -> None:
        self._owner = owner
        self._current: object = None
        self._sql = ""

    def execute(self, sql: str, params: object = None) -> None:
        del params
        self._sql = str(sql)
        text = self._sql
        if (
            text.startswith("SAVEPOINT")
            or text.startswith("ROLLBACK")
            or text.startswith("RELEASE")
            or text.startswith("SET ")
            or "set_config" in text
        ):
            self._current = None
            return
        if "require_active_role" in text:
            principal = UUID("00000000-0000-4000-8000-000000000001")
            if self._owner.row_factory is tuple_row:
                self._current = (principal,)
            else:
                self._current = {"require_active_role": principal}
            return
        self._current = self._owner.next_result()

    def fetchall(self) -> list[object]:
        current = self._current
        if current is None:
            return []
        if isinstance(current, list):
            return list(current)
        return [current]

    def fetchone(self) -> object:
        current = self._current
        if isinstance(current, list):
            return current[0] if current else None
        return current

    def __enter__(self) -> AdminScriptedCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class AdminScriptedConnection:
    def __init__(self, results: list[object]) -> None:
        self._results = list(results)
        self.row_factory: object = dict
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.info = type("Info", (), {"transaction_status": TransactionStatus.IDLE})()

    def next_result(self) -> object:
        if not self._results:
            return None
        return self._results.pop(0)

    def cursor(self) -> AdminScriptedCursor:
        return AdminScriptedCursor(self)

    def commit(self) -> None:
        self.commits += 1
        self.info.transaction_status = TransactionStatus.IDLE

    def rollback(self) -> None:
        self.rollbacks += 1
        self.info.transaction_status = TransactionStatus.IDLE

    def close(self) -> None:
        self.closed = True


class AdminScriptedPool:
    def __init__(self, connection: AdminScriptedConnection) -> None:
        self._connection = connection

    @contextmanager
    def read_transaction(self, principal_id: UUID) -> Iterator[AdminScriptedConnection]:
        del principal_id
        yield self._connection

    @contextmanager
    def write_transaction(
        self, principal_id: UUID, request_id: UUID
    ) -> Iterator[AdminScriptedConnection]:
        del principal_id, request_id
        yield self._connection

    @contextmanager
    def lookup_transaction(self) -> Iterator[AdminScriptedConnection]:
        yield self._connection


def _admin_codec() -> CursorCodec:
    return CursorCodec(b"k" * 32)


def _admin_service(results: list[object]) -> AdminQueryService:
    connection = AdminScriptedConnection(results)
    return AdminQueryService(cast(AdminApiPool, AdminScriptedPool(connection)), _admin_codec())


def test_submit_publication_review_handles_tuple_rows_and_rolls_back_on_error() -> None:
    document_id = UUID("00000000-0000-7300-8000-000000000101")
    version_id = UUID("00000000-0000-7300-8000-000000000102")
    revision_id = UUID("00000000-0000-7300-8000-000000000103")
    principal_id = UUID("00000000-0000-7300-8000-000000000104")
    request_id = UUID("00000000-0000-7300-8000-000000000105")
    case_id = UUID("00000000-0000-7300-8000-000000000106")
    request = PublicationReviewRequest(
        document_version_id=version_id,
        editorial_revision_id=revision_id,
        editorial_revision_no=4,
        expected_revision=4,
        reason="submit the selected revision",
    )

    success_connection = AdminScriptedConnection([(4, document_id, None)])
    success_service = AdminQueryService(
        cast(AdminApiPool, AdminScriptedPool(success_connection)), _admin_codec()
    )
    with patch(
        "uap_platform.admin_api.service.open_document_publication_review_case",
        return_value=case_id,
    ) as open_case:
        result = success_service.submit_publication_review(
            principal_id=principal_id,
            request_id=request_id,
            document_id=document_id,
            request=request,
        )
    assert result.resource_id == case_id
    open_case.assert_called_once_with(
        success_connection, version_id, revision_id, request.priority, request.reason
    )

    class RollbackPool(AdminScriptedPool):
        @contextmanager
        def write_transaction(
            self, principal_id: UUID, request_id: UUID
        ) -> Iterator[AdminScriptedConnection]:
            del principal_id, request_id
            try:
                yield self._connection
            except Exception:
                self._connection.rollback()
                raise

    failed_connection = AdminScriptedConnection([(4, document_id, None)])
    failed_service = AdminQueryService(
        cast(AdminApiPool, RollbackPool(failed_connection)), _admin_codec()
    )
    with pytest.raises(AdminError, match="editorial_document_version_mismatch"):
        failed_service.submit_publication_review(
            principal_id=principal_id,
            request_id=request_id,
            document_id=UUID("00000000-0000-7300-8000-000000000199"),
            request=request,
        )
    assert failed_connection.rollbacks == 1


def _case_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": UUID("00000000-0000-4000-8000-000000000010"),
        "case_type": "document",
        "status": "open",
        "priority": 1,
        "subject_id": UUID("00000000-0000-4000-8000-000000000003"),
        "assigned_to": None,
        "opened_by": UUID("00000000-0000-4000-8000-000000000001"),
        "opened_at": datetime(2026, 8, 31, tzinfo=UTC),
        "closed_at": None,
    }
    row.update(overrides)
    return row


def test_editorial_admin_can_read_analysis_and_evidence_without_reviewer_role() -> None:
    from uap_platform.admin_api.errors import AdminError
    from uap_platform.review.errors import ReviewSessionError

    principal = UUID("00000000-0000-4000-8000-000000000001")
    version_id = UUID("00000000-0000-4000-8000-000000000042")
    analysis = {
        "id": UUID("00000000-0000-4000-8000-000000000041"),
        "document_version_id": version_id,
        "result_type": "claim_extraction",
        "schema_version": "1",
        "validation_status": "valid",
        "created_at": datetime(2026, 8, 31, tzinfo=UTC),
    }
    span = {
        "id": UUID("00000000-0000-4000-8000-000000000061"),
        "document_version_id": version_id,
        "locator_type": "text",
        "char_start": 0,
        "char_end": 4,
        "page_start": None,
        "page_end": None,
        "time_start_ms": None,
        "time_end_ms": None,
        "locator": {"char_start": 0},
        "created_at": datetime(2026, 8, 31, tzinfo=UTC),
    }

    def editorial_only(_connection: object, role: str) -> UUID:
        if role == "reviewer":
            raise ReviewSessionError("review_role_denied", "42501")
        return principal

    with patch("uap_platform.admin_api.service.require_active_role", side_effect=editorial_only):
        service = _admin_service([[analysis], [span]])
        assert service.list_analysis_results(
            principal_id=principal,
            document_version_id=version_id,
            result_type=None,
            validation_status=None,
            limit=20,
            cursor=None,
        ).items
        assert service.list_evidence_spans(
            principal_id=principal,
            document_version_id=version_id,
            limit=20,
            cursor=None,
        ).items

    def ordinary_denied(_connection: object, _role: str) -> UUID:
        raise ReviewSessionError("review_role_denied", "42501")

    with patch("uap_platform.admin_api.service.require_active_role", side_effect=ordinary_denied):
        service = _admin_service([[], []])
        with pytest.raises(AdminError) as analysis_denied:
            service.list_analysis_results(
                principal_id=principal,
                document_version_id=version_id,
                result_type=None,
                validation_status=None,
                limit=20,
                cursor=None,
            )
        assert analysis_denied.value.code == "review_role_denied"
        with pytest.raises(AdminError) as evidence_denied:
            service.list_evidence_spans(
                principal_id=principal,
                document_version_id=version_id,
                limit=20,
                cursor=None,
            )
        assert evidence_denied.value.code == "review_role_denied"


def test_admin_query_service_reads_writes_cursors_and_errors() -> None:
    from datetime import UTC, datetime
    from unittest.mock import patch
    from uuid import UUID

    from uap_platform.admin_api.contracts import GrantStatus, ProjectionState
    from uap_platform.admin_api.cursor import CursorError, filters_digest
    from uap_platform.admin_api.errors import AdminError
    from uap_platform.admin_api.service import (
        CASE_SORT,
        CREATED_SORT,
        OCCURRED_SORT,
        AdminQueryService,
    )
    from uap_platform.review.errors import ReviewSessionError

    principal = UUID("00000000-0000-4000-8000-000000000001")
    first = _case_row()
    second = _case_row(id=UUID("00000000-0000-4000-8000-000000000011"), priority=0)
    service = _admin_service([[first, second]])
    page = service.list_review_cases(
        principal_id=principal,
        status="open",
        case_type="document",
        assigned_to=None,
        limit=1,
        cursor=None,
    )
    assert page.next_cursor is not None
    codec = _admin_codec()
    digest = filters_digest({"assigned_to": None, "case_type": "document", "status": "open"})
    cursor = codec.encode(
        resource="review-cases",
        sort=CASE_SORT,
        last=[1, datetime(2026, 8, 31, tzinfo=UTC).isoformat(), str(first["id"])],
        filters_sha256=digest,
    )
    continued = _admin_service([[second]]).list_review_cases(
        principal_id=principal,
        status="open",
        case_type="document",
        assigned_to=None,
        limit=20,
        cursor=cursor,
    )
    assert continued.items[0].id == second["id"]
    with pytest.raises(CursorError):
        _admin_service([]).list_review_cases(
            principal_id=principal,
            status=None,
            case_type=None,
            assigned_to=None,
            limit=20,
            cursor=codec.encode(
                resource="review-cases",
                sort=CASE_SORT,
                last=[1, "x"],
                filters_sha256=filters_digest(
                    {"assigned_to": None, "case_type": None, "status": None}
                ),
            ),
        )
    with pytest.raises(CursorError):
        _admin_service([]).list_review_cases(
            principal_id=principal,
            status=None,
            case_type=None,
            assigned_to=None,
            limit=20,
            cursor=codec.encode(
                resource="review-cases",
                sort=CASE_SORT,
                last=["nope", datetime(2026, 8, 31, tzinfo=UTC).isoformat(), str(first["id"])],
                filters_sha256=filters_digest(
                    {"assigned_to": None, "case_type": None, "status": None}
                ),
            ),
        )
    with pytest.raises(CursorError):
        _admin_service([]).list_review_cases(
            principal_id=principal,
            status=None,
            case_type=None,
            assigned_to=None,
            limit=20,
            cursor=codec.encode(
                resource="review-cases",
                sort=CASE_SORT,
                last=[1, "2026-08-31T00:00:00", str(first["id"])],
                filters_sha256=filters_digest(
                    {"assigned_to": None, "case_type": None, "status": None}
                ),
            ),
        )

    decision = {
        "id": UUID("00000000-0000-4000-8000-000000000021"),
        "sequence_no": 1,
        "decision": "approve",
        "reason": "approved after review",
        "decided_by": principal,
        "decided_at": datetime(2026, 8, 31, tzinfo=UTC),
    }
    publication = {
        "grant_id": UUID("00000000-0000-4000-8000-000000000031"),
        "grant_status": "active",
        "revision_no": 1,
        "outbox_event_id": UUID("00000000-0000-4000-8000-000000000032"),
        "projection_state": "visible",
    }
    detail = _admin_service([first, [decision], publication]).get_review_case(
        principal, UUID(str(first["id"]))
    )
    assert detail is not None
    assert detail.publication is not None
    assert detail.publication.grant_status == GrantStatus.ACTIVE
    assert detail.publication.projection_state == ProjectionState.VISIBLE
    assert _admin_service([None]).get_review_case(principal, UUID(str(first["id"]))) is None

    created = datetime(2026, 8, 31, tzinfo=UTC)
    analysis = {
        "id": UUID("00000000-0000-4000-8000-000000000041"),
        "document_version_id": UUID("00000000-0000-4000-8000-000000000042"),
        "result_type": "claim_extraction",
        "schema_version": "1",
        "validation_status": "valid",
        "created_at": created,
    }
    analysis_page = _admin_service([[analysis, analysis]]).list_analysis_results(
        principal_id=principal,
        document_version_id=None,
        result_type=None,
        validation_status=None,
        limit=1,
        cursor=None,
    )
    assert analysis_page.next_cursor is not None
    created_digest = filters_digest(
        {"document_version_id": None, "result_type": None, "validation_status": None}
    )
    created_cursor = codec.encode(
        resource="analysis-results",
        sort=CREATED_SORT,
        last=[created.isoformat(), str(analysis["id"])],
        filters_sha256=created_digest,
    )
    _admin_service([[analysis]]).list_analysis_results(
        principal_id=principal,
        document_version_id=None,
        result_type=None,
        validation_status=None,
        limit=20,
        cursor=created_cursor,
    )
    with pytest.raises(CursorError):
        _admin_service([]).list_analysis_results(
            principal_id=principal,
            document_version_id=None,
            result_type=None,
            validation_status=None,
            limit=20,
            cursor=codec.encode(
                resource="analysis-results",
                sort=CREATED_SORT,
                last=["only"],
                filters_sha256=created_digest,
            ),
        )
    with pytest.raises(CursorError):
        _admin_service([]).list_analysis_results(
            principal_id=principal,
            document_version_id=None,
            result_type=None,
            validation_status=None,
            limit=20,
            cursor=codec.encode(
                resource="analysis-results",
                sort=CREATED_SORT,
                last=["bad", str(analysis["id"])],
                filters_sha256=created_digest,
            ),
        )
    with pytest.raises(CursorError):
        _admin_service([]).list_analysis_results(
            principal_id=principal,
            document_version_id=None,
            result_type=None,
            validation_status=None,
            limit=20,
            cursor=codec.encode(
                resource="analysis-results",
                sort=CREATED_SORT,
                last=["2026-08-31T00:00:00", str(analysis["id"])],
                filters_sha256=created_digest,
            ),
        )

    candidate = {
        "id": UUID("00000000-0000-4000-8000-000000000051"),
        "analysis_result_id": UUID("00000000-0000-4000-8000-000000000041"),
        "document_version_id": UUID("00000000-0000-4000-8000-000000000042"),
        "ordinal": 1,
        "proposed_entity_type": "organization",
        "proposed_name": "Candidate",
        "status": "pending",
        "created_at": created,
    }
    candidates = _admin_service([[candidate, candidate]]).list_entity_candidates(
        principal_id=principal, status=None, analysis_result_id=None, limit=1, cursor=None
    )
    assert candidates.next_cursor is not None

    span = {
        "id": UUID("00000000-0000-4000-8000-000000000061"),
        "document_version_id": UUID("00000000-0000-4000-8000-000000000042"),
        "locator_type": "text",
        "char_start": 0,
        "char_end": 4,
        "page_start": None,
        "page_end": None,
        "time_start_ms": None,
        "time_end_ms": None,
        "locator": {"char_start": 0},
        "created_at": created,
    }
    spans = _admin_service([[span, span]]).list_evidence_spans(
        principal_id=principal,
        document_version_id=UUID("00000000-0000-4000-8000-000000000042"),
        limit=1,
        cursor=None,
    )
    assert spans.next_cursor is not None

    entity = {
        "id": UUID("00000000-0000-4000-8000-000000000071"),
        "entity_type": "organization",
        "canonical_name": "Entity",
        "description": None,
        "country_code": "US",
        "status": "active",
        "created_at": created,
    }
    entities = _admin_service([[entity, entity]]).list_entities(
        principal_id=principal, query="ent", status=None, entity_type=None, limit=1, cursor=None
    )
    assert entities.next_cursor is not None

    event = {
        "id": UUID("00000000-0000-4000-8000-000000000081"),
        "event_type": "publication.document.granted",
        "aggregate_type": "document",
        "aggregate_id": UUID("00000000-0000-4000-8000-000000000003"),
        "occurred_at": created,
        "publish_attempts": 2,
        "terminal_at": created,
        "terminal_error_code": "publication_manifest_invalid",
        "last_error_code": None,
        "available_at": None,
        "published_at": None,
    }
    waiting = {
        **event,
        "id": UUID("00000000-0000-4000-8000-000000000082"),
        "terminal_at": None,
        "terminal_error_code": None,
        "last_error_code": "publication_lease_lost",
        "available_at": created,
    }
    events = _admin_service([[event, waiting]]).list_publication_events(
        principal_id=principal, state=None, limit=1, cursor=None
    )
    assert events.next_cursor is not None
    assert events.items[0].state.value == "terminal"
    waiting_page = _admin_service([[waiting]]).list_publication_events(
        principal_id=principal, state="retry_wait", limit=20, cursor=None
    )
    assert waiting_page.items[0].error_summary is not None
    occurred_digest = filters_digest({"state": None})
    occurred_cursor = codec.encode(
        resource="publication-events",
        sort=OCCURRED_SORT,
        last=[created.isoformat(), str(event["id"])],
        filters_sha256=occurred_digest,
    )
    _admin_service([[waiting]]).list_publication_events(
        principal_id=principal, state=None, limit=20, cursor=occurred_cursor
    )
    with pytest.raises(CursorError):
        _admin_service([]).list_publication_events(
            principal_id=principal,
            state=None,
            limit=20,
            cursor=codec.encode(
                resource="publication-events",
                sort=OCCURRED_SORT,
                last=["only"],
                filters_sha256=occurred_digest,
            ),
        )
    with pytest.raises(CursorError):
        _admin_service([]).list_publication_events(
            principal_id=principal,
            state=None,
            limit=20,
            cursor=codec.encode(
                resource="publication-events",
                sort=OCCURRED_SORT,
                last=["bad", str(event["id"])],
                filters_sha256=occurred_digest,
            ),
        )
    with pytest.raises(CursorError):
        _admin_service([]).list_publication_events(
            principal_id=principal,
            state=None,
            limit=20,
            cursor=codec.encode(
                resource="publication-events",
                sort=OCCURRED_SORT,
                last=["2026-08-31T00:00:00", str(event["id"])],
                filters_sha256=occurred_digest,
            ),
        )

    request_id = UUID("00000000-0000-4000-8000-000000000044")
    with pytest.raises(AdminError) as closed:
        _admin_service([]).open_case(
            principal_id=principal,
            request_id=request_id,
            case_type="relation",
            subject_id=UUID("00000000-0000-4000-8000-000000000003"),
            priority=0,
            reason="open a relation case",
        )
    assert closed.value.code == "api_capability_closed"

    with patch(
        "uap_platform.admin_api.service.open_review_case",
        return_value=UUID("00000000-0000-4000-8000-000000000010"),
    ):
        opened = _admin_service([]).open_case(
            principal_id=principal,
            request_id=request_id,
            case_type="document",
            subject_id=UUID("00000000-0000-4000-8000-000000000003"),
            priority=0,
            reason="open a document case",
        )
    assert opened.operation == "review.case.open"

    with patch(
        "uap_platform.admin_api.service.assign_review_case",
        return_value=UUID("00000000-0000-4000-8000-000000000010"),
    ):
        assigned = _admin_service([]).assign_case(
            principal_id=principal,
            request_id=request_id,
            case_id=UUID("00000000-0000-4000-8000-000000000010"),
            assignee_id=principal,
        )
    assert assigned.operation == "review.case.assign"

    with patch(
        "uap_platform.admin_api.service.close_review_case",
        return_value=UUID("00000000-0000-4000-8000-000000000010"),
    ):
        closed_case = _admin_service([]).close_case(
            principal_id=principal,
            request_id=request_id,
            case_id=UUID("00000000-0000-4000-8000-000000000010"),
            reason="close this review case",
        )
    assert closed_case.operation == "review.case.close"

    with patch(
        "uap_platform.admin_api.service.record_review_decision",
        return_value=UUID("00000000-0000-4000-8000-000000000021"),
    ):
        decided = _admin_service([publication]).record_decision(
            principal_id=principal,
            request_id=request_id,
            case_id=UUID("00000000-0000-4000-8000-000000000010"),
            decision="approve",
            reason="approved after review",
            structured_changes={},
        )
    assert decided.publication is not None

    with patch(
        "uap_platform.admin_api.service.select_analysis_result",
        return_value=UUID("00000000-0000-4000-8000-000000000041"),
    ):
        assert (
            _admin_service([])
            .select_result(
                principal_id=principal,
                request_id=request_id,
                analysis_result_id=UUID("00000000-0000-4000-8000-000000000041"),
                reason="select this analysis",
            )
            .operation
            == "review.selection"
        )
    with patch(
        "uap_platform.admin_api.service.accept_entity_candidate",
        return_value=UUID("00000000-0000-4000-8000-000000000051"),
    ):
        assert (
            _admin_service([])
            .accept_candidate(
                principal_id=principal,
                request_id=request_id,
                candidate_id=UUID("00000000-0000-4000-8000-000000000051"),
                reason="accept this candidate",
            )
            .operation
            == "review.candidate.accept"
        )
    with patch(
        "uap_platform.admin_api.service.bind_entity_candidate",
        return_value=UUID("00000000-0000-4000-8000-000000000071"),
    ):
        bound = _admin_service([]).bind_candidate(
            principal_id=principal,
            request_id=request_id,
            candidate_id=UUID("00000000-0000-4000-8000-000000000051"),
            entity_id=UUID("00000000-0000-4000-8000-000000000071"),
            reason="bind this candidate",
        )
    assert bound.resource_id == UUID("00000000-0000-4000-8000-000000000071")
    with patch(
        "uap_platform.admin_api.service.apply_entity_merge",
        return_value=UUID("00000000-0000-4000-8000-000000000091"),
    ):
        assert (
            _admin_service([])
            .merge_entities(
                principal_id=principal,
                request_id=request_id,
                source_entity_id=UUID("00000000-0000-4000-8000-000000000071"),
                target_entity_id=UUID("00000000-0000-4000-8000-000000000072"),
                reason="merge two entities",
            )
            .operation
            == "review.entity.merge"
        )
    with patch(
        "uap_platform.admin_api.service.apply_entity_merge_reverse",
        return_value=UUID("00000000-0000-4000-8000-000000000091"),
    ):
        assert (
            _admin_service([])
            .reverse_merge(
                principal_id=principal,
                request_id=request_id,
                merge_event_id=UUID("00000000-0000-4000-8000-000000000091"),
                reason="reverse that merge",
            )
            .operation
            == "review.entity.merge_reverse"
        )
    with patch(
        "uap_platform.admin_api.service.create_manual_claim",
        return_value=UUID("00000000-0000-4000-8000-000000000002"),
    ):
        assert (
            _admin_service([])
            .create_claim(
                principal_id=principal,
                request_id=request_id,
                document_version_id=UUID("00000000-0000-4000-8000-000000000042"),
                claim_text="manual claim",
                claim_type="observation",
                assertion_status="reported",
                attribution=None,
                span_ids=[UUID("00000000-0000-4000-8000-000000000061")],
            )
            .operation
            == "review.claim.manual"
        )

    replayed = _admin_service(
        [(UUID("00000000-0000-4000-8000-000000000081"),)]
    ).replay_publication_event(
        principal_id=principal,
        request_id=request_id,
        event_id=UUID("00000000-0000-4000-8000-000000000081"),
        reason="replay publication",
    )
    assert replayed.operation == "publication.replay"
    with pytest.raises(AdminError):
        _admin_service([None]).replay_publication_event(
            principal_id=principal,
            request_id=request_id,
            event_id=UUID("00000000-0000-4000-8000-000000000081"),
            reason="replay publication",
        )
    with pytest.raises(AdminError):
        _admin_service([{"requeue_publication_event": None}]).replay_publication_event(
            principal_id=principal,
            request_id=request_id,
            event_id=UUID("00000000-0000-4000-8000-000000000081"),
            reason="replay publication",
        )

    def _raise_db(_connection: object) -> UUID:
        raise psycopg.OperationalError("missing")

    service_write = _admin_service([])
    with pytest.raises(AdminError) as mapped:
        service_write._write(
            principal,
            request_id,
            "review.case.open",
            _raise_db,
        )
    assert mapped.value.code == "api_internal_error"

    def _raise_review(_connection: object) -> UUID:
        raise ReviewSessionError("review_role_denied", "42501")

    with pytest.raises(AdminError) as denied:
        service_write._write(
            principal,
            request_id,
            "review.case.open",
            _raise_review,
        )
    assert denied.value.code == "review_role_denied"

    resolved = _admin_service(
        [{"id": principal, "active": True, "principal_type": "person"}]
    ).resolve_principal("https://issuer.test/wp10.5", "reviewer-subject")
    assert resolved == principal
    with pytest.raises(AdminError):
        _admin_service([None]).resolve_principal("https://issuer.test/wp10.5", "missing")
    with pytest.raises(AdminError):
        _admin_service(
            [{"id": principal, "active": False, "principal_type": "person"}]
        ).resolve_principal("https://issuer.test/wp10.5", "inactive")

    class RoleProbeService(AdminQueryService):
        pass

    probe_connection = AdminScriptedConnection([])
    typed_probe = cast(Connection[dict[str, object]], probe_connection)
    probe = RoleProbeService(cast(AdminApiPool, AdminScriptedPool(probe_connection)), codec)

    def _deny_then_allow(connection: object, role: str) -> UUID:
        del connection
        if role == "senior_reviewer":
            raise ReviewSessionError("review_role_denied", "42501")
        return principal

    with patch("uap_platform.admin_api.service.require_active_role", side_effect=_deny_then_allow):
        assert (
            probe._require_any_role(typed_probe, ("senior_reviewer", "data_operator")) == principal
        )

    def _deny_all(connection: object, role: str) -> UUID:
        del connection, role
        raise ReviewSessionError("review_role_denied", "42501")

    with patch("uap_platform.admin_api.service.require_active_role", side_effect=_deny_all):
        with pytest.raises(AdminError) as any_denied:
            probe._require_any_role(typed_probe, ("senior_reviewer", "data_operator"))
        assert any_denied.value.code == "review_role_denied"

    def _other_review(connection: object, role: str) -> UUID:
        del connection, role
        raise ReviewSessionError("review_principal_missing", "42501")

    with patch("uap_platform.admin_api.service.require_active_role", side_effect=_other_review):
        with pytest.raises(AdminError) as missing:
            probe._require_any_role(typed_probe, ("senior_reviewer", "data_operator"))
        assert missing.value.code == "review_principal_missing"

    def _db_role(connection: object, role: str) -> UUID:
        del connection, role
        raise psycopg.OperationalError("missing")

    with patch("uap_platform.admin_api.service.require_active_role", side_effect=_db_role):
        with pytest.raises(AdminError):
            probe._require_any_role(typed_probe, ("senior_reviewer",))

    with patch(
        "uap_platform.admin_api.service.require_active_role",
        side_effect=ReviewSessionError("review_role_denied", "42501"),
    ):
        with pytest.raises(AdminError):
            probe._require_role(typed_probe, "reviewer")
    with patch(
        "uap_platform.admin_api.service.require_active_role",
        side_effect=psycopg.OperationalError("missing"),
    ):
        with pytest.raises(AdminError):
            probe._require_role(typed_probe, "reviewer")

    with pytest.raises(CursorError):
        AdminQueryService._iso("not-a-datetime")
    assert AdminQueryService._iso(created).startswith("2026-08-31")


def test_admin_handler_covers_remaining_routes_and_errors() -> None:
    from datetime import UTC, datetime
    from uuid import UUID

    import psycopg

    from uap_platform.admin_api.contracts import (
        AdminEntityPage,
        AnalysisResultPage,
        EntityCandidatePage,
        EvidenceSpanPage,
        PublicationEventPage,
        ReviewCaseDetail,
        ReviewCasePage,
        ReviewCaseSummary,
        ReviewCaseType,
        ReviewStatus,
    )
    from uap_platform.admin_api.cursor import CursorError
    from uap_platform.admin_api.handler import AdminApiApplication
    from uap_platform.admin_api.oidc import TokenError

    principal = UUID("00000000-0000-4000-8000-000000000001")
    case_id = UUID("00000000-0000-4000-8000-000000000010")
    reason = "justified administrative action"

    class RichAdminService(FakeService):
        def list_analysis_results(self, **kwargs: object) -> AnalysisResultPage:
            del kwargs
            return AnalysisResultPage(items=[], next_cursor=None)

        def list_entity_candidates(self, **kwargs: object) -> EntityCandidatePage:
            del kwargs
            return EntityCandidatePage(items=[], next_cursor=None)

        def list_evidence_spans(self, **kwargs: object) -> EvidenceSpanPage:
            del kwargs
            return EvidenceSpanPage(items=[], next_cursor=None)

        def list_entities(self, **kwargs: object) -> AdminEntityPage:
            if kwargs.get("cursor") == "bad":
                raise CursorError("cursor is invalid")
            return AdminEntityPage(items=[], next_cursor=None)

        def list_publication_events(self, **kwargs: object) -> PublicationEventPage:
            del kwargs
            return PublicationEventPage(items=[], next_cursor=None)

        def get_review_case(self, principal_id: UUID, case_id: UUID) -> ReviewCaseDetail | None:
            del principal_id
            if str(case_id).endswith("099"):
                return None
            summary = ReviewCaseSummary(
                id=case_id,
                case_type=ReviewCaseType.DOCUMENT,
                status=ReviewStatus.OPEN,
                priority=0,
                subject_id=UUID("00000000-0000-4000-8000-000000000003"),
                assigned_to=None,
                opened_by=principal,
                opened_at=datetime(2026, 8, 31, tzinfo=UTC),
                closed_at=None,
            )
            return ReviewCaseDetail(**summary.model_dump(), decisions=[], publication=None)

        def assign_case(self, **kwargs: object) -> WriteResult:
            return WriteResult(
                operation="review.case.assign",
                resource_id=UUID(str(kwargs["case_id"])),
                request_id=UUID(str(kwargs["request_id"])),
                publication=None,
            )

        def close_case(self, **kwargs: object) -> WriteResult:
            return WriteResult(
                operation="review.case.close",
                resource_id=UUID(str(kwargs["case_id"])),
                request_id=UUID(str(kwargs["request_id"])),
                publication=None,
            )

        def record_decision(self, **kwargs: object) -> WriteResult:
            return WriteResult(
                operation="review.decision",
                resource_id=UUID(str(kwargs["case_id"])),
                request_id=UUID(str(kwargs["request_id"])),
                publication=None,
            )

        def select_result(self, **kwargs: object) -> WriteResult:
            return WriteResult(
                operation="review.selection",
                resource_id=UUID(str(kwargs["analysis_result_id"])),
                request_id=UUID(str(kwargs["request_id"])),
                publication=None,
            )

        def accept_candidate(self, **kwargs: object) -> WriteResult:
            return WriteResult(
                operation="review.candidate.accept",
                resource_id=UUID(str(kwargs["candidate_id"])),
                request_id=UUID(str(kwargs["request_id"])),
                publication=None,
            )

        def bind_candidate(self, **kwargs: object) -> WriteResult:
            return WriteResult(
                operation="review.candidate.bind",
                resource_id=UUID(str(kwargs["entity_id"])),
                request_id=UUID(str(kwargs["request_id"])),
                publication=None,
            )

        def merge_entities(self, **kwargs: object) -> WriteResult:
            return WriteResult(
                operation="review.entity.merge",
                resource_id=UUID(str(kwargs["target_entity_id"])),
                request_id=UUID(str(kwargs["request_id"])),
                publication=None,
            )

        def reverse_merge(self, **kwargs: object) -> WriteResult:
            return WriteResult(
                operation="review.entity.merge_reverse",
                resource_id=UUID(str(kwargs["merge_event_id"])),
                request_id=UUID(str(kwargs["request_id"])),
                publication=None,
            )

        def create_claim(self, **kwargs: object) -> WriteResult:
            return WriteResult(
                operation="review.claim.manual",
                resource_id=UUID("00000000-0000-4000-8000-000000000002"),
                request_id=UUID(str(kwargs["request_id"])),
                publication=None,
            )

        def replay_publication_event(self, **kwargs: object) -> WriteResult:
            reason = str(kwargs.get("reason") or "")
            if reason.startswith("explode"):
                raise RuntimeError("boom")
            if reason.startswith("dbdown"):
                raise psycopg.OperationalError("down")
            return WriteResult(
                operation="publication.replay",
                resource_id=UUID(str(kwargs["event_id"])),
                request_id=UUID(str(kwargs["request_id"])),
                publication=None,
            )

        def list_review_cases(self, **kwargs: object) -> ReviewCasePage:
            if kwargs.get("cursor") == "token-error":
                raise TokenError("api_token_invalid")
            return ReviewCasePage(items=[], next_cursor=None)

    app = AdminApiApplication(
        cast(AdminQueryService, RichAdminService()),
        cast(OidcValidator, FakeOidc()),
        "https://issuer.test/wp10.5",
    )
    auth = {"Authorization": "Bearer reviewer-subject"}
    write = {
        **auth,
        "Idempotency-Key": "00000000-0000-4000-8000-000000000044",
    }
    assert app.handle("GET", "/admin/v1/review-cases", auth).status == 200
    assert app.handle("GET", f"/admin/v1/review-cases/{case_id}", auth).status == 200
    missing = app.handle("GET", "/admin/v1/review-cases/00000000-0000-4000-8000-000000000099", auth)
    assert missing.status == 404
    assert app.handle("GET", "/admin/v1/analysis-results", auth).status == 200
    assert app.handle("GET", "/admin/v1/entity-candidates", auth).status == 200
    spans = app.handle(
        "GET",
        "/admin/v1/evidence-spans?document_version_id=00000000-0000-4000-8000-000000000042",
        auth,
    )
    assert spans.status == 200
    assert app.handle("GET", "/admin/v1/evidence-spans", auth).status == 422
    assert app.handle("GET", "/admin/v1/entities", auth).status == 200
    assert app.handle("GET", "/admin/v1/publication-events", auth).status == 200
    assert app.handle("GET", "/admin/v1/unknown", auth).status == 404
    assert app.handle("GET", "/admin/v1/entities?cursor=bad", auth).status == 400
    assert app.handle("PATCH", "/admin/v1/review-cases", auth).status == 404
    too_long = "/admin/v1/" + ("a" * 9000)
    assert app.handle("GET", too_long, auth).status == 404
    assert app.handle("GET", "http://evil.test/admin/v1/review-cases", auth).status == 422
    assert app.handle("GET", "/admin/v1/review-cases?a=1&a=2", auth).status == 422
    assert app.handle("GET", "/admin/v1/review-cases?limit=", auth).status == 422
    assert app.handle("GET", "/admin/v1/review-cases?limit=no", auth).status == 422
    assert app.handle("GET", "/admin/v1/review-cases?limit=0", auth).status == 422
    assert app.handle("GET", "/admin/v1/review-cases?status=nope", auth).status == 422
    assert app.handle("GET", "/admin/v1/review-cases?assigned_to=not-a-uuid", auth).status == 422
    assert app.handle("POST", "/healthz", auth).status == 404
    assert (
        app.handle("GET", "/admin/v1/review-cases", {"X-Request-ID": "not-a-uuid", **auth}).status
        == 422
    )
    assert (
        app.handle(
            "GET",
            "/admin/v1/review-cases",
            {"X-Request-ID": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".upper(), **auth},
        ).status
        == 422
    )
    assert app.handle("POST", "/admin/v1/review-cases", write, b"").status == 422
    assert app.handle("POST", "/admin/v1/review-cases", write, b"[]").status == 422
    assert app.handle("POST", "/admin/v1/review-cases", write, b"{").status == 422
    assert app.handle("POST", "/admin/v1/review-cases", write, b"x" * 1_000_001).status == 422
    assert (
        app.handle(
            "POST",
            "/admin/v1/review-cases",
            {**auth, "Idempotency-Key": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".upper()},
            b"{}",
        ).status
        == 400
    )

    def _write(path: str, payload: dict[str, object], method: str = "POST") -> int:
        return app.handle(method, path, write, json.dumps(payload).encode()).status

    assert (
        _write(
            f"/admin/v1/review-cases/{case_id}/assignment",
            {"assignee_id": str(principal)},
            "PUT",
        )
        == 200
    )
    assert _write(f"/admin/v1/review-cases/{case_id}/close", {"reason": reason}) == 200
    assert (
        _write(
            f"/admin/v1/review-cases/{case_id}/decisions",
            {"decision": "approve", "reason": reason},
        )
        == 200
    )
    assert (
        _write(
            "/admin/v1/analysis-results/00000000-0000-4000-8000-000000000041/selection",
            {"reason": reason},
        )
        == 200
    )
    assert (
        _write(
            "/admin/v1/entity-candidates/00000000-0000-4000-8000-000000000051/accept",
            {"reason": reason},
        )
        == 200
    )
    assert (
        _write(
            "/admin/v1/entity-candidates/00000000-0000-4000-8000-000000000051/bind",
            {"entity_id": "00000000-0000-4000-8000-000000000071", "reason": reason},
        )
        == 200
    )
    assert (
        _write(
            "/admin/v1/entities/merges",
            {
                "source_entity_id": "00000000-0000-4000-8000-000000000071",
                "target_entity_id": "00000000-0000-4000-8000-000000000072",
                "reason": reason,
            },
        )
        == 200
    )
    assert (
        _write(
            "/admin/v1/entities/merge-events/00000000-0000-4000-8000-000000000091/reverse",
            {"reason": reason},
        )
        == 200
    )
    assert (
        _write(
            "/admin/v1/claims/manual",
            {
                "document_version_id": "00000000-0000-4000-8000-000000000042",
                "claim_text": "manual",
                "claim_type": "observation",
                "assertion_status": "reported",
                "attribution": None,
                "span_ids": ["00000000-0000-4000-8000-000000000061"],
            },
        )
        == 200
    )
    assert (
        _write(
            "/admin/v1/publication-events/00000000-0000-4000-8000-000000000081/replay",
            {"reason": reason},
        )
        == 200
    )
    assert _write("/admin/v1/review-cases/not-uuid/close", {"reason": reason}) == 404
    explode = app.handle(
        "POST",
        "/admin/v1/publication-events/00000000-0000-4000-8000-000000000081/replay",
        write,
        json.dumps({"reason": "explode-me-now"}).encode(),
    )
    assert explode.status == 500
    db = app.handle(
        "POST",
        "/admin/v1/publication-events/00000000-0000-4000-8000-000000000081/replay",
        write,
        json.dumps({"reason": "dbdown-now!"}).encode(),
    )
    assert db.status == 503


def test_admin_pool_and_config_remaining_branches() -> None:
    from uuid import UUID, uuid4

    from uap_platform.admin_api.cursor import CursorCodec
    from uap_platform.admin_api.errors import AdminError, map_database_error

    with pytest.raises(ValueError):
        AdminApiPool("postgresql://uap_api@db/uap", min_size=0, max_size=1)
    with pytest.raises(ValueError):
        CursorCodec(b"short")

    jwks = json.dumps({"keys": [{"kty": "RSA", "n": "sA", "e": "AQAB"}]})
    base = {
        "UAP_ADMIN_DATABASE_URL": "postgresql://uap_api:secret@db/uap",
        "UAP_ADMIN_CURSOR_SECRET": "s" * 32,
        "UAP_ADMIN_OIDC_ISSUER": "https://issuer.test/wp10.5",
        "UAP_ADMIN_OIDC_AUDIENCE": "uap-admin",
        "UAP_ADMIN_OIDC_JWKS": jwks,
    }
    settings = load_admin_api_settings(base)
    assert settings.jwks_document["keys"]
    with pytest.raises(ValueError, match="issuer"):
        load_admin_api_settings({**base, "UAP_ADMIN_OIDC_ISSUER": "issuer.test"})
    with pytest.raises(ValueError, match="JWKS"):
        load_admin_api_settings({**base, "UAP_ADMIN_OIDC_JWKS": "{"})
    with pytest.raises(ValueError, match="JWKS"):
        load_admin_api_settings({**base, "UAP_ADMIN_OIDC_JWKS": json.dumps({"keys": []})})
    with pytest.raises(ValueError, match="pool_min_size"):
        load_admin_api_settings(
            {**base, "UAP_ADMIN_POOL_MIN_SIZE": "5", "UAP_ADMIN_POOL_MAX_SIZE": "2"}
        )
    with pytest.raises(ValueError, match="cursor secret"):
        load_admin_api_settings({**base, "UAP_ADMIN_CURSOR_SECRET": "tiny"})
    with pytest.raises(ValueError, match="invalid"):
        load_admin_api_settings({**base, "UAP_ADMIN_DATABASE_URL": "not-a-dsn"})

    unknown = map_database_error(psycopg.OperationalError("nope"))
    assert unknown.code == "api_internal_error"
    rewritten = AdminError("knowledge_relation_review_not_in_wp9")
    assert rewritten.code == "api_capability_closed"
    fallback = AdminError("not-a-real-code")
    assert fallback.code == "api_internal_error"

    pool = object.__new__(AdminApiPool)
    pool._dsn = "postgresql://uap_api@localhost/uap"
    pool._max_size = 1
    pool._created = 1
    pool._lock = Lock()
    pool._closed = False
    pool._connections = LifoQueue()
    connection = AdminScriptedConnection([])
    connection.info.transaction_status = TransactionStatus.IDLE
    pool._connections.put(cast(Connection[dict[str, object]], connection))
    principal = UUID("00000000-0000-4000-8000-000000000001")
    request_id = uuid4()
    with pool.read_transaction(principal) as checked:
        assert checked.closed is False
    with pool.write_transaction(principal, request_id) as checked:
        assert checked.closed is False
    pool._closed = True
    with pytest.raises(RuntimeError, match="closed"):
        pool._checkout()
    pool.close()

    class RoleCursor(AdminScriptedCursor):
        def fetchone(self) -> dict[str, object]:
            return {"session_user": "uap_public_reader"}

    class RoleConnection(AdminScriptedConnection):
        def cursor(self) -> RoleCursor:
            return RoleCursor(self)

    role = RoleConnection([])
    with patch("uap_platform.admin_api.pool.psycopg.connect", return_value=role):
        with pytest.raises(RuntimeError, match="role verification"):
            pool._open_reserved()
    assert role.closed is True
