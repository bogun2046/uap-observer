from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from uap_observer.ai_analysis import AnalysisRun, ProviderFailure, ProviderHealth
from uap_observer.article_extraction import ExtractionRun
from uap_observer.cli import main


class FakeAuthenticationError(RuntimeError):
    status_code = 401


class CliTests(unittest.TestCase):
    def test_extract_cli_reports_duplicate_skip_without_failing_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            service = Mock()
            service.run.return_value = ExtractionRun(
                queued=2,
                claimed=2,
                completed=1,
                failed=0,
                skipped_duplicates=1,
                skipped_unavailable=1,
            )
            output = io.StringIO()

            with (
                patch("uap_observer.cli.ArticleExtractionService", return_value=service),
                redirect_stdout(output),
            ):
                exit_code = main(
                    [
                        "--database",
                        str(database_path),
                        "extract-articles",
                        "--limit",
                        "2",
                    ]
                )

        self.assertEqual(exit_code, 0)
        service.run.assert_called_once_with(
            limit=2,
            retry_failed=False,
            retry_blocked=False,
            max_failed_attempts=3,
        )
        self.assertIn("queued=2 claimed=2", output.getvalue())
        self.assertIn("completed=1 failed=0", output.getvalue())
        self.assertIn("skipped_duplicates=1", output.getvalue())
        self.assertIn("skipped_unavailable=1", output.getvalue())

    def test_extract_cli_can_force_retry_exhausted_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            service = Mock()
            service.run.return_value = ExtractionRun()

            with patch(
                "uap_observer.cli.ArticleExtractionService",
                return_value=service,
            ):
                exit_code = main(
                    [
                        "--database",
                        str(database_path),
                        "extract-articles",
                        "--retry-failed",
                        "--force-retry-exhausted",
                    ]
                )

        self.assertEqual(exit_code, 0)
        service.run.assert_called_once_with(
            limit=20,
            retry_failed=True,
            retry_blocked=False,
            max_failed_attempts=None,
        )

    def test_extract_cli_rejects_force_without_retry_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with self.assertRaisesRegex(
                SystemExit,
                "--force-retry-exhausted requires --retry-failed",
            ):
                main(
                    [
                        "--database",
                        str(database_path),
                        "extract-articles",
                        "--force-retry-exhausted",
                    ]
                )

    def test_daily_workflow_caps_failed_extraction_retries(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "daily-uap.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("--max-failed-attempts 3", workflow)
        self.assertIn("Extraction metadata-only", workflow)
        self.assertIn("reddit-metadata-only", workflow)

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

    def test_analyze_cli_reports_safe_item_failure_and_blocks_publishing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            service = Mock()
            service.run.return_value = AnalysisRun(
                titles_failed=1,
                failures=(
                    ProviderFailure(
                        stage="title_translation",
                        news_id=391,
                        attempts=3,
                        error="DeepSeek 响应无效（title_invalid_json）。",
                        response_id="resp_safe_391",
                    ),
                ),
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

        diagnostic = output.getvalue()
        self.assertEqual(exit_code, 1)
        self.assertIn("stage=title_translation news_id=391", diagnostic)
        self.assertIn("provider_attempts=3", diagnostic)
        self.assertIn("response_id=resp_safe_391", diagnostic)
        self.assertIn("public publishing is blocked", diagnostic)
        self.assertNotIn("test-secret-key", diagnostic)


if __name__ == "__main__":
    unittest.main()
