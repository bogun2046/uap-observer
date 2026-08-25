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
    assert "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777" in probe
