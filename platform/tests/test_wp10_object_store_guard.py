"""Disposable object-store guard tests. No live MinIO/SeaweedFS."""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import time
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from tools.wp10_object_store_guard import (
    SENTINEL_KEY,
    SIDECAR_ENDPOINT,
    WRITE_BUCKETS,
    DisposableObjectStoreGuard,
    ObjectStoreGuardError,
    install_sentinel,
    is_forbidden_endpoint,
    normalize_endpoint,
)
from uap_platform.config import Settings


class MemoryStore:
    def __init__(self) -> None:
        self.buckets: dict[str, dict[str, bytes]] = {name: {} for name in WRITE_BUCKETS}
        self.removed: list[tuple[str, str]] = []
        self.puts: list[tuple[str, str]] = []

    def bucket_exists(self, name: str) -> bool:
        return name in self.buckets

    def list_objects(self, bucket: str, *, recursive: bool = True) -> list[SimpleNamespace]:
        del recursive
        return [SimpleNamespace(object_name=key) for key in self.buckets.get(bucket, {})]

    def get_object(self, bucket: str, key: str) -> bytes:
        try:
            return self.buckets[bucket][key]
        except KeyError as exc:
            raise FileNotFoundError(key) from exc

    def put_object(
        self,
        bucket: str,
        key: str,
        data: object,
        length: int,
        content_type: str = "application/json",
    ) -> None:
        del length, content_type
        if isinstance(data, (bytes, bytearray)):
            body = bytes(data)
        else:
            read = getattr(data, "read", None)
            if not callable(read):
                raise TypeError("unsupported put body")
            raw = read()
            if not isinstance(raw, (bytes, bytearray)):
                raise TypeError("put body is not bytes")
            body = bytes(raw)
        self.buckets[bucket][key] = body
        self.puts.append((bucket, key))

    def remove_object(self, bucket: str, key: str) -> None:
        self.removed.append((bucket, key))
        self.buckets[bucket].pop(key, None)


def make_settings(*, endpoint: str = "g10-25-object-store:8333") -> Settings:
    return Settings(
        database_url=SecretStr("postgresql://user:database-secret@postgres/db"),
        s3_endpoint=endpoint,
        s3_access_key=SecretStr("access-secret"),
        s3_secret_key=SecretStr("object-secret"),
    )


@pytest.fixture
def disposable_env(monkeypatch: pytest.MonkeyPatch) -> str:
    ident = "g10-25-run-abc123"
    monkeypatch.setenv("UAP_S3_DISPOSABLE", "1")
    monkeypatch.setenv("UAP_S3_DISPOSABLE_ID", ident)
    return ident


def _prime(store: MemoryStore, settings: Settings) -> None:
    install_sentinel(client=store, settings=settings)


def test_rejects_shared_endpoint(disposable_env: str) -> None:
    del disposable_env
    store = MemoryStore()
    settings = make_settings(endpoint="uap-wp3-test-object-store-1:8333")
    with pytest.raises(ObjectStoreGuardError, match="shared object-store"):
        install_sentinel(client=store, settings=settings)
    assert store.puts == []


def test_rejects_platform_object_store_dns(disposable_env: str) -> None:
    del disposable_env
    store = MemoryStore()
    settings = make_settings(endpoint="object-store:8333")
    with pytest.raises(ObjectStoreGuardError, match="shared object-store"):
        install_sentinel(client=store, settings=settings)
    assert store.puts == []


def test_rejects_host_port_colliding_with_platform_publish(disposable_env: str) -> None:
    del disposable_env
    store = MemoryStore()
    settings = make_settings(endpoint="127.0.0.1:8333")
    with pytest.raises(ObjectStoreGuardError, match="shared object-store"):
        install_sentinel(client=store, settings=settings)
    assert store.puts == []


def test_rejects_unmarked_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UAP_S3_DISPOSABLE", raising=False)
    monkeypatch.delenv("UAP_S3_DISPOSABLE_ID", raising=False)
    store = MemoryStore()
    settings = make_settings()
    with pytest.raises(ObjectStoreGuardError, match="not marked disposable"):
        DisposableObjectStoreGuard(client=store, settings=settings).preflight()
    assert store.puts == []


