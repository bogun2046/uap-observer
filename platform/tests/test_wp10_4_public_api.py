"""WP10.4 public API and read-index contract tests."""

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
from uuid import UUID

import pytest
from psycopg import Connection
from psycopg.pq import TransactionStatus

from tools import validate_wp10_4
from uap_platform.public_api.config import load_public_api_settings
from uap_platform.public_api.contracts import (
    DocumentCategory,
    DocumentPage,
    DocumentSummary,
    EntityPage,
    FactStatus,
    PublicSource,
    SearchPage,
)
from uap_platform.public_api.cursor import CursorCodec, CursorError, filters_digest
from uap_platform.public_api.handler import PublicApiApplication
from uap_platform.public_api.pool import PublicReaderPool
from uap_platform.public_api.service import PublicQueryService


def platform_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_wp10_4_migration_is_linear_and_index_only() -> None:
    migration = (platform_root() / "alembic/versions/0023_wp10_api_read_indexes.py").read_text(
        encoding="utf-8"
    )
    assert 'revision = "0023_wp10_api_read_indexes"' in migration
    assert 'down_revision = "0022_wp10_claim_search_projection"' in migration
    assert migration.count("CREATE INDEX") == 6
    assert migration.count("DROP INDEX") == 6
    assert "CREATE TABLE" not in migration
    assert "ALTER TABLE" not in migration
    assert "CREATE TYPE" not in migration
    assert "INSERT INTO" not in migration
    assert "UPDATE " not in migration
    assert "DELETE FROM" not in migration


def test_wp10_4_migration_reuses_signed_projection_indexes() -> None:
    migration = (platform_root() / "alembic/versions/0023_wp10_api_read_indexes.py").read_text(
        encoding="utf-8"
    )
    for token in (
        "ix_public_claims_document_ordinal",
        "UNIQUE(document_id, entity_id)",
        "ix_public_document_entities_entity",
        "ix_search_documents_vector",
        "ix_search_documents_facets",
    ):
        assert token in migration


def test_wp10_4_static_contract() -> None:
    assert [
        item.name for item in validate_wp10_4.evaluate(platform_root()) if not item.passed
    ] == []


def test_wp10_4_accepts_only_reviewed_implementation_ticket_versions() -> None:
    current = dict(validate_wp10_4.FROZEN_DOC_HASHES)
    current["implementation-ticket.md"] = validate_wp10_4.WP106_D_IMPLEMENTATION_TICKET_SHA256
    assert validate_wp10_4.frozen_docs_are_valid(current)

    historical = dict(validate_wp10_4.FROZEN_DOC_HASHES)
    assert validate_wp10_4.frozen_docs_are_valid(historical)

    unapproved = {**current, "implementation-ticket.md": "0" * 64}
    assert not validate_wp10_4.frozen_docs_are_valid(unapproved)

    changed_contract = {**current, "api-contract.md": "0" * 64}
    assert not validate_wp10_4.frozen_docs_are_valid(changed_contract)


def _document() -> DocumentSummary:
    return DocumentSummary(
        id=UUID("00000000-0000-4000-8000-000000000001"),
        slug="public-document",
        title="Public document",
        summary=None,
        category=DocumentCategory.OFFICIAL_REPORT,
        fact_status=FactStatus.OFFICIAL_RECORD,
        source=PublicSource(name="Public source", url="https://example.test/source"),
        source_published_at=None,
        published_at=datetime(2026, 8, 31, tzinfo=UTC),
        revised_at=None,
        revision=1,
    )


class FakeService:
    def list_documents(self, **kwargs: object) -> DocumentPage:
        return DocumentPage(items=[_document()], next_cursor=None)

    def get_document(self, document_id: UUID) -> None:
        return None

    def get_claim(self, claim_id: UUID) -> None:
        return None

    def list_entities(self, **kwargs: object) -> EntityPage:
        return EntityPage(items=[], next_cursor=None)

    def get_entity(self, entity_id: UUID) -> None:
        return None

    def search(self, **kwargs: object) -> SearchPage:
        return SearchPage(items=[], next_cursor=None)


