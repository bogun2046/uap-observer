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
    assert "wp9_2" not in probe
    assert "open_review_case" not in probe
    orchestrator = (platform_root() / "tools/wp8_runtime_probe.py").read_text(encoding="utf-8")
    assert "wp9_1_runtime_probe.py" not in orchestrator