def test_nonempty_preexisting_bucket_rejects_with_zero_writes(disposable_env: str) -> None:
    store = MemoryStore()
    settings = make_settings()
    _prime(store, settings)
    store.buckets["raw"]["raw/" + ("a" * 64)] = b"preexisting"
    puts_after_sentinel = list(store.puts)
    with pytest.raises(ObjectStoreGuardError, match="pre-existing"):
        DisposableObjectStoreGuard(client=store, settings=settings).preflight()
    assert store.puts == puts_after_sentinel
    assert store.removed == []


def test_success_path_exact_cleanup_and_residue_zero(disposable_env: str) -> None:
    store = MemoryStore()
    settings = make_settings()
    _prime(store, settings)
    guard = DisposableObjectStoreGuard(client=store, settings=settings)
    report = guard.preflight()
    assert report["status"] == "passed"
    created_key = "raw/" + ("b" * 64)
    store.buckets["raw"][created_key] = b"this-run"
    cleanup = guard.cleanup()
    assert cleanup["status"] == "passed"
    assert cleanup["residue"] == 0
    assert cleanup["deleted"] == 1
    assert created_key not in store.buckets["raw"]
    assert SENTINEL_KEY in store.buckets["raw"]
    assert store.removed == [("raw", created_key)]


def test_mid_run_failure_still_cleans_created_objects(disposable_env: str) -> None:
    store = MemoryStore()
    settings = make_settings()
    _prime(store, settings)
    guard = DisposableObjectStoreGuard(client=store, settings=settings)
    guard.preflight()
    store.buckets["derived"]["derived/" + ("c" * 64)] = b"partial"
    try:
        raise RuntimeError("probe exploded")
    except RuntimeError:
        cleanup = guard.cleanup()
    assert cleanup["residue"] == 0
    assert cleanup["deleted"] == 1
    assert store.buckets["derived"] == {}
    assert SENTINEL_KEY in store.buckets["raw"]


def test_does_not_delete_preexisting_sentinel(disposable_env: str) -> None:
    store = MemoryStore()
    settings = make_settings()
    _prime(store, settings)
    sentinel_body = store.buckets["raw"][SENTINEL_KEY]
    guard = DisposableObjectStoreGuard(client=store, settings=settings)
    guard.preflight()
    guard.cleanup()
    assert store.buckets["raw"][SENTINEL_KEY] == sentinel_body
    assert all(key != SENTINEL_KEY for _bucket, key in store.removed)


def test_inventory_digest_roundtrip(disposable_env: str) -> None:
    store = MemoryStore()
    settings = make_settings()
    _prime(store, settings)
    guard = DisposableObjectStoreGuard(client=store, settings=settings)
    before = guard.preflight()
    created = "model-io/" + ("d" * 64)
    store.buckets["model-io"][created] = b"payload"
    after = guard.cleanup()
    assert before["inventory"]["digest"] == after["inventory"]["digest"]
    assert after["residue"] == 0


def test_credentials_stay_out_of_evidence_and_argv(disposable_env: str) -> None:
    store = MemoryStore()
    settings = make_settings()
    _prime(store, settings)
    guard = DisposableObjectStoreGuard(client=store, settings=settings)
    report = guard.preflight()
    cleanup = guard.cleanup()
    dumped = json.dumps({"preflight": report, "cleanup": cleanup})
    assert "access-secret" not in dumped
    assert "object-secret" not in dumped
    assert "database-secret" not in dumped
    assert "--access-key" not in dumped
    install = install_sentinel(client=store, settings=settings)
    assert "access-secret" not in json.dumps(install)


def test_install_sentinel_uses_bytesio_body(disposable_env: str) -> None:
    store = MemoryStore()
    settings = make_settings()
    result = install_sentinel(client=store, settings=settings)
    body = store.buckets["raw"][SENTINEL_KEY]
    payload = json.loads(body.decode("utf-8"))
    assert payload["disposable"] is True
    assert payload["id"] == disposable_env
    assert result["sentinel_key"] == SENTINEL_KEY
    assert isinstance(BytesIO(body), BytesIO)


def test_normalizes_scheme_for_python312_minio(disposable_env: str) -> None:
    store = MemoryStore()
    settings = make_settings(endpoint="http://127.0.0.1:18333")
    result = install_sentinel(client=store, settings=settings)
    assert result["identity"]["endpoint"] == "127.0.0.1:18333"
    assert SENTINEL_KEY in store.buckets["raw"]


