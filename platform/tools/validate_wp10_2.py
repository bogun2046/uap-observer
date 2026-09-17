"""Validate the WP10.2 document/entity Publisher contract."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

CURRENT_HEAD = "0021_wp10_publisher_projection"
WP10_3_HEAD = "0022_wp10_claim_search_projection"
WP10_4_HEAD = "0023_wp10_api_read_indexes"
WP10_5_HEAD = "0024_wp10_admin_replay"
PARENT_HEAD = "0020_wp10_publication_contract"
MIGRATION = "alembic/versions/0021_wp10_publisher_projection.py"
WP10_2_RANGE = "G10-06" + chr(0x2013) + "G10-10"


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    actual: object
    expected: object


def check(name: str, passed: bool, actual: object, expected: object = True) -> Check:
    return Check(name, passed, actual, expected)


def _ordered(source: str, first: str, second: str) -> bool:
    left = source.find(first)
    right = source.find(second)
    return left >= 0 and right >= 0 and left < right


def evaluate(platform: Path) -> list[Check]:
    platform = platform.resolve()
    repository = platform.parent
    migration_path = platform / MIGRATION
    migration = migration_path.read_text(encoding="utf-8") if migration_path.is_file() else ""
    config = Config(str(platform / "alembic.ini"))
    config.set_main_option("script_location", str(platform / "alembic"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    revision = script.get_revision(CURRENT_HEAD)
    revision_ids = {item.revision for item in script.walk_revisions(base="base", head="heads")}

    forbidden = (
        "CREATE FUNCTION ops.rebuild_public_projection",
        "CREATE FUNCTION audit.requeue_publication_event",
        "CREATE FUNCTION ops.apply_relation",
        "CREATE TABLE audit.publication_rebuild_runs",
        "public.search_documents",
        "public.document_entities",
        "INSERT INTO public.claims",
        "INSERT INTO public.evidence",
        "fastapi",
        "uvicorn",
    )
    forbidden_hits = [token for token in forbidden if token.lower() in migration.lower()]
    required_migration = (
        "CREATE TABLE audit.publication_delivery_attempts",
        "CREATE FUNCTION ops.claim_publication_outbox",
        "CREATE FUNCTION ops.apply_publication_event",
        "CREATE FUNCTION ops.fail_publication_event",
        "operation_payload_hash",
        "lease_token_hash",
        "publication_delivery_attempts_immutable",
        "publication_delivery_attempt_conflict",
        "publication_lease_lost",
        "publication_event_schema_unsupported",
        "relation_publication_grants",
        "terminal_error_code = 'publication_event_schema_unsupported'",
        "publication-outbox.v2",
        "publication-document:",
        "publication-entity:",
        "v_entity_is_active_canonical",
        "core.canonical_entity_id(v_manifest_subject_id)",
        "FROM core.entities AS entity",
        "p_error_summary IS DISTINCT FROM v_summary",
        "publication_failure_parameters_invalid",
        "INSERT INTO public.documents",
        "INSERT INTO public.entities",
        "PERFORM ops._publication_inject_failure('attempt')",
        "PERFORM ops._publication_inject_failure('identity')",
        "PERFORM ops._publication_inject_failure('public_upsert')",
        "PERFORM ops._publication_inject_failure('deferred_constraint')",
        "PERFORM ops._publication_inject_failure('ack')",
    )
    migration_hits = {token: token in migration for token in required_migration}
    fail_start = migration.find("CREATE FUNCTION ops.fail_publication_event")
    fail_function = migration[fail_start:] if fail_start >= 0 else ""
    fail_lease_lookup = fail_function.find("FROM ops.outbox_events")
    fail_summary_validation = fail_function.find("p_error_summary IS DISTINCT FROM v_summary")
    downgrade_start = migration.find("def downgrade")
    downgrade = migration[downgrade_start:] if downgrade_start >= 0 else ""
    first_destructive = min(
        (
            position
            for position in (downgrade.find("DROP TABLE"), downgrade.find("DROP FUNCTION"))
            if position >= 0
        ),
        default=-1,
    )
    guard = downgrade.find("DO $wp10_2_downgrade_guard$")
    guard_terms = (
        "public.documents",
        "public.entities",
        "audit.document_public_identities",
        "audit.entity_public_identities",
        "audit.publication_delivery_attempts",
        "event.event_type LIKE 'publication.%'",
        "event.published_at IS NOT NULL",
        "event.terminal_at IS NOT NULL",
        "event.publish_attempts > 0",
        "event.last_error_code IS NOT NULL",
        "event.lease_token IS NOT NULL",
        "publication_contract_state_blocks_downgrade",
    )
    guard_hits = {term: term in downgrade for term in guard_terms}
    downgrade_outbox_scope = (
        "FROM ops.outbox_events AS event" in downgrade
        and "event.event_type LIKE 'publication.%'" in downgrade
        and "event.publish_attempts > 0" in downgrade
    )

    service_paths = (
        repository / "platform/src/uap_platform/publishing/service.py",
        repository / "platform/src/uap_platform/publishing/loop.py",
        repository / "platform/tools/wp10_2_runtime_probe.py",
        repository / "platform/tools/wp10_2_migration_probe.py",
        repository / "platform/tests/test_wp10_2_publisher.py",
    )
    service_text = "\n".join(
        path.read_text(encoding="utf-8") for path in service_paths[:2] if path.is_file()
    )
    probe_text = "\n".join(
        path.read_text(encoding="utf-8") for path in service_paths[2:4] if path.is_file()
    )
    migration_probe_path = repository / "platform/tools/wp10_2_migration_probe.py"
    migration_probe = (
        migration_probe_path.read_text(encoding="utf-8") if migration_probe_path.is_file() else ""
    )
    projection_contract_path = repository / "docs/wp10/projection-contract.md"
    projection_contract = (
        projection_contract_path.read_text(encoding="utf-8")
        if projection_contract_path.is_file()
        else ""
    )
    start_path = repository / "docs/wp10/implementation-start-wp10.2.md"
    start_text = start_path.read_text(encoding="utf-8") if start_path.is_file() else ""
    runtime_path = repository / "docs/wp10/runtime-validation.md"
    runtime_text = runtime_path.read_text(encoding="utf-8") if runtime_path.is_file() else ""
    validation_record_path = repository / "docs/wp10/validation-results-wp10.2-20260828.md"
    validation_record = (
        validation_record_path.read_text(encoding="utf-8")
        if validation_record_path.is_file()
        else ""
    )
    migration_plan_path = repository / "docs/wp10/migration-plan.md"
    migration_plan = (
        migration_plan_path.read_text(encoding="utf-8") if migration_plan_path.is_file() else ""
    )
    frozen_downgrade_rule = (
        "Downgrade"
        + chr(0xFF1A)
        + "public document/entity 行、identity、delivery-attempt、"
        + "terminal/retry event 任一存在即拒绝。"
    )
    frozen_relation_rule = (
        "Publisher 遇到 relation aggregate 必须 terminal "
        + "`publication_event_schema_unsupported`"
        + chr(0xFF0C)
        + "不能 no-op ack 成功。"
    )

    return [
        check(
            "single WP10.2-or-linear-successor head",
            heads in ([CURRENT_HEAD], [WP10_3_HEAD], [WP10_4_HEAD], [WP10_5_HEAD]),
            heads,
            [[CURRENT_HEAD], [WP10_3_HEAD], [WP10_4_HEAD], [WP10_5_HEAD]],
        ),
        check(
            "WP10.2 parent",
            revision is not None and revision.down_revision == PARENT_HEAD,
            revision.down_revision if revision else None,
            PARENT_HEAD,
        ),
        check(
            "linear chain contains parent",
            PARENT_HEAD in revision_ids,
            PARENT_HEAD in revision_ids,
        ),
        check("WP10.2 migration objects", all(migration_hits.values()), migration_hits),
        check(
            "fail validates lease before caller summary",
            fail_lease_lookup >= 0
            and fail_summary_validation >= 0
            and fail_lease_lookup < fail_summary_validation,
            (fail_lease_lookup, fail_summary_validation),
        ),
        check("only document/entity projection", forbidden_hits == [], forbidden_hits, []),
        check(
            "publisher-only execute grants",
            all(
                token in migration
                for token in (
                    "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA ops FROM uap_publisher",
                    "GRANT EXECUTE ON FUNCTION ops.claim_publication_outbox",
                    "GRANT EXECUTE ON FUNCTION ops.apply_publication_event",
                    "GRANT EXECUTE ON FUNCTION ops.fail_publication_event",
                    "FROM PUBLIC, uap_api, uap_worker",
                )
            ),
            True,
        ),
        check("downgrade guard covers state", all(guard_hits.values()), guard_hits),
        check(
            "downgrade scopes outbox state to publication events",
            downgrade_outbox_scope,
            downgrade_outbox_scope,
        ),
        check(
            "downgrade guard precedes destructive DDL",
            guard >= 0 and first_destructive >= 0 and guard < first_destructive,
            (guard, first_destructive),
        ),
        check(
            "Python service uses only dedicated functions",
            all(
                token in service_text
                for token in (
                    "ops.claim_publication_outbox",
                    "ops.apply_publication_event",
                    "ops.fail_publication_event",
                )
            )
            and "ack_outbox" not in service_text,
            True,
        ),
        check(
            "G10-06 to G10-10 probes present",
            all(
                token in probe_text
                for token in (
                    "G10-06",
                    "G10-07",
                    "G10-08",
                    "G10-09",
                    "G10-10",
                    "G10-09 relation is terminal",
                    "G10-09 relation terminal attempt recorded",
                )
            ),
            True,
        ),
        check(
            "signed WP10.2 start and stop line",
            all(
                token in start_text
                for token in (
                    "8b6ae4fc6e0b46e1cf724742ded6a3db70d7f3a5",
                    "codex/wp10.2",
                    "不签署 `G10-GATE-10.2`",
                    "不进入 WP10.3",
                    "不 push",
                )
            ),
            True,
        ),
        check(
            "frozen WP10.2 downgrade rule retained",
            frozen_downgrade_rule in migration_plan,
            frozen_downgrade_rule in migration_plan,
        ),
        check(
            "frozen projection relation rule retained",
            frozen_relation_rule in projection_contract,
            projection_contract_path.as_posix(),
        ),
        check(
            "terminal and retry event-only downgrade probes present",
            all(
                token in migration_probe
                for token in (
                    'event_only_guard(admin_url, "terminal")',
                    'event_only_guard(admin_url, "retry")',
                    '"delivery_attempt_rows_before": 0',
                    '"delivery_attempt_rows_after": 0',
                    '"event_state_unchanged": True',
                )
            ),
            True,
        ),
        check(
            "runtime instructions cover five gates",
            all(
                token in runtime_text
                for token in (
                    "wp10_2_migration_probe.py",
                    "wp10_2_runtime_probe.py",
                    "G10-06",
                    "G10-07",
                    "G10-08",
                    "G10-09",
                    "G10-10",
                )
            ),
            True,
        ),
        check(
            "WP10.2 validation record present",
            validation_record_path.is_file()
            and WP10_2_RANGE in validation_record
            and "SQLSTATE" in validation_record,
            validation_record_path.as_posix(),
        ),
        check(
            "WP10.3 successor is stage-coherent",
            (
                heads == [CURRENT_HEAD]
                and not (
                    repository / "platform/alembic/versions/0022_wp10_claim_search_projection.py"
                ).exists()
            )
            or (
                heads == [WP10_3_HEAD]
                and (
                    repository / "platform/alembic/versions/0022_wp10_claim_search_projection.py"
                ).is_file()
            )
            or (
                heads == [WP10_4_HEAD]
                and (
                    repository / "platform/alembic/versions/0022_wp10_claim_search_projection.py"
                ).is_file()
                and (
                    repository / "platform/alembic/versions/0023_wp10_api_read_indexes.py"
                ).is_file()
            )
            or (
                heads == [WP10_5_HEAD]
                and (
                    repository / "platform/alembic/versions/0022_wp10_claim_search_projection.py"
                ).is_file()
                and (
                    repository / "platform/alembic/versions/0023_wp10_api_read_indexes.py"
                ).is_file()
                and (repository / "platform/alembic/versions/0024_wp10_admin_replay.py").is_file()
            ),
            {"heads": heads, "repository": repository.as_posix()},
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
        raise SystemExit("WP10.2 validation failed: " + ", ".join(failed))
    print(
        "WP10.2 static contract validation passed; run both G10-06-G10-10 probes "
        "for runtime evidence."
    )


if __name__ == "__main__":
    main()
