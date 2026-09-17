"""Small static contract checks for the V1-2 Internal Library presentation layer."""

from __future__ import annotations

from pathlib import Path


def library_html() -> str:
    return (Path(__file__).parents[1] / "src/uap_platform/v1/library.html").read_text(
        encoding="utf-8"
    )


def test_internal_library_has_frozen_views_and_actions() -> None:
    html = library_html()
    for label in (
        "Internal Library",
        "Recycle Bin",
        "SOURCE / RAW",
        "CURRENT EDITORIAL",
        "AI RESULTS",
        "AUDIT",
        "Save",
        "Cancel",
        "Reload latest",
        "Move to Trash",
        "Restore",
        "Adopt AI Summary",
        "Adopt AI Claims",
        "Adopt AI Entities",
    ):
        assert label in html
    for task in ("classification", "summary", "claim_extraction", "entity_extraction"):
        assert task in html
    assert "task_type:task" in html
    assert "/admin/v1/documents/" in html
    assert "expected_revision" in html
    assert "Idempotency-Key" in html


def test_ui_uses_in_memory_oidc_and_escapes_untrusted_content() -> None:
    html = library_html()
    assert "credentials: 'include'" in html
    assert "Bearer ${state.token}" in html
    assert "localStorage" not in html
    assert "UAP_V1_LOCAL_ADMIN_TOKEN" not in html
    assert "innerHTML" not in html
    assert "textContent" in html
    assert "未保存的输入仍保留" in html


def test_ui_preserves_patch_and_lifecycle_contracts() -> None:
    html = library_html()
    assert "state.changed.has('title')" in html
    assert "state.changed.has('claims')" in html
    assert "state.changed.has('entities')" in html
    assert "editorial/adopt" in html
    assert "documents/trash" in html
    assert "documents/${state.document.document_id}/restore" in html
    assert "public_authorized=false" in html