def test_accepts_sidecar_dns_and_host_published_port(disposable_env: str) -> None:
    store = MemoryStore()
    sidecar = install_sentinel(client=store, settings=make_settings(endpoint=SIDECAR_ENDPOINT))
    assert sidecar["identity"]["endpoint"] == SIDECAR_ENDPOINT
    host = install_sentinel(client=store, settings=make_settings(endpoint="127.0.0.1:18333"))
    assert host["identity"]["endpoint"] == "127.0.0.1:18333"
    assert is_forbidden_endpoint("http://object-store:8333") is True
    assert normalize_endpoint("https://127.0.0.1:18333/") == "127.0.0.1:18333"


def test_sidecar_script_loads_env_and_tears_down_on_sentinel_failure() -> None:
    platform = Path(__file__).resolve().parents[1]
    script_path = platform / "scripts" / "g10-25-disposable-object-store.sh"
    script = script_path.read_text(encoding="utf-8")
    assert "load_uap_env_file" in script
    assert "UAP_DATABASE_URL" in script
    assert "UAP_S3_ACCESS_KEY" in script
    assert "UAP_S3_SECRET_KEY" in script
    assert "require_sentinel_env" in script
    assert "UAP_G10_25_ENV_FILE" in script
    assert "Process environment wins" in script
    assert "platform/.env.versions and platform/.env are required" not in script
    assert 'export UAP_S3_ENDPOINT="$HOST_ENDPOINT"' in script
    assert "127.0.0.1:" in script
    assert "g10-25-object-store:8333" in script
    assert "trap teardown_if_needed EXIT" in script
    assert "down --volumes --remove-orphans" in script
    assert "STARTED_OK" in script
    assert "host_endpoint=" in script
    assert "sidecar_endpoint=" in script
    assert "sidecar_network=" in script
    assert "sentinel_via=sidecar" in script
    assert "sentinel_via=host" not in script
    assert "/app/.venv/bin/python" in script
    assert "PYTHONPATH=/workspace/src" in script
    assert "trap 'handle_signal 129' HUP" in script
    assert "trap 'handle_signal 130' INT" in script
    assert "trap 'handle_signal 143' TERM" in script
    compose = (platform / "compose.g10-25-object-store.yaml").read_text(encoding="utf-8")
    assert "uap.g10-25.disposable" in compose
    assert "aliases" in compose
    assert "127.0.0.1:${UAP_G10_25_S3_API_PORT:-18333}:8333" in compose
    assert "UAP_G10_25_NETWORK" in compose


IMAGE_PINS = {
    "UAP_OBJECT_STORE_IMAGE": "uap-seaweedfs:test",
    "UAP_GO_IMAGE": "golang:test",
    "UAP_SEAWEEDFS_COMMIT": "deadbeef",
    "UAP_SEAWEEDFS_BASE_IMAGE": "chrislusf/seaweedfs:test",
}

PROCESS_SECRETS = {
    "UAP_DATABASE_URL": "postgresql://uap:process-dsn-r4@127.0.0.1:5432/uap",
    "UAP_S3_ACCESS_KEY": "process-access-key-r4",
    "UAP_S3_SECRET_KEY": "process-secret-key-r4",
}

FILE_SECRETS = {
    "UAP_DATABASE_URL": "postgresql://uap:file-dsn-r4@127.0.0.1:5432/uap",
    "UAP_S3_ACCESS_KEY": "file-access-key-r4",
    "UAP_S3_SECRET_KEY": "file-secret-key-r4",
}


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _prepare_script_tree(tmp_path: Path) -> Path:
    src = Path(__file__).resolve().parents[1]
    platform = tmp_path / "platform"
    scripts = platform / "scripts"
    scripts.mkdir(parents=True)
    dest = scripts / "g10-25-disposable-object-store.sh"
    shutil.copy(src / "scripts" / "g10-25-disposable-object-store.sh", dest)
    shutil.copy(
        src / "compose.g10-25-object-store.yaml",
        platform / "compose.g10-25-object-store.yaml",
    )
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    assert not (platform / ".env").exists()
    return platform