def test_strict_public_dto_schema_and_no_internal_fields() -> None:
    schema = DocumentSummary.model_json_schema()
    assert schema["additionalProperties"] is False
    payload = json.loads(_document().model_dump_json())
    assert set(payload) == {
        "id",
        "slug",
        "title",
        "summary",
        "category",
        "fact_status",
        "source",
        "source_published_at",
        "published_at",
        "revised_at",
        "revision",
    }
    assert not {
        "document_grant_id",
        "decision_id",
        "reviewer_id",
        "object_key",
        "raw_body",
    } & set(payload)


def test_cursor_is_canonical_context_bound_and_tamper_evident() -> None:
    codec = CursorCodec(b"x" * 32)
    digest = filters_digest({"category": "official_report"})
    cursor = codec.encode(
        resource="documents",
        sort="published_at_desc,id_desc",
        last=["2026-08-31T00:00:00+00:00", "00000000-0000-4000-8000-000000000001"],
        filters_sha256=digest,
    )
    assert (
        codec.decode(
            cursor,
            resource="documents",
            sort="published_at_desc,id_desc",
            filters_sha256=digest,
        )[0]
        == "2026-08-31T00:00:00+00:00"
    )
    invalid = (
        cursor[:-1] + ("A" if cursor[-1] != "A" else "B"),
        cursor,
        cursor,
        cursor,
        "x" * 2049,
    )
    contexts = (
        ("documents", "published_at_desc,id_desc", digest),
        ("entities", "published_at_desc,id_desc", digest),
        ("documents", "wrong_sort", digest),
        ("documents", "published_at_desc,id_desc", filters_digest({})),
        ("documents", "published_at_desc,id_desc", digest),
    )
    for token, (resource, sort, bound_filters) in zip(invalid, contexts, strict=True):
        with pytest.raises(CursorError):
            codec.decode(
                token,
                resource=resource,
                sort=sort,
                filters_sha256=bound_filters,
            )


def test_public_config_requires_reader_and_rejects_privileged_dsn() -> None:
    base = {
        "UAP_PUBLIC_DATABASE_URL": "postgresql://uap_public_reader:secret@db/uap",
        "UAP_PUBLIC_CURSOR_SECRET": "s" * 32,
    }
    settings = load_public_api_settings(base)
    assert settings.safe_summary()["port"] == 8081
    assert "uap_public_reader:secret" not in repr(settings)
    with pytest.raises(ValueError, match="privileged"):
        load_public_api_settings({**base, "UAP_DATABASE_URL": "postgresql://owner/db"})
    with pytest.raises(ValueError, match="privileged"):
        load_public_api_settings({**base, "UNRELATED_OWNER_DSN": "postgresql://owner/db"})
    with pytest.raises(ValueError, match="public reader"):
        load_public_api_settings(
            {**base, "UAP_PUBLIC_DATABASE_URL": "postgresql://uap_api:secret@db/uap"}
        )


def test_http_routes_media_request_id_and_closed_relation() -> None:
    app = PublicApiApplication(FakeService())  # type: ignore[arg-type]
    request_id = "00000000-0000-4000-8000-000000000099"
    response = app.handle("GET", "/v1/documents", {"X-Request-ID": request_id})
    assert response.status == 200
    assert response.headers == {
        "Content-Type": "application/json",
        "X-Request-ID": request_id,
    }
    assert json.loads(response.body)["items"][0]["id"] == str(_document().id)

    relation = app.handle("GET", "/v1/relations")
    assert relation.status == 404
    assert relation.headers["Content-Type"] == "application/problem+json"
    assert json.loads(relation.body)["code"] == "api_capability_closed"
    relation_detail = app.handle("GET", "/v1/relations/00000000-0000-4000-8000-000000000099")
    assert relation_detail.status == 404
    assert json.loads(relation_detail.body)["code"] == "api_capability_closed"
    health = app.handle("GET", "/healthz")
    assert health.status == 200
    assert health.headers["Content-Type"] == "application/json"
    assert json.loads(health.body) == {"status": "ok"}
    unknown = app.handle("GET", "/v1/not-a-resource")
    assert json.loads(unknown.body)["code"] == "api_resource_not_found"
    write = app.handle("POST", "/v1/documents")
    assert write.status == 404
    assert write.headers["Content-Type"] == "application/problem+json"
    assert UUID(write.headers["X-Request-ID"])


