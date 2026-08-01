from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
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
            build = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-deps",
                    "--no-build-isolation",
                    "--wheel-dir",
                    str(wheel_directory),
                    str(PROJECT_ROOT),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(build.returncode, 0, build.stdout + "\n" + build.stderr)
            wheel = next(wheel_directory.glob("uap_observer-*.whl"))
            with zipfile.ZipFile(wheel) as archive:
                packaged_files = set(archive.namelist())
            self.assertIn("uap_observer/resources/site.css", packaged_files)
            self.assertIn("uap_observer/resources/site.js", packaged_files)
            self.assertIn("uap_observer/resources/og.png", packaged_files)
            self.assertIn("uap_observer/resources/silver-metal-background-hero.png", packaged_files)
            create_venv = subprocess.run(
                [sys.executable, "-m", "venv", "--system-site-packages", str(venv_directory)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(create_venv.returncode, 0, create_venv.stdout + "\n" + create_venv.stderr)
            executable = venv_directory / "bin" / "uap-observer"
            if os.name == "nt":
                executable = venv_directory / "Scripts" / "uap-observer.exe"
            install = subprocess.run(
                [
                    str(venv_directory / "bin" / "python"),
                    "-m",
                    "pip",
                    "install",
                    "--force-reinstall",
                    "--no-deps",
                    str(wheel),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(install.returncode, 0, install.stdout + "\n" + install.stderr)
            environment = {
                **os.environ,
                "UAP_DB_PATH": str(database_path),
                # Reuse the test runner's already-installed dependencies while
                # the application itself comes from the wheel.
                "PYTHONPATH": get_paths()["purelib"],
            }
            init = subprocess.run(
                [str(executable), "init-db"],
                capture_output=True,
                text=True,
                env=environment,
                check=False,
            )
            self.assertEqual(init.returncode, 0, init.stdout + "\n" + init.stderr)
            sync = subprocess.run(
                [str(executable), "sync-sources"],
                capture_output=True,
                text=True,
                env=environment,
                check=False,
            )
            self.assertEqual(sync.returncode, 0, sync.stdout + "\n" + sync.stderr)

            self.assertIn("005_organizations.sql", init.stdout)
            self.assertIn("Synced 21 source(s)", sync.stdout)
            self.assertTrue(database_path.exists())


if __name__ == "__main__":
    unittest.main()
