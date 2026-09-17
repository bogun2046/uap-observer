"""Validate the frozen WP10.3 claim/search projection contract."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

CURRENT_HEAD = "0022_wp10_claim_search_projection"
WP10_4_HEAD = "0023_wp10_api_read_indexes"
WP10_5_HEAD = "0024_wp10_admin_replay"
PARENT_HEAD = "0021_wp10_publisher_projection"
MIGRATION = "alembic/versions/0022_wp10_claim_search_projection.py"


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    actual: object
    expected: object = True


def check(name: str, passed: bool, actual: object, expected: object = True) -> Check:
    return Check(name, passed, actual, expected)


def evaluate(platform: Path) -> list[Check]:
    platform = platform.resolve()
    repository = platform.parent
    migration_path = platform / MIGRATION
    migration = migration_path.read_text(encoding="utf-8") if migration_path.is_file() else ""
    config = Config(str(platform / "alembic.ini"))
    config.set_main_option("script_location", str(platform / "alembic"))
    script = ScriptDirectory.from_config(config)
    revision = script.get_revision(CURRENT_HEAD)
    heads = script.get_heads()

    required = (
        "CREATE TABLE audit.publication_rebuild_runs",
        "CREATE FUNCTION ops.rebuild_public_projection",
        "CREATE FUNCTION ops._apply_claim_publication_event",
        "CREATE FUNCTION ops._wp10_3_rebuild_inputs_valid()",
        "CREATE FUNCTION ops._wp10_3_grant_has_unresolved_quarantine",
        "claim_publication_grants",
        "audit.claim_public_identities",
        "audit.evidence_public_identities",
        "INSERT INTO public.claims",
        "INSERT INTO public.evidence",
        "INSERT INTO public.claim_evidence",
        "INSERT INTO public.document_entities",
        "INSERT INTO public.search_documents",
        "to_tsvector(",
        "publication_dependency_not_ready",
        "publication_rebuild_id_conflict",
        "publication_rebuild_mismatch",
        "publication_contract_state_blocks_downgrade",
        "pg_advisory_xact_lock(hashtextextended('publication-rebuild'",
        "SELECT DISTINCT ON (claim.document_id, entity.id)",
        "manifest.grant_id IS NULL",
        "manifest.grant_id IS NOT NULL",
    )
    required_hits = {token: token in migration for token in required}
    forbidden = (
        "INSERT INTO public.relations",
        "INSERT INTO public.relation_evidence",
        "INSERT INTO audit.relation_publication_grants",
        "CREATE FUNCTION ops.apply_relation",
        "fastapi",
        "uvicorn",
        "jwt",
        "0023_wp10_api_read_indexes",
        "requeue_publication_event",
    )
    forbidden_hits = [token for token in forbidden if token.lower() in migration.lower()]
    downgrade = migration[migration.find("def downgrade") :]
    guard = downgrade.find("DO $wp10_3_downgrade_guard$")
    destructive = min(
        (
            index
            for index in (downgrade.find("DROP FUNCTION"), downgrade.find("DROP TABLE"))
            if index >= 0
        ),
        default=-1,
    )
    rebuild_validation = migration.find(
        "SELECT ops._wp10_3_rebuild_inputs_valid() INTO v_inputs_valid;"
    )
    rebuild_replay_lookup = migration.find(
        "SELECT * INTO run_row FROM audit.publication_rebuild_runs"
    )
    guard_terms = (
        "public.claims",
        "public.evidence",
        "public.claim_evidence",
        "public.document_entities",
        "public.search_documents",
        "audit.claim_public_identities",
        "audit.evidence_public_identities",
        "audit.publication_rebuild_runs",
    )
    guard_hits = {term: term in downgrade for term in guard_terms}
    permission_terms = (
        "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA ops FROM uap_publisher",
        "GRANT EXECUTE ON FUNCTION ops.claim_publication_outbox",
        "GRANT EXECUTE ON FUNCTION ops.apply_publication_event",
        "GRANT EXECUTE ON FUNCTION ops.fail_publication_event",
        "GRANT EXECUTE ON FUNCTION ops.rebuild_public_projection(uuid) TO uap_migrator",
        "REVOKE ALL ON FUNCTION ops.rebuild_public_projection(uuid)",
        "REVOKE INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public FROM uap_publisher",
    )
    probe_paths = (
        platform / "tools/wp10_3_runtime_probe.py",
        platform / "tools/wp10_3_migration_probe.py",
    )
    probe = "\n".join(path.read_text(encoding="utf-8") for path in probe_paths if path.is_file())
    start_path = repository / "docs/wp10/implementation-start-wp10.3.md"
    start = start_path.read_text(encoding="utf-8") if start_path.is_file() else ""
    projection_path = repository / "docs/wp10/projection-contract.md"
    projection = projection_path.read_text(encoding="utf-8")

    return [
        check(
            "single WP10.3-or-linear-successor head",
            heads in ([CURRENT_HEAD], [WP10_4_HEAD], [WP10_5_HEAD]),
            heads,
            [[CURRENT_HEAD], [WP10_4_HEAD], [WP10_5_HEAD]],
        ),
        check(
            "WP10.3 parent",
            revision is not None and revision.down_revision == PARENT_HEAD,
            revision.down_revision if revision else None,
            PARENT_HEAD,
        ),
        check("WP10.3 migration objects", all(required_hits.values()), required_hits),
        check("WP10.3 forbidden scope absent", not forbidden_hits, forbidden_hits, []),
        check("WP10.3 downgrade categories", all(guard_hits.values()), guard_hits),
        check(
            "WP10.3 guard precedes destructive DDL",
            guard >= 0 and destructive >= 0 and guard < destructive,
            (guard, destructive),
        ),
        check(
            "rebuild validates before replay lookup",
            rebuild_validation >= 0
            and rebuild_replay_lookup >= 0
            and rebuild_validation < rebuild_replay_lookup,
            (rebuild_validation, rebuild_replay_lookup),
        ),
        check(
            "WP10.3 least privilege",
            all(token in migration for token in permission_terms),
            True,
        ),
        check(
            "G10-11 through G10-15 probes",
            all(
                token in probe
                for token in (
                    "G10-11",
                    "G10-12",
                    "G10-13",
                    "G10-14",
                    "G10-15",
                    "publication_dependency_not_ready",
                    "publication_rebuild_id_conflict",
                    "publication_rebuild_mismatch",
                    "independent_downgrade_guard",
                    "legacy_v1_quarantine_with_v2_rebuild",
                    "input_digest_after_second_quarantine",
                )
            ),
            True,
        ),
        check(
            "signed WP10.3 start and stop line",
            all(
                token in start
                for token in (
                    "2718ba2dc8f6773e088f5cfb8cfe90d5070aa7ae",
                    "codex/wp10.3",
                    "不签署",
                    "不进入 WP10.4",
                    "不 push",
                )
            ),
            start_path.as_posix(),
        ),
        check(
            "frozen search contract retained",
            "search_vector=to_tsvector('simple',display_text)" in projection
            and "facets 只含 `{category,fact_status,source_name}`" in projection,
            projection_path.as_posix(),
        ),
        check(
            "WP10.4 successor is stage-coherent",
            (
                heads == [CURRENT_HEAD]
                and not (platform / "alembic/versions/0023_wp10_api_read_indexes.py").exists()
            )
            or (
                heads == [WP10_4_HEAD]
                and (platform / "alembic/versions/0023_wp10_api_read_indexes.py").is_file()
            )
            or (
                heads == [WP10_5_HEAD]
                and (platform / "alembic/versions/0023_wp10_api_read_indexes.py").is_file()
                and (platform / "alembic/versions/0024_wp10_admin_replay.py").is_file()
            ),
            heads,
        ),
    ]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("platform", nargs="?", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    checks = evaluate(Path(args.platform))
    print(json.dumps([asdict(item) for item in checks], ensure_ascii=False, indent=2))
    failed = [item.name for item in checks if not item.passed]
    if failed:
        raise SystemExit("WP10.3 validation failed: " + ", ".join(failed))
    print("WP10.3 static contract validation passed; run migration/runtime probes.")


if __name__ == "__main__":
    main()
