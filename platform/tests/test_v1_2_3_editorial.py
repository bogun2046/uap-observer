"""V1-2.3 editorial evidence/provenance contract checks."""

from pathlib import Path

ROOT = Path(__file__).parents[1]


def _migration() -> str:
    return (ROOT / "alembic/versions/0029_v123_editorial_claim_entity_provenance.py").read_text(
        encoding="utf-8"
    )


def test_v123_reuses_canonical_evidence_spans() -> None:
    text = _migration()
    assert 'revision = "0029_v123_editorial_claim_entity_provenance"' in text
    assert "core.evidence_spans" in text
    assert "core.compute_evidence_locator_sha256" in text
    assert "materialize_editorial_evidence" in text
    assert "CREATE TABLE core.evidence" not in text


def test_v123_enforces_editorial_evidence_ownership_and_fail_close_codes() -> None:
    text = _migration()
    for code in (
        "editorial_evidence_not_found",
        "editorial_evidence_version_mismatch",
        "editorial_evidence_extraction_mismatch",
        "editorial_evidence_malformed_uuid",
        "editorial_evidence_provenance_mismatch",
    ):
        assert code in text
    assert "editorial_revisions_evidence_ownership" in text
    assert "session_user IS DISTINCT FROM 'uap_api'" in text


def test_ai_adoption_does_not_drop_raw_evidence_in_ui() -> None:
    html = (ROOT / "src/uap_platform/v1/library.html").read_text(encoding="utf-8")
    assert "item_ordinal" in html
    assert "Remove Evidence" in html
    assert "editorialCandidate" not in html
    assert "evidence_span_ids: candidate.evidence_span_ids || []" not in html


def test_admin_contract_exposes_structured_claim_entity_mutations() -> None:
    contracts = (ROOT / "src/uap_platform/admin_api/contracts.py").read_text(encoding="utf-8")
    handler = (ROOT / "src/uap_platform/admin_api/handler.py").read_text(encoding="utf-8")
    service = (ROOT / "src/uap_platform/admin_api/service.py").read_text(encoding="utf-8")
    for token in (
        "EditorialClaimMutationRequest",
        "EditorialEntityMutationRequest",
        "item_ordinal",
    ):
        assert token in contracts
    for token in ("mutate_editorial_claim", "mutate_editorial_entity", "remove_evidence"):
        assert token in handler and token in service


def test_v123_does_not_change_frozen_model_or_provider_files() -> None:
    assert (
        not (ROOT / "alembic/versions/0029_v123_editorial_claim_entity_provenance.py")
        .read_text(encoding="utf-8")
        .__contains__("model.v1")
    )