@pytest.mark.parametrize("length,status", [(1, 400), (2, 200), (200, 200), (201, 400)])
def test_search_nfkc_code_point_boundaries(length: int, status: int) -> None:
    app = PublicApiApplication(FakeService())  # type: ignore[arg-type]
    response = app.handle("GET", f"/v1/search?q={'测' * length}")
    assert response.status == status


def test_query_validation_and_cursor_error_are_sanitized() -> None:
    app = PublicApiApplication(FakeService())  # type: ignore[arg-type]
    invalid = app.handle("GET", "/v1/documents?unknown=value")
    assert invalid.status == 422
    body = json.loads(invalid.body)
    assert body["code"] == "api_request_invalid"
    assert set(body) == {"type", "title", "status", "code", "request_id", "detail"}
    assert not any(token in invalid.body.decode() for token in ("SQLSTATE", "public.", "audit."))


def test_service_sql_is_public_only_parameterized_and_relation_free() -> None:
    source = (platform_root() / "src/uap_platform/public_api/service.py").read_text(
        encoding="utf-8"
    )
    assert "websearch_to_tsquery('simple', %s)" in source
    assert "public.search_documents" in source
    assert "public.relations" not in source
    assert not any(f"{schema}." in source for schema in ("ingest", "core", "ops", "audit"))
    assert ' f"""' not in source


def test_pool_slot_reservation_is_atomic_and_failure_releases_slot() -> None:
    pool = object.__new__(PublicReaderPool)
    pool._dsn = "postgresql://uap_public_reader@localhost/uap"
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
    with patch("uap_platform.public_api.pool.psycopg.connect", side_effect=RuntimeError("connect")):
        with pytest.raises(RuntimeError, match="connect"):
            pool._open_reserved()
    assert pool._created == 0


def _document_row(**overrides: object) -> dict[str, object]:
    from uap_platform.public_api.contracts import DocumentCategory, FactStatus

    row: dict[str, object] = {
        "id": UUID("00000000-0000-4000-8000-000000000001"),
        "slug": "public-document",
        "title": "Public document",
        "summary": None,
        "category": DocumentCategory.OFFICIAL_REPORT,
        "fact_status": FactStatus.OFFICIAL_RECORD,
        "source_name": "Public source",
        "canonical_source_url": "https://example.test/source",
        "source_published_at": None,
        "published_at": datetime(2026, 8, 31, tzinfo=UTC),
        "revised_at": None,
        "revision_no": 1,
        "headline": "matched excerpt",
        "rank": 0.9,
    }
    row.update(overrides)
    return row


def _claim_row() -> dict[str, object]:
    from uap_platform.public_api.contracts import AssertionStatus, ClaimType

    return {
        "id": UUID("00000000-0000-4000-8000-000000000002"),
        "document_id": UUID("00000000-0000-4000-8000-000000000001"),
        "ordinal": 1,
        "claim_text": "A public claim",
        "claim_type": ClaimType.OBSERVATION,
        "assertion_status": AssertionStatus.REPORTED,
        "attribution": None,
        "revision_no": 1,
    }


def _evidence_row() -> dict[str, object]:
    from uap_platform.public_api.contracts import LocatorType

    return {
        "id": UUID("00000000-0000-4000-8000-000000000003"),
        "excerpt": "excerpt from the public source",
        "locator_type": LocatorType.TEXT,
        "page_start": None,
        "page_end": None,
        "time_start_ms": None,
        "time_end_ms": None,
        "public_locator": {"char_start": 0, "char_end": 12},
        "source_url": "https://example.test/source",
    }


def _entity_row(**overrides: object) -> dict[str, object]:
    from uap_platform.public_api.contracts import EntityType

    row: dict[str, object] = {
        "id": UUID("00000000-0000-4000-8000-000000000004"),
        "slug": "public-entity",
        "entity_type": EntityType.ORGANIZATION,
        "name": "Public entity",
        "description": None,
        "country_code": "US",
        "revision_no": 1,
        "published_at": datetime(2026, 8, 31, tzinfo=UTC),
    }
    row.update(overrides)
    return row


