"""Run WP9 runtime probes in frozen order: WP9.1 -> WP9.6."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parent
_CHAIN: tuple[tuple[str, str], ...] = (
    ("wp9.1", "wp9_1_runtime_probe.py"),
    ("wp9.2", "wp9_2_runtime_probe.py"),
    ("wp9.3", "wp9_3_runtime_probe.py"),
    ("wp9.4", "wp9_4_runtime_probe.py"),
    ("wp9.5", "wp9_5_runtime_probe.py"),
    ("wp9.6", "wp9_6_runtime_probe.py"),
)


def _run(script: str) -> None:
    path = _TOOLS / script
    completed = subprocess.run(  # noqa: S603
        [sys.executable, str(path)], check=False
    )
    if completed.returncode != 0:
        raise SystemExit(f"WP9 runtime probe failed: {script}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--from",
        dest="start",
        choices=[stage for stage, _ in _CHAIN],
        default="wp9.1",
        help="First WP9 stage to run. Default wp9.1.",
    )
    args = parser.parse_args()
    started = False
    ran: list[str] = []
    for stage, script in _CHAIN:
        if stage == args.start:
            started = True
        if not started:
            continue
        print(f"== {stage} {script} ==", flush=True)
        _run(script)
        ran.append(stage)
    print("WP9 runtime probe passed: " + " ".join(ran))


if __name__ == "__main__":
    main()
