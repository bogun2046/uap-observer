"""Static WP9 freeze checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.validate_wp9 import evaluate


def platform_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_wp9_1_static_contract() -> None:
    failed = [item.name for item in evaluate(platform_root()) if not item.passed]
    assert failed == []


def test_wp9_1_does_not_open_later_stages() -> None:
    migration = (platform_root() / "alembic/versions/0014_review_session_authority.py").read_text(
        encoding="utf-8"
    )
    probe = (platform_root() / "tools/wp9_1_runtime_probe.py").read_text(encoding="utf-8")
    assert "CREATE FUNCTION audit.open_review_case" not in migration
    assert "CREATE FUNCTION audit.record_review_decision" not in migration
    assert "CREATE TABLE" not in migration
    assert "open_review_case" not in probe
    orchestrator = (platform_root() / "tools/wp8_runtime_probe.py").read_text(encoding="utf-8")
    assert "wp9_1_runtime_probe.py" not in orchestrator
    assert "wp9_2_runtime_probe.py" not in orchestrator


def test_wp9_2_does_not_open_decision_stage() -> None:
    migration = (platform_root() / "alembic/versions/0015_review_case_lifecycle.py").read_text(
        encoding="utf-8"
    )
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
    dockerfile = (platform_root() / "Dockerfile").read_text(encoding="utf-8")
    assert "sqlite-libs>=3.53.4-r0" in dockerfile
    assert "libcrypto3>=3.5.8-r0" in dockerfile
    assert "libssl3>=3.5.8-r0" in dockerfile


def test_wp9_4_does_not_open_later_stages() -> None:
    migration = (platform_root() / "alembic/versions/0017_selection_and_promotion.py").read_text(
        encoding="utf-8"
    )
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
    dockerfile = (platform_root() / "Dockerfile").read_text(encoding="utf-8")
    assert "sqlite-libs>=3.53.4-r0" in dockerfile
    assert "libcrypto3>=3.5.8-r0" in dockerfile
    assert "libssl3>=3.5.8-r0" in dockerfile


def test_wp9_5_does_not_open_later_stages() -> None:
    migration = (platform_root() / "alembic/versions/0018_authorized_entity_merge.py").read_text(
        encoding="utf-8"
    )
    probe = (platform_root() / "tools/wp9_5_runtime_probe.py").read_text(encoding="utf-8")
    assert "CREATE FUNCTION audit.apply_entity_merge" in migration
    assert "CREATE FUNCTION audit.apply_entity_merge_reverse" in migration
    assert "CREATE FUNCTION audit._require_openable_entity_subject" in migration
    assert "CREATE FUNCTION audit.create_manual_claim" not in migration
    assert "CREATE TABLE" not in migration
    assert "enqueue_job" not in migration
    assert "GRANT EXECUTE ON FUNCTION core.merge_entities" not in migration
    assert "GRANT EXECUTE ON FUNCTION core.reverse_entity_merge" not in migration
    assert "create_manual_claim" not in probe
    assert "g9_20" in probe and "g9_21" in probe and "g9_22" in probe and "g9_37" in probe
    assert "extra_concurrent_same_request" in probe
    assert "extra_concurrent_cross_resource" in probe
    assert "g8_16c" not in probe
    assert "WP9.5 runtime probe passed: G9-20 G9-21 G9-22 G9-37" in probe
    assert migration.count("pg_advisory_xact_lock(9175, hashtext(v_key))") >= 2
    assert "EVENT_KEY_LOCK_CLASS = 9175" in probe
    assert probe.count("LIKE 'publish_%%'") == 2
    assert "LIKE 'publish_%'" not in probe.replace("LIKE 'publish_%%'", "")
    dockerfile = (platform_root() / "Dockerfile").read_text(encoding="utf-8")
    assert "sqlite-libs>=3.53.4-r0" in dockerfile
    assert "libcrypto3>=3.5.8-r0" in dockerfile
    assert "libssl3>=3.5.8-r0" in dockerfile


def test_wp9_5_probe_escapes_like_percent_for_psycopg() -> None:
    """Psycopg treats a lone % in SQL as a placeholder. LIKE wildcards must be %%."""

    from psycopg import ProgrammingError
    from psycopg._queries import _split_query

    probe = (platform_root() / "tools/wp9_5_runtime_probe.py").read_text(encoding="utf-8")
    assert probe.count("LIKE 'publish_%%'") == 2
    assert "LIKE 'publish_%'" not in probe.replace("LIKE 'publish_%%'", "")

    broken = b"SELECT count(*) FROM ops.jobs WHERE job_type LIKE 'publish_%'"
    with pytest.raises(ProgrammingError, match=r"only '%s', '%b', '%t' are allowed"):
        _split_query(broken)

    escaped = b"SELECT count(*) FROM ops.jobs WHERE job_type LIKE 'publish_%%'"
    parts = _split_query(escaped)
    rendered = b"".join(part.pre for part in parts)
    assert b"LIKE 'publish_%'" in rendered
    assert b"%%" not in rendered


def test_wp9_6_manual_claims_contract() -> None:
    migration = (
        platform_root() / "alembic/versions/0019_manual_claims_and_subject_binding.py"
    ).read_text(encoding="utf-8")
    probe = (platform_root() / "tools/wp9_6_runtime_probe.py").read_text(encoding="utf-8")
    orchestrator = (platform_root() / "tools/wp9_runtime_probe.py").read_text(encoding="utf-8")
    assert 'down_revision = "0018_authorized_entity_merge"' in migration
    assert "CREATE FUNCTION audit.create_manual_claim" in migration
    assert "CREATE FUNCTION core.require_manual_claim_supports" in migration
    assert "CREATE FUNCTION audit._apply_claim_subject_bind" in migration
    assert "CREATE FUNCTION audit._replace_claim_evidence" in migration
    replace_start = migration.find("CREATE FUNCTION audit._replace_claim_evidence")
    replace_end = migration.find("$_replace_claim_evidence$;")
    replace_body = migration[replace_start:replace_end]
    assert replace_start > 0 and replace_end > replace_start
    assert "DELETE FROM core.claim_evidence WHERE claim_id = v_claim;" not in replace_body
    assert "AND support_type = 'supports'::core.support_type" in replace_body
    assert "CREATE FUNCTION audit._retire_manual_claim_supports" in migration
    assert "CREATE OR REPLACE FUNCTION audit.record_review_decision" in migration
    assert "require_ai_claim_supports" not in migration
    assert "bind_claim_subject_entity" not in migration
    assert "CREATE TABLE" not in migration
    assert "enqueue_job" not in migration
    assert "GRANT EXECUTE ON FUNCTION audit._apply_claim_subject_bind" not in migration
    assert "g9_23" in probe and "g9_24" in probe and "g9_25" in probe
    assert "g9_32" in probe and "g9_33" in probe and "g9_38" in probe
    assert "g9_replace_preserves_nonsupport" in probe
    assert "g8_16c" not in probe
    assert "WP9.6 runtime probe passed: G9-23 G9-24 G9-25 G9-32 G9-33 G9-38" in probe
    assert "wp9_6_runtime_probe.py" in orchestrator
    dockerfile = (platform_root() / "Dockerfile").read_text(encoding="utf-8")
    assert "sqlite-libs>=3.53.4-r0" in dockerfile
    assert "libcrypto3>=3.5.8-r0" in dockerfile
    assert "libssl3>=3.5.8-r0" in dockerfile
