"""Small persistence layer for the initial domain models."""

from __future__ import annotations

import json

from uap_observer.database import Database
from uap_observer.models import Event, News, Person, Relationship


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
                    ai_model, ai_processed_at, processing_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
            return int(cursor.lastrowid)

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
