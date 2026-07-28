"""Create idempotent person/event relationships from validated AI JSON."""

from __future__ import annotations

import json
from dataclasses import dataclass

from uap_observer.models import EntityType, Event, EventStatus, Person, Relationship
from uap_observer.repositories import Repository


@dataclass(frozen=True)
class EntityLinkRun:
    records: int = 0
    persons_created: int = 0
    events_created: int = 0
    relationships_created: int = 0
    skipped_invalid: int = 0


class EntityLinkingService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def run(self, *, limit: int = 1000) -> EntityLinkRun:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        records = self.repository.get_completed_analysis_records(limit=limit)
        persons_created = events_created = relationships_created = skipped = 0
        for record in records:
            try:
                analysis = json.loads(str(record["analysis_json"]))
            except (TypeError, json.JSONDecodeError):
                skipped += 1
                continue
            if not isinstance(analysis, dict):
                skipped += 1
                continue
            confidence = float(record.get("analysis_confidence") or 0.5)
            news_id = int(record["id"])
            organizations = _string_list(analysis.get("named_organizations"))
            organization = ", ".join(organizations) or None
            for name in _string_list(analysis.get("named_persons")):
                person_id = self.repository.get_person_id(name=name)
                if person_id is None:
                    person_id = self.repository.add_person(
                        Person(name=name, organization=organization)
                    )
                    persons_created += 1
                relationships_created += self._link(
                    news_id,
                    EntityType.PERSON,
                    person_id,
                    confidence,
                )
            for event_name in _string_list(analysis.get("related_events")):
                event_id = self.repository.get_event_id(event_name=event_name)
                if event_id is None:
                    event_id = self.repository.add_event(
                        Event(
                            event_name=event_name,
                            status=EventStatus.UNVERIFIED,
                            credibility=int(record.get("credibility") or 1),
                            description="AI 从已分析新闻中提取的相关事件，待人工核验。",
                        )
                    )
                    events_created += 1
                relationships_created += self._link(
                    news_id,
                    EntityType.EVENT,
                    event_id,
                    confidence,
                )
        return EntityLinkRun(
            records=len(records),
            persons_created=persons_created,
            events_created=events_created,
            relationships_created=relationships_created,
            skipped_invalid=skipped,
        )

    def _link(
        self,
        news_id: int,
        target_type: EntityType,
        target_id: int,
        confidence: float,
    ) -> int:
        relationship_type = "mentions_person" if target_type is EntityType.PERSON else "mentions_event"
        if self.repository.relationship_exists(
            source_type=EntityType.NEWS,
            source_id=news_id,
            target_type=target_type,
            target_id=target_id,
            relationship_type=relationship_type,
        ):
            return 0
        self.repository.add_relationship(
            Relationship(
                source_type=EntityType.NEWS,
                source_id=news_id,
                target_type=target_type,
                target_id=target_id,
                relationship_type=relationship_type,
                evidence_news_id=news_id,
                confidence=confidence,
            )
        )
        return 1


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result
