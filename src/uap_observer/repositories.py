"""Small persistence layer for the initial domain models."""

from __future__ import annotations

import hashlib
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
    PersonRelationship,
    Relationship,
    Source,
    SourceType,
    Tag,
    TagAssignment,
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

    def get_news_id(self, *, source_id: int, feed_entry_id: str) -> int | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id FROM news WHERE source_id = ? AND feed_entry_id = ? LIMIT 1",
                (source_id, feed_entry_id),
            ).fetchone()
        return int(row["id"]) if row else None

    def get_article_tasks(
        self,
        *,
        limit: int,
        retry_failed: bool = False,
        retry_blocked: bool = False,
        max_failed_attempts: int | None = 3,
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
                  AND (
                    extraction_status <> 'failed'
                    OR ? IS NULL
                    OR extraction_attempts < ?
                  )
                  AND COALESCE(canonical_url, source_url) IS NOT NULL
                ORDER BY CASE extraction_status
                             WHEN 'pending' THEN 0
                             ELSE 1
                         END ASC,
                         publish_date ASC,
                         id ASC
                LIMIT ?
                """,
                (
                    *statuses,
                    int(retry_blocked),
                    max_failed_attempts,
                    max_failed_attempts,
                    limit,
                ),
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

    def claim_article_task(
        self,
        news_id: int,
        *,
        retry_failed: bool = False,
        max_failed_attempts: int | None = 3,
    ) -> bool:
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
                  AND (
                    extraction_status <> 'failed'
                    OR ? IS NULL
                    OR extraction_attempts < ?
                  )
                """,
                (
                    news_id,
                    *statuses,
                    max_failed_attempts,
                    max_failed_attempts,
                ),
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
    ) -> int | None:
        """Complete an extraction, or atomically skip an existing content hash.

        Returns the existing news ID when the claimed task is skipped as a
        duplicate. A ``None`` return value means the task completed normally.
        """

        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE OR IGNORE news
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
            if cursor.rowcount == 1:
                return None

            duplicate = connection.execute(
                """
                SELECT id FROM news
                WHERE content_hash = ? AND id <> ?
                LIMIT 1
                """,
                (content_hash, news_id),
            ).fetchone()
            if duplicate is None:
                raise RuntimeError(
                    f"Article extraction task news_id={news_id} is no longer processing"
                )

            duplicate_id = int(duplicate["id"])
            skipped = connection.execute(
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
                    f"duplicate content of news_id={duplicate_id}; hash={content_hash}",
                    news_id,
                ),
            )
            if skipped.rowcount != 1:
                raise RuntimeError(
                    f"Article extraction task news_id={news_id} is no longer processing"
                )
            return duplicate_id

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
                       extraction_status, extraction_error, extracted_by
                FROM news
                WHERE source_url IS NOT NULL
                ORDER BY COALESCE(publish_date, '') DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_untranslated_titles(
        self,
        *,
        limit: int = 100,
        retry_failed: bool = False,
    ) -> list[dict[str, object]]:
        """Return English or mixed-language titles that still equal the source title."""
        statuses = ("not_started", "failed") if retry_failed else ("not_started",)
        placeholders = ", ".join("?" for _ in statuses)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, title, original_title, source
                FROM news
                WHERE title GLOB '*[A-Za-z]*'
                  AND title NOT GLOB '*[一-龥]*'
                  AND title_translation_status IN ({placeholders})
                -- Title translation is deliberately newest-first.  Article analysis
                -- is constrained by extraction throughput, whereas a short title
                -- must be translated promptly so newly published entries do not
                -- appear in English while waiting for body extraction.
                ORDER BY
                    CASE WHEN source = 'YouTube UAP Channel Watchlist' THEN 0 ELSE 1 END,
                    COALESCE(publish_date, '') DESC,
                    id DESC
                LIMIT ?
                """,
                (*statuses, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_title_translation(self, news_id: int, *, model: str, retry_failed: bool) -> bool:
        statuses = ("not_started", "failed") if retry_failed else ("not_started",)
        placeholders = ", ".join("?" for _ in statuses)
        with self.database.connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE news
                SET title_translation_status = 'processing',
                    title_translation_attempts = title_translation_attempts + 1,
                    title_translation_error = NULL,
                    title_translation_model = ?,
                    title_translation_response_id = NULL,
                    title_translation_last_attempt_at = strftime(
                        '%Y-%m-%dT%H:%M:%fZ', 'now'
                    ),
                    updated_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                  AND title_translation_status IN ({placeholders})
                """,
                (model, news_id, *statuses),
            )
        return cursor.rowcount == 1

    def complete_title_translation(
        self,
        news_id: int,
        *,
        title: str,
        model: str,
        response_id: str | None,
    ) -> None:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE news
                SET title = ?,
                    title_translation_status = 'completed',
                    title_translation_error = NULL,
                    title_translation_model = ?,
                    title_translation_response_id = ?,
                    title_translation_last_attempt_at = strftime(
                        '%Y-%m-%dT%H:%M:%fZ', 'now'
                    ),
                    updated_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ? AND title_translation_status = 'processing'
                """,
                (title, model, response_id, news_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"Title translation task is not claimed: news_id={news_id}")

    def fail_title_translation(
        self,
        news_id: int,
        *,
        error: str,
        model: str,
        response_id: str | None,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE news
                SET title_translation_status = 'failed',
                    title_translation_error = ?,
                    title_translation_model = ?,
                    title_translation_response_id = ?,
                    title_translation_last_attempt_at = strftime(
                        '%Y-%m-%dT%H:%M:%fZ', 'now'
                    ),
                    updated_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ? AND title_translation_status = 'processing'
                """,
                (error[:1000], model, response_id, news_id),
            )

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

    def get_events(self, *, limit: int = 500) -> list[dict[str, object]]:
        """Return all public event records, including records without a known date."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, event_name, date_start, date_end, location, country,
                       description, status, credibility
                FROM events
                ORDER BY CASE WHEN date_start IS NULL OR date_start = '' THEN 1 ELSE 0 END,
                         date_start ASC, id ASC
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

    def get_tags(self, *, limit: int = 1000) -> list[dict[str, object]]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, name, slug, tag_type, description, parent_id
                FROM tags
                ORDER BY tag_type, lower(name), id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_tag_assignments(self, *, limit: int = 5000) -> list[dict[str, object]]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT a.id, a.tag_id, t.name AS tag_name, t.slug AS tag_slug,
                       t.tag_type, a.entity_type, a.entity_id,
                       a.source_news_id, a.confidence, a.method, a.status
                FROM tag_assignments AS a
                JOIN tags AS t ON t.id = a.tag_id
                ORDER BY a.id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_person_relationships(self, *, limit: int = 5000) -> list[dict[str, object]]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT pr.id, pr.source_person_id, source.name AS source_name,
                       pr.target_person_id, target.name AS target_name,
                       pr.relationship_type, pr.confidence, pr.status, pr.method,
                       pr.first_seen_at, pr.last_seen_at,
                       COUNT(DISTINCT pre.news_id) AS evidence_count,
                       GROUP_CONCAT(DISTINCT pre.news_id) AS evidence_news_ids,
                       GROUP_CONCAT(pre.evidence_text) AS evidence_quotes
                FROM person_relationships AS pr
                JOIN persons AS source ON source.id = pr.source_person_id
                JOIN persons AS target ON target.id = pr.target_person_id
                LEFT JOIN person_relationship_evidence AS pre
                  ON pre.person_relationship_id = pr.id
                GROUP BY pr.id
                ORDER BY evidence_count DESC, pr.id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_person_cooccurrences(self, *, limit: int = 5000) -> list[dict[str, object]]:
        """Return person pairs that share source news, as statistical links."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT r1.target_id AS source_person_id, p1.name AS source_name,
                       r2.target_id AS target_person_id, p2.name AS target_name,
                       COUNT(DISTINCT r1.source_id) AS evidence_count,
                       MIN(n.publish_date) AS first_seen_at,
                       MAX(n.publish_date) AS last_seen_at,
                       GROUP_CONCAT(DISTINCT r1.source_id) AS evidence_news_ids
                FROM relationships AS r1
                JOIN relationships AS r2
                  ON r1.source_type = 'news' AND r2.source_type = 'news'
                 AND r1.source_id = r2.source_id
                 AND r1.target_type = 'person' AND r2.target_type = 'person'
                 AND r1.target_id < r2.target_id
                JOIN persons AS p1 ON p1.id = r1.target_id
                JOIN persons AS p2 ON p2.id = r2.target_id
                JOIN news AS n ON n.id = r1.source_id
                GROUP BY r1.target_id, r2.target_id
                ORDER BY evidence_count DESC, r1.target_id, r2.target_id
                LIMIT ?
                """,
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

    def get_entity_news(self, *, entity_type: str, entity_id: int, limit: int = 1000) -> list[dict[str, object]]:
        """Return news evidence linked to one person or organization."""
        if entity_type not in {"person", "organization", "event"}:
            raise ValueError("unsupported entity type")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT n.id, n.title, n.publish_date, n.summary, n.category,
                       n.fact_status, r.relationship_type, r.confidence
                FROM relationships AS r
                JOIN news AS n
                  ON r.source_type = 'news' AND r.source_id = n.id
                WHERE r.target_type = ? AND r.target_id = ?
                ORDER BY n.publish_date DESC, n.id DESC
                LIMIT ?
                """,
                (entity_type, entity_id, limit),
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
                SELECT id, publish_date, analysis_json, analysis_confidence, credibility
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

    def merge_organization_alias(self, *, alias_id: int, canonical_name: str) -> int:
        """Rename or merge one verified organization alias without losing evidence."""
        with self.database.connect() as connection:
            alias = connection.execute(
                "SELECT id, name, country, description FROM organizations WHERE id = ?",
                (alias_id,),
            ).fetchone()
            if alias is None:
                raise ValueError("organization alias does not exist")
            canonical = connection.execute(
                "SELECT id FROM organizations WHERE lower(name) = lower(?) LIMIT 1",
                (canonical_name,),
            ).fetchone()
            if canonical is None:
                connection.execute(
                    """
                    UPDATE organizations
                    SET name = ?, updated_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE id = ?
                    """,
                    (canonical_name, alias_id),
                )
                connection.execute(
                    "UPDATE persons SET organization = ? WHERE lower(organization) = lower(?)",
                    (canonical_name, alias["name"]),
                )
                return alias_id

            canonical_id = int(canonical["id"])
            if canonical_id == alias_id:
                return canonical_id
            connection.execute(
                """
                INSERT OR IGNORE INTO relationships (
                    source_type, source_id, target_type, target_id,
                    relationship_type, evidence_news_id, confidence, created_time
                )
                SELECT source_type, source_id, target_type, ?, relationship_type,
                       evidence_news_id, confidence, created_time
                FROM relationships
                WHERE target_type = 'organization' AND target_id = ?
                """,
                (canonical_id, alias_id),
            )
            connection.execute(
                "DELETE FROM relationships WHERE target_type = 'organization' AND target_id = ?",
                (alias_id,),
            )
            connection.execute(
                """
                UPDATE organizations
                SET country = COALESCE(country, ?),
                    description = COALESCE(description, ?),
                    updated_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,
                (alias["country"], alias["description"], canonical_id),
            )
            connection.execute(
                "UPDATE persons SET organization = ? WHERE lower(organization) = lower(?)",
                (canonical_name, alias["name"]),
            )
            connection.execute("DELETE FROM organizations WHERE id = ?", (alias_id,))
            return canonical_id

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
                    default_fact_status, include_keywords, exclude_keywords,
                    fallback_urls, enabled, refresh_interval_hours
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    fallback_urls = excluded.fallback_urls,
                    enabled = excluded.enabled,
                    refresh_interval_hours = excluded.refresh_interval_hours,
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
                    json.dumps(source.fallback_urls, ensure_ascii=False),
                    int(source.enabled),
                    source.refresh_interval_hours,
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
                fallback_urls=json.loads(dict(row).get("fallback_urls") or "[]"),
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
                refresh_interval_hours=dict(row).get("refresh_interval_hours", 24),
                next_retry_at=dict(row).get("next_retry_at"),
                consecutive_failures=dict(row).get("consecutive_failures", 0),
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
        cooldown_seconds: int | None = None,
    ) -> None:
        if cooldown_seconds is not None and cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must not be negative")
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
                    next_retry_at = CASE
                        WHEN ? IS NULL THEN NULL
                        ELSE strftime(
                            '%Y-%m-%dT%H:%M:%fZ',
                            'now',
                            printf('+%d seconds', ?)
                        )
                    END,
                    consecutive_failures = CASE
                        WHEN ? IS NULL THEN 0
                        ELSE consecutive_failures + 1
                    END,
                    updated_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,
                (
                    etag,
                    last_modified,
                    error,
                    error,
                    cooldown_seconds,
                    cooldown_seconds,
                    error,
                    source_id,
                ),
            )

    def start_source_run(self, source_id: int) -> int:
        if source_id < 1:
            raise ValueError("source_id must be positive")
        with self.database.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO source_runs (source_id, status) VALUES (?, 'running')",
                (source_id,),
            )
            return int(cursor.lastrowid)

    def finish_source_run(
        self,
        run_id: int,
        *,
        status: str,
        http_status: int | None = None,
        fetched_count: int = 0,
        parsed_count: int = 0,
        inserted_count: int = 0,
        duplicate_count: int = 0,
        filtered_count: int = 0,
        invalid_count: int = 0,
        error: str | None = None,
    ) -> None:
        if status not in {"success", "not_modified", "empty", "failed"}:
            raise ValueError(f"Unsupported source run status: {status}")
        counts = (
            fetched_count,
            parsed_count,
            inserted_count,
            duplicate_count,
            filtered_count,
            invalid_count,
        )
        if any(count < 0 for count in counts):
            raise ValueError("Source run counts must not be negative")
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE source_runs
                SET status = ?,
                    http_status = ?,
                    fetched_count = ?,
                    parsed_count = ?,
                    inserted_count = ?,
                    duplicate_count = ?,
                    filtered_count = ?,
                    invalid_count = ?,
                    error = ?,
                    finished_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ? AND status = 'running'
                """,
                (
                    status,
                    http_status,
                    fetched_count,
                    parsed_count,
                    inserted_count,
                    duplicate_count,
                    filtered_count,
                    invalid_count,
                    error[:1000] if error else None,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"Source run is not active: run_id={run_id}")

    def get_latest_source_runs(self) -> dict[int, dict[str, object]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT runs.*
                FROM source_runs AS runs
                JOIN (
                    SELECT source_id, MAX(id) AS latest_id
                    FROM source_runs
                    GROUP BY source_id
                ) AS latest ON latest.latest_id = runs.id
                """
            ).fetchall()
        return {int(row["source_id"]): dict(row) for row in rows}

    def record_youtube_metric(
        self,
        *,
        news_id: int,
        video_id: str,
        view_count: int,
        like_count: int,
        comment_count: int,
        view_growth_24h: int = 0,
        priority: bool = False,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO youtube_metrics
                    (news_id, video_id, view_count, like_count, comment_count,
                     view_growth_24h, priority)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (news_id, video_id, view_count, like_count, comment_count,
                 view_growth_24h, int(priority)),
            )

    def get_previous_youtube_views(self, *, video_id: str) -> int | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT view_count FROM youtube_metrics
                WHERE video_id = ? ORDER BY captured_at DESC, id DESC LIMIT 1
                """,
                (video_id,),
            ).fetchone()
        return int(row["view_count"]) if row else None

    def get_priority_youtube_news(self, *, limit: int) -> list[dict[str, object]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT n.id, n.source_url, n.feed_entry_id
                FROM news AS n
                JOIN youtube_metrics AS m ON m.news_id = n.id
                WHERE n.transcript_status IN ('not_requested', 'failed')
                  AND m.priority = 1
                  AND m.id = (
                      SELECT latest.id FROM youtube_metrics AS latest
                      WHERE latest.video_id = m.video_id
                      ORDER BY latest.captured_at DESC, latest.id DESC LIMIT 1
                  )
                ORDER BY m.view_growth_24h DESC, m.view_count DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_youtube_transcript(
        self,
        *,
        news_id: int,
        status: str,
        transcript: str | None = None,
        token_count: int | None = None,
    ) -> None:
        with self.database.connect() as connection:
            if status == "completed":
                if not transcript:
                    raise ValueError("completed YouTube transcript must contain text")
                content_hash = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
                connection.execute(
                    """
                    UPDATE news
                    SET transcript_status = ?, transcript_tokens = ?,
                        extracted_content = ?, extraction_status = 'completed',
                        content_hash = ?, extracted_by = 'youtube-captions',
                        content_extracted_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                        extraction_error = NULL,
                        updated_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE id = ?
                    """,
                    (status, token_count, transcript, content_hash, news_id),
                )
                return
            connection.execute(
                """
                UPDATE news
                SET transcript_status = ?, transcript_tokens = ?,
                    updated_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,
                (status, token_count, news_id),
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

    def add_tag(self, item: Tag) -> int:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO tags (name, slug, tag_type, description, parent_id)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    name = excluded.name,
                    tag_type = excluded.tag_type,
                    description = COALESCE(excluded.description, tags.description),
                    parent_id = COALESCE(excluded.parent_id, tags.parent_id),
                    updated_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (item.name, item.slug, item.tag_type.value, item.description, item.parent_id),
            )
            if cursor.lastrowid:
                return int(cursor.lastrowid)
            row = connection.execute("SELECT id FROM tags WHERE slug = ?", (item.slug,)).fetchone()
            if row is None:
                raise RuntimeError("tag upsert did not return an id")
            return int(row["id"])

    def get_tag_id(self, *, slug: str) -> int | None:
        with self.database.connect() as connection:
            row = connection.execute("SELECT id FROM tags WHERE slug = ? LIMIT 1", (slug,)).fetchone()
        return int(row["id"]) if row is not None else None

    def add_tag_assignment(self, item: TagAssignment) -> int:
        with self.database.connect() as connection:
            existing = connection.execute(
                """
                SELECT id FROM tag_assignments
                WHERE tag_id = ? AND entity_type = ? AND entity_id = ?
                  AND source_news_id IS ?
                LIMIT 1
                """,
                (item.tag_id, item.entity_type.value, item.entity_id, item.source_news_id),
            ).fetchone()
            if existing is not None:
                connection.execute(
                    """
                    UPDATE tag_assignments
                    SET confidence = CASE
                            WHEN confidence IS NULL THEN ?
                            WHEN ? IS NULL THEN confidence
                            ELSE MAX(confidence, ?)
                        END,
                        method = ?, status = ?
                    WHERE id = ?
                    """,
                    (
                        item.confidence,
                        item.confidence,
                        item.confidence,
                        item.method.value,
                        item.status.value,
                        int(existing["id"]),
                    ),
                )
                return 0
            cursor = connection.execute(
                """
                INSERT INTO tag_assignments (
                    tag_id, entity_type, entity_id, source_news_id,
                    confidence, method, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.tag_id,
                    item.entity_type.value,
                    item.entity_id,
                    item.source_news_id,
                    item.confidence,
                    item.method.value,
                    item.status.value,
                ),
            )
            return int(cursor.lastrowid)

    def add_person_relationship(
        self,
        item: PersonRelationship,
        *,
        evidence_news_id: int | None = None,
        evidence_text: str | None = None,
    ) -> tuple[int, bool]:
        """Upsert a person relation and optionally attach one news evidence row."""
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO person_relationships (
                    source_person_id, target_person_id, relationship_type,
                    confidence, status, method, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_person_id, target_person_id, relationship_type)
                DO UPDATE SET
                    confidence = CASE
                        WHEN person_relationships.confidence IS NULL THEN excluded.confidence
                        WHEN excluded.confidence IS NULL THEN person_relationships.confidence
                        ELSE MAX(person_relationships.confidence, excluded.confidence)
                    END,
                    first_seen_at = CASE
                        WHEN person_relationships.first_seen_at IS NULL THEN excluded.first_seen_at
                        WHEN excluded.first_seen_at IS NULL THEN person_relationships.first_seen_at
                        ELSE MIN(person_relationships.first_seen_at, excluded.first_seen_at)
                    END,
                    last_seen_at = CASE
                        WHEN person_relationships.last_seen_at IS NULL THEN excluded.last_seen_at
                        WHEN excluded.last_seen_at IS NULL THEN person_relationships.last_seen_at
                        ELSE MAX(person_relationships.last_seen_at, excluded.last_seen_at)
                    END
                """,
                (
                    item.source_person_id,
                    item.target_person_id,
                    item.relationship_type,
                    item.confidence,
                    item.status.value,
                    item.method.value,
                    item.first_seen_at,
                    item.last_seen_at,
                ),
            )
            relation_id = int(cursor.lastrowid or 0)
            if not relation_id:
                row = connection.execute(
                    """
                    SELECT id FROM person_relationships
                    WHERE source_person_id = ? AND target_person_id = ?
                      AND relationship_type = ?
                    """,
                    (item.source_person_id, item.target_person_id, item.relationship_type),
                ).fetchone()
                if row is None:
                    raise RuntimeError("person relationship upsert did not return an id")
                relation_id = int(row["id"])
            evidence_added = False
            if evidence_news_id is not None:
                evidence_cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO person_relationship_evidence (
                        person_relationship_id, news_id, evidence_text
                    ) VALUES (?, ?, ?)
                    """,
                    (relation_id, evidence_news_id, evidence_text),
                )
                evidence_added = evidence_cursor.rowcount > 0
            return relation_id, evidence_added
