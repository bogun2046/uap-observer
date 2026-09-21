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
        "REVISION HISTORY",
        "View read-only",
        "Restore as New Revision",
        "HISTORICAL REVISION",
        "Save",
        "Cancel",
        "Reload latest",
        "Move to Trash",
        "Restore",
        "Adopt AI Summary",
        "Adopt AI Claims",
        "Adopt AI Entities",
        "Adopt claim #",
        "Adopt entity #",
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


def test_ui_uses_admin_evidence_limit() -> None:
    html = library_html()
    assert (
        "evidence-spans?document_version_id=${encodeURIComponent(document.document_version_id)}&limit=100"
        in html
    )
    assert (
        "evidence-spans?document_version_id=${encodeURIComponent(document.document_version_id)}&limit=200"
        not in html
    )


def test_editorial_claim_entity_mutations_use_dom_forms() -> None:
    html = library_html()
    assert "role', 'dialog'" in html
    assert "modal-claim" in html
    assert "modal-source_statement" in html
    assert "modal-claim_type" in html
    assert "modal-assertion_status" in html
    assert "modal-name" in html
    assert "modal-entity_type" in html
    assert "modal-aliases" in html
    assert "editorial/${kind}/${value.claim_id || value.entity_id}" in html
    assert "expected_revision:revision()" in html
    assert "evidence_span_ids: manual ? [] : (value.evidence_span_ids || [])" in html
    assert "prompt(" not in html
    assert "confirm(" not in html