class ScriptedCursor:
    def __init__(self, owner: ScriptedConnection) -> None:
        self._owner = owner
        self._current: object = None

    def execute(self, sql: str, params: object = None) -> None:
        del sql, params
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

    def __enter__(self) -> ScriptedCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class ScriptedConnection:
    def __init__(self, results: list[object]) -> None:
        self._results = list(results)
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.info = type("Info", (), {"transaction_status": TransactionStatus.IDLE})()

    def next_result(self) -> object:
        if not self._results:
            return None
        return self._results.pop(0)

    def cursor(self) -> ScriptedCursor:
        return ScriptedCursor(self)

    def commit(self) -> None:
        self.commits += 1
        self.info.transaction_status = TransactionStatus.IDLE

    def rollback(self) -> None:
        self.rollbacks += 1
        self.info.transaction_status = TransactionStatus.IDLE

    def close(self) -> None:
        self.closed = True


class ScriptedPool:
    def __init__(self, connection: ScriptedConnection) -> None:
        self._connection = connection

    @contextmanager
    def transaction(self) -> Iterator[ScriptedConnection]:
        yield self._connection


def _public_service(results: list[object]) -> PublicQueryService:
    from uap_platform.public_api.cursor import CursorCodec

    connection = ScriptedConnection(results)
    pool = ScriptedPool(connection)
    codec = CursorCodec(b"k" * 32)
    return PublicQueryService(cast(PublicReaderPool, pool), codec)


