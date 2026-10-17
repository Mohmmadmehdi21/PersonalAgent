"""Offline Groq integration tests; all provider responses are mocked."""
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

# This test suite can run with the standard-library Python even when optional
# project dependencies are absent. It does not provide a Groq SDK or API key.
if "dotenv" not in sys.modules:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: False
    sys.modules["dotenv"] = dotenv

import find_clips


class GroqMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.work_patch = patch.object(find_clips, "WORK", self.temp.name)
        self.work_patch.start()
        self.env_patch = patch.dict(os.environ, {"GROQ_API_KEY": "mock-key-not-a-secret"})
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.work_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def transcript():
        return [{"start": 0, "end": 35,
                 "text": "A detailed transcript gives this candidate enough spoken context "
                         "to evaluate the clip and confirm that the generated selection remains "
                         "grounded in the source material."}]

    @staticmethod
    def response(content):
        message = types.SimpleNamespace(content=content)
        choice = types.SimpleNamespace(message=message)
        usage = types.SimpleNamespace(prompt_tokens=12, completion_tokens=18, total_tokens=30)
        return types.SimpleNamespace(choices=[choice], usage=usage)

    def run_with_response(self, content):
        payload = self.response(content)
        fake_client = types.SimpleNamespace(chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=lambda **kwargs: payload)))
        with patch.object(find_clips, "create_groq_client", return_value=fake_client), \
             patch.object(find_clips, "retry_groq", side_effect=lambda call: call()), \
             patch.object(find_clips, "_store_analysis"):
            return find_clips.find_clips(self.transcript(), 60, 1, video_id="mock-video")

    def test_valid_json_chat_completion_is_normalized(self):
        clips = self.run_with_response(json.dumps({"clips": [{
            "start": 0, "end": 30, "hook_score": 9,
            "reason": "The transcript captures a clear reveal and reaction.",
            "title": "The Reveal", "commentary_script": "This moment changes the outcome because the response reframes the entire exchange.",
        }]}))
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0]["analysis_status"], "ai_success")
        self.assertEqual(clips[0]["hook_score"], 9)

    def test_malformed_response_uses_marked_existing_fallback(self):
        fallback = [{"start": 0, "end": 30, "hook_score": 8, "title": "Local",
                     "commentary_script": "Fallback result"}]
        with patch.object(find_clips, "local_fallback_clips", return_value=fallback):
            clips = self.run_with_response("not valid json")
        self.assertEqual(clips[0]["analysis_status"], "local_fallback")
        self.assertIn("invalid", clips[0]["analysis_reason"])

    def test_missing_key_uses_explicit_fallback_without_client_creation(self):
        fallback = [{"start": 0, "end": 30, "hook_score": 8, "title": "Local",
                     "commentary_script": "Fallback result"}]
        with patch.dict(os.environ, {"GROQ_API_KEY": ""}), \
             patch.object(find_clips, "local_fallback_clips", return_value=fallback), \
             patch.object(find_clips, "create_groq_client") as create_client:
            clips = find_clips.find_clips(self.transcript(), 60, 1)
        create_client.assert_not_called()
        self.assertEqual(clips[0]["analysis_status"], "local_fallback")


if __name__ == "__main__":
    unittest.main()
