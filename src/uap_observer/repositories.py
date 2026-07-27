"""Small persistence layer for the initial domain models."""

from __future__ import annotations

import json

from uap_observer.database import Database
from uap_observer.models import (
    Event,
    FactStatus,
    News,
    NewsCategory,
    Person,
    Relationship,
    Source,
    SourceType,
)


class Repository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def add_news(self, item: News) -> int:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO news (
                    title, original_title, source, source_url, canonical_url,
                    publish_date, country, category, summary, credibility,
                    fact_status, key_facts, viewpoints, raw_content, content_hash,
                    ai_model, ai_processed_at, processing_status, source_id,
                    feed_entry_id
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    item.title,
                    item.original_title,
                    item.source,
                    item.source_url,
                    item.canonical_url,
                    item.publish_date,
                    item.country,
                    item.category.value,
                    item.summary,
                    item.credibility,
                    item.fact_status.value,
                    json.dumps(item.key_facts, ensure_ascii=False),
                    json.dumps(item.viewpoints, ensure_ascii=False),
                    item.raw_content,
                    item.content_hash,
                    item.ai_model,
                    item.ai_processed_at,
                    item.processing_status.value,
                    item.source_id,
                    item.feed_entry_id,
                ),
            )
            return int(cursor.lastrowid)

    def news_exists(
        self,
        *,
        canonical_url: str | None,
        source_id: int | None,
        feed_entry_id: str | None,
    ) -> bool:
        clauses: list[str] = []
        values: list[object] = []
        if canonical_url:
            clauses.append("canonical_url = ?")
            values.append(canonical_url)
        if source_id is not None and feed_entry_id:
            clauses.append("(source_id = ? AND feed_entry_id = ?)")
            values.extend((source_id, feed_entry_id))
        if not clauses:
            return False
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT 1 FROM news WHERE {' OR '.join(clauses)} LIMIT 1",
                values,
            ).fetchone()
        return row is not None

    def upsert_source(self, source: Source) -> int:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO sources (
                    slug, name, source_type, homepage_url, feed_url, country,
                    language, default_category, default_credibility,
                    default_fact_status, include_keywords, exclude_keywords, enabled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    name = excluded.name,
                    source_type = excluded.source_type,
                    homepage_url = excluded.homepage_url,
                    feed_url = excluded.feed_url,
                    country = excluded.country,
                    language = excluded.language,
                    default_category = excluded.default_category,
                    default_credibility = excluded.default_credibility,
                    default_fact_status = excluded.default_fact_status,
                    include_keywords = excluded.include_keywords,
                    exclude_keywords = excluded.exclude_keywords,
                    enabled = excluded.enabled,
                    updated_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (
                    source.slug,
                    source.name,
                    source.source_type.value,
                    source.homepage_url,
                    source.feed_url,
                    source.country,
                    source.language,
                    source.default_category.value,
                    source.default_credibility,
                    source.default_fact_status.value,
                    json.dumps(source.include_keywords, ensure_ascii=False),
                    json.dumps(source.exclude_keywords, ensure_ascii=False),
                    int(source.enabled),
                ),
            )
            row = connection.execute(
                "SELECT id FROM sources WHERE slug = ?",
                (source.slug,),
            ).fetchone()
        return int(row["id"])

    def get_sources(
        self,
        *,
        source_type: SourceType | None = None,
        enabled_only: bool = True,
        slug: str | None = None,
    ) -> list[Source]:
        clauses: list[str] = []
        values: list[object] = []
        if source_type is not None:
            clauses.append("source_type = ?")
            values.append(source_type.value)
        if enabled_only:
            clauses.append("enabled = 1")
        if slug:
            clauses.append("slug = ?")
            values.append(slug)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM sources {where} ORDER BY slug",
                values,
            ).fetchall()
        return [
            Source(
                id=row["id"],
                slug=row["slug"],
                name=row["name"],
                source_type=SourceType(row["source_type"]),
                homepage_url=row["homepage_url"],
                feed_url=row["feed_url"],
                country=row["country"],
                language=row["language"],
                default_category=NewsCategory(row["default_category"]),
                default_credibility=row["default_credibility"],
                default_fact_status=FactStatus(row["default_fact_status"]),
                include_keywords=json.loads(row["include_keywords"]),
                exclude_keywords=json.loads(row["exclude_keywords"]),
                enabled=bool(row["enabled"]),
                etag=row["etag"],
                last_modified=row["last_modified"],
                last_fetched_at=row["last_fetched_at"],
                last_success_at=row["last_success_at"],
                last_error=row["last_error"],
            )
            for row in rows
        ]

    def record_source_fetch(
        self,
        source_id: int,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        error: str | None = None,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE sources
                SET etag = COALESCE(?, etag),
                    last_modified = COALESCE(?, last_modified),
                    last_fetched_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    last_success_at = CASE
                        WHEN ? IS NULL THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        ELSE last_success_at
                    END,
                    last_error = ?,
                    updated_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,
                (etag, last_modified, error, error, source_id),
            )

    def add_event(self, item: Event) -> int:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO events (
                    event_name, date_start, date_end, location, country,
                    description, status, credibility
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.event_name,
                    item.date_start,
                    item.date_end,
                    item.location,
                    item.country,
                    item.description,
                    item.status.value,
                    item.credibility,
                ),
            )
            return int(cursor.lastrowid)

    def add_person(self, item: Person) -> int:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO persons (name, country, organization, description)
                VALUES (?, ?, ?, ?)
                """,
                (item.name, item.country, item.organization, item.description),
            )
            return int(cursor.lastrowid)

    def add_relationship(self, item: Relationship) -> int:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO relationships (
                    source_type, source_id, target_type, target_id,
                    relationship_type, evidence_news_id, confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.source_type.value,
                    item.source_id,
                    item.target_type.value,
                    item.target_id,
                    item.relationship_type,
                    item.evidence_news_id,
                    item.confidence,
                ),
            )
            return int(cursor.lastrowid)