def test_public_query_service_lists_details_search_and_cursors() -> None:
    from math import inf

    from uap_platform.public_api.cursor import CursorCodec, CursorError, filters_digest
    from uap_platform.public_api.service import DOCUMENT_SORT, ENTITY_SORT, SEARCH_SORT

    first = _document_row()
    second = _document_row(id=UUID("00000000-0000-4000-8000-000000000011"), slug="second-doc")
    service = _public_service([[first, second]])
    page = service.list_documents(
        limit=1,
        category="official_report",
        fact_status="official_record",
        published_after=datetime(2026, 1, 1, tzinfo=UTC),
        cursor=None,
    )
    assert len(page.items) == 1
    assert page.next_cursor is not None

    codec = CursorCodec(b"k" * 32)
    digest = filters_digest(
        {
            "category": "official_report",
            "fact_status": "official_record",
            "published_after": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
        }
    )
    cursor = codec.encode(
        resource="documents",
        sort=DOCUMENT_SORT,
        last=[datetime(2026, 8, 31, tzinfo=UTC).isoformat(), str(first["id"])],
        filters_sha256=digest,
    )
    paged = _public_service([[second]]).list_documents(
        limit=20,
        category="official_report",
        fact_status="official_record",
        published_after=datetime(2026, 1, 1, tzinfo=UTC),
        cursor=cursor,
    )
    assert paged.items[0].slug == "second-doc"

    with pytest.raises(CursorError):
        _public_service([]).list_documents(
            limit=20,
            category=None,
            fact_status=None,
            published_after=None,
            cursor=codec.encode(
                resource="documents",
                sort=DOCUMENT_SORT,
                last=["only-one"],
                filters_sha256=filters_digest(
                    {"category": None, "fact_status": None, "published_after": None}
                ),
            ),
        )
    with pytest.raises(CursorError):
        _public_service([]).list_documents(
            limit=20,
            category=None,
            fact_status=None,
            published_after=None,
            cursor=codec.encode(
                resource="documents",
                sort=DOCUMENT_SORT,
                last=["not-a-date", str(first["id"])],
                filters_sha256=filters_digest(
                    {"category": None, "fact_status": None, "published_after": None}
                ),
            ),
        )
    naive = codec.encode(
        resource="documents",
        sort=DOCUMENT_SORT,
        last=["2026-08-31T00:00:00", str(first["id"])],
        filters_sha256=filters_digest(
            {"category": None, "fact_status": None, "published_after": None}
        ),
    )
    with pytest.raises(CursorError):
        _public_service([]).list_documents(
            limit=20,
            category=None,
            fact_status=None,
            published_after=None,
            cursor=naive,
        )

    detail = _public_service(
        [first, [_claim_row()], [_evidence_row()], [_entity_row()]]
    ).get_document(UUID(str(first["id"])))
    assert detail is not None
    assert detail.claims[0].evidence[0].excerpt.startswith("excerpt")
    assert detail.related_entities[0].slug == "public-entity"
    assert (
        _public_service([None]).get_document(UUID("00000000-0000-4000-8000-000000000099")) is None
    )

    claim = _public_service([_claim_row(), [_evidence_row()]]).get_claim(
        UUID("00000000-0000-4000-8000-000000000002")
    )
    assert claim is not None
    assert claim.text == "A public claim"
    assert _public_service([None]).get_claim(UUID("00000000-0000-4000-8000-000000000002")) is None

    entity_first = _entity_row()
    entity_second = _entity_row(
        id=UUID("00000000-0000-4000-8000-000000000014"), slug="second-entity"
    )
    entities = _public_service([[entity_first, entity_second]]).list_entities(
        limit=1, entity_type="organization", cursor=None
    )
    assert entities.next_cursor is not None
    entity_digest = filters_digest({"type": "organization"})
    entity_cursor = codec.encode(
        resource="entities",
        sort=ENTITY_SORT,
        last=[datetime(2026, 8, 31, tzinfo=UTC).isoformat(), str(entity_first["id"])],
        filters_sha256=entity_digest,
    )
    listed = _public_service([[entity_second]]).list_entities(
        limit=20, entity_type="organization", cursor=entity_cursor
    )
    assert listed.items[0].slug == "second-entity"
    with pytest.raises(CursorError):
        _public_service([]).list_entities(
            limit=20,
            entity_type=None,
            cursor=codec.encode(
                resource="entities",
                sort=ENTITY_SORT,
                last=["only"],
                filters_sha256=filters_digest({"type": None}),
            ),
        )
    with pytest.raises(CursorError):
        _public_service([]).list_entities(
            limit=20,
            entity_type=None,
            cursor=codec.encode(
                resource="entities",
                sort=ENTITY_SORT,
                last=["nope", str(entity_first["id"])],
                filters_sha256=filters_digest({"type": None}),
            ),
        )
    with pytest.raises(CursorError):
        _public_service([]).list_entities(
            limit=20,
            entity_type=None,
            cursor=codec.encode(
                resource="entities",
                sort=ENTITY_SORT,
                last=["2026-08-31T00:00:00", str(entity_first["id"])],
                filters_sha256=filters_digest({"type": None}),
            ),
        )

    entity_detail = _public_service([entity_first, [first]]).get_entity(
        UUID(str(entity_first["id"]))
    )
    assert entity_detail is not None
    assert entity_detail.related_documents[0].slug == "public-document"
    assert _public_service([None]).get_entity(UUID(str(entity_first["id"]))) is None

    search_first = _document_row(rank=1.0)
    search_second = _document_row(
        id=UUID("00000000-0000-4000-8000-000000000015"), slug="search-two", rank=0.2
    )
    search_page = _public_service([[search_first, search_second]]).search(
        query="uap", limit=1, category=None, fact_status=None, cursor=None
    )
    assert search_page.next_cursor is not None
    search_digest = filters_digest({"q": "uap", "category": None, "fact_status": None})
    search_cursor = codec.encode(
        resource="search",
        sort=SEARCH_SORT,
        last=[1.0, datetime(2026, 8, 31, tzinfo=UTC).isoformat(), str(search_first["id"])],
        filters_sha256=search_digest,
    )
    continued = _public_service([[search_second]]).search(
        query="uap", limit=20, category=None, fact_status=None, cursor=search_cursor
    )
    assert continued.items[0].document.slug == "search-two"
    with pytest.raises(CursorError):
        _public_service([]).search(
            query="uap",
            limit=20,
            category=None,
            fact_status=None,
            cursor=codec.encode(
                resource="search",
                sort=SEARCH_SORT,
                last=[1.0, "x"],
                filters_sha256=search_digest,
            ),
        )
    with pytest.raises(CursorError):
        _public_service([]).search(
            query="uap",
            limit=20,
            category=None,
            fact_status=None,
            cursor=codec.encode(
                resource="search",
                sort=SEARCH_SORT,
                last=[
                    "bad",
                    datetime(2026, 8, 31, tzinfo=UTC).isoformat(),
                    str(search_first["id"]),
                ],
                filters_sha256=search_digest,
            ),
        )
    with pytest.raises(CursorError):
        _public_service([]).search(
            query="uap",
            limit=20,
            category=None,
            fact_status=None,
            cursor=codec.encode(
                resource="search",
                sort=SEARCH_SORT,
                last=[inf, datetime(2026, 8, 31, tzinfo=UTC).isoformat(), str(search_first["id"])],
                filters_sha256=search_digest,
            ),
        )


