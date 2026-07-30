"""Create idempotent person/event relationships from validated AI JSON."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass

from uap_observer.models import (
    EntityType,
    Event,
    EventStatus,
    Organization,
    Person,
    Relationship,
)
from uap_observer.repositories import Repository


@dataclass(frozen=True)
class EntityLinkRun:
    records: int = 0
    persons_created: int = 0
    events_created: int = 0
    organizations_created: int = 0
    organizations_normalized: int = 0
    relationships_created: int = 0
    skipped_invalid: int = 0


class EntityLinkingService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def run(self, *, limit: int = 1000) -> EntityLinkRun:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        organizations_normalized = self._normalize_existing_organizations()
        records = self.repository.get_completed_analysis_records(limit=limit)
        persons_created = 0
        events_created = 0
        organizations_created = 0
        relationships_created = 0
        skipped = 0
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
            organizations = _unique_strings(
                canonicalize_organization_name(name)
                for name in _string_list(analysis.get("named_organizations"))
            )
            organization = ", ".join(organizations) or None
            for organization_name in organizations:
                organization_id = self.repository.get_organization_id(name=organization_name)
                if organization_id is None:
                    organization_id = self.repository.add_organization(
                        Organization(name=organization_name)
                    )
                    organizations_created += 1
                relationships_created += self._link(
                    news_id,
                    EntityType.ORGANIZATION,
                    organization_id,
                    confidence,
                )
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
            organizations_created=organizations_created,
            organizations_normalized=organizations_normalized,
            skipped_invalid=skipped,
        )

    def _normalize_existing_organizations(self) -> int:
        normalized = 0
        for organization in self.repository.get_organizations(limit=10000):
            name = str(organization["name"])
            canonical_name = canonicalize_organization_name(name)
            if canonical_name == name:
                continue
            self.repository.merge_organization_alias(
                alias_id=int(organization["id"]),
                canonical_name=canonical_name,
            )
            normalized += 1
        return normalized

    def _link(
        self,
        news_id: int,
        target_type: EntityType,
        target_id: int,
        confidence: float,
    ) -> int:
        relationship_type = {
            EntityType.PERSON: "mentions_person",
            EntityType.EVENT: "mentions_event",
            EntityType.ORGANIZATION: "mentions_organization",
        }[target_type]
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


def _unique_strings(values: object) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in result:
            result.append(text)
    return result


def _organization_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)


_ORGANIZATION_ALIAS_GROUPS = {
    "AARO": (
        "AARO",
        "All-domain Anomaly Resolution Office",
        "All-Domain Anomaly Resolution Office (AARO)",
        "全域异常解决办公室（AARO）",
    ),
    "DoD": (
        "Department of Defense",
        "Department of Defense (DOD)",
        "United States Defence Department",
        "国防部 (DoD)",
        "国防部（DoD）",
    ),
    "FAA": (
        "FAA",
        "Federal Aviation Administration",
        "联邦航空管理局 (FAA)",
        "联邦航空管理局（FAA）",
    ),
    "FBI": (
        "FBI",
        "Federal Bureau of Investigation",
        "联邦调查局 (FBI)",
        "联邦调查局（FBI）",
    ),
    "NASA": (
        "NASA",
        "National Aeronautics and Space Administration",
        "美国国家航空航天局（NASA）",
    ),
    "ODNI": (
        "Office of the Director of National Intelligence",
        "国家情报总监办公室 (ODNI)",
        "国家情报总监办公室（ODNI）",
    ),
    "UAPTF": (
        "UAP任务部队（UAPTF）",
        "Unidentified Aerial Phenomena Task Force",
        "不明空中现象任务小组 (UAPTF)",
    ),
}

_ORGANIZATION_ALIASES = {
    _organization_key(alias): canonical
    for canonical, aliases in _ORGANIZATION_ALIAS_GROUPS.items()
    for alias in aliases
}


def canonicalize_organization_name(value: str) -> str:
    stripped = " ".join(value.split())
    return _ORGANIZATION_ALIASES.get(_organization_key(stripped), stripped)
