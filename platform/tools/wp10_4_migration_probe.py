"""Exercise WP10.4 index roundtrip and representative query plans."""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import Any

import psycopg

from tools.wp10_2_migration_probe import (
    create_database,
    database_url,
    drop_database,
    libpq_url,
    run_alembic,
    scalar,
)

REVISION = "0023_wp10_api_read_indexes"
PARENT = "0022_wp10_claim_search_projection"
NEW_INDEXES = (
    "ix_public_documents_published_id",
    "ix_public_documents_category_published_id",
    "ix_public_documents_fact_status_published_id",
    "ix_public_documents_category_fact_published_id",
    "ix_public_entities_published_id",
    "ix_public_entities_type_published_id",
)
SIGNED_INDEXES = (
    "ix_public_claims_document_ordinal",
    "ix_public_document_entities_entity",
    "ix_search_documents_vector",
    "ix_search_documents_facets",
)
EVIDENCE: list[dict[str, Any]] = []


def index_present(connection: psycopg.Connection[object], name: str) -> bool:
    return bool(scalar(connection, "SELECT to_regclass(%s) IS NOT NULL", f"public.{name}"))


def index_definition(connection: psycopg.Connection[object], name: str) -> str:
    value = scalar(
        connection,
        "SELECT pg_get_indexdef(to_regclass(%s))",
        f"public.{name}",
    )
    if not isinstance(value, str):
        raise RuntimeError(f"missing index definition for {name}")
    return value


def plan_indexes(node: object) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        index_name = node.get("Index Name")
        if isinstance(index_name, str):
            found.add(index_name)
        for value in node.values():
            found.update(plan_indexes(value))
    elif isinstance(node, list):
        for value in node:
            found.update(plan_indexes(value))
    return found


def explain(
    connection: psycopg.Connection[object], statement: str, *params: object
) -> tuple[dict[str, Any], list[str]]:
    with connection.cursor() as cursor:
        cursor.execute("SET enable_seqscan = off")
        try:
            cursor.execute("EXPLAIN (FORMAT JSON, COSTS true) " + statement, params)
            row = cursor.fetchone()
        finally:
            cursor.execute("RESET enable_seqscan")
    if row is None or not isinstance(row, tuple):
        raise RuntimeError("EXPLAIN returned no JSON plan")
    root = row[0]
    if not isinstance(root, list) or not root:
        raise RuntimeError("EXPLAIN returned no JSON plan")
    plan = root[0]
    if not isinstance(plan, dict):
        raise RuntimeError("EXPLAIN JSON root was not an object")
    return plan, sorted(plan_indexes(plan))


def seed_capacity(connection: psycopg.Connection[object]) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SET session_replication_role = replica")
        cursor.execute(
            """
            INSERT INTO public.documents (
                id, document_grant_id, slug, title, summary, category, fact_status,
                source_name, canonical_source_url, source_published_at,
                published_at, revised_at, revision_no
            )
            SELECT gen_random_uuid(), gen_random_uuid(), 'read-' || item,
                   CASE WHEN item = 777
                        THEN 'rareterm golden document'
                        ELSE 'document ' || item
                   END,
                   'summary ' || item,
                   (CASE WHEN item % 2 = 0 THEN 'official_report' ELSE 'other' END)
                       ::public.document_category,
                   (CASE WHEN item % 3 = 0 THEN 'corroborated' ELSE 'unverified' END)
                       ::public.fact_status,
                   'probe', 'https://example.test/read/' || item, NULL,
                   clock_timestamp() - item * interval '1 second', NULL, 1
              FROM generate_series(1, 2000) AS item
            """
        )
        cursor.execute(
            """
            INSERT INTO public.entities (
                id, entity_grant_id, slug, entity_type, name, description,
                country_code, published_at, revision_no
            )
            SELECT gen_random_uuid(), gen_random_uuid(), 'entity-' || item,
                   CASE WHEN item = 777 THEN 'organization' ELSE 'person' END,
                   'entity ' || item, NULL, NULL,
                   clock_timestamp() - item * interval '1 second', 1
              FROM generate_series(1, 2000) AS item
            """
        )
        cursor.execute(
            """
            INSERT INTO public.search_documents (
                document_id, search_vector, display_text, facets, indexed_at
            )
            SELECT document.id,
                   to_tsvector('simple', document.title || ' ' || coalesce(document.summary, '')),
                   document.title || ' ' || coalesce(document.summary, ''),
                   jsonb_build_object(
                       'category', document.category,
                       'fact_status', document.fact_status,
                       'source_name', document.source_name
                   ),
                   clock_timestamp()
              FROM public.documents AS document
            """
        )
        cursor.execute("SET session_replication_role = origin")
        cursor.execute("ANALYZE public.documents")
        cursor.execute("ANALYZE public.entities")
        cursor.execute("ANALYZE public.search_documents")


