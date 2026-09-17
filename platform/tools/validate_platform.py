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

EXPECTED_RUNTIME_VERSIONS = {
    "UAP_PYTHON_IMAGE": (
        "python:3.12.13-alpine3.23@sha256:"
        "601d3d3797e90e2534782e69c85fafb7971b43f24c7b1b079b7e48dd435e458d"
    ),
    "UAP_UV_VERSION": "0.12.3",
    "UAP_POSTGRES_IMAGE": (
        "postgres:16.14-alpine@sha256:"
        "57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
    ),
    "UAP_POSTGRES_RUNTIME_IMAGE": "uap-postgres:16.14-hardened",
    "UAP_GO_IMAGE": (
        "golang:1.26-alpine3.23@sha256:"
        "33ce311e5eecedee48ec1b84419c1306e9fbd71009f0d5c3f2a6904b579c1ecc"
    ),
    "UAP_SEAWEEDFS_COMMIT": "e919bec9d194b61aa07cef75f46b207dd687a019",
    "UAP_SEAWEEDFS_BASE_IMAGE": (
        "chrislusf/seaweedfs:4.41@sha256:"
        "43b768cd62b00d132439cda881b93fd1adebf1b315e996e794087743821d771d"
    ),
    "UAP_OBJECT_STORE_IMAGE": "uap-seaweedfs:4.41-hardened",
    "UAP_TRIVY_IMAGE": (
        "aquasec/trivy:0.73.0@sha256:"
        "7cced7cae583819fc7806d4cbc0dbbc7cad18b99f7d3e235192e6da8c091045c"
    ),
}
EXPECTED_GRPC_VERSION = "v1.85.0-dev.0.20260825072537-93e31b48545e"

EXPECTED_APP_PATCH_RUN = (
    "apk add --no-cache --upgrade "
    "'libuuid>=2.41.6-r1' 'sqlite-libs>=3.53.4-r0' "
    "'libcrypto3>=3.5.8-r0' 'libssl3>=3.5.8-r0' "
    "&& addgroup -S -g 10001 uap "
    "&& adduser -S -D -H -u 10001 -G uap uap "
    '&& python -m pip install --no-cache-dir "uv==${UV_VERSION}"'
)
EXPECTED_POSTGRES_PATCH_RUN = (
    "rm -f /usr/local/bin/gosu "
    "&& apk add --no-cache --upgrade "
    "'libuuid>=2.42.3-r1' 'libcrypto3>=3.5.8-r0' 'libssl3>=3.5.8-r0' "
    "&& if apk info -e sqlite-libs >/dev/null 2>&1; then "
    "apk add --no-cache --upgrade sqlite-libs; fi"
)
EXPECTED_OBJECT_STORE_PATCH_RUN = (
    "apk add --no-cache --upgrade 'libcrypto3>=3.5.8-r0' 'libssl3>=3.5.8-r0'"
)
EXPECTED_GRPC_PATCH_RUN = 'go get "google.golang.org/grpc@${UAP_GRPC_VERSION}" && go mod tidy'

PROTECTED_DOCKER_VARIABLES = frozenset(
    {
        "PYTHON_IMAGE",
        "POSTGRES_IMAGE",
        "UAP_GO_IMAGE",
        "UAP_SEAWEEDFS_BASE_IMAGE",
        "UAP_SEAWEEDFS_COMMIT",
        "UAP_GRPC_VERSION",
    }
)

DockerInstruction = tuple[str, str, int]


def _dockerfile_instructions(source: str) -> list[DockerInstruction] | None:
    """Parse the small instruction subset used by the approved Dockerfile templates."""

    instructions: list[DockerInstruction] = []
    logical_line = ""
    stage = -1
    for raw_line in source.splitlines():
        stripped = raw_line.strip()
        if not logical_line and (not stripped or stripped.startswith("#")):
            continue
        if logical_line and (not stripped or stripped.startswith("#")):
            return None

        continued = stripped.endswith("\\")
        part = stripped[:-1].rstrip() if continued else stripped
        logical_line = f"{logical_line} {part}".strip()
        if continued:
            continue

        match = re.fullmatch(r"([A-Za-z]+)\s+(.+)", logical_line)
        if match is None:
            return None
        instruction = match.group(1).upper()
        value = match.group(2).strip()
        if instruction == "FROM":
            stage += 1
        instructions.append((instruction, value, stage))
        logical_line = ""

    return None if logical_line else instructions


