"""Exercise WP10.3 empty and independently stateful downgrade contracts."""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from tools.wp10_2_migration_probe import (
    create_database,
    database_url,
    drop_database,
    libpq_url,
    run_alembic,
    scalar,
)
from tools.wp10_2_runtime_probe import entity_manifest

EVIDENCE: list[dict[str, Any]] = []


FIXTURES = {
    "claims": """
        INSERT INTO public.claims (
            id, document_id, claim_grant_id, ordinal, claim_text,
            claim_type, assertion_status, revision_no
        ) VALUES (%s, %s, %s, 0, 'guard', 'observation', 'reported', 1)
    """,
    "evidence": """
        INSERT INTO public.evidence (
            id, document_id, excerpt, locator_type, public_locator,
            locator_sha256, source_url
        ) VALUES (%s, %s, 'guard', 'text', '{}'::jsonb, %s,
                  'https://example.test/guard')
    """,
    "claim_evidence": """
        INSERT INTO public.claim_evidence (id, claim_id, evidence_id)
        VALUES (%s, %s, %s)
    """,
    "document_entities": """
        INSERT INTO public.document_entities (
            id, document_id, entity_id, basis_evidence_id, basis_claim_id
        ) VALUES (%s, %s, %s, %s, %s)
    """,
    "search_documents": """
        INSERT INTO public.search_documents (
            document_id, search_vector, display_text, facets, indexed_at
        ) VALUES (%s, to_tsvector('simple', 'guard'), 'guard', '{}'::jsonb, now())
    """,
    "claim_identity": """
        INSERT INTO audit.claim_public_identities (
            claim_id, document_id, public_id, display_ordinal
        ) VALUES (%s, %s, %s, 0)
    """,
    "evidence_identity": """
        INSERT INTO audit.evidence_public_identities (evidence_span_id, public_id)
        VALUES (%s, %s)
    """,
    "rebuild_run": """
        INSERT INTO audit.publication_rebuild_runs (
            rebuild_id, input_digest, result_digest, counts,
            status, started_at, finished_at
        ) VALUES (%s, %s, %s, '{}'::jsonb, 'succeeded', now(), now())
    """,
}


def fixture_params(case: str) -> tuple[object, ...]:
    if case == "claims":
        return uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    if case == "evidence":
        return uuid.uuid4(), uuid.uuid4(), "a" * 64
    if case == "claim_evidence":
        return uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    if case == "document_entities":
        return tuple(uuid.uuid4() for _item in range(5))
    if case == "search_documents":
        return (uuid.uuid4(),)
    if case == "claim_identity":
        return uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    if case == "evidence_identity":
        return uuid.uuid4(), uuid.uuid4()
    if case == "rebuild_run":
        return uuid.uuid4(), "b" * 64, "c" * 64
    raise ValueError(case)


def table_for(case: str) -> str:
    return {
        "claims": "public.claims",
        "evidence": "public.evidence",
        "claim_evidence": "public.claim_evidence",
        "document_entities": "public.document_entities",
        "search_documents": "public.search_documents",
        "claim_identity": "audit.claim_public_identities",
        "evidence_identity": "audit.evidence_public_identities",
        "rebuild_run": "audit.publication_rebuild_runs",
    }[case]


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("probe count is not an integer")
    return value


def table_count(connection: psycopg.Connection[object], table: str) -> int:
    schema, name = table.split(".", maxsplit=1)
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("SELECT count(*) FROM {}.{}").format(
                sql.Identifier(schema),
                sql.Identifier(name),
            )
        )
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("guard count query returned no row")
    if not isinstance(row, tuple):
        raise RuntimeError("guard count query returned no row")
    return _as_int(row[0])


def seed_legacy_entity_grant(connection: psycopg.Connection[object]) -> uuid.UUID:
    grant_id = uuid.uuid4()
    with connection.cursor() as cursor:
        cursor.execute("SET session_replication_role = replica")
        cursor.execute(
            """
            INSERT INTO audit.entity_publication_grants (
                id, review_case_id, entity_id, decision_id, revision_no,
                grant_status, granted_at, publication_payload_sha256
            ) VALUES (%s, %s, %s, %s, 1, 'active', clock_timestamp(), %s)
            """,
            (grant_id, uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "a" * 64),
        )
        cursor.execute("SET session_replication_role = origin")
    return grant_id