def _install_fakes(
    tmp_path: Path,
    *,
    sidecar_exit: int = 0,
    compose_up_exit: int = 0,
) -> tuple[Path, Path, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker_log = tmp_path / "docker.log"
    env_source = tmp_path / "env-source.log"
    _write_executable(
        bindir / "docker",
        "#!/bin/sh\n"
        "{\n"
        "    printf 'CALL\\n'\n"
        '    for arg in "$@"; do printf \'ARG=%s\\n\' "$arg"; done\n'
        f'}} >> "{docker_log}"\n'
        'if [ "${1:-}" = run ]; then\n'
        f'    if [ "${{UAP_S3_ACCESS_KEY:-}}" = "{PROCESS_SECRETS["UAP_S3_ACCESS_KEY"]}" ]; then\n'
        f'        printf "process\\n" > "{env_source}"\n'
        f'    elif [ "${{UAP_S3_ACCESS_KEY:-}}" = "{FILE_SECRETS["UAP_S3_ACCESS_KEY"]}" ]; then\n'
        f'        printf "file\\n" > "{env_source}"\n'
        "    else\n"
        f'        printf "unexpected\\n" > "{env_source}"\n'
        "    fi\n"
        '    if [ -n "${FAKE_DOCKER_RUN_STARTED:-}" ]; then\n'
        '        : > "$FAKE_DOCKER_RUN_STARTED"\n'
        "    fi\n"
        '    if [ -n "${FAKE_DOCKER_RUN_SLEEP:-}" ]; then\n'
        '        sleep "$FAKE_DOCKER_RUN_SLEEP"\n'
        "    fi\n"
        f"    exit {sidecar_exit}\n"
        "fi\n"
        'if [ "${1:-}" = compose ]; then\n'
        '    for arg in "$@"; do\n'
        '        if [ "$arg" = up ]; then\n'
        f"            exit {compose_up_exit}\n"
        "        fi\n"
        "    done\n"
        "fi\n"
        "exit 0\n",
    )
    assert not (bindir / "uv").exists()
    return bindir, docker_log, env_source


def _script_env(platform: Path, bindir: Path, extra_env: dict[str, str]) -> dict[str, str]:
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.defpath}",
        "HOME": str(platform.parent),
        "LANG": "C",
        "UAP_S3_DISPOSABLE": "1",
        "UAP_S3_DISPOSABLE_ID": "g1025runabc123",
        "UAP_G10_25_S3_API_PORT": "18333",
    }
    env.update(extra_env)
    return env


def _run_script(
    platform: Path,
    bindir: Path,
    extra_env: dict[str, str],
    *args: str,
) -> subprocess.CompletedProcess[str]:
    script = platform / "scripts" / "g10-25-disposable-object-store.sh"
    return subprocess.run(  # noqa: S603
        [str(script), *args],
        check=False,
        capture_output=True,
        text=True,
        env=_script_env(platform, bindir, extra_env),
        timeout=15,
    )


def _assert_no_secrets(result: subprocess.CompletedProcess[str], docker_log: Path) -> None:
    docker_argv = docker_log.read_text(encoding="utf-8") if docker_log.exists() else ""
    blob = f"{result.stdout}\n{result.stderr}\n{docker_argv}"
    for value in (*PROCESS_SECRETS.values(), *FILE_SECRETS.values()):
        assert value not in blob


def test_start_without_dotenv_refuses_missing_process_env(tmp_path: Path) -> None:
    platform = _prepare_script_tree(tmp_path)
    bindir, docker_log, _env_source = _install_fakes(tmp_path)
    result = _run_script(platform, bindir, {}, "start")
    assert result.returncode == 2
    assert "UAP_DATABASE_URL is required" in result.stderr
    assert "platform/.env.versions and platform/.env are required" not in result.stderr
    assert not docker_log.exists()
    _assert_no_secrets(result, docker_log)