def test_public_handler_covers_details_errors_and_validation() -> None:
    import psycopg

    from uap_platform.public_api.contracts import (
        AssertionStatus,
        ClaimDetail,
        ClaimType,
        DocumentDetail,
        EntityDetail,
        EntitySummary,
        EntityType,
        LocatorType,
        PublicEvidence,
    )
    from uap_platform.public_api.cursor import CursorError
    from uap_platform.public_api.handler import PublicApiApplication

    document = _document()
    claim = ClaimDetail(
        id=UUID("00000000-0000-4000-8000-000000000002"),
        document_id=document.id,
        ordinal=1,
        text="A public claim",
        type=ClaimType.OBSERVATION,
        assertion_status=AssertionStatus.REPORTED,
        attribution=None,
        revision=1,
        evidence=[
            PublicEvidence(
                id=UUID("00000000-0000-4000-8000-000000000003"),
                excerpt="excerpt from the public source",
                locator_type=LocatorType.TEXT,
                page_start=None,
                page_end=None,
                time_start_ms=None,
                time_end_ms=None,
                locator={"char_start": 0},
                source_url="https://example.test/source",
            )
        ],
    )
    entity = EntitySummary(
        id=UUID("00000000-0000-4000-8000-000000000004"),
        slug="public-entity",
        type=EntityType.ORGANIZATION,
        name="Public entity",
        description=None,
        country_code="US",
        revision=1,
    )

    class RichService:
        def list_documents(self, **kwargs: object) -> DocumentPage:
            del kwargs
            return DocumentPage(items=[_document()], next_cursor=None)

        def get_document(self, document_id: UUID) -> DocumentDetail | None:
            if document_id == document.id:
                return DocumentDetail(
                    **document.model_dump(), claims=[claim], related_entities=[entity]
                )
            return None

        def get_claim(self, claim_id: UUID) -> ClaimDetail | None:
            return claim if claim_id == claim.id else None

        def get_entity(self, entity_id: UUID) -> EntityDetail | None:
            if entity_id == entity.id:
                return EntityDetail(**entity.model_dump(), related_documents=[document])
            return None

        def list_entities(self, **kwargs: object) -> EntityPage:
            if kwargs.get("cursor") == "bad-cursor":
                raise CursorError("cursor is invalid")
            return EntityPage(items=[], next_cursor=None)

        def search(self, **kwargs: object) -> SearchPage:
            if kwargs.get("cursor") == "explode":
                raise RuntimeError("unexpected")
            if kwargs.get("cursor") == "db":
                raise psycopg.OperationalError("down")
            return SearchPage(items=[], next_cursor=None)

    app = PublicApiApplication(cast(PublicQueryService, RichService()))
    found = app.handle("GET", f"/v1/documents/{document.id}")
    assert found.status == 200
    missing = app.handle("GET", "/v1/documents/00000000-0000-4000-8000-000000000099")
    assert missing.status == 404
    assert app.handle("GET", f"/v1/claims/{claim.id}").status == 200
    assert app.handle("GET", f"/v1/entities/{entity.id}").status == 200
    assert app.handle("GET", "/v1/entities").status == 200
    assert app.handle("GET", "/v1/documents?limit=1&category=official_report").status == 200
    assert app.handle("GET", "/v1/documents?published_after=2026-08-31T00:00:00Z").status == 200
    bad_id = app.handle("GET", "/v1/documents", {"X-Request-ID": "not-a-uuid"})
    assert bad_id.status == 400
    mixed = app.handle(
        "GET", "/v1/documents", {"X-Request-ID": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".upper()}
    )
    assert mixed.status == 400
    assert app.handle("GET", "http://evil.test/v1/documents").status == 422
    assert app.handle("GET", "/v1/documents?a=1&a=2").status == 422
    assert app.handle("GET", "/v1/documents?limit=").status == 422
    assert app.handle("GET", "/v1/documents?limit=no").status == 422
    assert app.handle("GET", "/v1/documents?limit=0").status == 422
    assert app.handle("GET", "/v1/documents?limit=01").status == 422
    assert app.handle("GET", "/v1/documents?category=nope").status == 422
    assert app.handle("GET", "/v1/documents?published_after=not-a-date").status == 422
    assert app.handle("GET", "/v1/documents?published_after=2026-08-31T00:00:00").status == 422
    assert app.handle("GET", "/v1/documents/not-a-uuid").status == 404
    assert app.handle("GET", "/v1/search").status == 400
    assert app.handle("GET", "/v1/search?q=").status == 422
    assert app.handle("GET", "/v1/entities?cursor=bad-cursor").status == 400
    assert app.handle("GET", "/v1/search?q=ok&cursor=db").status == 503
    assert app.handle("GET", "/v1/search?q=ok&cursor=explode").status == 500
    too_long = "/v1/" + ("a" * 9000)
    assert app.handle("GET", too_long).status == 404


def test_public_pool_and_cursor_remaining_branches() -> None:
    from uap_platform.public_api.cursor import CursorCodec, CursorError, filters_digest

    with pytest.raises(ValueError):
        PublicReaderPool("postgresql://uap_public_reader@db/uap", min_size=0, max_size=1)
    with pytest.raises(ValueError):
        CursorCodec(b"short")
    codec = CursorCodec(b"k" * 32, max_length=256)
    digest = filters_digest({})
    with pytest.raises(CursorError):
        codec.encode(
            resource="documents",
            sort="published_at_desc,id_desc",
            last=["x" * 400],
            filters_sha256=digest,
        )
    with pytest.raises(CursorError):
        codec.decode(
            "",
            resource="documents",
            sort="published_at_desc,id_desc",
            filters_sha256=digest,
        )
    with pytest.raises(CursorError):
        codec.decode(
            "%%%",
            resource="documents",
            sort="published_at_desc,id_desc",
            filters_sha256=digest,
        )

    pool = object.__new__(PublicReaderPool)
    pool._dsn = "postgresql://uap_public_reader@localhost/uap"
    pool._max_size = 1
    pool._created = 1
    pool._lock = Lock()
    pool._closed = False
    pool._connections = LifoQueue()
    fake = ScriptedConnection([])
    pool._connections.put(cast(Connection[dict[str, object]], fake))
    with pool.transaction():
        pass
    assert fake.commits == 1
    assert pool._connections.qsize() == 1

    exploding = ScriptedConnection([])

    class BoomCursor(ScriptedCursor):
        def execute(self, sql: str, params: object = None) -> None:
            del params
            if "REPEATABLE" in sql:
                raise RuntimeError("set failed")
            super().execute(sql, None)

    class BoomConnection(ScriptedConnection):
        def cursor(self) -> BoomCursor:
            return BoomCursor(self)

    exploding = BoomConnection([])
    pool._connections.put(cast(Connection[dict[str, object]], exploding))
    with pytest.raises(RuntimeError, match="set failed"):
        with pool.transaction():
            pass
    assert exploding.closed is True

    pool._closed = True
    with pytest.raises(RuntimeError, match="closed"):
        pool._checkout()
    pool.close()

    class RoleCursor(ScriptedCursor):
        def fetchone(self) -> dict[str, object]:
            return {"session_user": "uap_api"}

    class RoleConnection(ScriptedConnection):
        def cursor(self) -> RoleCursor:
            return RoleCursor(self)

    role = RoleConnection([])
    with patch("uap_platform.public_api.pool.psycopg.connect", return_value=role):
        with pytest.raises(RuntimeError, match="role verification"):
            pool._open_reserved()
    assert role.closed is True

    env = {
        "UAP_PUBLIC_DATABASE_URL": "postgresql://uap_public_reader:secret@db/uap",
        "UAP_PUBLIC_CURSOR_SECRET": "s" * 32,
        "UAP_PUBLIC_POOL_MIN_SIZE": "5",
        "UAP_PUBLIC_POOL_MAX_SIZE": "2",
    }
    with pytest.raises(ValueError, match="pool_min_size"):
        load_public_api_settings(env)
    with pytest.raises(ValueError, match="cursor secret"):
        load_public_api_settings(
            {
                "UAP_PUBLIC_DATABASE_URL": "postgresql://uap_public_reader:secret@db/uap",
                "UAP_PUBLIC_CURSOR_SECRET": "tiny",
            }
        )
    with pytest.raises(ValueError, match="invalid"):
        load_public_api_settings(
            {
                "UAP_PUBLIC_DATABASE_URL": "not-a-dsn",
                "UAP_PUBLIC_CURSOR_SECRET": "s" * 32,
            }
        )
