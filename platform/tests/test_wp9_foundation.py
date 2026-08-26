"""Static WP9.1 freeze checks."""

from __future__ import annotations

from pathlib import Path

from tools.validate_wp9 import evaluate


def platform_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_wp9_1_static_contract() -> None:
    failed = [item.name for item in evaluate(platform_root()) if not item.passed]
    assert failed == []


def test_wp9_1_does_not_open_later_stages() -> None:
    migration = (
        platform_root() / "alembic/versions/0014_review_session_authority.py"
    ).read_text(encoding="utf-8")
    probe = (platform_root() / "tools/wp9_1_runtime_probe.py").read_text(encoding="utf-8")
    assert "CREATE FUNCTION audit.open_review_case" not in migration
    assert "CREATE FUNCTION audit.record_review_decision" not in migration
    assert "CREATE TABLE" not in migration
    assert "open_review_case" not in probe
    orchestrator = (platform_root() / "tools/wp8_runtime_probe.py").read_text(encoding="utf-8")
    assert "wp9_1_runtime_probe.py" not in orchestrator
    assert "wp9_2_runtime_probe.py" not in orchestrator


def test_wp9_2_does_not_open_decision_stage() -> None:
    migration = (
        platform_root() / "alembic/versions/0015_review_case_lifecycle.py"
    ).read_text(encoding="utf-8")
    probe = (platform_root() / "tools/wp9_2_runtime_probe.py").read_text(encoding="utf-8")
    assert "CREATE FUNCTION audit.open_review_case" in migration
    assert "CREATE FUNCTION audit.assign_review_case" in migration
    assert "CREATE FUNCTION audit.close_review_case" in migration
    assert "CREATE FUNCTION audit.record_review_decision" not in migration
    assert "CREATE TABLE" not in migration
    assert "record_review_decision" not in probe
    assert "g9_06" in probe and "g9_34" in probe
    assert "CREATE FUNCTION audit._canonical_json" in migration
    assert "p_payload::text" not in migration
    assert "CREATE FUNCTION audit._canonical_json_number" in migration
    assert "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777" in probe
    assert "2c39cedbb91a51d5591b068931c00b4204cf539bed72ca2508566841726a5022" in probe
    assert "d4e22924ae5b055f946dfeea48d109a17a5aa86b2edbc2340fcdb5361c19ed90" in probe


def test_wp9_3_does_not_open_later_stages() -> None:
    migration = (
        platform_root() / "alembic/versions/0016_review_decisions_and_grants.py"
    ).read_text(encoding="utf-8")
    probe = (platform_root() / "tools/wp9_3_runtime_probe.py").read_text(encoding="utf-8")
    assert "CREATE FUNCTION audit.record_review_decision" in migration
    assert "CREATE FUNCTION ops.enqueue_publication_outbox" in migration
    assert "uq_document_grant_live" in migration
    assert "review_grant_superseded_blocks_downgrade" in migration
    assert "CREATE FUNCTION audit.select_analysis_result" not in migration
    assert "CREATE FUNCTION audit.create_manual_claim" not in migration
    assert "CREATE TABLE" not in migration
    assert "enqueue_job" not in migration
    assert "select_analysis_result" not in probe
    assert "create_manual_claim" not in probe
    assert "g9_10" in probe and "g9_28" in probe and "g9_35" in probe
    assert migration.count("_existing_write_target") >= 2
    assert "extra_concurrent_same_request" in probe
    assert "sqlite-libs>=3.53.4-r0" in (
        platform_root() / "Dockerfile"
    ).read_text(encoding="utf-8")


def test_wp9_4_does_not_open_later_stages() -> None:
    migration = (
        platform_root() / "alembic/versions/0017_selection_and_promotion.py"
    ).read_text(encoding="utf-8")
    probe = (platform_root() / "tools/wp9_4_runtime_probe.py").read_text(encoding="utf-8")
    assert "CREATE FUNCTION audit.select_analysis_result" in migration
    assert "CREATE FUNCTION audit.accept_entity_candidate" in migration
    assert "CREATE FUNCTION audit.bind_entity_candidate" in migration
    assert "CREATE FUNCTION audit.apply_entity_merge" not in migration
    assert "CREATE FUNCTION audit.create_manual_claim" not in migration
    assert "CREATE TABLE" not in migration
    assert "enqueue_job" not in migration
    assert "core.merge_entities" not in migration
    assert "apply_entity_merge" not in probe
    assert "create_manual_claim" not in probe
    assert "g9_17" in probe and "g9_19" in probe and "g9_36" in probe
    assert migration.count("_existing_write_target") >= 9
    assert migration.count("pg_advisory_xact_lock(9175, hashtext(v_key))") >= 3
    assert "extra_concurrent_same_request" in probe
    assert "extra_concurrent_cross_resource" in probe
    assert "EVENT_KEY_LOCK_CLASS = 9175" in probe
    assert "sqlite-libs>=3.53.4-r0" in (
        platform_root() / "Dockerfile"
    ).read_text(encoding="utf-8")
