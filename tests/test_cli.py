from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from uap_observer.ai_analysis import AnalysisRun, ProviderHealth
from uap_observer.cli import main


class FakeAuthenticationError(RuntimeError):
    status_code = 401


class CliTests(unittest.TestCase):
    def test_deepseek_health_check_auth_failure_is_safe_and_nonzero(self) -> None:
        analyzer = Mock()
        analyzer.health_check.side_effect = FakeAuthenticationError(
            "Authentication Fails, Your api key: secret-tail"
        )
        output = io.StringIO()

        with (
            patch.dict(
                os.environ,
                {
                    "AI_PROVIDER": "deepseek",
                    "DEEPSEEK_API_KEY": "test-secret-key",
                    "DEEPSEEK_MODEL": "deepseek-v4-flash",
                },
                clear=False,
            ),
            patch("uap_observer.cli.DeepSeekAnalyzer", return_value=analyzer),
            redirect_stdout(output),
        ):
            exit_code = main(["deepseek-health-check"])

        self.assertEqual(exit_code, 1)
        analyzer.health_check.assert_called_once_with()
        self.assertIn("DeepSeek 鉴权失败（HTTP 401）", output.getvalue())
        self.assertNotIn("test-secret-key", output.getvalue())
        self.assertNotIn("secret-tail", output.getvalue())

    def test_deepseek_health_check_reports_configured_model(self) -> None:
        analyzer = Mock()
        analyzer.health_check.return_value = ProviderHealth(
            provider="DeepSeek",
            model="deepseek-v4-flash",
            available_models=2,
        )
        output = io.StringIO()

        with (
            patch.dict(
                os.environ,
                {
                    "AI_PROVIDER": "deepseek",
                    "DEEPSEEK_API_KEY": "test-secret-key",
                    "DEEPSEEK_MODEL": "deepseek-v4-flash",
                },
                clear=False,
            ),
            patch("uap_observer.cli.DeepSeekAnalyzer", return_value=analyzer),
            redirect_stdout(output),
        ):
            exit_code = main(["deepseek-health-check"])

        self.assertEqual(exit_code, 0)
        self.assertIn("model=deepseek-v4-flash", output.getvalue())
        self.assertIn("available_models=2", output.getvalue())

    def test_analyze_cli_reports_title_failures_and_returns_nonzero_on_auth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            service = Mock()
            service.run.return_value = AnalysisRun(
                titles_failed=1,
                provider_access_failed=True,
                fatal_error="DeepSeek 鉴权失败（HTTP 401）：请检查 API Key。",
            )
            output = io.StringIO()

            with (
                patch.dict(
                    os.environ,
                    {
                        "AI_PROVIDER": "deepseek",
                        "DEEPSEEK_API_KEY": "test-secret-key",
                    },
                    clear=False,
                ),
                patch("uap_observer.cli.AnalysisService", return_value=service),
                redirect_stdout(output),
            ):
                exit_code = main(
                    [
                        "--database",
                        str(database_path),
                        "analyze-articles",
                        "--limit",
                        "1",
                    ]
                )

        self.assertEqual(exit_code, 1)
        self.assertIn("titles_translated=0 titles_failed=1", output.getvalue())
        self.assertIn("AI analysis stopped: DeepSeek 鉴权失败", output.getvalue())
        self.assertNotIn("test-secret-key", output.getvalue())


if __name__ == "__main__":
    unittest.main()
