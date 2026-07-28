from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from sysconfig import get_paths

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_wheel_install_can_initialize_database_and_sync_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel_directory = root / "wheel"
            venv_directory = root / "venv"
            database_path = root / "data" / "uap.db"
            wheel_directory.mkdir()
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-deps",
                    "--wheel-dir",
                    str(wheel_directory),
                    str(PROJECT_ROOT),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            wheel = next(wheel_directory.glob("uap_observer-*.whl"))
            subprocess.run(
                [sys.executable, "-m", "venv", "--system-site-packages", str(venv_directory)],
                check=True,
                capture_output=True,
                text=True,
            )
            executable = venv_directory / "bin" / "uap-observer"
            if os.name == "nt":
                executable = venv_directory / "Scripts" / "uap-observer.exe"
            subprocess.run(
                [str(venv_directory / "bin" / "python"), "-m", "pip", "install", "--no-deps", str(wheel)],
                check=True,
                capture_output=True,
                text=True,
            )
            environment = {
                **os.environ,
                "UAP_DB_PATH": str(database_path),
                # Reuse the test runner's already-installed dependencies while
                # the application itself comes from the wheel.
                "PYTHONPATH": get_paths()["purelib"],
            }
            init = subprocess.run(
                [str(executable), "init-db"],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
            sync = subprocess.run(
                [str(executable), "sync-sources"],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )

            self.assertIn("005_organizations.sql", init.stdout)
            self.assertIn("Synced 4 source(s)", sync.stdout)
            self.assertTrue(database_path.exists())


if __name__ == "__main__":
    unittest.main()
