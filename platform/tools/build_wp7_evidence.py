"""Build the deterministic WP7 delivery manifest and report."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

EVIDENCE_REQUIRED = (
    "docs/wp7/implementation-ticket.md",
    "docs/wp7/acceptance-ticket.md",
    "platform/alembic/versions/0008_ai_model_governance.py",
    "platform/src/uap_platform/model_governance/__init__.py",
    "platform/src/uap_platform/model_governance/contracts.py",
    "platform/src/uap_platform/model_governance/providers.py",
    "platform/src/uap_platform/model_governance/persistence.py",
    "platform/src/uap_platform/model_governance/schemas.py",
    "platform/src/uap_platform/model_governance/workflow.py",
    "platform/tests/test_model_governance.py",
    "platform/tools/validate_wp7.py",
    "platform/tools/wp7_runtime_probe.py",
    "platform/tools/build_wp7_evidence.py",
    ".github/workflows/platform-ci.yml",
    "platform/Makefile",
    "platform/scripts/verify-migration-chain.sh",
    "platform/tools/validate_wp3.py",
    "platform/tools/validate_wp4.py",
    "platform/tools/wp3_runtime_probe.py",
    "platform/pyproject.toml",
    "platform/uv.lock",
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
        raise SystemExit(f"missing WP7 evidence files: {', '.join(missing)}")
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = args.output / "MANIFEST.sha256"
    lines = [f"{sha256(repository / path)}  {path}" for path in EVIDENCE_REQUIRED]
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = {
        "work_package": "WP7",
        "frozen_standard": "G7-FROZEN-20260820-01",
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