def _has_only_exact_arg(
    instructions: list[DockerInstruction], name: str, value: str, stage: int
) -> bool:
    definitions: list[tuple[str, int]] = []
    for instruction, body, body_stage in instructions:
        if instruction != "ARG":
            continue
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(\S+)", body)
        if match is None:
            if body.split("=", 1)[0].strip() == name:
                return False
            continue
        if match.group(1) == name:
            definitions.append((match.group(2), body_stage))
    return definitions == [(value, stage)]


def _has_exact_from(instructions: list[DockerInstruction], value: str, stage: int) -> bool:
    return any(
        instruction == "FROM" and body == value and body_stage == stage
        for instruction, body, body_stage in instructions
    )


def _has_only_exact_run(instructions: list[DockerInstruction], body: str, stage: int) -> bool:
    matches = [
        (candidate, body_stage)
        for instruction, candidate, body_stage in instructions
        if instruction == "RUN" and candidate == body
    ]
    return matches == [(body, stage)]


def _has_no_protected_env_overrides(instructions: list[DockerInstruction]) -> bool:
    """Reject ENV forms that could override an approved build-time input."""

    for instruction, body, _stage in instructions:
        if instruction != "ENV":
            continue
        if any(
            re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", body)
            for name in PROTECTED_DOCKER_VARIABLES
        ):
            return False
    return True


def _uses_approved_implicit_shell(instructions: list[DockerInstruction]) -> bool:
    """The approved templates rely exclusively on Docker's default shell."""

    return all(instruction != "SHELL" for instruction, _body, _stage in instructions)


