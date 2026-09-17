"""Unit and static contract tests for the G10-27 measurement probe."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from tools.wp10_6_performance_probe import (
    DEFAULT_CLAIMS,
    DEFAULT_DOCUMENTS,
    DEFAULT_ENTITIES,
    DEFAULT_FRESHNESS_SAMPLES,
    DEFAULT_GRANT_RATE,
    DatasetSpec,
    LatencySample,
    LoggedProcess,
    LoggedProcessGroup,
    _request,
    freshness_url,
    percentile,
    stable_uuid,
    summarize,
    workload_paths,
)

PLATFORM = Path(__file__).resolve().parents[1]


def test_frozen_capacity_defaults_and_valid_ranges() -> None:
    spec = DatasetSpec()
    assert (spec.documents, spec.claims, spec.entities) == (
        DEFAULT_DOCUMENTS,
        DEFAULT_CLAIMS,
        DEFAULT_ENTITIES,
    )
    assert (spec.claims, spec.entities) == (1_000, 500)
    spec.validate()
    for invalid in (
        DatasetSpec(documents=0),
        DatasetSpec(documents=10, claims=11),
        DatasetSpec(documents=10, claims=5, entities=6),
    ):
        with pytest.raises(ValueError):
            invalid.validate()


def test_deterministic_identity_and_workload_schedule() -> None:
    first = [stable_uuid("seed", "document", index) for index in range(20)]
    second = [stable_uuid("seed", "document", index) for index in range(20)]
    assert first == second
    assert len(set(first)) == 20
    assert workload_paths("seed", first, 50) == workload_paths("seed", second, 50)
    assert (
        workload_paths("different", first, 50)["detail"]
        != workload_paths("seed", second, 50)["detail"]
    )
    assert freshness_url("seed", 1) == freshness_url("seed", 1)
    assert freshness_url("seed", 1) != freshness_url("other-seed", 1)


def test_nearest_rank_percentile_keeps_tail_samples() -> None:
    values = [float(item) for item in range(1, 101)]
    assert percentile(values, 0.50) == 50
    assert percentile(values, 0.95) == 95
    assert percentile(values, 0.99) == 99
    assert percentile(values, 1.0) == 100
    with pytest.raises(ValueError):
        percentile([], 0.95)


def test_summary_retains_failures_and_timeouts() -> None:
    start = datetime(2026, 9, 9, tzinfo=UTC)
    samples = [
        LatencySample(
            "documents",
            0,
            "/v1/documents?limit=20",
            200,
            10.0,
            start.isoformat(),
            (start + timedelta(seconds=1)).isoformat(),
            None,
        ),
        LatencySample(
            "documents",
            1,
            "/v1/documents?limit=20",
            500,
            20.0,
            start.isoformat(),
            (start + timedelta(seconds=2)).isoformat(),
            None,
        ),
        LatencySample(
            "documents",
            2,
            "/v1/documents?limit=20",
            None,
            30.0,
            start.isoformat(),
            (start + timedelta(seconds=3)).isoformat(),
            "TimeoutError",
        ),
    ]
    result = summarize(samples)
    assert result["samples"] == 3
    assert result["failures"] == 2
    assert result["p99_ms"] == 30.0
    assert result["error_rate"] == pytest.approx(2 / 3)


def test_plan_is_real_module_entrypoint_and_machine_readable() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "tools.wp10_6_performance_probe", "plan"],
        cwd=PLATFORM,
        env={"PATH": "", "PYTHONPATH": f"{PLATFORM / 'src'}:{PLATFORM}"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    output = json.loads(completed.stdout)
    assert output["status"] == "plan"
    assert output["dataset"]["documents"] == 100_000
    assert output["capacity"]["thresholds"]["search"]["p99_ms"] == 1200.0
    assert output["freshness"]["samples"] == DEFAULT_FRESHNESS_SAMPLES == 120
    assert output["freshness"]["grant_rate_per_second"] == DEFAULT_GRANT_RATE == 2.0


def test_probe_does_not_disable_integrity_or_seed_public_tables_directly() -> None:
    source = (PLATFORM / "tools/wp10_6_performance_probe.py").read_text(encoding="utf-8")
    assert "session_replication_role" not in source
    assert "DISABLE TRIGGER" not in source
    assert "COPY public." not in source
    assert "INSERT INTO public.documents" not in source
    assert "audit.record_review_decision" in source
    assert "ops.rebuild_public_projection" in source
    assert "from uap_platform.publishing.loop import main; main()" in source
    assert "uap_platform.public_api.server" in source


def test_freshness_timer_starts_after_grant_commit_returns() -> None:
    source = (PLATFORM / "tools/wp10_6_performance_probe.py").read_text(encoding="utf-8")
    freshness = source[source.index("def measure_freshness(") :]
    decision_call = freshness.index("decision_id = _api_call(")
    commit_recorded = freshness.index("committed_at = utc_now()", decision_call)
    timer_started = freshness.index("committed_monotonic = time.perf_counter()", commit_recorded)
    assert decision_call < commit_recorded < timer_started
    assert "record_review_decision commit return through public HTTP" in freshness
    assert "normal-load freshness requires a settled publication queue baseline" in freshness


def test_document_detail_is_measured_separately_from_collection() -> None:
    paths = workload_paths("seed", [UUID("00000000-0000-4000-8000-000000000001")], 2)
    assert set(paths) == {"documents", "detail", "search"}
    assert all(
        path == "/v1/documents/00000000-0000-4000-8000-000000000001" for path in paths["detail"]
    )


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_logged_process_does_not_block_after_more_than_pipe_capacity(tmp_path: Path) -> None:
    port = _free_loopback_port()
    child = """
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        sys.stderr.write('L' * 2048 + '\\n')
        sys.stderr.flush()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{}')
    def log_message(self, *_args):
        return
ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
"""
    managed = None
    with LoggedProcessGroup(tmp_path) as processes:
        managed = processes.start(
            "noisy-api",
            [sys.executable, "-c", child, str(port)],
            cwd=PLATFORM,
            env=os.environ.copy(),
        )
        for _ in range(100):
            sample = _request(f"http://127.0.0.1:{port}", "/healthz", "test", 0)
            if sample.status == 200:
                break
            time.sleep(0.01)
        assert sample.status == 200
        statuses = [
            _request(f"http://127.0.0.1:{port}", "/v1/search?q=x", "test", index).status
            for index in range(200)
        ]
        managed.log_file.flush()
        assert statuses == [200] * 200
        assert managed.log_path.stat().st_size > 64 * 1024
        assert managed.process.poll() is None
    assert managed is not None and managed.process.poll() is not None


def _wait_for_log_marker(child: LoggedProcess, marker: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        output = child.log_path.read_text(encoding="utf-8")
        if marker in output:
            return
        return_code = child.process.poll()
        if return_code is not None:
            pytest.fail(
                f"{child.name} exited with {return_code} before log marker {marker!r}; "
                f"log={output!r}"
            )
        time.sleep(0.01)
    output = child.log_path.read_text(encoding="utf-8")
    pytest.fail(
        f"timed out after {timeout:.2f}s waiting for {child.name} log marker {marker!r}; "
        f"pid={child.process.pid}; log={output!r}"
    )


def test_partial_startup_failure_stops_child_and_preserves_log(tmp_path: Path) -> None:
    managed = None
    with pytest.raises(FileNotFoundError):
        with LoggedProcessGroup(tmp_path) as processes:
            managed = processes.start(
                "publisher",
                [
                    sys.executable,
                    "-c",
                    "import time; time.sleep(0.2); "
                    "print('publisher-started', flush=True); time.sleep(60)",
                ],
                cwd=PLATFORM,
                env=os.environ.copy(),
            )
            _wait_for_log_marker(managed, "publisher-started", timeout=5.0)
            processes.start(
                "cannot-start",
                [str(tmp_path / "missing-executable")],
                cwd=PLATFORM,
                env=os.environ.copy(),
            )
    assert managed is not None and managed.process.poll() is not None
    assert "publisher-started" in managed.log_path.read_text(encoding="utf-8")
