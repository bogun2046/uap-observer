from __future__ import annotations

from pathlib import Path

import pytest

from tools.validate_platform import (
    EXPECTED_GRPC_VERSION,
    EXPECTED_RUNTIME_VERSIONS,
    evaluate,
    parse_env,
    patched_runtime_versions_are_valid,
)

ROOT = Path(__file__).resolve().parents[1]


def runtime_inputs() -> tuple[dict[str, str], str, str, str]:
    return (
        parse_env(ROOT / ".env.versions"),
        (ROOT / "Dockerfile").read_text(encoding="utf-8"),
        (ROOT / "postgres/Dockerfile").read_text(encoding="utf-8"),
        (ROOT / "object-store/Dockerfile").read_text(encoding="utf-8"),
    )


def test_frozen_wp2_policy() -> None:
    failures = [item for item in evaluate(ROOT) if not item.passed]

    assert failures == []


def test_f3_runtime_versions_accept_only_the_approved_candidate() -> None:
    assert patched_runtime_versions_are_valid(*runtime_inputs()) is True


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("UAP_GO_IMAGE", "golang:1.26-alpine3.23"),
        ("UAP_GO_IMAGE", "golang:1.26-alpine3.23@sha256:" + "0" * 64),
        ("UAP_SEAWEEDFS_COMMIT", "0" * 40),
        ("UAP_PYTHON_IMAGE", "python:3.12.13-alpine3.23"),
        ("UAP_POSTGRES_IMAGE", "postgres:16.14-alpine"),
        ("UAP_TRIVY_IMAGE", "aquasec/trivy:0.73.0"),
    ],
)
def test_f3_runtime_versions_reject_unapproved_or_floating_env_values(key: str, value: str) -> None:
    versions, dockerfile, postgres, object_store = runtime_inputs()
    versions[key] = value
    assert patched_runtime_versions_are_valid(versions, dockerfile, postgres, object_store) is False


@pytest.mark.parametrize(
    ("source_index", "approved", "replacement"),
    [
        (1, "'libuuid>=2.41.6-r1'", "'libuuid'"),
        (2, "'libuuid>=2.42.3-r1'", "'libuuid'"),
        (1, "'sqlite-libs>=3.53.4-r0'", "'sqlite-libs'"),
        (1, "'libcrypto3>=3.5.8-r0'", "'libcrypto3'"),
        (2, "'libssl3>=3.5.8-r0'", "'libssl3'"),
        (
            3,
            "ARG UAP_GRPC_VERSION=v1.85.0-dev.0.20260825072537-93e31b48545e",
            "ARG UAP_GRPC_VERSION=v1.85.0-dev",
        ),
        (
            3,
            EXPECTED_RUNTIME_VERSIONS["UAP_SEAWEEDFS_COMMIT"],
            "0" * 40,
        ),
        (
            3,
            EXPECTED_RUNTIME_VERSIONS["UAP_GO_IMAGE"],
            "golang:1.26-alpine3.23@sha256:" + "0" * 64,
        ),
    ],
)
def test_f3_runtime_versions_reject_weakened_or_mismatched_dockerfiles(
    source_index: int, approved: str, replacement: str
) -> None:
    inputs: list[object] = list(runtime_inputs())
    source = inputs[source_index]
    assert isinstance(source, str) and approved in source
    inputs[source_index] = source.replace(approved, replacement, 1)
    versions, dockerfile, postgres, object_store = inputs
    assert isinstance(versions, dict)
    assert isinstance(dockerfile, str)
    assert isinstance(postgres, str)
    assert isinstance(object_store, str)
    assert patched_runtime_versions_are_valid(versions, dockerfile, postgres, object_store) is False