def patched_runtime_versions_are_valid(
    versions: dict[str, str],
    dockerfile: str,
    postgres_dockerfile: str,
    object_store_dockerfile: str,
) -> bool:
    """Validate the independently approved, immutable F3 runtime inputs."""

    if any(versions.get(key) != value for key, value in EXPECTED_RUNTIME_VERSIONS.items()):
        return False

    docker_instructions = _dockerfile_instructions(dockerfile)
    postgres_instructions = _dockerfile_instructions(postgres_dockerfile)
    object_store_instructions = _dockerfile_instructions(object_store_dockerfile)
    if (
        docker_instructions is None
        or postgres_instructions is None
        or object_store_instructions is None
    ):
        return False

    python_tag = EXPECTED_RUNTIME_VERSIONS["UAP_PYTHON_IMAGE"].split("@", 1)[0]
    postgres_tag = EXPECTED_RUNTIME_VERSIONS["UAP_POSTGRES_IMAGE"].split("@", 1)[0]
    return all(
        (
            _has_only_exact_arg(docker_instructions, "PYTHON_IMAGE", python_tag, -1),
            _has_exact_from(docker_instructions, "${PYTHON_IMAGE} AS base", 0),
            _has_only_exact_run(docker_instructions, EXPECTED_APP_PATCH_RUN, 0),
            _has_no_protected_env_overrides(docker_instructions),
            _uses_approved_implicit_shell(docker_instructions),
            _has_only_exact_arg(postgres_instructions, "POSTGRES_IMAGE", postgres_tag, -1),
            _has_exact_from(postgres_instructions, "${POSTGRES_IMAGE}", 0),
            _has_only_exact_run(postgres_instructions, EXPECTED_POSTGRES_PATCH_RUN, 0),
            _has_no_protected_env_overrides(postgres_instructions),
            _uses_approved_implicit_shell(postgres_instructions),
            _has_only_exact_arg(
                object_store_instructions,
                "UAP_GO_IMAGE",
                EXPECTED_RUNTIME_VERSIONS["UAP_GO_IMAGE"],
                -1,
            ),
            _has_only_exact_arg(
                object_store_instructions,
                "UAP_SEAWEEDFS_BASE_IMAGE",
                EXPECTED_RUNTIME_VERSIONS["UAP_SEAWEEDFS_BASE_IMAGE"],
                -1,
            ),
            _has_exact_from(object_store_instructions, "${UAP_GO_IMAGE} AS builder", 0),
            _has_only_exact_arg(
                object_store_instructions,
                "UAP_SEAWEEDFS_COMMIT",
                EXPECTED_RUNTIME_VERSIONS["UAP_SEAWEEDFS_COMMIT"],
                0,
            ),
            _has_only_exact_arg(
                object_store_instructions,
                "UAP_GRPC_VERSION",
                EXPECTED_GRPC_VERSION,
                0,
            ),
            _has_only_exact_run(object_store_instructions, EXPECTED_GRPC_PATCH_RUN, 0),
            _has_exact_from(object_store_instructions, "${UAP_SEAWEEDFS_BASE_IMAGE}", 1),
            _has_only_exact_run(object_store_instructions, EXPECTED_OBJECT_STORE_PATCH_RUN, 1),
            _has_no_protected_env_overrides(object_store_instructions),
            _uses_approved_implicit_shell(object_store_instructions),
        )
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
        "object-store/Dockerfile",
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
    go_image = versions.get("UAP_GO_IMAGE", "")
    seaweedfs_commit = versions.get("UAP_SEAWEEDFS_COMMIT", "")
    seaweedfs_base_image = versions.get("UAP_SEAWEEDFS_BASE_IMAGE", "")
    trivy_image = versions.get("UAP_TRIVY_IMAGE", "")
    postgres_dockerfile = (root / "postgres/Dockerfile").read_text(encoding="utf-8")
    object_store_dockerfile = (root / "object-store/Dockerfile").read_text(encoding="utf-8")
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
                patched_runtime_versions_are_valid(
                    versions,
                    dockerfile,
                    postgres_dockerfile,
                    object_store_dockerfile,
                ),
                {
                    "python": python_image,
                    "postgres": postgres_image,
                    "object_store": object_store_image,
                    "go": go_image,
                    "seaweedfs_commit": seaweedfs_commit,
                    "seaweedfs_base": seaweedfs_base_image,
                    "trivy": trivy_image,
                    "grpc": EXPECTED_GRPC_VERSION,
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
                        "${UAP_GO_IMAGE:",
                        "${UAP_SEAWEEDFS_COMMIT:",
                        "${UAP_SEAWEEDFS_BASE_IMAGE:",
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
                    "healthcheck" in services[name] for name in ("postgres", "object-store", "app")
                ),
                True,
            ),
            check(
                "empty_environment_bootstrap",
                "POSTGRES_DB" in services["postgres"]["environment"]
                and "uap-platform-object-store-init" in compose_text
                and all(
                    name in compose_text for name in ("raw", "derived", "model-io", "public-assets")
                )
                and "scripts/migrate-platform.sh" in compose_text,
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
                and 'echo "$postgres_password"' not in bootstrap
                and 'echo "$s3_secret_key"' not in bootstrap,
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
        "scripts/migrate-platform.sh",
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
                and "docker compose"
                not in "\n".join(
                    line for line in workflow.splitlines() if "run --rm --no-deps app" in line
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
                "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in workflow
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
                and "failure_injection" in workflow
                and "github.event_name == 'workflow_dispatch'" in workflow
                and "inputs.failure_injection" in workflow,
                True,
            ),
            check(
                "staging_idempotent_deploy",
                "config --quiet" in deploy
                and "uap-platform-object-store-init" in compose_text
                and "object-store-init" in deploy
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
            "\n".join(f"{item['sha256']}  {item['path']}" for item in report["manifest"]) + "\n",
            encoding="utf-8",
        )
    print(f"passed={passed} checks={len(checks)} files={len(files)}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
