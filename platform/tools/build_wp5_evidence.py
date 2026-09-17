"""Build deterministic WP5 evidence and a separated-anchor checksum manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

EVIDENCE_REQUIRED = (
    ".github/workflows/platform-ci.yml",
    "docs/wp5/README.md",
    "docs/wp5/acceptance-cases.md",
    "docs/wp5/acceptance-ticket.md",
    "docs/wp5/development-self-review.md",
    "docs/wp5/implementation-ticket.md",
    "platform/.env.versions",
    "platform/Dockerfile",
    "platform/Makefile",
    "platform/alembic/env.py",
    "platform/alembic/versions/0001_roles_and_schemas.py",
    "platform/alembic/versions/0002_authoritative_schema.py",
    "platform/alembic/versions/0003_permissions_and_guards.py",
    "platform/alembic/versions/0004_g3_semantic_repairs.py",
    "platform/alembic/versions/0005_durable_jobs.py",
    "platform/alembic/versions/0006_collectors.py",
    "platform/alembic/versions/0007_source_run_lease_guard.py",
    "platform/scripts/verify-migration-chain.sh",
    "platform/pyproject.toml",
    "platform/src/uap_platform/collectors/contracts.py",
    "platform/src/uap_platform/collectors/__init__.py",
    "platform/src/uap_platform/collectors/persistence.py",
    "platform/src/uap_platform/collectors/policy.py",
    "platform/src/uap_platform/collectors/rss.py",
    "platform/src/uap_platform/collectors/transport.py",
    "platform/src/uap_platform/collectors/workflow.py",
    "platform/src/uap_platform/object_registry.py",
    "platform/tests/fixtures/rss/g5-fixed-feed.xml",
    "platform/tests/test_collector_policy.py",
    "platform/tests/test_collector_transport.py",
    "platform/tests/test_object_registry.py",
    "platform/tests/test_rss_collector.py",
    "platform/tests/test_source_run_store.py",
    "platform/tests/test_source_run_workflow.py",
    "platform/tools/build_wp5_evidence.py",
    "platform/tools/reconcile_object_storage.py",
    "platform/tools/validate_platform.py",
    "platform/tools/validate_wp3.py",
    "platform/tools/validate_wp4.py",
    "platform/tools/validate_wp5.py",
    "platform/tools/wp3_runtime_probe.py",
    "platform/tools/wp5_runtime_probe.py",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    platform = Path(__file__).resolve().parents[1]
    repository = platform.parent
    output = args.output if args.output.is_absolute() else repository / args.output
    output.mkdir(parents=True, exist_ok=True)
    files = sorted({repository / path for path in EVIDENCE_REQUIRED})
    missing = [str(path.relative_to(repository)) for path in files if not path.is_file()]
    if missing:
        raise SystemExit(f"missing WP5 evidence files: {', '.join(missing)}")
    rows = [
        {
            "path": path.relative_to(repository).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in files
    ]
    report = output / "delivery-report.json"
    report.write_text(
        json.dumps(
            {"freeze": "G5-FROZEN-20260816-01", "files": rows},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = output / "MANIFEST.sha256"
    manifest.write_text(
        "\n".join(f"{row['sha256']}  {row['path']}" for row in rows) + "\n",
        encoding="utf-8",
    )
    anchor = output.parent / f"{output.name}.MANIFEST.sha256"
    anchor.write_text(f"{sha256_file(manifest)}  {output.name}/MANIFEST.sha256\n", encoding="utf-8")
    print(
        json.dumps({"files": len(rows), "manifest_sha256": sha256_file(manifest)}, sort_keys=True)
    )


if __name__ == "__main__":
    main()
