"""Validate the frozen WP2 engineering and CI contract without Docker."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

REPOSITORY_REQUIRED = (
    ".github/workflows/platform-ci.yml",
    ".gitignore",
    "Makefile",
    "docs/wp2/README.md",
    "docs/wp2/acceptance-cases.md",
    "docs/wp2/acceptance-ticket.md",
    "docs/wp2/development-self-review.md",
    "docs/wp2/g2-rejection-record.md",
    "docs/wp2/g2-second-rejection-record.md",
    "docs/wp2/g2-acceptance-record.md",
    "docs/wp2/implementation-ticket.md",
    "docs/wp2/remediation-round2-report.md",
    "docs/wp2/acceptance-amendment-01.md",
    "docs/wp2/security-remediation.md",
    "docs/wp2/staging-deployment.md",
)


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    actual: object
    expected: object


def check(name: str, passed: bool, actual: object, expected: object = True) -> Check:
    return Check(name=name, passed=passed, actual=actual, expected=expected)


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise ValueError(f"invalid env line in {path}: {raw_line}")
        values[key] = value
    return values


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def evaluate(root: Path) -> list[Check]:
    root = root.resolve()
    repository = root.parent
    required = (
        ".dockerignore",
        ".env.example",
        ".env.versions",
        ".python-version",
        "Dockerfile",
        "Makefile",
        "README.md",
        "alembic.ini",
        "alembic/env.py",
        "compose.staging.yaml",
        "compose.yaml",
        "postgres/Dockerfile",
        "pyproject.toml",
        "scripts/bootstrap-env.sh",
        "scripts/deploy-staging.sh",
        "scripts/scan-images.sh",
        "src/uap_platform/config.py",
        "src/uap_platform/devserver.py",
        "src/uap_platform/object_store_init.py",
        "src/uap_platform/readiness.py",
        "uv.lock",
    )
    missing = [path for path in required if not (root / path).is_file()]
    checks = [check("required_files", not missing, missing, [])]
    missing_repository_files = [
        path for path in REPOSITORY_REQUIRED if not (repository / path).is_file()
    ]
    checks.append(
        check(
            "repository_delivery_files",
            not missing_repository_files,
            missing_repository_files,
            [],
        )
    )

    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    versions = parse_env(root / ".env.versions")
    example = parse_env(root / ".env.example")
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (root / ".dockerignore").read_text(encoding="utf-8")
    compose_text = (root / "compose.yaml").read_text(encoding="utf-8")
    compose = yaml.safe_load(compose_text)
    staging_text = (root / "compose.staging.yaml").read_text(encoding="utf-8")
    staging = yaml.safe_load(staging_text)
    workflow_path = repository / ".github/workflows/platform-ci.yml"
    workflow = workflow_path.read_text(encoding="utf-8") if workflow_path.is_file() else ""
    bootstrap = (root / "scripts/bootstrap-env.sh").read_text(encoding="utf-8")
    deploy = (root / "scripts/deploy-staging.sh").read_text(encoding="utf-8")

    python_image = versions.get("UAP_PYTHON_IMAGE", "")
    postgres_image = versions.get("UAP_POSTGRES_IMAGE", "")
    object_store_image = versions.get("UAP_OBJECT_STORE_IMAGE", "")
    trivy_image = versions.get("UAP_TRIVY_IMAGE", "")
    postgres_dockerfile = (root / "postgres/Dockerfile").read_text(encoding="utf-8")
    image_scan = (root / "scripts/scan-images.sh").read_text(encoding="utf-8")
    checks.extend(
        [
            check(
                "python_312_policy",
                (root / ".python-version").read_text(encoding="utf-8").strip().startswith("3.12.")
                and pyproject["project"]["requires-python"] == ">=3.12,<3.13"
                and python_image.startswith("python:3.12."),
                {
                    "python_version": (root / ".python-version")
                    .read_text(encoding="utf-8")
                    .strip(),
                    "requires_python": pyproject["project"]["requires-python"],
                    "image": python_image,
                },
            ),
            check(
                "patched_runtime_versions",
                python_image.startswith("python:3.12.13-alpine")
                and postgres_image.startswith("postgres:16.14-alpine")
                and object_store_image.startswith("chrislusf/seaweedfs:4.41@sha256:")
                and trivy_image.startswith("aquasec/trivy:0.73.0@sha256:")
                and all(
                    "@sha256:" in image
                    for image in (python_image, postgres_image, object_store_image, trivy_image)
                ),
                {
                    "python": python_image,
                    "postgres": postgres_image,
                    "object_store": object_store_image,
                    "trivy": trivy_image,
                },
            ),
            check(
                "hardened_postgres_runtime",
                "rm -f /usr/local/bin/gosu" in postgres_dockerfile
                and postgres_dockerfile.rstrip().endswith("USER postgres"),
                True,
            ),
            check(
                "shared_version_source",
                all(
                    token in compose_text
                    for token in (
                        "${UAP_PYTHON_IMAGE:",
                        "${UAP_POSTGRES_IMAGE:",
                        "${UAP_POSTGRES_RUNTIME_IMAGE:",
                        "${UAP_OBJECT_STORE_IMAGE:",
                        "${UAP_UV_VERSION:",
                    )
                )
                and "--env-file .env.versions" in workflow
                and "--env-file .env.versions" in deploy,
                True,
            ),
            check(
                "locked_installation",
                (root / "uv.lock").stat().st_size > 1000
                and "uv sync --frozen" in dockerfile
                and "uv lock --check" in workflow,
                True,
            ),
        ]
    )

    services = compose.get("services", {})
    expected_services = {"postgres", "object-store", "object-store-init", "app"}
    checks.extend(
        [
            check("compose_services", set(services) == expected_services, sorted(services)),
            check(
                "compose_healthchecks",
                all(
                    "healthcheck" in services[name]
                    for name in ("postgres", "object-store", "app")
                ),
                True,
            ),
            check(
                "empty_environment_bootstrap",
                "POSTGRES_DB" in services["postgres"]["environment"]
                and "uap-platform-object-store-init" in compose_text
                and all(
                    name in compose_text
                    for name in ("raw", "derived", "model-io", "public-assets")
                )
                and "alembic -x role=migrator upgrade head" in compose_text,
                True,
            ),
            check(
                "staging_runtime_target",
                staging["services"]["app"]["build"]["target"] == "runtime"
                and staging["services"]["app"]["restart"] == "unless-stopped",
                staging["services"]["app"],
            ),
        ]
    )

    secret_keys = ("UAP_POSTGRES_PASSWORD", "UAP_S3_ACCESS_KEY", "UAP_S3_SECRET_KEY")
    checks.extend(
        [
            check(
                "secret_template_empty",
                all(example.get(key) == "" for key in secret_keys),
                {key: bool(example.get(key)) for key in secret_keys},
                {key: False for key in secret_keys},
            ),
            check(
                "secret_generation",
                "umask 077" in bootstrap
                and "openssl rand -hex" in bootstrap
                and "chmod 600" in bootstrap
                and "echo \"$postgres_password\"" not in bootstrap
                and "echo \"$s3_secret_key\"" not in bootstrap,
                True,
            ),
            check(
                "secret_build_context_exclusion",
                all(
                    pattern in dockerignore.splitlines()
                    for pattern in (".env", ".secrets", "*.pem", "*.key")
                )
                and "COPY .env" not in dockerfile,
                True,
            ),
        ]
    )

    required_ci_tokens = (
        "uv lock --check",
        "ruff check",
        "mypy",
        "pytest",
        "alembic -x role=migrator upgrade head",
        "pip-audit",
        "bandit",
        "gitleaks",
        "scripts/scan-images.sh",
        "needs: [quality, security, integration]",
    )
    checks.extend(
        [
            check(
                "ci_fail_closed_gate",
                bool(workflow) and all(token in workflow for token in required_ci_tokens),
                [token for token in required_ci_tokens if token not in workflow],
                [],
            ),
            check(
                "ci_full_checkout_context",
                '"$GITHUB_WORKSPACE:/repo:ro"' in workflow
                and "--workdir /repo/platform" in workflow
                and "docker compose" not in "\n".join(
                    line
                    for line in workflow.splitlines()
                    if "run --rm --no-deps app" in line
                ),
                True,
            ),
            check(
                "pip_audit_writable_cache",
                "XDG_CACHE_HOME=/tmp/.cache" in workflow
                and "--cache-dir /tmp/pip-audit" in workflow,
                True,
            ),
            check(
                "ci_read_only_permissions",
                "permissions:\n  contents: read" in workflow,
                True,
            ),
            check(
                "ci_actions_pinned",
                "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
                in workflow
                and "actions/checkout@v" not in workflow,
                True,
            ),
            check(
                "container_image_fail_closed",
                all(
                    token in image_scan
                    for token in (
                        "--scanners vuln",
                        "--severity HIGH,CRITICAL",
                        "--exit-code 1",
                        "scan_image app",
                        "scan_image postgres",
                        "scan_image object-store",
                    )
                )
                and "--ignore-unfixed" not in image_scan
                and "failure_injection" in workflow,
                True,
            ),
            check(
                "staging_idempotent_deploy",
                "config --quiet" in deploy
                and "uap-platform-object-store-init" in compose_text
                and "alembic upgrade head" in deploy
                and "up --build --detach --wait" in deploy
                and "down --volumes" not in deploy
                and "permissions must be 600" in deploy,
                True,
            ),
        ]
    )

    forbidden_migration_dirs = [
        path.relative_to(repository).as_posix()
        for path in (repository / "platform/src/uap_platform").rglob("migrations")
        if path.is_dir()
    ]
    checks.append(
        check(
            "single_new_migration_authority",
            not forbidden_migration_dirs,
            forbidden_migration_dirs,
            [],
        )
    )

    tracked_secret_patterns = (
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        re.compile(
            r"(?i)(?:password|secret|token|api_key)\s*=\s*['\"]"
            r"(?!\$\{)[^'\"]{12,}['\"]"
        ),
    )
    candidates = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.name != "uv.lock"
        and not any(
            part.startswith(".") and part not in {".env.example", ".env.versions"}
            for part in path.parts
        )
    ]
    secret_hits: list[str] = []
    for path in candidates:
        content = path.read_text(encoding="utf-8", errors="ignore")
        if any(pattern.search(content) for pattern in tracked_secret_patterns):
            secret_hits.append(path.relative_to(root).as_posix())
    checks.append(check("static_secret_scan", not secret_hits, secret_hits, []))
    return checks


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    repository = root.parent
    checks = evaluate(root)
    passed = all(item.passed for item in checks)
    ignored_parts = {
        ".coverage",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "htmlcov",
    }
    platform_files = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.name != ".env"
        and not any(part in ignored_parts for part in path.parts)
    ]
    external_files = [repository / path for path in REPOSITORY_REQUIRED]
    files = sorted(set(platform_files + external_files))
    report: dict[str, Any] = {
        "passed": passed,
        "checks": [asdict(item) for item in checks],
        "manifest": [
            {
                "path": path.relative_to(root.parent).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in files
        ],
    }
    if args.output:
        output = args.output if args.output.is_absolute() else root.parent / args.output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (output.parent / "MANIFEST.sha256").write_text(
            "\n".join(
                f"{item['sha256']}  {item['path']}" for item in report["manifest"]
            )
            + "\n",
            encoding="utf-8",
        )
    print(f"passed={passed} checks={len(checks)} files={len(files)}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
