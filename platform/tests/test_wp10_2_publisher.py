"""Static WP10.2 document/entity Publisher checks."""

from __future__ import annotations

from pathlib import Path

from tools.validate_wp10_2 import evaluate


def platform_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_wp10_2_static_contract() -> None:
    failed = [item.name for item in evaluate(platform_root()) if not item.passed]
    assert failed == []


def test_wp10_2_does_not_open_claim_search_or_http() -> None:
    migration = (platform_root() / "alembic/versions/0021_wp10_publisher_projection.py").read_text(
        encoding="utf-8"
    )
    assert "INSERT INTO public.claims" not in migration
    assert "INSERT INTO public.evidence" not in migration
    assert "INSERT INTO public.document_entities" not in migration
    assert "public.document_entities" not in migration
    assert "public.search_documents" not in migration
    assert "ops.rebuild_public_projection" not in migration
    assert "fastapi" not in migration.lower()
    assert "uvicorn" not in migration.lower()
    assert "core.canonical_entity_id(v_manifest_subject_id)" in migration
    assert "v_entity_status = 'active'::core.entity_status" in migration
    assert "p_error_summary IS DISTINCT FROM v_summary" in migration
    assert "publication_failure_parameters_invalid" in migration
    assert "relation_publication_grants" in migration
    assert "terminal_error_code = 'publication_event_schema_unsupported'" in migration
    fail = migration[migration.find("CREATE FUNCTION ops.fail_publication_event") :]
    assert fail.find("FROM ops.outbox_events") < fail.find(
        "p_error_summary IS DISTINCT FROM v_summary"
    )


def test_wp10_2_publisher_cannot_use_generic_ack() -> None:
    package = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (platform_root() / "src/uap_platform/publishing").glob("*.py")
    )
    assert "ops.ack_outbox" not in package
    assert "ops.claim_publication_outbox" in package
    assert "ops.apply_publication_event" in package
    assert "ops.fail_publication_event" in package
