"""Small persistence layer for the initial domain models."""

from __future__ import annotations

import json

from uap_observer.database import Database
from uap_observer.models import (
    AnalysisRiskFlag,
    AnalysisTask,
    ArticleTask,
    EntityType,
    Event,
    FactStatus,
    News,
    NewsCategory,
    Organization,
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

    def get_article_tasks(
        self,
        *,
        limit: int,
        retry_failed: bool = False,
        retry_blocked: bool = False,
    ) -> list[ArticleTask]:
        statuses = ("pending", "failed") if retry_failed else ("pending",)
        placeholders = ", ".join("?" for _ in statuses)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, COALESCE(canonical_url, source_url) AS article_url,
                       original_title, extraction_attempts, source, summary, raw_content
                FROM news
                WHERE extraction_status IN ({placeholders})
                  AND (
                    ? = 1
                    OR extraction_status <> 'failed'
                    OR extraction_error IS NULL
                    OR extraction_error NOT LIKE '%403%'
                    OR raw_content IS NOT NULL
                  )
                  AND COALESCE(canonical_url, source_url) IS NOT NULL
                ORDER BY publish_date ASC, id ASC
                LIMIT ?
                """,
                (*statuses, int(retry_blocked), limit),
            ).fetchall()
        return [
            ArticleTask(
                news_id=row["id"],
                url=row["article_url"],
                original_title=row["original_title"],
                extraction_attempts=row["extraction_attempts"],
                source=row["source"],
                fallback_content=row["raw_content"] or row["summary"],
            )
            for row in rows
        ]

    def reset_stale_article_tasks(self, *, stale_after_minutes: int = 60) -> int:
        if stale_after_minutes < 1:
            raise ValueError("stale_after_minutes must be at least 1")
        modifier = f"-{stale_after_minutes} minutes"
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE news
                SET extraction_status = 'pending',
                    extraction_started_at = NULL,
                    extraction_error = 'Recovered stale processing task',
                    updated_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE extraction_status = 'processing'
                  AND extraction_started_at < strftime(
                      '%Y-%m-%dT%H:%M:%fZ', 'now', ?
                  )
                """,
                (modifier,),
            )
        return cursor.rowcount

    def claim_article_task(self, news_id: int, *, retry_failed: bool = False) -> bool:
        statuses = ("pending", "failed") if retry_failed else ("pending",)
        placeholders = ", ".join("?" for _ in statuses)
        with self.database.connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE news
                SET extraction_status = 'processing',
                    extraction_attempts = extraction_attempts + 1,
                    extraction_started_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    extraction_error = NULL,
                    updated_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ? AND extraction_status IN ({placeholders})
                """,
                (news_id, *statuses),
            )
        return cursor.rowcount == 1

    def find_news_by_content_hash(
        self,
        content_hash: str,
        *,
        exclude_news_id: int,
    ) -> int | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM news
                WHERE content_hash = ? AND id <> ?
                LIMIT 1
                """,
                (content_hash, exclude_news_id),
            ).fetchone()
        return int(row["id"]) if row else None

    def complete_article_extraction(
        self,
        news_id: int,
        *,
        content: str,
        content_hash: str,
        title: str | None,
        author: str | None,
        publish_date: str | None,
        language: str | None,
        extracted_by: str,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE news
                SET extraction_status = 'completed',
                    extracted_content = ?,
                    content_hash = ?,
                    extracted_title = ?,
                    extracted_author = ?,
                    extracted_publish_date = ?,
                    extracted_language = ?,
                    extracted_by = ?,
                    content_extracted_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    extraction_error = NULL,
                    updated_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ? AND extraction_status = 'processing'
                """,
                (
                    content,
                    content_hash,
                    title,
                    author,
                    publish_date,
                    language,
                    extracted_by,
                    news_id,
                ),
            )

    def fail_article_extraction(self, news_id: int, error: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE news
                SET extraction_status = 'failed',
                    extraction_error = ?,
                    updated_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ? AND extraction_status = 'processing'
                """,
                (error[:1000], news_id),
            )

    def skip_duplicate_article(
        self,
        news_id: int,
        *,
        duplicate_of_news_id: int,
        content_hash: str,
        extracted_by: str,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE news
                SET extraction_status = 'skipped',
                    extracted_by = ?,
                    extraction_error = ?,
                    content_extracted_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    updated_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ? AND extraction_status = 'processing'
                """,
                (
                    extracted_by,
                    f"duplicate content of news_id={duplicate_of_news_id}; hash={content_hash}",
                    news_id,
                ),
            )

    def get_analysis_tasks(
        self,
        *,
        limit: int,
        retry_failed: bool = False,
    ) -> list[AnalysisTask]:
        statuses = ("pending", "failed") if retry_failed else ("pending",)
        placeholders = ", ".join("?" for _ in statuses)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, original_title, source, source_url, publish_date,
                       extracted_content, analysis_attempts
                FROM news
                WHERE processing_status IN ({placeholders})
                  AND extraction_status = 'completed'
                  AND extracted_content IS NOT NULL
                  AND length(trim(extracted_content)) > 0
                ORDER BY publish_date ASC, id ASC
                LIMIT ?
                """,
                (*statuses, limit),
            ).fetchall()
        return [
            AnalysisTask(
                news_id=row["id"],
                original_title=row["original_title"],
                source=row["source"],
                source_url=row["source_url"],
                publish_date=row["publish_date"],
                extracted_content=row["extracted_content"],
                analysis_attempts=row["analysis_attempts"],
            )
            for row in rows
        ]

    def reset_stale_analysis_tasks(self, *, stale_after_minutes: int = 60) -> int:
        if stale_after_minutes < 1:
            raise ValueError("stale_after_minutes must be at least 1")
        modifier = f"-{stale_after_minutes} minutes"
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE news
                SET processing_status = 'pending',
                    analysis_started_at = NULL,
                    analysis_error = 'Recovered stale processing task',
                    updated_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE processing_status = 'processing'
                  AND analysis_started_at < strftime(
                      '%Y-%m-%dT%H:%M:%fZ', 'now', ?
                  )
                """,
                (modifier,),
            )
        return cursor.rowcount

    def claim_analysis_task(self, news_id: int, *, retry_failed: bool = False) -> bool:
        statuses = ("pending", "failed") if retry_failed else ("pending",)
        placeholders = ", ".join("?" for _ in statuses)
        with self.database.connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE news
                SET processing_status = 'processing',
                    analysis_attempts = analysis_attempts + 1,
                    analysis_started_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    analysis_error = NULL,
                    updated_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                  AND processing_status IN ({placeholders})
                  AND extraction_status = 'completed'
                """,
                (news_id, *statuses),
            )
        return cursor.rowcount == 1

    def complete_analysis(
        self,
        news_id: int,
        *,
        title: str,
        summary: str,
        category: NewsCategory,
        fact_status: FactStatus,
        key_facts: list[str],
        viewpoints: list[str],
        model: str,
        response_id: str | None,
        analysis_version: str,
        confidence: float,
        risk_flags: list[AnalysisRiskFlag],
        analysis_json: str,
    ) -> None:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE news
                SET title = ?,
                    summary = ?,
                    category = ?,
                    fact_status = ?,
                    key_facts = ?,
                    viewpoints = ?,
                    ai_model = ?,
                    ai_processed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    processing_status = 'completed',
                    analysis_version = ?,
                    analysis_error = NULL,
                    analysis_response_id = ?,
                    analysis_confidence = ?,
                    risk_flags = ?,
                    analysis_json = ?,
                    updated_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ? AND processing_status = 'processing'
                """,
                (
                    title,
                    summary,
                    category.value,
                    fact_status.value,
                    json.dumps(key_facts, ensure_ascii=False),
                    json.dumps(viewpoints, ensure_ascii=False),
                    model,
                    analysis_version,
                    response_id,
                    confidence,
                    json.dumps([flag.value for flag in risk_flags], ensure_ascii=False),
                    analysis_json,
                    news_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"Analysis task is not claimed: news_id={news_id}")

    def fail_analysis(self, news_id: int, error: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE news
                SET processing_status = 'failed',
                    analysis_error = ?,
                    updated_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ? AND processing_status = 'processing'
                """,
                (error[:1000], news_id),
            )

    def get_published_news(self, *, limit: int = 1000) -> list[dict[str, object]]:
        """Return source-filtered news safe for public metadata publishing.

        AI-completed rows receive summaries and entity links; queued rows remain
        visible with an explicit pending-analysis state instead of disappearing.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, title, original_title, source, source_url, publish_date,
                       country, category, summary, credibility, fact_status,
                       key_facts, viewpoints, analysis_confidence, risk_flags,
                       ai_model, ai_processed_at, processing_status,
                       extraction_status, extraction_error
                FROM news
                WHERE source_url IS NOT NULL
                ORDER BY COALESCE(publish_date, '') DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_untranslated_titles(self, *, limit: int = 100) -> list[dict[str, object]]:
        """Return English or mixed-language titles that still equal the source title."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, title, original_title, source
                FROM news
                WHERE title GLOB '*[A-Za-z]*'
                  AND title NOT GLOB '*[一-龥]*'
                ORDER BY id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_translated_title(self, news_id: int, title: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE news
                SET title = ?, updated_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,
                (title, news_id),
            )

    def get_pipeline_counts(self) -> dict[str, int]:
        """Return queue counts used by local and scheduled-run diagnostics."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT processing_status, COUNT(*) AS count FROM news GROUP BY processing_status"
            ).fetchall()
        return {str(row["processing_status"]): int(row["count"]) for row in rows}

    def get_events_for_timeline(self, *, limit: int = 500) -> list[dict[str, object]]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, event_name, date_start, date_end, location, country,
                       description, status, credibility
                FROM events
                WHERE date_start IS NOT NULL
                ORDER BY date_start ASC, id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_persons(self, *, limit: int = 1000) -> list[dict[str, object]]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id, name, country, organization, description FROM persons ORDER BY lower(name) LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_organizations(self, *, limit: int = 1000) -> list[dict[str, object]]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id, name, country, description FROM organizations ORDER BY lower(name) LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_news_entities(self, news_id: int) -> list[dict[str, object]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT r.target_type AS entity_type, r.relationship_type,
                       r.confidence, p.name AS entity_name, e.event_name,
                       o.name AS organization_name
                FROM relationships AS r
                LEFT JOIN persons AS p
                  ON r.target_type = 'person' AND r.target_id = p.id
                LEFT JOIN events AS e
                  ON r.target_type = 'event' AND r.target_id = e.id
                LEFT JOIN organizations AS o
                  ON r.target_type = 'organization' AND r.target_id = o.id
                WHERE r.source_type = 'news' AND r.source_id = ?
                ORDER BY r.target_type, COALESCE(p.name, e.event_name)
                """,
                (news_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_relationships(self, *, limit: int = 2000) -> list[dict[str, object]]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT r.id, r.source_id AS news_id, n.title AS news_title,
                       r.target_type AS entity_type, r.relationship_type,
                       r.confidence, p.name AS person_name, e.event_name,
                       o.name AS organization_name
                FROM relationships AS r
                JOIN news AS n ON r.source_type = 'news' AND r.source_id = n.id
                LEFT JOIN persons AS p
                  ON r.target_type = 'person' AND r.target_id = p.id
                LEFT JOIN events AS e
                  ON r.target_type = 'event' AND r.target_id = e.id
                LEFT JOIN organizations AS o
                  ON r.target_type = 'organization' AND r.target_id = o.id
                ORDER BY r.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_completed_analysis_records(self, *, limit: int = 1000) -> list[dict[str, object]]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, analysis_json, analysis_confidence, credibility
                FROM news
                WHERE processing_status = 'completed'
                  AND analysis_json IS NOT NULL
                ORDER BY id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_person_id(self, *, name: str) -> int | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id FROM persons WHERE lower(name) = lower(?) LIMIT 1",
                (name,),
            ).fetchone()
        return int(row["id"]) if row else None

    def get_organization_id(self, *, name: str) -> int | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id FROM organizations WHERE lower(name) = lower(?) LIMIT 1",
                (name,),
            ).fetchone()
        return int(row["id"]) if row else None

    def get_event_id(self, *, event_name: str) -> int | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id FROM events WHERE lower(event_name) = lower(?) LIMIT 1",
                (event_name,),
            ).fetchone()
        return int(row["id"]) if row else None

    def relationship_exists(
        self,
        *,
        source_type: EntityType,
        source_id: int,
        target_type: EntityType,
        target_id: int,
        relationship_type: str,
    ) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM relationships
                WHERE source_type = ? AND source_id = ?
                  AND target_type = ? AND target_id = ?
                  AND relationship_type = ?
                LIMIT 1
                """,
                (
                    source_type.value,
                    source_id,
                    target_type.value,
                    target_id,
                    relationship_type,
                ),
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

    def event_exists(self, *, event_name: str, date_start: str | None) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM events
                WHERE event_name = ? AND (date_start = ? OR (date_start IS NULL AND ? IS NULL))
                LIMIT 1
                """,
                (event_name, date_start, date_start),
            ).fetchone()
        return row is not None

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

    def add_organization(self, item: Organization) -> int:
        with self.database.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO organizations (name, country, description) VALUES (?, ?, ?)",
                (item.name, item.country, item.description),
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