@pytest.mark.parametrize(
    ("approved", "replacement"),
    [
        (
            f"ARG UAP_GRPC_VERSION={EXPECTED_GRPC_VERSION}",
            f"# ARG UAP_GRPC_VERSION={EXPECTED_GRPC_VERSION}\nARG UAP_GRPC_VERSION=v1.70.0",
        ),
        (
            f"ARG UAP_GO_IMAGE={EXPECTED_RUNTIME_VERSIONS['UAP_GO_IMAGE']}",
            f"# ARG UAP_GO_IMAGE={EXPECTED_RUNTIME_VERSIONS['UAP_GO_IMAGE']}\n"
            "ARG UAP_GO_IMAGE=golang:1.25.13-alpine3.23",
        ),
        (
            'RUN go get "google.golang.org/grpc@${UAP_GRPC_VERSION}" \\\n    && go mod tidy',
            '# go get "google.golang.org/grpc@${UAP_GRPC_VERSION}"\nRUN go mod tidy',
        ),
        (
            f"ARG UAP_GRPC_VERSION={EXPECTED_GRPC_VERSION}",
            f"ARG UAP_GRPC_VERSION={EXPECTED_GRPC_VERSION}\nARG UAP_GRPC_VERSION=v1.70.0",
        ),
    ],
    ids=[
        "wrong-grpc-with-comment",
        "wrong-go-with-comment",
        "no-grpc-upgrade-with-comment",
        "grpc-duplicate-override",
    ],
)
def test_f3_runtime_policy_rejects_independent_review_reproductions(
    approved: str, replacement: str
) -> None:
    versions, dockerfile, postgres, object_store = runtime_inputs()
    assert approved in object_store
    object_store = object_store.replace(approved, replacement, 1)
    assert patched_runtime_versions_are_valid(versions, dockerfile, postgres, object_store) is False


@pytest.mark.parametrize(
    ("approved", "replacement"),
    [
        (
            f"ARG UAP_GRPC_VERSION={EXPECTED_GRPC_VERSION}",
            f"ARG UAP_GRPC_VERSION={EXPECTED_GRPC_VERSION}-suffix",
        ),
        (
            f"ARG UAP_GO_IMAGE={EXPECTED_RUNTIME_VERSIONS['UAP_GO_IMAGE']}",
            f"ARG UAP_GO_IMAGE={EXPECTED_RUNTIME_VERSIONS['UAP_GO_IMAGE']}-suffix",
        ),
        (
            'RUN go get "google.golang.org/grpc@${UAP_GRPC_VERSION}" \\\n    && go mod tidy',
            "RUN echo 'go get \"google.golang.org/grpc@${UAP_GRPC_VERSION}\"' \\\n"
            "    && go mod tidy",
        ),
        (
            f"ARG UAP_GRPC_VERSION={EXPECTED_GRPC_VERSION}",
            f"ARG UAP_GRPC_VERSION={EXPECTED_GRPC_VERSION}\n"
            "FROM ${UAP_SEAWEEDFS_BASE_IMAGE}\nARG UAP_GRPC_VERSION=v1.70.0",
        ),
        (
            "'libuuid>=2.41.6-r1'",
            "'libuuid' # 'libuuid>=2.41.6-r1'",
        ),
    ],
    ids=[
        "grpc-suffix",
        "go-image-suffix",
        "grpc-only-echoed",
        "grpc-overridden-in-later-stage",
        "package-minimum-only-in-comment",
    ],
)
def test_f3_runtime_policy_rejects_non_effective_or_ambiguous_instructions(
    approved: str, replacement: str
) -> None:
    versions, dockerfile, postgres, object_store = runtime_inputs()
    if approved in object_store:
        object_store = object_store.replace(approved, replacement, 1)
    else:
        assert approved in dockerfile
        dockerfile = dockerfile.replace(approved, replacement, 1)
    assert patched_runtime_versions_are_valid(versions, dockerfile, postgres, object_store) is False


@pytest.mark.parametrize(
    ("case"),
    [
        "package-short-circuit",
        "package-echo-quoted-separator",
        "package-builder-only",
        "grpc-env-override",
    ],
)
def test_f3_runtime_policy_rejects_second_review_reproductions(case: str) -> None:
    versions, dockerfile, postgres, object_store = runtime_inputs()
    install = "apk add --no-cache --upgrade 'libcrypto3>=3.5.8-r0' 'libssl3>=3.5.8-r0'"
    grpc_arg = f"ARG UAP_GRPC_VERSION={EXPECTED_GRPC_VERSION}"

    if case == "package-short-circuit":
        object_store = object_store.replace(f"RUN {install}", f"RUN true || {install}", 1)
    elif case == "package-echo-quoted-separator":
        object_store = object_store.replace(f"RUN {install}", f"RUN echo '&&' {install}", 1)
    elif case == "package-builder-only":
        object_store = object_store.replace(f"RUN {install}\n", "", 1).replace(
            "RUN apk add --no-cache git",
            f"RUN apk add --no-cache git\nRUN {install}",
            1,
        )
    else:
        assert case == "grpc-env-override"
        object_store = object_store.replace(
            grpc_arg, f"{grpc_arg}\nENV UAP_GRPC_VERSION=v1.70.0", 1
        )

    assert patched_runtime_versions_are_valid(versions, dockerfile, postgres, object_store) is False


