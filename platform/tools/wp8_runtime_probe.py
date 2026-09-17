"""Run WP runtime probes in frozen order: WP3 -> WP4 -> WP5 -> WP6 -> WP7 -> WP8."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parent
_CHAIN: tuple[tuple[str, str], ...] = (
    ("wp3", "wp3_runtime_probe.py"),
    ("wp4", "wp4_runtime_probe.py"),
    ("wp5", "wp5_runtime_probe.py"),
    ("wp6", "wp6_runtime_probe.py"),
    ("wp7", "wp7_runtime_probe.py"),
    ("wp8.1", "wp8_1_runtime_probe.py"),
    ("wp8.3", "wp8_3_runtime_probe.py"),
    ("wp8.4", "wp8_4_runtime_probe.py"),
    ("wp8.5", "wp8_5_runtime_probe.py"),
    ("wp8.6", "wp8_6_runtime_probe.py"),
)


def _run(script: str) -> None:
    path = _TOOLS / script
    completed = subprocess.run(  # noqa: S603
        [sys.executable, str(path)], check=False
    )
    if completed.returncode != 0:
        raise SystemExit(f"WP runtime probe failed: {script}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--from",
        dest="start",
        choices=[stage for stage, _ in _CHAIN],
        default="wp3",
        help="First stage to run. Default wp3 (full G8-20 chain). CI uses wp8.1 after WP7.",
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
    print("WP8 runtime probe passed: " + " ".join(ran))


if __name__ == "__main__":
    main()
