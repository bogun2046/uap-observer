"""Static WP10.1 freeze checks."""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest

from tools import wp10_1_migration_probe, wp10_1_runtime_probe
from tools.validate_wp10_1 import evaluate


def platform_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_wp10_1_static_contract() -> None:
    failed = [
        item.name for item in evaluate(platform_root(), allow_later_head=True) if not item.passed
    ]
    assert failed == []


def test_wp10_1_does_not_open_publisher_or_http_stages() -> None:
    migration = (platform_root() / "alembic/versions/0020_wp10_publication_contract.py").read_text(
        encoding="utf-8"
    )
    assert "CREATE FUNCTION ops.claim_publication_outbox" not in migration
    assert "CREATE FUNCTION ops.apply_publication_event" not in migration
    assert "CREATE FUNCTION ops.fail_publication_event" not in migration
    assert "CREATE FUNCTION audit.requeue_publication_event" not in migration
    assert "CREATE TABLE audit.publication_delivery_attempts" not in migration
    assert "fastapi" not in migration.lower()
    assert "uvicorn" not in migration.lower()


def test_wp10_1_keeps_relations_closed() -> None:
    migration = (platform_root() / "alembic/versions/0020_wp10_publication_contract.py").read_text(
        encoding="utf-8"
    )
    assert "public.relations" in migration
    assert "public.relation_evidence" in migration
    assert "relation_publication_manifests" not in migration
    assert "CREATE FUNCTION ops.apply_relation" not in migration


def test_wp10_1_review_findings_are_closed_in_source() -> None:
    migration = (platform_root() / "alembic/versions/0020_wp10_publication_contract.py").read_text(
        encoding="utf-8"
    )
    assert "FOR UPDATE OF grant_row" in migration
    assert "AT TIME ZONE 'UTC'" in migration
    assert "CREATE FUNCTION audit._resolve_publication_quarantine" in migration
    assert "PERFORM audit._resolve_publication_quarantine(v_table, v_old)" in migration
    assert "resolved_by_v2_revision" in migration

    downgrade = migration[migration.find("def downgrade") :]
    assert "bind_subject_entity_id" in downgrade
    assert "replace_supporting_span_ids" in downgrade
    assert "retire_supporting_evidence" in downgrade
    assert "PERFORM audit._apply_claim_subject_bind" in downgrade
    assert "PERFORM audit._replace_claim_evidence" in downgrade
    assert "PERFORM audit._retire_manual_claim_supports" in downgrade
    downgrade_compact = " ".join(downgrade.split())
    assert (
        "IF v_type IS DISTINCT FROM 'claim'::audit.review_case_type THEN RAISE EXCEPTION "
        "'review_structured_changes_unsupported'"
    ) in downgrade_compact
    assert "v_type IS DISTINCT FROM 'document'::audit.review_case_type" not in downgrade_compact
    assert downgrade.index("PERFORM audit._apply_publication_grant(") < downgrade.index(
        "IF p_structured_changes ? 'bind_subject_entity_id'"
    )
    for signature in (
        "audit.open_review_case(",
        "audit.assign_review_case(uuid, uuid)",
        "audit.close_review_case(uuid, text)",
        "audit.select_analysis_result(uuid, text)",
        "audit.accept_entity_candidate(uuid, text)",
        "audit.bind_entity_candidate(uuid, uuid, text)",
        "audit.apply_entity_merge(uuid, uuid, text)",
        "audit.apply_entity_merge_reverse(uuid, text)",
        "audit.create_manual_claim(",
    ):
        assert signature in downgrade_compact


def test_wp10_1_runtime_evidence_is_not_claimed_by_static_validator() -> None:
    migration_probe = platform_root() / "tools/wp10_1_migration_probe.py"
    validation_record = platform_root().parent / "docs/wp10/validation-results-20260828.md"
    assert migration_probe.is_file()
    assert "G10-05" in migration_probe.read_text(encoding="utf-8")
    assert "publication_document_grant_required" in migration_probe.read_text(encoding="utf-8")
    assert "246 passed" in validation_record.read_text(encoding="utf-8")
    assert "runtime evidence" in (platform_root() / "tools/validate_wp10_1.py").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    "probe_module",
    [wp10_1_migration_probe, wp10_1_runtime_probe],
)
def test_wp10_1_evidence_write_is_atomic(tmp_path: Path, probe_module: ModuleType) -> None:
    output = tmp_path / "evidence.json"
    probe_module.atomic_write_json(output, {"status": "passed"})
    assert json.loads(output.read_text(encoding="utf-8")) == {"status": "passed"}
    assert list(tmp_path.glob(".*.tmp")) == []


@pytest.mark.parametrize(
    "probe_module",
    [wp10_1_migration_probe, wp10_1_runtime_probe],
)
def test_wp10_1_failed_atomic_replace_leaves_no_partial_or_temp_file(
    tmp_path: Path,
    probe_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "evidence.json"

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(probe_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        probe_module.atomic_write_json(output, {"status": "passed"})
    assert not output.exists()
    assert list(tmp_path.glob(".*.tmp")) == []


def test_wp10_1_failure_payload_counts_failed_and_not_run() -> None:
    recorder = wp10_1_runtime_probe.EvidenceRecorder("0019_manual_claims_binding")
    recorder.cases.extend(
        [
            {"status": "failed"},
            {"status": "not_run"},
        ]
    )
    payload = recorder.payload(
        status="failed",
        final_revision="0020_wp10_publication_contract",
        postgres_version="16.15",
        failure="InjectedFailure",
    )
    assert payload["status"] == "failed"
    assert payload["summary"] == {"total": 2, "passed": 0, "failed": 1, "not_run": 1}
