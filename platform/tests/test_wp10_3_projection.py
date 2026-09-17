"""Static WP10.3 claim/search/rebuild contract tests."""

from __future__ import annotations

from pathlib import Path

from tools.validate_wp10_3 import evaluate


def platform_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_wp10_3_static_contract() -> None:
    assert [item.name for item in evaluate(platform_root()) if not item.passed] == []


def test_wp10_3_is_linear_and_does_not_open_relation_or_http() -> None:
    source = (platform_root() / "alembic/versions/0022_wp10_claim_search_projection.py").read_text(
        encoding="utf-8"
    )
    assert 'down_revision = "0021_wp10_publisher_projection"' in source
    assert "INSERT INTO public.relations" not in source
    assert "INSERT INTO public.relation_evidence" not in source
    assert "INSERT INTO audit.relation_publication_grants" not in source
    assert "fastapi" not in source.lower()
    assert "uvicorn" not in source.lower()


def test_wp10_3_entity_links_dedupe_shared_subjects() -> None:
    source = (platform_root() / "alembic/versions/0022_wp10_claim_search_projection.py").read_text(
        encoding="utf-8"
    )
    assert "SELECT DISTINCT ON (claim.document_id, entity.id)" in source
    assert "ORDER BY claim.document_id, entity.id, claim.ordinal, claim.id" in source


def test_wp10_3_rebuild_validates_complete_current_manifests_before_replay() -> None:
    source = (platform_root() / "alembic/versions/0022_wp10_claim_search_projection.py").read_text(
        encoding="utf-8"
    )
    assert "CREATE FUNCTION ops._wp10_3_rebuild_inputs_valid()" in source
    assert source.count("manifest.grant_id IS NULL") >= 3
    validation = source.find("SELECT ops._wp10_3_rebuild_inputs_valid() INTO v_inputs_valid;")
    replay_lookup = source.find("SELECT * INTO run_row FROM audit.publication_rebuild_runs")
    assert validation >= 0 and validation < replay_lookup


def test_wp10_3_rebuild_excludes_unresolved_legacy_quarantine() -> None:
    source = (platform_root() / "alembic/versions/0022_wp10_claim_search_projection.py").read_text(
        encoding="utf-8"
    )
    probe = (platform_root() / "tools/wp10_3_migration_probe.py").read_text(encoding="utf-8")
    assert "CREATE FUNCTION ops._wp10_3_grant_has_unresolved_quarantine" in source
    assert source.count("NOT ops._wp10_3_grant_has_unresolved_quarantine(") >= 12
    assert source.count("manifest.grant_id IS NOT NULL") >= 3
    assert "legacy_v1_quarantine_with_v2_rebuild" in probe
    assert "input_digest_after_second_quarantine" in probe


def test_wp10_3_publisher_keeps_dedicated_protocol() -> None:
    package = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (platform_root() / "src/uap_platform/publishing").glob("*.py")
    )
    assert "ops.claim_publication_outbox" in package
    assert "ops.apply_publication_event" in package
    assert "ops.fail_publication_event" in package
    assert "ops.ack_outbox" not in package


def test_wp10_3_search_text_is_manifest_projection_only() -> None:
    source = (platform_root() / "alembic/versions/0022_wp10_claim_search_projection.py").read_text(
        encoding="utf-8"
    )
    refresh = source[source.find("CREATE FUNCTION ops._wp10_3_refresh_search") :]
    refresh = refresh[: refresh.find("CREATE FUNCTION", 20)]
    assert "document.title" in refresh
    assert "document.summary" in refresh
    assert "claim.claim_text" in refresh
    assert "evidence.excerpt" not in refresh
    assert "canonical_source_url" not in refresh
    assert "public_locator" not in refresh
