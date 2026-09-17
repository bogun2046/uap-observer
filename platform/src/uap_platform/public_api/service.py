"""Fixed, parameterized reads over the current public projection only."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from math import isfinite
from typing import Any, cast
from uuid import UUID

from psycopg import Connection

from .contracts import (
    ClaimDetail,
    DocumentDetail,
    DocumentPage,
    DocumentSummary,
    EntityDetail,
    EntityPage,
    EntitySummary,
    LocatorType,
    PublicEvidence,
    PublicSource,
    SearchHit,
    SearchPage,
)
from .cursor import CursorCodec, CursorError, filters_digest
from .pool import PublicReaderPool

DOCUMENT_SORT = "published_at_desc,id_desc"
ENTITY_SORT = "published_at_desc,id_desc"
SEARCH_SORT = "rank_desc,published_at_desc,document_id_desc"


class PublicQueryService:
    def __init__(self, pool: PublicReaderPool, cursor_codec: CursorCodec) -> None:
        self._pool = pool
        self._cursor = cursor_codec

    def list_documents(
        self,
        *,
        limit: int,
        category: str | None,
        fact_status: str | None,
        published_after: datetime | None,
        cursor: str | None,
    ) -> DocumentPage:
        filters = {
            "category": category,
            "fact_status": fact_status,
            "published_after": published_after.isoformat() if published_after else None,
        }
        digest = filters_digest(filters)
        last_published: datetime | None = None
        last_id: UUID | None = None
        if cursor:
            last = self._cursor.decode(
                cursor,
                resource="documents",
                sort=DOCUMENT_SORT,
                filters_sha256=digest,
            )
            if len(last) != 2:
                raise CursorError("cursor is invalid")
            try:
                last_published = datetime.fromisoformat(str(last[0]))
                last_id = UUID(str(last[1]))
            except ValueError as error:
                raise CursorError("cursor is invalid") from error
            if last_published.tzinfo is None or str(last_id) != str(last[1]):
                raise CursorError("cursor is invalid")
        with self._pool.transaction() as connection:
            with connection.cursor() as db_cursor:
                db_cursor.execute(
                    """
                    SELECT document.id, document.slug, document.title, document.summary,
                           document.category, document.fact_status, document.source_name,
                           document.canonical_source_url, document.source_published_at,
                           document.published_at, document.revised_at, document.revision_no
                      FROM public.documents AS document
                     WHERE (%s::public.document_category IS NULL OR
                            document.category = %s::public.document_category)
                       AND (%s::public.fact_status IS NULL OR
                            document.fact_status = %s::public.fact_status)
                       AND (%s::timestamptz IS NULL OR document.published_at > %s::timestamptz)
                       AND (%s::timestamptz IS NULL OR
                            (document.published_at, document.id) < (%s::timestamptz, %s::uuid))
                     ORDER BY document.published_at DESC, document.id DESC
                     LIMIT %s
                    """,
                    (
                        category,
                        category,
                        fact_status,
                        fact_status,
                        published_after,
                        published_after,
                        last_published,
                        last_published,
                        last_id,
                        limit + 1,
                    ),
                )
                rows = db_cursor.fetchall()
            items = [self._document(row) for row in rows[:limit]]
            next_cursor = None
            if len(rows) > limit and items:
                tail = rows[limit - 1]
                next_cursor = self._cursor.encode(
                    resource="documents",
                    sort=DOCUMENT_SORT,
                    last=[cast(datetime, tail["published_at"]).isoformat(), str(tail["id"])],
                    filters_sha256=digest,
                )
            return DocumentPage(items=items, next_cursor=next_cursor)

    def get_document(self, document_id: UUID) -> DocumentDetail | None:
        with self._pool.transaction() as connection:
            row = self._fetch_document(connection, document_id)
            if row is None:
                return None
            return DocumentDetail(
                **self._document(row).model_dump(),
                claims=self._document_claims(connection, document_id),
                related_entities=self._document_entities(connection, document_id),
            )

    def get_claim(self, claim_id: UUID) -> ClaimDetail | None:
        with self._pool.transaction() as connection:
            with connection.cursor() as db_cursor:
                db_cursor.execute(
                    """
                    SELECT claim.id, claim.document_id, claim.ordinal, claim.claim_text,
                           claim.claim_type, claim.assertion_status, claim.attribution,
                           claim.revision_no
                      FROM public.claims AS claim
                     WHERE claim.id = %s
                    """,
                    (claim_id,),
                )
                row = db_cursor.fetchone()
            return None if row is None else self._claim(connection, row)

    def list_entities(
        self, *, limit: int, entity_type: str | None, cursor: str | None
    ) -> EntityPage:
        filters = {"type": entity_type}
        digest = filters_digest(filters)
        last_published: datetime | None = None
        last_id: UUID | None = None
        if cursor:
            last = self._cursor.decode(
                cursor,
                resource="entities",
                sort=ENTITY_SORT,
                filters_sha256=digest,
            )
            if len(last) != 2:
                raise CursorError("cursor is invalid")
            try:
                last_published = datetime.fromisoformat(str(last[0]))
                last_id = UUID(str(last[1]))
            except ValueError as error:
                raise CursorError("cursor is invalid") from error
            if last_published.tzinfo is None or str(last_id) != str(last[1]):
                raise CursorError("cursor is invalid")
        with self._pool.transaction() as connection:
            with connection.cursor() as db_cursor:
                db_cursor.execute(
                    """
                    SELECT entity.id, entity.slug, entity.entity_type, entity.name,
                           entity.description, entity.country_code, entity.revision_no,
                           entity.published_at
                      FROM public.entities AS entity
                     WHERE (%s::text IS NULL OR entity.entity_type = %s::text)
                       AND (%s::timestamptz IS NULL OR
                            (entity.published_at, entity.id) < (%s::timestamptz, %s::uuid))
                     ORDER BY entity.published_at DESC, entity.id DESC
                     LIMIT %s
                    """,
                    (
                        entity_type,
                        entity_type,
                        last_published,
                        last_published,
                        last_id,
                        limit + 1,
                    ),
                )
                rows = db_cursor.fetchall()
            items = [self._entity(row) for row in rows[:limit]]
            next_cursor = None
            if len(rows) > limit and items:
                tail = rows[limit - 1]
                next_cursor = self._cursor.encode(
                    resource="entities",
                    sort=ENTITY_SORT,
                    last=[cast(datetime, tail["published_at"]).isoformat(), str(tail["id"])],
                    filters_sha256=digest,
                )
            return EntityPage(items=items, next_cursor=next_cursor)

    def get_entity(self, entity_id: UUID) -> EntityDetail | None:
        with self._pool.transaction() as connection:
            with connection.cursor() as db_cursor:
                db_cursor.execute(
                    """
                    SELECT entity.id, entity.slug, entity.entity_type, entity.name,
                           entity.description, entity.country_code, entity.revision_no
                      FROM public.entities AS entity
                     WHERE entity.id = %s
                    """,
                    (entity_id,),
                )
                row = db_cursor.fetchone()
            if row is None:
                return None
            with connection.cursor() as db_cursor:
                db_cursor.execute(
                    """
                    SELECT document.id, document.slug, document.title, document.summary,
                           document.category, document.fact_status, document.source_name,
                           document.canonical_source_url, document.source_published_at,
                           document.published_at, document.revised_at, document.revision_no
                      FROM public.document_entities AS link
                      JOIN public.documents AS document ON document.id = link.document_id
                     WHERE link.entity_id = %s
                     ORDER BY document.published_at DESC, document.id DESC
                    """,
                    (entity_id,),
                )
                documents = [self._document(item) for item in db_cursor.fetchall()]
            return EntityDetail(**self._entity(row).model_dump(), related_documents=documents)

    def search(
        self,
        *,
        query: str,
        limit: int,
        category: str | None,
        fact_status: str | None,
        cursor: str | None,
    ) -> SearchPage:
        filters = {"q": query, "category": category, "fact_status": fact_status}
        digest = filters_digest(filters)
        last_rank: float | None = None
        last_published: datetime | None = None
        last_id: UUID | None = None
        if cursor:
            last = self._cursor.decode(
                cursor,
                resource="search",
                sort=SEARCH_SORT,
                filters_sha256=digest,
            )
            if len(last) != 3:
                raise CursorError("cursor is invalid")
            try:
                last_rank = float(last[0])
                last_published = datetime.fromisoformat(str(last[1]))
                last_id = UUID(str(last[2]))
            except (TypeError, ValueError) as error:
                raise CursorError("cursor is invalid") from error
            if (
                not isfinite(last_rank)
                or last_published.tzinfo is None
                or str(last_id) != str(last[2])
            ):
                raise CursorError("cursor is invalid")
        with self._pool.transaction() as connection:
            with connection.cursor() as db_cursor:
                db_cursor.execute(
                    """
                    WITH query_input AS (
                        SELECT websearch_to_tsquery('simple', %s) AS query
                    ), ranked AS (
                        SELECT document.id, document.slug, document.title, document.summary,
                               document.category, document.fact_status, document.source_name,
                               document.canonical_source_url, document.source_published_at,
                               document.published_at, document.revised_at, document.revision_no,
                               search.display_text,
                               ts_rank_cd(search.search_vector, query_input.query)::real AS rank,
                               ts_headline(
                                   'simple', search.display_text, query_input.query,
                                   'StartSel=,StopSel=,MaxFragments=2,MaxWords=20,MinWords=5'
                               ) AS headline
                          FROM public.search_documents AS search
                          JOIN public.documents AS document ON document.id = search.document_id
                          CROSS JOIN query_input
                         WHERE search.search_vector @@ query_input.query
                           AND (%s::public.document_category IS NULL OR
                                document.category = %s::public.document_category)
                           AND (%s::public.fact_status IS NULL OR
                                document.fact_status = %s::public.fact_status)
                    )
                    SELECT * FROM ranked
                     WHERE (%s::real IS NULL OR
                            (rank, published_at, id) < (%s::real, %s::timestamptz, %s::uuid))
                     ORDER BY rank DESC, published_at DESC, id DESC
                     LIMIT %s
                    """,
                    (
                        query,
                        category,
                        category,
                        fact_status,
                        fact_status,
                        last_rank,
                        last_rank,
                        last_published,
                        last_id,
                        limit + 1,
                    ),
                )
                rows = db_cursor.fetchall()
            items = [
                SearchHit(document=self._document(row), highlights=[str(row["headline"])])
                for row in rows[:limit]
            ]
            next_cursor = None
            if len(rows) > limit and items:
                tail = rows[limit - 1]
                next_cursor = self._cursor.encode(
                    resource="search",
                    sort=SEARCH_SORT,
                    last=[
                        float(cast(float, tail["rank"])),
                        cast(datetime, tail["published_at"]).isoformat(),
                        str(tail["id"]),
                    ],
                    filters_sha256=digest,
                )
            return SearchPage(items=items, next_cursor=next_cursor)

    @staticmethod
    def _document(row: Mapping[str, Any]) -> DocumentSummary:
        return DocumentSummary(
            id=row["id"],
            slug=row["slug"],
            title=row["title"],
            summary=row["summary"],
            category=row["category"],
            fact_status=row["fact_status"],
            source=PublicSource(name=row["source_name"], url=row["canonical_source_url"]),
            source_published_at=row["source_published_at"],
            published_at=row["published_at"],
            revised_at=row["revised_at"],
            revision=row["revision_no"],
        )

    @staticmethod
    def _entity(row: Mapping[str, Any]) -> EntitySummary:
        return EntitySummary(
            id=row["id"],
            slug=row["slug"],
            type=row["entity_type"],
            name=row["name"],
            description=row["description"],
            country_code=row["country_code"],
            revision=row["revision_no"],
        )

    def _fetch_document(
        self, connection: Connection[dict[str, object]], document_id: UUID
    ) -> Mapping[str, Any] | None:
        with connection.cursor() as db_cursor:
            db_cursor.execute(
                """
                SELECT document.id, document.slug, document.title, document.summary,
                       document.category, document.fact_status, document.source_name,
                       document.canonical_source_url, document.source_published_at,
                       document.published_at, document.revised_at, document.revision_no
                  FROM public.documents AS document
                 WHERE document.id = %s
                """,
                (document_id,),
            )
            return db_cursor.fetchone()

    def _document_claims(
        self, connection: Connection[dict[str, object]], document_id: UUID
    ) -> list[ClaimDetail]:
        with connection.cursor() as db_cursor:
            db_cursor.execute(
                """
                SELECT claim.id, claim.document_id, claim.ordinal, claim.claim_text,
                       claim.claim_type, claim.assertion_status, claim.attribution,
                       claim.revision_no
                  FROM public.claims AS claim
                 WHERE claim.document_id = %s
                 ORDER BY claim.ordinal ASC, claim.id ASC
                """,
                (document_id,),
            )
            rows = db_cursor.fetchall()
        return [self._claim(connection, row) for row in rows]

    def _claim(
        self, connection: Connection[dict[str, object]], row: Mapping[str, Any]
    ) -> ClaimDetail:
        with connection.cursor() as db_cursor:
            db_cursor.execute(
                """
                SELECT evidence.id, evidence.excerpt, evidence.locator_type,
                       evidence.page_start, evidence.page_end, evidence.time_start_ms,
                       evidence.time_end_ms, evidence.public_locator, evidence.source_url
                  FROM public.claim_evidence AS claim_evidence
                  JOIN public.evidence AS evidence ON evidence.id = claim_evidence.evidence_id
                 WHERE claim_evidence.claim_id = %s
                 ORDER BY evidence.id ASC
                """,
                (row["id"],),
            )
            evidence_rows = db_cursor.fetchall()
        evidence = [
            PublicEvidence(
                id=cast(UUID, item["id"]),
                excerpt=str(item["excerpt"]),
                locator_type=cast(LocatorType, item["locator_type"]),
                page_start=cast(int | None, item["page_start"]),
                page_end=cast(int | None, item["page_end"]),
                time_start_ms=cast(int | None, item["time_start_ms"]),
                time_end_ms=cast(int | None, item["time_end_ms"]),
                locator=cast(dict[str, object], item["public_locator"]),
                source_url=str(item["source_url"]),
            )
            for item in evidence_rows
        ]
        return ClaimDetail(
            id=row["id"],
            document_id=row["document_id"],
            ordinal=row["ordinal"],
            text=row["claim_text"],
            type=row["claim_type"],
            assertion_status=row["assertion_status"],
            attribution=row["attribution"],
            revision=row["revision_no"],
            evidence=evidence,
        )

    def _document_entities(
        self, connection: Connection[dict[str, object]], document_id: UUID
    ) -> list[EntitySummary]:
        with connection.cursor() as db_cursor:
            db_cursor.execute(
                """
                SELECT entity.id, entity.slug, entity.entity_type, entity.name,
                       entity.description, entity.country_code, entity.revision_no
                  FROM public.document_entities AS link
                  JOIN public.entities AS entity ON entity.id = link.entity_id
                 WHERE link.document_id = %s
                 ORDER BY entity.name COLLATE "C" ASC, entity.id ASC
                """,
                (document_id,),
            )
            return [self._entity(row) for row in db_cursor.fetchall()]
