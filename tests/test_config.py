from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from uap_observer.config import Settings


class SettingsTests(unittest.TestCase):
    def test_deepseek_is_auto_selected_when_only_deepseek_key_exists(self) -> None:
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True):
            self.assertEqual(Settings.from_environment().ai_provider, "deepseek")

    def test_openai_is_selected_when_deepseek_key_is_absent(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True):
            self.assertEqual(Settings.from_environment().ai_provider, "openai")

    def test_explicit_provider_overrides_key_detection(self) -> None:
        with patch.dict(
            os.environ,
            {"AI_PROVIDER": "openai", "DEEPSEEK_API_KEY": "test-key"},
            clear=True,
        ):
            self.assertEqual(Settings.from_environment().ai_provider, "openai")


if __name__ == "__main__":
    unittest.main()