def run(admin_url: str) -> None:
    name = f"uap_wp10_4_index_{uuid.uuid4().hex[:10]}"
    create_database(admin_url, name)
    isolated = database_url(admin_url, name)
    try:
        run_alembic(isolated, "upgrade", PARENT)
        with psycopg.connect(libpq_url(isolated), autocommit=True) as connection:
            if any(index_present(connection, name) for name in NEW_INDEXES):
                raise RuntimeError("WP10.4 index existed before 0023")
            if not all(index_present(connection, name) for name in SIGNED_INDEXES):
                raise RuntimeError("signed WP10.3 read index baseline was incomplete")

        run_alembic(isolated, "upgrade", REVISION)
        with psycopg.connect(libpq_url(isolated), autocommit=True) as connection:
            if not all(index_present(connection, name) for name in NEW_INDEXES):
                raise RuntimeError("0023 did not create every frozen read index")
            definitions = {name: index_definition(connection, name) for name in NEW_INDEXES}
            seed_capacity(connection)
            document_plan, document_indexes = explain(
                connection,
                """
                SELECT id FROM public.documents
                 WHERE category = %s AND fact_status = %s
                 ORDER BY published_at DESC, id DESC LIMIT 21
                """,
                "official_report",
                "corroborated",
            )
            entity_plan, entity_indexes = explain(
                connection,
                """
                SELECT id FROM public.entities
                 WHERE entity_type = %s
                 ORDER BY published_at DESC, id DESC LIMIT 21
                """,
                "organization",
            )
            search_plan, search_indexes = explain(
                connection,
                """
                SELECT search.document_id
                  FROM public.search_documents AS search
                 WHERE search.search_vector @@ websearch_to_tsquery('simple', %s)
                 LIMIT 21
                """,
                "rareterm",
            )
            if "ix_public_documents_category_fact_published_id" not in document_indexes:
                raise RuntimeError("document filter/keyset plan missed the 0023 index")
            if "ix_public_entities_type_published_id" not in entity_indexes:
                raise RuntimeError("entity filter/keyset plan missed the 0023 index")
            if "ix_search_documents_vector" not in search_indexes:
                raise RuntimeError("search plan missed the signed GIN index")
            version = str(scalar(connection, "SHOW server_version"))

        run_alembic(isolated, "downgrade", PARENT)
        with psycopg.connect(libpq_url(isolated), autocommit=True) as connection:
            if any(index_present(connection, name) for name in NEW_INDEXES):
                raise RuntimeError("0023 downgrade left a WP10.4 index")
            if not all(index_present(connection, name) for name in SIGNED_INDEXES):
                raise RuntimeError("0023 downgrade removed a signed WP10.3 index")

        run_alembic(isolated, "upgrade", REVISION)
        with psycopg.connect(libpq_url(isolated), autocommit=True) as connection:
            if not all(index_present(connection, name) for name in NEW_INDEXES):
                raise RuntimeError("0023 re-upgrade did not restore read indexes")
            migration_version = scalar(connection, "SELECT version_num FROM alembic_version")

        EVIDENCE.append(
            {
                "case": "0022_0023_0022_0023_roundtrip_and_explain",
                "status": "passed",
                "postgresql_version": version,
                "migration_version": migration_version,
                "capacity_documents": 2000,
                "capacity_entities": 2000,
                "index_definitions": definitions,
                "document_plan_indexes": document_indexes,
                "entity_plan_indexes": entity_indexes,
                "search_plan_indexes": search_indexes,
                "document_plan": document_plan,
                "entity_plan": entity_plan,
                "search_plan": search_plan,
                "signed_indexes_preserved": list(SIGNED_INDEXES),
            }
        )
    finally:
        drop_database(admin_url, name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--admin-url", required=True)
    parser.add_argument("--evidence-out", type=Path)
    args = parser.parse_args()
    status = "passed"
    try:
        run(args.admin_url)
    except Exception:
        status = "failed"
        raise
    finally:
        if args.evidence_out:
            args.evidence_out.parent.mkdir(parents=True, exist_ok=True)
            args.evidence_out.write_text(
                json.dumps(
                    {
                        "schema": "wp10.4-migration-evidence.v1",
                        "status": status,
                        "checks": EVIDENCE,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
    print("WP10.4 migration index roundtrip and EXPLAIN probe passed")


if __name__ == "__main__":
    main()