def test_start_uses_process_env_when_platform_dotenv_absent(tmp_path: Path) -> None:
    platform = _prepare_script_tree(tmp_path)
    bindir, docker_log, env_source = _install_fakes(tmp_path)
    extra = {**PROCESS_SECRETS, **IMAGE_PINS}
    result = _run_script(platform, bindir, extra, "start")
    assert result.returncode == 0, result.stderr
    assert "status=ready" in result.stdout
    assert "platform_dotenv=absent" in result.stdout
    assert "external_env_file=unset" in result.stdout
    assert "host_endpoint=127.0.0.1:18333" in result.stdout
    assert "sidecar_endpoint=g10-25-object-store:8333" in result.stdout
    assert "sidecar_network=uap-g10-25-os-g1025runabc1" in result.stdout
    assert "sentinel_image=uap-platform:development" in result.stdout
    assert "sentinel_python_executable=/app/.venv/bin/python" in result.stdout
    assert "sentinel_python=3.12" in result.stdout
    assert "sentinel_via=sidecar" in result.stdout
    assert "sentinel_via=host" not in result.stdout
    docker_args = docker_log.read_text(encoding="utf-8")
    assert "ARG=up" in docker_args
    assert "ARG=--build" in docker_args
    assert "ARG=run" in docker_args
    assert "ARG=--rm" in docker_args
    assert "ARG=--network" in docker_args
    assert "ARG=uap-g10-25-os-g1025runabc1" in docker_args
    assert f"ARG={platform}:/workspace:ro" in docker_args
    assert "ARG=/workspace" in docker_args
    assert "ARG=uap-platform:development" in docker_args
    assert "ARG=PYTHONPATH=/workspace/src" in docker_args
    assert "ARG=UAP_S3_ENDPOINT=g10-25-object-store:8333" in docker_args
    assert "ARG=UAP_S3_SECURE=false" in docker_args
    assert "ARG=UAP_S3_BUCKETS=raw,derived,model-io,public-assets,backups" in docker_args
    for key in (
        "UAP_DATABASE_URL",
        "UAP_S3_ACCESS_KEY",
        "UAP_S3_SECRET_KEY",
        "UAP_S3_DISPOSABLE",
        "UAP_S3_DISPOSABLE_ID",
    ):
        assert f"ARG={key}\n" in docker_args
        assert f"ARG={key}=" not in docker_args
    assert "/app/.venv/bin/python -m tools.wp10_object_store_guard --install-sentinel" in (
        docker_args
    )
    assert "127.0.0.1:18333" not in docker_args
    assert "ARG=UAP_S3_ENDPOINT=object-store:8333\n" not in docker_args
    assert "ARG=--env-file" not in docker_args
    assert shutil.which("uv", path=_script_env(platform, bindir, extra)["PATH"]) is None
    assert env_source.read_text(encoding="utf-8").strip() == "process"
    _assert_no_secrets(result, docker_log)


def test_start_with_versions_file_without_platform_dotenv(tmp_path: Path) -> None:
    """Codex isolated worktree: committed .env.versions, no platform/.env."""
    platform = _prepare_script_tree(tmp_path)
    (platform / ".env.versions").write_text(
        "\n".join(f"{key}={value}" for key, value in IMAGE_PINS.items()) + "\n",
        encoding="utf-8",
    )
    bindir, docker_log, env_source = _install_fakes(tmp_path)
    result = _run_script(platform, bindir, dict(PROCESS_SECRETS), "start")
    assert result.returncode == 0, result.stderr
    assert "status=ready" in result.stdout
    assert "platform_dotenv=absent" in result.stdout
    docker_args = docker_log.read_text(encoding="utf-8")
    assert "ARG=--env-file" in docker_args
    assert f"ARG={platform / '.env.versions'}\n" in docker_args
    assert f"ARG={platform / '.env'}\n" not in docker_args
    assert env_source.read_text(encoding="utf-8").strip() == "process"
    _assert_no_secrets(result, docker_log)


def test_start_uses_explicit_external_env_file(tmp_path: Path) -> None:
    platform = _prepare_script_tree(tmp_path)
    bindir, docker_log, env_source = _install_fakes(tmp_path)
    env_file = tmp_path / "external.env"
    lines = [f"{key}={value}" for key, value in {**FILE_SECRETS, **IMAGE_PINS}.items()]
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = _run_script(
        platform,
        bindir,
        {"UAP_G10_25_ENV_FILE": str(env_file)},
        "start",
    )
    assert result.returncode == 0, result.stderr
    assert "status=ready" in result.stdout
    assert "platform_dotenv=absent" in result.stdout
    assert "external_env_file=set" in result.stdout
    assert "host_endpoint=127.0.0.1:18333" in result.stdout
    docker_args = docker_log.read_text(encoding="utf-8")
    assert "--env-file" in docker_args
    assert str(env_file) in docker_args
    assert env_source.read_text(encoding="utf-8").strip() == "file"
    _assert_no_secrets(result, docker_log)