@pytest.mark.parametrize(
    "env_instruction",
    [
        "ENV UAP_GRPC_VERSION v1.70.0",
        "ENV UAP_GRPC_VERSION=v1.70.0",
        'ENV UAP_GRPC_VERSION="v1.70.0"',
        "ENV OTHER=value UAP_GRPC_VERSION=v1.70.0",
        "ENV OTHER=value " + "\\\n" + "    UAP_GRPC_VERSION=v1.70.0",
    ],
)
def test_f3_runtime_policy_rejects_equivalent_protected_env_overrides(
    env_instruction: str,
) -> None:
    versions, dockerfile, postgres, object_store = runtime_inputs()
    grpc_arg = f"ARG UAP_GRPC_VERSION={EXPECTED_GRPC_VERSION}"
    object_store = object_store.replace(grpc_arg, f"{grpc_arg}\n{env_instruction}", 1)

    assert patched_runtime_versions_are_valid(versions, dockerfile, postgres, object_store) is False


@pytest.mark.parametrize("source_index", [1, 2, 3])
def test_f3_runtime_policy_requires_patch_run_in_approved_runtime_stage(
    source_index: int,
) -> None:
    inputs: list[object] = list(runtime_inputs())
    source = inputs[source_index]
    assert isinstance(source, str)
    if source_index == 1:
        marker = "RUN apk add --no-cache --upgrade " + "\\\n"
    elif source_index == 2:
        marker = "RUN rm -f /usr/local/bin/gosu " + "\\\n"
    else:
        marker = "RUN apk add --no-cache --upgrade 'libcrypto3>=3.5.8-r0' 'libssl3>=3.5.8-r0'"
    assert marker in source
    inputs[source_index] = source.replace(marker, f"RUN true || {marker.removeprefix('RUN ')}", 1)
    versions, dockerfile, postgres, object_store = inputs
    assert isinstance(versions, dict)
    assert isinstance(dockerfile, str)
    assert isinstance(postgres, str)
    assert isinstance(object_store, str)
    assert patched_runtime_versions_are_valid(versions, dockerfile, postgres, object_store) is False


@pytest.mark.parametrize("target", ["runtime-package", "builder-grpc"])
def test_f3_runtime_policy_rejects_shell_review_reproductions(target: str) -> None:
    versions, dockerfile, postgres, object_store = runtime_inputs()
    if target == "runtime-package":
        instruction = "RUN apk add --no-cache --upgrade 'libcrypto3>=3.5.8-r0' 'libssl3>=3.5.8-r0'"
    else:
        instruction = (
            'RUN go get "google.golang.org/grpc@${UAP_GRPC_VERSION}" \\\n    && go mod tidy'
        )
    assert instruction in object_store
    object_store = object_store.replace(
        instruction,
        f'SHELL ["/bin/echo"]\n{instruction}\nSHELL ["/bin/sh", "-c"]',
        1,
    )

    assert patched_runtime_versions_are_valid(versions, dockerfile, postgres, object_store) is False


@pytest.mark.parametrize(
    ("source_index", "marker", "shell_instruction"),
    [
        (1, "FROM ${PYTHON_IMAGE} AS base", 'SHELL ["/bin/bash", "-c"]'),
        (2, "FROM ${POSTGRES_IMAGE}", 'SHELL [ "/bin/false" ]'),
        (3, "FROM ${UAP_GO_IMAGE} AS builder", 'SHELL ["/bin/sh", "-euxc"]'),
        (3, "FROM ${UAP_SEAWEEDFS_BASE_IMAGE}", 'SHELL ["custom-runner"]'),
    ],
    ids=[
        "app-runtime-stage",
        "postgres-runtime-stage",
        "object-store-builder-stage",
        "object-store-runtime-stage",
    ],
)
def test_f3_runtime_policy_rejects_any_explicit_shell_in_approved_templates(
    source_index: int,
    marker: str,
    shell_instruction: str,
) -> None:
    inputs: list[object] = list(runtime_inputs())
    source = inputs[source_index]
    assert isinstance(source, str) and marker in source
    inputs[source_index] = source.replace(marker, f"{marker}\n{shell_instruction}", 1)
    versions, dockerfile, postgres, object_store = inputs
    assert isinstance(versions, dict)
    assert isinstance(dockerfile, str)
    assert isinstance(postgres, str)
    assert isinstance(object_store, str)

    assert patched_runtime_versions_are_valid(versions, dockerfile, postgres, object_store) is False
