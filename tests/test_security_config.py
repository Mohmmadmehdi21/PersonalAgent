import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import logger


def load_config(environ):
    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *a, **k: False
    module_path = Path(__file__).resolve().parents[1] / "config.py"
    spec = importlib.util.spec_from_file_location("phase3_config_test", module_path)
    config = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"dotenv": dotenv_stub}), patch.dict(os.environ, environ, clear=True):
        spec.loader.exec_module(config)
    return config


def validate(config, environ, command):
    with patch.dict(os.environ, environ, clear=True), patch.object(config, "load_project_env", return_value=None):
        return config.validate_startup_config(command)


class SecretRedactionTests(unittest.TestCase):
    def test_configured_nonstandard_secret_is_redacted_everywhere(self):
        secret_value = "synthetic-secret-value-9284"
        with patch.dict(os.environ, {"MY_PROVIDER_API_KEY": secret_value}):
            result = logger.mask_secrets(
                f"request failed for {secret_value}; https://api.telegram.org/bot{secret_value}/sendMessage"
            )
        self.assertNotIn(secret_value, result)
        self.assertIn("********", result)

    def test_bearer_headers_and_credential_file_paths_are_redacted(self):
        sample = "Authorization: Bearer fake.access.token /tmp/profile/token_growth.json refresh_token=1//syntheticrefreshvaluenotreal12345"
        result = logger.mask_secrets(sample)
        self.assertNotIn("fake.access.token", result)
        self.assertNotIn("syntheticrefreshvaluenotreal12345", result)
        self.assertNotIn("/tmp/profile/token_growth.json", result)

    def test_telegram_commands_and_task_instructions_are_not_masked(self):
        message = "Use /task 7b10e3d79776 for status. Commands: /start /help /status /health."
        self.assertEqual(logger.mask_secrets(message), message)

    def test_user_home_path_is_redacted(self):
        home = str(Path.home())
        result = logger.mask_secrets(f"private path: {home}\\Documents\\secrets.txt")
        self.assertNotIn(home, result)
        self.assertIn("<USER_HOME>", result)

    def test_telegram_payload_masks_secret_text_without_network_access(self):
        dotenv_stub = types.ModuleType("dotenv")
        dotenv_stub.load_dotenv = lambda *a, **k: False
        notify_path = Path(__file__).resolve().parents[1] / "notify.py"
        spec = importlib.util.spec_from_file_location("phase3_notify_test", notify_path)
        notify = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"dotenv": dotenv_stub}):
            spec.loader.exec_module(notify)

        token = "9876543210:syntheticTelegramTokenValue_1234567890123456789"
        captured = []

        class Response:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        def fake_urlopen(request, timeout):
            captured.append(json.loads(request.data.decode("utf-8")))
            return Response()

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": "12345"}):
            with patch.object(notify.urllib.request, "urlopen", side_effect=fake_urlopen):
                self.assertTrue(notify.send_telegram(f"Use /task task-123 for status. A user pasted {token} into a message."))
        self.assertNotIn(token, captured[0]["text"])
        self.assertIn("********", captured[0]["text"])
        self.assertIn("/task task-123", captured[0]["text"])


class StartupConfigTests(unittest.TestCase):
    def test_missing_telegram_configuration_fails_without_echoing_values(self):
        config = load_config({"NOTIFY_CHANNEL": "telegram", "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""})
        with self.assertRaises(config.ConfigurationError) as raised:
            validate(config, {"NOTIFY_CHANNEL": "telegram", "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}, "run")
        self.assertIn("TELEGRAM_BOT_TOKEN", str(raised.exception))
        self.assertNotIn("fake", str(raised.exception))

    def test_cleanup_does_not_require_pipeline_credentials(self):
        config = load_config({"NOTIFY_CHANNEL": "telegram"})
        self.assertEqual(validate(config, {"NOTIFY_CHANNEL": "telegram"}, "cleanup"), [])

    def test_live_render_requires_youtube_auth_configuration(self):
        config = load_config({"NOTIFY_CHANNEL": "none", "DRY_RUN": "false"})
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as tmp:
            previous = os.getcwd()
            os.chdir(tmp)
            try:
                with self.assertRaises(config.ConfigurationError) as raised:
                    validate(config, {"NOTIFY_CHANNEL": "none", "DRY_RUN": "false"}, "render")
                self.assertIn("token.json", str(raised.exception))
                self.assertNotIn("secret-value", str(raised.exception))
            finally:
                os.chdir(previous)

    def test_prepare_with_direct_video_sources_does_not_require_oauth(self):
        config = load_config({"NOTIFY_CHANNEL": "none", "DRY_RUN": "true"})
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as tmp:
            Path(tmp, "sources.txt").write_text("https://www.youtube.com/watch?v=abcdefghijk\n", encoding="utf-8")
            previous = os.getcwd()
            os.chdir(tmp)
            try:
                warnings = validate(config, {"NOTIFY_CHANNEL": "none", "DRY_RUN": "true"}, "prepare")
                self.assertFalse(any("OAuth" in warning for warning in warnings))
            finally:
                os.chdir(previous)

    def test_prepare_with_channel_sources_requires_oauth(self):
        config = load_config({"NOTIFY_CHANNEL": "none", "DRY_RUN": "true"})
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as tmp:
            Path(tmp, "sources.txt").write_text("https://www.youtube.com/@sample\n", encoding="utf-8")
            previous = os.getcwd()
            os.chdir(tmp)
            try:
                with self.assertRaises(config.ConfigurationError) as raised:
                    validate(config, {"NOTIFY_CHANNEL": "none", "DRY_RUN": "true"}, "prepare")
                self.assertIn("YouTube OAuth is required", str(raised.exception))
            finally:
                os.chdir(previous)

    def test_invalid_integer_has_setting_name_but_not_input_value(self):
        with self.assertRaisesRegex(ValueError, "CLIPS_PER_DAY"):
            load_config({"CLIPS_PER_DAY": "not-a-number-private"})

    def test_agent_platform_partial_configuration_is_rejected(self):
        config = load_config({"NOTIFY_CHANNEL": "none", "GOOGLE_CLOUD_PROJECT": "sample-project"})
        with self.assertRaises(config.ConfigurationError) as raised:
            validate(config, {"NOTIFY_CHANNEL": "none", "GOOGLE_CLOUD_PROJECT": "sample-project"}, "run")
        self.assertIn("GOOGLE_CLOUD_LOCATION", str(raised.exception))
        self.assertIn("VERTEX_AI_MODEL", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