def rebuild_as_migrator(
    connection: psycopg.Connection[object], rebuild_id: uuid.UUID
) -> tuple[object, ...]:
    with connection.cursor() as cursor:
        cursor.execute("SET SESSION AUTHORIZATION uap_migrator")
        try:
            cursor.execute("SELECT * FROM ops.rebuild_public_projection(%s)", (rebuild_id,))
            row = cursor.fetchone()
        finally:
            cursor.execute("RESET SESSION AUTHORIZATION")
    if row is None:
        raise RuntimeError("legacy quarantine rebuild returned no row")
    if not isinstance(row, tuple):
        raise RuntimeError("legacy quarantine rebuild returned no row")
    return tuple(row)


def legacy_v1_quarantine_with_v2_rebuild(admin_url: str) -> None:
    """Upgrade a real v1 grant, then prove it is absent from v2 rebuild input."""

    name = f"uap_wp10_3_legacy_{uuid.uuid4().hex[:9]}"
    create_database(admin_url, name)
    isolated = database_url(admin_url, name)
    try:
        run_alembic(isolated, "upgrade", "0019_manual_claims_binding")
        with psycopg.connect(libpq_url(isolated), autocommit=True) as connection:
            legacy_grant = seed_legacy_entity_grant(connection)

        run_alembic(isolated, "upgrade", "0022_wp10_claim_search_projection")
        with psycopg.connect(libpq_url(isolated), autocommit=True) as connection:
            cross_stage_quarantine = _as_int(
                scalar(
                    connection,
                    """
                    SELECT count(*)
                      FROM audit.publication_quarantine AS quarantine
                     WHERE quarantine.grant_table = 'entity_publication_grants'
                       AND quarantine.grant_id = %s
                       AND quarantine.resolves_quarantine_id IS NULL
                       AND NOT EXISTS (
                            SELECT 1
                              FROM audit.publication_quarantine AS resolution
                             WHERE resolution.resolves_quarantine_id = quarantine.id
                       )
                    """,
                    legacy_grant,
                )
            )
            if cross_stage_quarantine != 1:
                raise RuntimeError("0019 active v1 grant did not upgrade into quarantine")

            _entity_id, v2_grant, _decision_id, _manifest = entity_manifest(
                connection, f"legacy-mixed-{uuid.uuid4().hex[:7]}"
            )
            rebuild_id = uuid.uuid4()
            first = rebuild_as_migrator(connection, rebuild_id)
            if first[4] != "succeeded" or first[5] is not False:
                raise RuntimeError("legal v1 quarantine blocked a mixed-state rebuild")

            second_legacy = seed_legacy_entity_grant(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO audit.publication_quarantine (
                        id, grant_table, grant_id, reason_code
                    ) VALUES (
                        %s, 'entity_publication_grants', %s,
                        'publication_manifest_required'
                    )
                    """,
                    (uuid.uuid4(), second_legacy),
                )
            replay = rebuild_as_migrator(connection, rebuild_id)

            active_grants = _as_int(
                scalar(
                    connection,
                    "SELECT count(*) FROM audit.entity_publication_grants "
                    "WHERE grant_status = 'active'::audit.grant_status",
                )
            )
            active_manifests = _as_int(
                scalar(
                    connection,
                    """
                    SELECT count(*)
                      FROM audit.entity_publication_grants AS grant_row
                      JOIN audit.entity_publication_manifests AS manifest
                        ON manifest.grant_id = grant_row.id
                     WHERE grant_row.grant_status = 'active'::audit.grant_status
                    """,
                )
            )
            unresolved_quarantines = _as_int(
                scalar(
                    connection,
                    """
                    SELECT count(*)
                      FROM audit.publication_quarantine AS quarantine
                     WHERE quarantine.grant_table = 'entity_publication_grants'
                       AND quarantine.resolves_quarantine_id IS NULL
                       AND NOT EXISTS (
                            SELECT 1
                              FROM audit.publication_quarantine AS resolution
                             WHERE resolution.resolves_quarantine_id = quarantine.id
                       )
                    """,
                )
            )
            public_entities = _as_int(scalar(connection, "SELECT count(*) FROM public.entities"))
            identity_count = _as_int(
                scalar(connection, "SELECT count(*) FROM audit.entity_public_identities")
            )

        if replay[4] != "succeeded" or replay[5] is not True:
            raise RuntimeError("adding legal quarantine changed rebuild replay outcome")
        if replay[1] != first[1] or replay[2] != first[2]:
            raise RuntimeError("legal quarantine entered rebuild input/result digest")
        if (active_grants, active_manifests, unresolved_quarantines) != (3, 1, 2):
            raise RuntimeError("mixed v1/v2 fixture counts were not isolated")
        if public_entities != 1 or identity_count != 1:
            raise RuntimeError("quarantined v1 grant entered the public projection")

        EVIDENCE.append(
            {
                "case": "legacy_v1_quarantine_with_v2_rebuild",
                "status": "passed",
                "cross_stage_legacy_grant_id": str(legacy_grant),
                "active_entity_grants": active_grants,
                "active_v2_entity_manifests": active_manifests,
                "unresolved_legacy_quarantines": unresolved_quarantines,
                "v2_grant_id": str(v2_grant),
                "rebuild_status": first[4],
                "replay_status": replay[4],
                "replayed": replay[5],
                "input_digest_before": str(first[1]).strip(),
                "input_digest_after_second_quarantine": str(replay[1]).strip(),
                "result_digest_before": str(first[2]).strip(),
                "result_digest_after_second_quarantine": str(replay[2]).strip(),
                "public_entities": public_entities,
                "entity_public_identities": identity_count,
                "quarantined_v1_excluded": True,
            }
        )
    finally:
        drop_database(admin_url, name)


def independent_guard(admin_url: str, case: str) -> None:
    name = f"uap_wp10_3_{case[:10]}_{uuid.uuid4().hex[:7]}"
    create_database(admin_url, name)
    isolated = database_url(admin_url, name)
    table = table_for(case)
    try:
        run_alembic(isolated, "upgrade", "0022_wp10_claim_search_projection")
        with psycopg.connect(libpq_url(isolated), autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET session_replication_role = replica")
                cursor.execute(FIXTURES[case], fixture_params(case))
                cursor.execute("SET session_replication_role = origin")
            before = table_count(connection, table)
            if before != 1:
                raise RuntimeError(f"{case} guard fixture was not isolated")
            for other_case in FIXTURES:
                if other_case == case:
                    continue
                if table_count(connection, table_for(other_case)) != 0:
                    raise RuntimeError(f"{case} fixture contaminated {other_case}")
        try:
            run_alembic(isolated, "downgrade", "0021_wp10_publisher_projection")
        except RuntimeError as error:
            if "22023/publication_contract_state_blocks_downgrade" not in str(error):
                raise RuntimeError(f"{case} guard lacked stable SQLSTATE/code") from error
        else:
            raise RuntimeError(f"{case} downgrade unexpectedly succeeded")
        with psycopg.connect(libpq_url(isolated), autocommit=True) as connection:
            version = scalar(connection, "SELECT version_num FROM alembic_version")
            after = table_count(connection, table)
            function_present = scalar(
                connection,
                "SELECT to_regprocedure('ops.rebuild_public_projection(uuid)') IS NOT NULL",
            )
        if version != "0022_wp10_claim_search_projection" or after != before:
            raise RuntimeError(f"{case} downgrade guard changed state")
        if function_present is not True:
            raise RuntimeError(f"{case} guard ran after destructive DDL")
        EVIDENCE.append(
            {
                "case": f"{case}_independent_downgrade_guard",
                "status": "passed",
                "sqlstate": "22023",
                "primary": "publication_contract_state_blocks_downgrade",
                "target_rows_before": before,
                "target_rows_after": after,
                "all_other_wp10_3_guard_rows": 0,
                "migration_version_preserved": version,
                "destructive_ddl_not_run": True,
            }
        )
    finally:
        drop_database(admin_url, name)


def run(admin_url: str) -> None:
    name = f"uap_wp10_3_empty_{uuid.uuid4().hex[:10]}"
    create_database(admin_url, name)
    isolated = database_url(admin_url, name)
    try:
        run_alembic(isolated, "upgrade", "0021_wp10_publisher_projection")
        run_alembic(isolated, "upgrade", "0022_wp10_claim_search_projection")
        run_alembic(isolated, "downgrade", "0021_wp10_publisher_projection")
        run_alembic(isolated, "upgrade", "0022_wp10_claim_search_projection")
        EVIDENCE.append(
            {
                "case": "empty_state_0021_0022_0021_0022_roundtrip",
                "status": "passed",
            }
        )
    finally:
        drop_database(admin_url, name)
    legacy_v1_quarantine_with_v2_rebuild(admin_url)
    for case in FIXTURES:
        independent_guard(admin_url, case)


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
                        "schema": "wp10.3-migration-evidence.v1",
                        "status": status,
                        "checks": EVIDENCE,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
    print("WP10.3 migration roundtrip and independent downgrade guards passed")


if __name__ == "__main__":
    main()
