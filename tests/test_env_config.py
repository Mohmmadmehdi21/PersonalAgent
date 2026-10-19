import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from env_config import load_project_env
from telegram_bot.access import AccessConfigurationError, get_owner_ids


class ProjectEnvLoadingTests(unittest.TestCase):
    def _env_file(self, contents):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        path = Path(temp.name) / ".env"
        path.write_text(contents, encoding="utf-8")
        self.addCleanup(temp.cleanup)
        return path

    def test_loads_owner_ids_from_configured_project_env_path(self):
        path = self._env_file("TELEGRAM_OWNER_IDS=123456789,987654321\n")
        with patch("env_config.ENV_FILE", path), patch.dict(os.environ, {"TELEGRAM_OWNER_IDS": ""}, clear=True):
            self.assertEqual(get_owner_ids(), frozenset({123456789, 987654321}))

    def test_telegram_startup_loads_file_before_required_setting_validation(self):
        import config
        path = self._env_file("TELEGRAM_BOT_TOKEN=test-token\nTELEGRAM_OWNER_IDS=123456789\n")
        with patch("env_config.ENV_FILE", path), patch.dict(
                os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_OWNER_IDS": ""}, clear=True):
            self.assertEqual(config.validate_startup_config("telegram-bot"), [])

    def test_nonempty_process_override_wins(self):
        path = self._env_file("TELEGRAM_OWNER_IDS=111\n")
        with patch.dict(os.environ, {"TELEGRAM_OWNER_IDS": "222"}, clear=True):
            load_project_env(path)
            self.assertEqual(os.environ["TELEGRAM_OWNER_IDS"], "222")

    def test_empty_process_override_falls_back_to_env_file(self):
        path = self._env_file("TELEGRAM_OWNER_IDS=123456789\n")
        with patch.dict(os.environ, {"TELEGRAM_OWNER_IDS": ""}, clear=True):
            load_project_env(path)
            self.assertEqual(os.environ["TELEGRAM_OWNER_IDS"], "123456789")

    def test_missing_empty_and_malformed_file_values_fail_closed(self):
        for contents in ("OTHER_SETTING=value\n", "TELEGRAM_OWNER_IDS=\n", "TELEGRAM_OWNER_IDS=123,nope\n"):
            path = self._env_file(contents)
            with self.subTest(contents=contents), patch("env_config.ENV_FILE", path), patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(AccessConfigurationError) as raised:
                    get_owner_ids()
                self.assertIn("TELEGRAM_OWNER_IDS", str(raised.exception))
                self.assertNotIn("123", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