def test_process_env_wins_over_dotenv_and_external_file(tmp_path: Path) -> None:
    platform = _prepare_script_tree(tmp_path)
    bindir, docker_log, env_source = _install_fakes(tmp_path)
    (platform / ".env").write_text(
        "\n".join(f"{key}={value}" for key, value in FILE_SECRETS.items()) + "\n",
        encoding="utf-8",
    )
    extra = {**PROCESS_SECRETS, **IMAGE_PINS}
    result = _run_script(platform, bindir, extra, "start")
    assert result.returncode == 0, result.stderr
    assert "platform_dotenv=present" in result.stdout
    assert env_source.read_text(encoding="utf-8").strip() == "process"
    _assert_no_secrets(result, docker_log)


def test_missing_external_env_file_refuses_before_docker(tmp_path: Path) -> None:
    platform = _prepare_script_tree(tmp_path)
    bindir, docker_log, _env_source = _install_fakes(tmp_path)
    extra = {
        **PROCESS_SECRETS,
        **IMAGE_PINS,
        "UAP_G10_25_ENV_FILE": str(tmp_path / "missing.env"),
    }
    result = _run_script(platform, bindir, extra, "start")
    assert result.returncode == 2
    assert "UAP_G10_25_ENV_FILE is not a readable file" in result.stderr
    assert not docker_log.exists()
    _assert_no_secrets(result, docker_log)


def test_sentinel_failure_runs_compose_down_volumes(tmp_path: Path) -> None:
    platform = _prepare_script_tree(tmp_path)
    bindir, docker_log, _env_source = _install_fakes(tmp_path, sidecar_exit=1)
    extra = {**PROCESS_SECRETS, **IMAGE_PINS}
    result = _run_script(platform, bindir, extra, "start")
    assert result.returncode != 0
    assert "status=failed-and-removed" in result.stderr
    docker_args = docker_log.read_text(encoding="utf-8")
    assert "ARG=up" in docker_args
    assert "ARG=run" in docker_args
    assert "ARG=down" in docker_args
    assert "ARG=--volumes" in docker_args
    assert "ARG=--remove-orphans" in docker_args
    assert "ARG=rm" in docker_args
    assert "ARG=--force" in docker_args
    _assert_no_secrets(result, docker_log)


def test_object_store_start_failure_runs_compose_down_volumes(tmp_path: Path) -> None:
    platform = _prepare_script_tree(tmp_path)
    bindir, docker_log, _env_source = _install_fakes(tmp_path, compose_up_exit=1)
    extra = {**PROCESS_SECRETS, **IMAGE_PINS}
    result = _run_script(platform, bindir, extra, "start")
    assert result.returncode != 0
    assert "status=failed-and-removed" in result.stderr
    docker_args = docker_log.read_text(encoding="utf-8")
    assert "ARG=up" in docker_args
    assert "ARG=run" not in docker_args
    assert "ARG=down" in docker_args
    assert "ARG=--volumes" in docker_args
    assert "ARG=--remove-orphans" in docker_args
    _assert_no_secrets(result, docker_log)


def test_term_during_sidecar_run_removes_compose_project_and_volumes(tmp_path: Path) -> None:
    platform = _prepare_script_tree(tmp_path)
    bindir, docker_log, _env_source = _install_fakes(tmp_path)
    started = tmp_path / "sidecar-started"
    extra = {
        **PROCESS_SECRETS,
        **IMAGE_PINS,
        "FAKE_DOCKER_RUN_STARTED": str(started),
        "FAKE_DOCKER_RUN_SLEEP": "30",
    }
    script = platform / "scripts" / "g10-25-disposable-object-store.sh"
    process = subprocess.Popen(  # noqa: S603
        [str(script), "start"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_script_env(platform, bindir, extra),
        start_new_session=True,
    )
    deadline = time.monotonic() + 5
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert started.exists()
    os.killpg(process.pid, signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=10)
    result = subprocess.CompletedProcess([str(script), "start"], process.returncode, stdout, stderr)
    assert process.returncode != 0
    assert "status=failed-and-removed" in stderr
    docker_args = docker_log.read_text(encoding="utf-8")
    assert "ARG=run" in docker_args
    assert "ARG=rm" in docker_args
    assert "ARG=down" in docker_args
    assert "ARG=--volumes" in docker_args
    assert "ARG=--remove-orphans" in docker_args
    _assert_no_secrets(result, docker_log)
