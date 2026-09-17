"""Build the deterministic WP8 delivery manifest and report."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

EVIDENCE_REQUIRED = (
    "docs/wp8/implementation-ticket.md",
    "docs/wp8/acceptance-ticket.md",
    "docs/wp8/acceptance-cases.md",
    "docs/wp8/adr/0013-relations-out-of-scope.md",
    "platform/alembic/versions/0010_knowledge_foundation.py",
    "platform/alembic/versions/0011_claim_materialization.py",
    "platform/alembic/versions/0012_entity_materialization.py",
    "platform/alembic/versions/0013_entity_merge_state_machine.py",
    "platform/src/uap_platform/knowledge/__init__.py",
    "platform/src/uap_platform/knowledge/handler.py",
    "platform/src/uap_platform/knowledge/job_types.py",
    "platform/src/uap_platform/knowledge/reasons.py",
    "platform/src/uap_platform/knowledge/worker.py",
    "platform/tests/test_wp8_foundation.py",
    "platform/tools/validate_wp8.py",
    "platform/tools/wp8_runtime_probe.py",
    "platform/tools/wp8_1_runtime_probe.py",
    "platform/tools/wp8_3_runtime_probe.py",
    "platform/tools/wp8_4_runtime_probe.py",
    "platform/tools/wp8_5_runtime_probe.py",
    "platform/tools/wp8_6_runtime_probe.py",
    "platform/tools/build_wp8_evidence.py",
    "platform/Makefile",
    ".github/workflows/platform-ci.yml",
    "platform/scripts/verify-migration-chain.sh",
    "platform/tools/validate_wp3.py",
    "platform/tools/validate_wp7.py",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[2]
    missing = [path for path in EVIDENCE_REQUIRED if not (repository / path).is_file()]
    if missing:
        raise SystemExit(f"missing WP8 evidence files: {', '.join(missing)}")
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = args.output / "MANIFEST.sha256"
    lines = [f"{sha256(repository / path)}  {path}" for path in EVIDENCE_REQUIRED]
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = {
        "work_package": "WP8",
        "frozen_standard": "G8-FROZEN-20260821-02",
        "files": len(EVIDENCE_REQUIRED),
        "manifest_sha256": sha256(manifest),
        "paths": list(EVIDENCE_REQUIRED),
    }
    (args.output / "delivery-report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    anchor = args.output.parent / f"{args.output.name}.MANIFEST.sha256"
    anchor.write_text(
        f"{sha256(manifest)}  {args.output.name}/MANIFEST.sha256\n",
        encoding="utf-8",
    )
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
