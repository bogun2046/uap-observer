"""Focused V1-3.3 UI/status wiring contracts."""

from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]


def test_post_publication_withdraw_action_is_available_for_active_publication() -> None:
    source = (PLATFORM / "src/uap_platform/v1/library.html").read_text(encoding="utf-8")
    assert "publication.grant_status === 'active'" in source
    assert "publication.public_visible ? 'Withdraw publication' : 'Revoke authorization'" in source


def test_publication_status_exposes_withdrawn_and_projection_error_states() -> None:
    source = (PLATFORM / "src/uap_platform/admin_api/service.py").read_text(encoding="utf-8")
    assert "item.event_type" in source
    assert 'status = "WITHDRAW_PENDING"' in source
    assert 'status = "WITHDRAWN"' in source
    assert 'status = "PROJECTION_ERROR"' in source
