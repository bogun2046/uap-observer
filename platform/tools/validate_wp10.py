"""Static validator for WP10.6-A / G10-25 chain wiring.

Runtime and remote CI results are never treated as passed here.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

_PLATFORM_ROOT = Path(__file__).resolve().parents[1]
if str(_PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLATFORM_ROOT))

from alembic.config import Config  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402

from tools.wp10_runtime_probe import (  # noqa: E402
    FROZEN_STEPS,
    REQUIRED_SCRIPTS,
    ProbeConfigurationError,
    Step,
    discover_source_boundary,
    os_argv,
    resolve_evidence_directory,
)
from tools.wp10_stage_revisions import DATABASE_TOPOLOGY_ENVS  # noqa: E402

HEAD = "0024_wp10_admin_replay"
PARENT = "0023_wp10_api_read_indexes"
EXPECTED_REVISIONS = 24
SIGNED_SHA = "4e15bdd8cdb92d4406cc46b38f8cef92320a1881"
START_SHA = "34c57bcadfeb67053c4c47f8cde237a3af185ba8"
SHA256_LINE = re.compile(r"^[0-9a-f]{64}  \S.+$")
LIVE_DISCLAIMER = "runtime/CI remain NOT PROVEN until live probes."
RUNTIME_MODULE = "tools.wp10_runtime_probe"
RUNTIME_MODULE_ENTRY = f"python -m {RUNTIME_MODULE}"
ALLOWED_WP106A_PATHS = frozenset(
    {
        "Makefile",
        "platform/Makefile",
        ".github/workflows/platform-ci.yml",
        "platform/tools/wp10_runtime_probe.py",
        "platform/tools/validate_wp10.py",
        "platform/tests/test_wp10_6_chain.py",
        "platform/src/uap_platform/publishing/service.py",
        "platform/src/uap_platform/public_api/config.py",
        "platform/src/uap_platform/public_api/service.py",
        "platform/src/uap_platform/public_api/handler.py",
        "platform/src/uap_platform/admin_api/config.py",
        "platform/src/uap_platform/admin_api/service.py",
        "platform/src/uap_platform/admin_api/handler.py",
        "platform/src/uap_platform/admin_api/errors.py",
        ".gitleaks.toml",
        "platform/tests/test_wp10_4_public_api.py",
        "platform/tests/test_wp10_5_admin_api.py",
        "platform/tests/test_wp10_5_oidc.py",
        "platform/tools/wp10_1_migration_probe.py",
        "platform/tools/wp10_1_runtime_probe.py",
        "platform/tools/wp10_2_migration_probe.py",
        "platform/tools/wp10_2_runtime_probe.py",
        "platform/tools/wp10_3_migration_probe.py",
        "platform/tools/wp10_3_runtime_probe.py",
        "platform/tools/wp10_4_migration_probe.py",
        "platform/tools/wp10_4_runtime_probe.py",
        "platform/tools/wp10_5_runtime_probe.py",
        "platform/tools/wp10_stage_revisions.py",
        "platform/tools/wp10_object_store_guard.py",
        "platform/tests/test_wp10_stage_revisions.py",
        "platform/tests/test_wp10_object_store_guard.py",
        "platform/scripts/g10-25-disposable-object-store.sh",
        "platform/compose.g10-25-object-store.yaml",
    }
)
ALLOWED_WP106B_PATHS = frozenset(
    {
        "platform/alembic/versions/0020_wp10_publication_contract.py",
        "platform/tools/wp10_6_migration_probe.py",
        "platform/tools/validate_wp10_6.py",
        "platform/tests/test_wp10_6_migration.py",
        "platform/tests/test_wp10_6_acl_roundtrip.py",
        "platform/tools/validate_wp10_1.py",
    }
)
ALLOWED_WP106C_PATHS = frozenset(
    {
        "platform/tools/wp10_6_performance_probe.py",
        "platform/tests/test_wp10_6_performance.py",
        "platform/tools/validate_wp10_6.py",
        "platform/tools/validate_wp10.py",
    }
)
ALLOWED_WP106D_PATHS = frozenset(
    {
        "docs/wp10/README.md",
        "docs/wp10/implementation-ticket.md",
        "docs/wp10/SHA256SUMS",
    }
)
ALLOWED_WP106_FINAL_GATE_PATHS = frozenset(
    {
        "platform/scripts/verify-migration-chain.sh",
        "platform/tools/validate_wp10_4.py",
        "platform/tools/validate_wp10_5.py",
    }
)
ALLOWED_WP106F3_PATHS = frozenset(
    {
        "platform/.env.versions",
        "platform/Dockerfile",
        "platform/postgres/Dockerfile",
        "platform/object-store/Dockerfile",
        "platform/tools/validate_platform.py",
        "platform/tests/test_platform_policy.py",
    }
)
ALLOWED_WP106F4_PATHS = frozenset(
    {
        "platform/alembic/env.py",
        "platform/alembic/versions/0008_ai_model_governance.py",
        "platform/src/uap_platform/collectors/contracts.py",
        "platform/src/uap_platform/collectors/workflow.py",
        "platform/src/uap_platform/documents/contracts.py",
        "platform/src/uap_platform/model_governance/persistence.py",
        "platform/src/uap_platform/model_governance/workflow.py",
        "platform/src/uap_platform/object_registry.py",
        "platform/src/uap_platform/review/canonical.py",
        "platform/tests/test_config.py",
        "platform/tests/test_configure_roles.py",
        "platform/tests/test_document_extraction.py",
        "platform/tests/test_document_persistence.py",
        "platform/tests/test_document_workflow.py",
        "platform/tests/test_model_governance.py",
        "platform/tests/test_object_registry.py",
        "platform/tests/test_source_run_store.py",
        "platform/tests/test_subtitle_extraction.py",
        "platform/tests/test_wp10_foundation.py",
        "platform/tests/test_wp8_claim_bundle.py",
        "platform/tests/test_wp8_claim_handler.py",
        "platform/tests/test_wp8_relation_reject.py",
        "platform/tests/test_wp9_cases.py",
        "platform/tests/test_wp9_claims.py",
        "platform/tests/test_wp9_decisions.py",
        "platform/tools/build_wp3_evidence.py",
        "platform/tools/build_wp4_evidence.py",
        "platform/tools/ensure_model_governance_role.py",
        "platform/tools/object_backup.py",
        "platform/tools/validate_wp6.py",
        "platform/tools/validate_wp7.py",
        "platform/tools/wp4_runtime_probe.py",
        "platform/tools/wp5_runtime_probe.py",
        "platform/tools/wp6_runtime_probe.py",
        "platform/tools/wp7_runtime_probe.py",
        "platform/tools/wp8_1_runtime_probe.py",
        "platform/tools/wp8_3_runtime_probe.py",
        "platform/tools/wp8_5_runtime_probe.py",
        "platform/tools/wp9_2_runtime_probe.py",
        "platform/tools/wp9_3_runtime_probe.py",
        "platform/tools/wp9_4_runtime_probe.py",
        "platform/tools/wp9_6_runtime_probe.py",
    }
)
HEAD_UPGRADE_MARKERS = (
    'upgrade", "head"',
    "upgrade', 'head'",
    "upgrade head",
)
EXPECTED_CHAIN = (
    "0001_roles_and_schemas",
    "0002_authoritative_schema",
    "0003_permissions_and_guards",
    "0004_g3_semantic_repairs",
    "0005_durable_jobs",
    "0006_collectors",
    "0007_source_run_lease_guard",
    "0008_ai_model_governance",
    "0009_model_governance_boundaries",
    "0010_knowledge_foundation",
    "0011_claim_materialization",
    "0012_entity_materialization",
    "0013_entity_merge_state_machine",
    "0014_review_session_authority",
    "0015_review_case_lifecycle",
    "0016_review_decisions_and_grants",
    "0017_selection_and_promotion",
    "0018_authorized_entity_merge",
    "0019_manual_claims_binding",
    "0020_wp10_publication_contract",
    "0021_wp10_publisher_projection",
    "0022_wp10_claim_search_projection",
    "0023_wp10_api_read_indexes",
    "0024_wp10_admin_replay",
)
EXPECTED_STEP_IDS = (
    "WP3",
    "WP4",
    "WP5",
    "WP6",
    "WP7",
    "WP8",
    "WP9.1",
    "WP9.2",
    "WP9.3",
    "WP9.4",
    "WP9.5",
    "WP9.6",
    "WP10.1-migration",
    "WP10.1-runtime",
    "WP10.2-migration",
    "WP10.2-runtime",
    "WP10.3-migration",
    "WP10.3-runtime",
    "WP10.3-wp10.2-regression",
    "WP10.4-migration",
    "WP10.4-runtime",
    "WP10.5-migration",
    "WP10.5-runtime",
)
WP10_PROBES = (
    "wp10_1_migration_probe.py",
    "wp10_1_runtime_probe.py",
    "wp10_2_migration_probe.py",
    "wp10_2_runtime_probe.py",
    "wp10_3_migration_probe.py",
    "wp10_3_runtime_probe.py",
    "wp10_4_migration_probe.py",
    "wp10_4_runtime_probe.py",
    "wp10_5_migration_probe.py",
    "wp10_5_runtime_probe.py",
)
WP9_PROBES = (
    "wp9_1_runtime_probe.py",
    "wp9_2_runtime_probe.py",
    "wp9_3_runtime_probe.py",
    "wp9_4_runtime_probe.py",
    "wp9_5_runtime_probe.py",
    "wp9_6_runtime_probe.py",
)


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    actual: object
    expected: object = True


def check(name: str, passed: bool, actual: object, expected: object = True) -> Check:
    return Check(name, passed, actual, expected)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def migration_filenames(versions: Path) -> list[str]:
    if not versions.is_dir():
        return []
    return sorted(
        item.name for item in versions.iterdir() if item.suffix == ".py" and item.name[0].isdigit()
    )


def forbidden_versions(names: list[str]) -> list[str]:
    return [name for name in names if name.startswith("0025") or "wp11" in name.lower()]


def linear_revision_ids(script: ScriptDirectory) -> list[str]:
    heads = script.get_heads()
    if len(heads) != 1:
        return list(heads)
    ordered: list[str] = []
    current = script.get_revision(heads[0])
    while current is not None:
        ordered.append(current.revision)
        down = current.down_revision
        parent: str | None
        if down is None:
            parent = None
        elif isinstance(down, str):
            parent = down
        else:
            return list(heads)
        if parent is None:
            break
        current = script.get_revision(parent)
    ordered.reverse()
    return ordered


def workflow_job(source: str, name: str) -> str:
    pattern = re.compile(rf"^  {re.escape(name)}:\n", re.M)
    match = pattern.search(source)
    if not match:
        return ""
    nxt = re.search(r"^  [A-Za-z0-9_-]+:\n", source[match.end() :], re.M)
    end = match.end() + nxt.start() if nxt else len(source)
    return source[match.start() : end]


def migration_probes_using_head(platform: Path) -> list[str]:
    hits: list[str] = []
    for name in WP10_PROBES:
        if "migration" not in name:
            continue
        text = _read(platform / "tools" / name)
        for marker in HEAD_UPGRADE_MARKERS:
            if marker in text:
                hits.append(f"{name}:{marker}")
    return hits


def makefile_check_body(makefile: str) -> str:
    if "check:" not in makefile:
        return ""
    body = makefile.split("check:", 1)[1]
    for marker in ("\nwp10-runtime:", "\nlock:", "\ndev:"):
        if marker in body:
            body = body.split(marker, 1)[0]
            break
    return body


def makefile_target_body(makefile: str, target: str) -> str:
    marker = f"{target}:"
    if marker not in makefile:
        return ""
    body = makefile.split(marker, 1)[1]
    next_target = re.search(r"^[-A-Za-z0-9_.]+:\s*", body, re.M)
    return body[: next_target.start()] if next_target else body


def has_python_module_invocation(text: str, module: str) -> bool:
    pattern = rf"(?:^|\s)python -m {re.escape(module)}(?:\s|$)"
    return re.search(pattern, text, flags=re.MULTILINE) is not None


def evidence_boundary_contract() -> dict[str, bool]:
    results = {
        "git_root": False,
        "git_worktree_file": False,
        "container_root": False,
        "external_allowed": False,
        "inside_rejected": False,
        "symlink_escape_rejected": False,
        "symlink_ingress_rejected": False,
        "root_rejected": False,
    }
    with tempfile.TemporaryDirectory(prefix="uap-wp10-boundary-") as raw:
        base = Path(raw)
        repository = base / "repo"
        git_platform = repository / "platform"
        git_platform.mkdir(parents=True)
        (repository / ".git").mkdir()
        results["git_root"] = discover_source_boundary(git_platform) == repository.resolve()

        worktree_repository = base / "worktree-repository"
        worktree_platform = worktree_repository / "platform"
        worktree_platform.mkdir(parents=True)
        (worktree_repository / ".git").write_text(
            "gitdir: /tmp/uap-wp10-gitdir\n", encoding="utf-8"
        )
        results["git_worktree_file"] = (
            discover_source_boundary(worktree_platform) == worktree_repository.resolve()
        )

        container = base / "workspace"
        container.mkdir()
        boundary = discover_source_boundary(container)
        results["container_root"] = boundary == container.resolve() and boundary.parent != boundary
        outside = base / "evidence"
        results["external_allowed"] = (
            resolve_evidence_directory(outside, source_boundary=boundary) == outside.resolve()
        )
        try:
            resolve_evidence_directory(container / "evidence", source_boundary=boundary)
        except ProbeConfigurationError:
            results["inside_rejected"] = True

        outside.mkdir()
        escape = container / "escape"
        escape.symlink_to(outside, target_is_directory=True)
        try:
            resolve_evidence_directory(escape, source_boundary=boundary)
        except ProbeConfigurationError:
            results["symlink_escape_rejected"] = True

        ingress = base / "ingress"
        ingress.symlink_to(container, target_is_directory=True)
        try:
            resolve_evidence_directory(ingress / "evidence", source_boundary=boundary)
        except ProbeConfigurationError:
            results["symlink_ingress_rejected"] = True

        try:
            resolve_evidence_directory(outside, source_boundary=Path("/"))
        except ProbeConfigurationError:
            results["root_rejected"] = True
    return results


def classify_git_paths(paths: list[str]) -> tuple[str, list[str]]:
    allowed = (
        ALLOWED_WP106A_PATHS
        | ALLOWED_WP106B_PATHS
        | ALLOWED_WP106C_PATHS
        | ALLOWED_WP106D_PATHS
        | ALLOWED_WP106_FINAL_GATE_PATHS
        | ALLOWED_WP106F3_PATHS
        | ALLOWED_WP106F4_PATHS
    )
    extra = sorted({path for path in paths if path and path not in allowed})
    if extra:
        return "forbidden", extra
    return "allowed", []


def git_changed_paths(repository: Path) -> tuple[str, list[str]]:
    env_file = os.environ.get("UAP_WP10_GIT_PATHS_FILE")
    if env_file:
        path = Path(env_file)
        if not path.is_file():
            return "git-error", [f"missing git paths file: {env_file}"]
        names = [
            line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        return classify_git_paths(names)
    git_dir = repository / ".git"
    if not git_dir.exists() and not (repository / ".git").is_file():
        return "git-unavailable", []
    git = shutil.which("git")
    if git is None:
        return "git-unavailable", []
    try:
        diff = subprocess.run(  # noqa: S603
            [git, "diff", "--name-only", START_SHA],
            cwd=str(repository),
            check=False,
            capture_output=True,
            text=True,
        )
        untracked = subprocess.run(  # noqa: S603
            [git, "ls-files", "--others", "--exclude-standard"],
            cwd=str(repository),
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return "git-unavailable", []
    if diff.returncode != 0:
        return "git-error", [diff.stderr.strip() or f"exit {diff.returncode}"]
    names = [line.strip() for line in (diff.stdout + untracked.stdout).splitlines() if line.strip()]
    return classify_git_paths(names)


def evaluate(
    platform: Path,
    *,
    makefile: str | None = None,
    workflow: str | None = None,
    probe_source: str | None = None,
    version_names: list[str] | None = None,
    git_paths: list[str] | None = None,
    frozen_steps: tuple[Step, ...] | None = None,
) -> list[Check]:
    platform = platform.resolve()
    repository = platform.parent
    docs = repository / "docs/wp10"
    versions = platform / "alembic/versions"
    probe_path = platform / "tools/wp10_runtime_probe.py"
    validator_path = platform / "tools/validate_wp10.py"
    makefile_text = makefile if makefile is not None else _read(platform / "Makefile")
    root_makefile = _read(repository / "Makefile")
    workflow_text = (
        workflow
        if workflow is not None
        else _read(repository / ".github/workflows/platform-ci.yml")
    )
    probe_text = probe_source if probe_source is not None else _read(probe_path)
    validator_source = _read(validator_path)
    pyproject = _read(platform / "pyproject.toml")
    gate_record = _read(docs / "g10-gate-10.5-signed.md")
    sha256sums = _read(docs / "SHA256SUMS")
    public_handler = _read(platform / "src/uap_platform/public_api/handler.py")
    names = version_names if version_names is not None else migration_filenames(versions)
    extra_versions = forbidden_versions(names)
    heads: list[str] = []
    chain: list[str] = []
    down_revision: object = None
    try:
        config = Config(str(platform / "alembic.ini"))
        config.set_main_option("script_location", str(platform / "alembic"))
        script = ScriptDirectory.from_config(config)
        heads = list(script.get_heads())
        chain = linear_revision_ids(script)
        revision = script.get_revision(HEAD)
        down_revision = revision.down_revision if revision is not None else None
    except Exception as exc:
        heads = [f"alembic-error:{type(exc).__name__}"]
    steps = frozen_steps if frozen_steps is not None else FROZEN_STEPS
    step_ids = [step.step_id for step in steps]
    referenced_scripts = [step.script for step in steps]
    sha_lines = [line for line in sha256sums.splitlines() if line.strip()]
    sha_ok = bool(sha_lines) and all(SHA256_LINE.match(line) for line in sha_lines)
    listed_names = [line.split("  ", 1)[1] for line in sha_lines if "  " in line]
    quality_block = workflow_job(workflow_text, "quality")
    integration_block = workflow_job(workflow_text, "integration")
    security_block = workflow_job(workflow_text, "security")
    gate_block = workflow_job(workflow_text, "gate")
    coverage_fail = re.search(r"fail_under\s*=\s*(\d+)", pyproject)
    coverage_value = int(coverage_fail.group(1)) if coverage_fail else 0
    relation_closed = "api_capability_closed" in public_handler
    docs_wp11 = (repository / "docs/wp11").exists()
    check_body = makefile_check_body(makefile_text)
    runtime_make_body = makefile_target_body(makefile_text, "wp10-runtime")
    git_status, git_extra = (
        classify_git_paths(git_paths) if git_paths is not None else git_changed_paths(repository)
    )
    sample: list[str] = []
    os_secret = False
    os_module_entry = False
    try:
        sample = os_argv(FROZEN_STEPS[0], Path("wp10-evidence"))
        os_secret = any(
            token in " ".join(sample)
            for token in ("--admin-url", "--database-url", "--publisher-url", "--reader-url")
        )
        os_module_entry = sample[:3] == [sys.executable, "-m", RUNTIME_MODULE] and not any(
            token.endswith("wp10_runtime_probe.py") for token in sample
        )
    except Exception:
        os_secret = True
    probe_module_entry = (
        '"-m"' in probe_text
        and f'"{RUNTIME_MODULE}"' in probe_text
        and '"wp10_runtime_probe.py"' not in probe_text
    )
    boundary_results = evidence_boundary_contract()
    boundary_source = (
        "SOURCE_BOUNDARY = discover_source_boundary(PLATFORM_DIR)" in probe_text
        and "SOURCE_BOUNDARY = PLATFORM_DIR.parent" not in probe_text
        and 'SOURCE_BOUNDARY = Path("/")' not in probe_text
        and "REPOSITORY_DIR = PLATFORM_DIR.parent" not in probe_text
        and "resolve_evidence_directory(" in probe_text
        and "source_boundary=SOURCE_BOUNDARY" in probe_text
    )
    child_context_source = (
        "def run_exec_child" in probe_text
        and "original_argv = sys.argv" in probe_text
        and "original_sys_path = sys.path" in probe_text
        and "sys.path.insert(0, str(TOOLS_DIR))" in probe_text
        and "finally:" in probe_text
        and "sys.path = original_sys_path" in probe_text
        and "sys.argv = original_argv" in probe_text
    )
    stage_source = _read(platform / "tools/wp10_stage_revisions.py")
    database_topology_source = (
        "database_url_for_step(step.step_id)" in probe_text
        and 'environ["UAP_DATABASE_URL"] = selected_database_url' in probe_text
        and all(name in stage_source for name in DATABASE_TOPOLOGY_ENVS)
        and "def database_env_for_step" in stage_source
        and "def database_topology" in stage_source
        and "len(set(identities)) != len(identities)" in stage_source
    )
    ci_database_topology = (
        all(name in integration_block for name in DATABASE_TOPOLOGY_ENVS)
        and all(
            marker in integration_block
            for marker in (
                "UAP_WP10_LEGACY_DB",
                "UAP_WP10_LEGACY_PROBE_DB",
                "UAP_WP10_1_DB",
                "UAP_WP10_2_DB",
                "UAP_WP10_3_DB",
                "UAP_WP10_4_DB",
                "UAP_WP10_5_DB",
                "UAP_WP10_REGRESSION_DB",
            )
        )
        and all(
            marker in integration_block
            for marker in (
                'legacy_db="uap_wp10_legacy_',
                'legacy_probe_db="uap_wp10_legacy_probe_',
                'wp10_1_db="uap_wp10_1_',
                'wp10_2_db="uap_wp10_2_',
                'wp10_3_db="uap_wp10_3_',
                'wp10_4_db="uap_wp10_4_',
                'wp10_5_db="uap_wp10_5_',
                'regression_db="uap_wp10_regression_',
                'create_database "$legacy_db"',
                'create_database "$legacy_probe_db"',
                'create_database "$wp10_1_db"',
                'create_database "$wp10_2_db"',
                'create_database "$wp10_3_db"',
                'create_database "$wp10_4_db"',
                'create_database "$wp10_5_db"',
                'create_database "$regression_db"',
            )
        )
        and "up --build --detach --wait postgres" in integration_block
        and integration_block.count("g10-25-disposable-object-store.sh start") == 2
        and "g10-25-disposable-object-store.sh stop" in integration_block
        and (
            "Run legacy WP3 through WP9.6 probes on isolated legacy probe database"
            in integration_block
        )
        and "Rotate disposable object store before full G10-25" in integration_block
        and '"$UAP_WP10_LEGACY_PROBE_DB"' in integration_block
        and integration_block.index("g10-25-disposable-object-store.sh start")
        < integration_block.index(
            "Run legacy WP3 through WP9.6 probes on isolated legacy probe database"
        )
        and integration_block.index(
            "Run legacy WP3 through WP9.6 probes on isolated legacy probe database"
        )
        < integration_block.index("Rotate disposable object store before full G10-25")
        and integration_block.index("Rotate disposable object store before full G10-25")
        < integration_block.rindex("g10-25-disposable-object-store.sh start")
        and integration_block.rindex("g10-25-disposable-object-store.sh start")
        < integration_block.index("Run WP10 WP3-through-WP10.5 runtime probe")
        and "close_migrator()" in integration_block
        and 'run_tool "$1" python tools/configure_roles.py disable-migrator' in integration_block
        and integration_block.count('close_migrator "$') == 8
        and 'docker network connect "$UAP_G10_25_NETWORK"' in integration_block
        and '--network "$UAP_WP10_BACKEND_NETWORK"' in integration_block
        and "--env-file .env" in integration_block
    )
    makefile_module_entry = (
        f"uv run --frozen {RUNTIME_MODULE_ENTRY}" in runtime_make_body
        and '--evidence-dir "$(UAP_WP10_EVIDENCE_DIR)"' in runtime_make_body
        and "wp10_runtime_probe.py" not in runtime_make_body
    )
    ci_module_entry = (
        RUNTIME_MODULE_ENTRY in integration_block
        and "--evidence-dir /tmp/wp10-runtime-evidence" in integration_block
        and "wp10_runtime_probe.py" not in integration_block
    )
    makefile_wired = has_python_module_invocation(makefile_text, "tools.validate_wp10") or (
        "python tools/validate_wp10.py" in makefile_text
    )
    quality_wired = (
        has_python_module_invocation(quality_block, "tools.validate_wp10")
        or "tools/validate_wp10.py" in quality_block
    )
    return [
        check("single WP10.6 head", heads == [HEAD], heads, [HEAD]),
        check(
            "0001-0024 strictly linear",
            len(names) == EXPECTED_REVISIONS and tuple(chain) == EXPECTED_CHAIN,
            {"files": names, "chain": chain},
            list(EXPECTED_CHAIN),
        ),
        check("0024 down_revision", down_revision == PARENT, down_revision, PARENT),
        check("no 0025 or WP11 migration", extra_versions == [], extra_versions, []),
        check("wp10_runtime_probe.py exists", probe_path.is_file(), str(probe_path)),
        check(
            "frozen WP3→WP10.5 step order",
            tuple(step_ids) == EXPECTED_STEP_IDS,
            step_ids,
            list(EXPECTED_STEP_IDS),
        ),
        check(
            "WP10.1-WP10.5 probes referenced",
            all(name in referenced_scripts for name in WP10_PROBES),
            [name for name in WP10_PROBES if name not in referenced_scripts],
            [],
        ),
        check(
            "WP9.1-WP9.6 probes referenced",
            all(name in referenced_scripts for name in WP9_PROBES),
            [name for name in WP9_PROBES if name not in referenced_scripts],
            [],
        ),
        check(
            "orchestrator fail-closed",
            "continue-on-error" not in probe_text
            and "break" in probe_text
            and "check=False" in probe_text
            and "skipped" not in probe_text
            and "missing-probe-evidence" in probe_text
            and "clear_step_evidence" in probe_text
            and "STALE_GRACE" not in probe_text
            and "stdout_kind" in probe_text
            and "wp3-json" in probe_text
            and "--exec-child" in probe_text
            and "not_run" in probe_text
            and "default_stage_gate" in probe_text
            and "object-store-preflight" in probe_text,
            True,
        ),
        check(
            "Makefile check wires validate_wp10.py",
            makefile_wired and "python tools/validate_wp9.py" in makefile_text,
            makefile_wired,
        ),
        check(
            "Makefile WP10 runtime entry",
            "wp10-runtime:" in makefile_text
            and makefile_module_entry
            and "wp10-runtime:" in root_makefile,
            makefile_module_entry,
        ),
        check(
            "workflow paths include docs/wp10/**",
            workflow_text.count("docs/wp10/**") >= 2,
            workflow_text.count("docs/wp10/**"),
            2,
        ),
        check(
            "quality job runs validate_wp10.py",
            quality_wired
            and "UAP_WP10_GIT_PATHS_FILE" in quality_block
            and "git cat-file -e" in quality_block
            and START_SHA in quality_block
            and "fetch-depth: 0" in quality_block,
            quality_wired,
        ),
        check(
            "integration job runs wp10_runtime_probe.py",
            ci_module_entry and "WP3-through-WP10.5" in integration_block,
            ci_module_entry,
        ),
        check(
            "gate depends on quality/security/integration",
            "needs: [quality, security, integration]" in gate_block,
            gate_block,
        ),
        check(
            "quality gates not lowered",
            "ruff check" in quality_block
            and "mypy" in quality_block
            and "pytest" in quality_block
            and "bandit" in security_block
            and "pip-audit" in security_block
            and "gitleaks" in security_block
            and coverage_value >= 80
            and "validate_wp3.py" in makefile_text,
            {"coverage_fail_under": coverage_value},
        ),
        check(
            "no new migration/route/dependency/role grant in WP10.6-A files",
            "CREATE TABLE" not in probe_text
            and "GRANT " not in probe_text
            and validator_path.is_file(),
            True,
        ),
        check(
            "relation and WP11 remain closed",
            not docs_wp11 and extra_versions == [] and relation_closed,
            {"docs_wp11": docs_wp11, "extra_versions": extra_versions},
        ),
        check(
            "G10-GATE-10.5 signed SHA 4e15bdd",
            SIGNED_SHA in gate_record and "G10-GATE-10.5: SIGNED" in gate_record,
            SIGNED_SHA in gate_record,
        ),
        check(
            "WP10.6 start SHA 34c57bc",
            START_SHA in probe_text and START_SHA in validator_source,
            START_SHA,
        ),
        check(
            "docs/wp10/SHA256SUMS format",
            sha_ok and "SHA256SUMS" not in listed_names,
            len(sha_lines),
        ),
        check(
            "required probe scripts exist",
            all((platform / "tools" / name).is_file() for name in REQUIRED_SCRIPTS),
            [name for name in REQUIRED_SCRIPTS if not (platform / "tools" / name).is_file()],
            [],
        ),
        check(
            "workflow has no continue-on-error",
            "continue-on-error" not in workflow_text,
            False,
            False,
        ),
        check(
            "validator does not claim live runtime passed",
            LIVE_DISCLAIMER in validator_source,
            True,
        ),
        check(
            "static check does not start runtime probe",
            "wp10_runtime_probe.py" not in check_body,
            True,
        ),
        check(
            "WP10.6 changes stay in the authorized surface",
            git_status == "allowed",
            {"status": git_status, "extra": git_extra},
            "allowed",
        ),
        check(
            "OS argv has no DSN flags",
            not os_secret and "--exec-child" in probe_text,
            os_secret,
            False,
        ),
        check(
            "OS argv uses WP10 runtime module entry",
            os_module_entry and probe_module_entry,
            {"argv": sample, "source": probe_module_entry},
            [sys.executable, "-m", RUNTIME_MODULE],
        ),
        check(
            "evidence directory source boundary is fail-closed",
            boundary_source and all(boundary_results.values()),
            {"source": boundary_source, **boundary_results},
            True,
        ),
        check(
            "exec child restores import context",
            child_context_source,
            child_context_source,
            True,
        ),
        check(
            "isolated WP10 database topology and disposable CI store",
            database_topology_source and ci_database_topology,
            {
                "source": database_topology_source,
                "ci": ci_database_topology,
            },
            True,
        ),
        check(
            "CI persists WP10 evidence",
            "upload-artifact" in integration_block
            and "wp10-runtime-evidence" in integration_block
            and "--volume" in integration_block
            and bool(re.search(r"upload-artifact@[0-9a-f]{40}", integration_block))
            and "upload-artifact@v4\n" not in integration_block
            and "upload-artifact@v4 " not in integration_block,
            "upload-artifact" in integration_block,
        ),
        check(
            "WP10.1-WP10.5 migration probes do not upgrade head",
            migration_probes_using_head(platform) == [],
            migration_probes_using_head(platform),
            [],
        ),
        check(
            "WP10.1 migration targets 0020",
            "STAGE_REVISION" in _read(platform / "tools/wp10_1_migration_probe.py")
            and "REVISION_0020" in _read(platform / "tools/wp10_1_migration_probe.py")
            and "0020_wp10_publication_contract"
            in _read(platform / "tools/wp10_stage_revisions.py"),
            True,
        ),
        check(
            "stage-aware runtime advancement exists",
            (platform / "tools/wp10_stage_revisions.py").is_file()
            and "MAIN_DB_ADVANCE_BEFORE" in _read(platform / "tools/wp10_stage_revisions.py")
            and "0020_wp10_publication_contract"
            in _read(platform / "tools/wp10_stage_revisions.py")
            and "refusing repository head alias"
            in _read(platform / "tools/wp10_stage_revisions.py"),
            True,
        ),
        check(
            "disposable object-store guard exists",
            (platform / "tools/wp10_object_store_guard.py").is_file()
            and "uap-wp3-test-object-store-1"
            in _read(platform / "tools/wp10_object_store_guard.py")
            and "residue" in _read(platform / "tools/wp10_object_store_guard.py")
            and "object-store:8333" in _read(platform / "tools/wp10_object_store_guard.py")
            and (platform / "scripts/g10-25-disposable-object-store.sh").is_file()
            and "load_uap_env_file" in _read(platform / "scripts/g10-25-disposable-object-store.sh")
            and "UAP_G10_25_ENV_FILE"
            in _read(platform / "scripts/g10-25-disposable-object-store.sh")
            and "Process environment wins"
            in _read(platform / "scripts/g10-25-disposable-object-store.sh")
            and "platform/.env.versions and platform/.env are required"
            not in _read(platform / "scripts/g10-25-disposable-object-store.sh")
            and "trap teardown_if_needed EXIT"
            in _read(platform / "scripts/g10-25-disposable-object-store.sh")
            and "sentinel_via=sidecar"
            in _read(platform / "scripts/g10-25-disposable-object-store.sh")
            and "sentinel_via=host"
            not in _read(platform / "scripts/g10-25-disposable-object-store.sh")
            and "/app/.venv/bin/python"
            in _read(platform / "scripts/g10-25-disposable-object-store.sh")
            and "PYTHONPATH=/workspace/src"
            in _read(platform / "scripts/g10-25-disposable-object-store.sh")
            and "UAP_S3_ENDPOINT=$SIDECAR_ENDPOINT"
            in _read(platform / "scripts/g10-25-disposable-object-store.sh")
            and "host_endpoint=" in _read(platform / "scripts/g10-25-disposable-object-store.sh")
            and "sidecar_endpoint=" in _read(platform / "scripts/g10-25-disposable-object-store.sh")
            and (platform / "compose.g10-25-object-store.yaml").is_file(),
            True,
        ),
        check(
            "plan mode does not claim live passed",
            '"status": "plan"' in probe_text and "NOT RUN / NOT PROVEN" in probe_text,
            True,
        ),
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("platform", nargs="?", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    checks = evaluate(Path(args.platform))
    print(json.dumps([asdict(item) for item in checks], ensure_ascii=False, indent=2))
    failed = [item.name for item in checks if not item.passed]
    if failed:
        raise SystemExit("WP10 validation failed: " + ", ".join(failed))
    print(f"WP10 static contract validation passed; {LIVE_DISCLAIMER}")


if __name__ == "__main__":
    main()
