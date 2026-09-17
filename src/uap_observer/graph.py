"""Build a compact, source-aware person graph for the static site."""

from __future__ import annotations

from datetime import datetime, timezone

from uap_observer.repositories import Repository


def build_person_graph(
    repository: Repository,
    *,
    person_limit: int = 500,
    edge_limit: int = 3000,
    min_cooccurrence: int = 2,
) -> dict[str, object]:
    """Return Cytoscape-compatible graph data with explicit and statistical edges."""
    if person_limit < 1 or edge_limit < 1 or min_cooccurrence < 1:
        raise ValueError("graph limits must be at least 1")

    persons = repository.get_persons(limit=person_limit)
    person_ids = {int(person["id"]) for person in persons}
    nodes = [
        {
            "data": {
                "id": f"person:{int(person['id'])}",
                "entity_id": int(person["id"]),
                "type": "person",
                "name": str(person.get("name") or "未命名人物"),
                "organization": person.get("organization"),
                "country": person.get("country"),
            }
        }
        for person in persons
    ]

    edges: list[dict[str, object]] = []
    for relation in repository.get_person_relationships(limit=edge_limit):
        source_id = int(relation["source_person_id"])
        target_id = int(relation["target_person_id"])
        if source_id not in person_ids or target_id not in person_ids:
            continue
        edges.append(
            {
                "data": {
                    "id": f"person-relation:{int(relation['id'])}",
                    "source": f"person:{source_id}",
                    "target": f"person:{target_id}",
                    "kind": "explicit",
                    "label": str(relation["relationship_type"]),
                    "status": str(relation.get("status") or "candidate"),
                    "method": str(relation.get("method") or "ai_extracted"),
                    "confidence": relation.get("confidence"),
                    "evidence_count": int(relation.get("evidence_count") or 0),
                    "evidence_news_ids": _int_list(relation.get("evidence_news_ids")),
                    "evidence_quotes": _text_list(relation.get("evidence_quotes")),
                    "first_seen_at": relation.get("first_seen_at"),
                    "last_seen_at": relation.get("last_seen_at"),
                }
            }
        )
        if len(edges) >= edge_limit:
            break

    explicit_pairs = {
        frozenset((edge["data"]["source"], edge["data"]["target"]))
        for edge in edges
    }
    if len(edges) < edge_limit:
        for relation in repository.get_person_cooccurrences(limit=edge_limit):
            source_id = int(relation["source_person_id"])
            target_id = int(relation["target_person_id"])
            if source_id not in person_ids or target_id not in person_ids:
                continue
            evidence_count = int(relation.get("evidence_count") or 0)
            if evidence_count < min_cooccurrence:
                continue
            source = f"person:{source_id}"
            target = f"person:{target_id}"
            if frozenset((source, target)) in explicit_pairs:
                continue
            edges.append(
                {
                    "data": {
                        "id": f"cooccurrence:{source_id}:{target_id}",
                        "source": source,
                        "target": target,
                        "kind": "cooccurrence",
                        "label": "共同出现",
                        "status": "candidate",
                        "method": "co_occurrence",
                        "confidence": None,
                        "evidence_count": evidence_count,
                        "evidence_news_ids": _int_list(relation.get("evidence_news_ids")),
                        "first_seen_at": relation.get("first_seen_at"),
                        "last_seen_at": relation.get("last_seen_at"),
                    }
                }
            )
            if len(edges) >= edge_limit:
                break

    tags: list[dict[str, object]] = []
    assignments = repository.get_tag_assignments(limit=10000)
    for tag in repository.get_tags(limit=1000):
        members = sorted(
            {
                int(row["entity_id"])
                for row in assignments
                if row.get("tag_slug") == tag.get("slug")
                and row.get("entity_type") == "person"
                and int(row["entity_id"]) in person_ids
            }
        )
        if not members:
            continue
        tags.append(
            {
                "name": str(tag["name"]),
                "slug": str(tag["slug"]),
                "tag_type": str(tag["tag_type"]),
                "person_ids": members,
            }
        )

    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "nodes": nodes,
        "edges": edges,
        "tags": tags,
        "meta": {
            "person_count": len(nodes),
            "edge_count": len(edges),
            "explicit_edge_count": sum(1 for edge in edges if edge["data"]["kind"] == "explicit"),
            "cooccurrence_edge_count": sum(1 for edge in edges if edge["data"]["kind"] == "cooccurrence"),
            "cooccurrence_minimum": min_cooccurrence,
        },
    }


def _int_list(value: object) -> list[int]:
    if not value:
        return []
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, list):
        values = value
    else:
        return []
    result: list[int] = []
    for item in values:
        try:
            number = int(str(item).strip())
        except (TypeError, ValueError):
            continue
        if number not in result:
            result.append(number)
    return result


def _text_list(value: object) -> list[str]:
    if not value:
        return []
    values = value.split(",") if isinstance(value, str) else value
    if not isinstance(values, list):
        return []
    return [str(item).strip() for item in values if str(item).strip()]
