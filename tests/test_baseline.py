"""Offline characterization tests for stable source URL behavior."""
import sys
import types
import unittest
from unittest.mock import patch


try:
    import yt_dlp  # noqa: F401
except ImportError:
    sys.modules["yt_dlp"] = types.ModuleType("yt_dlp")

from source import is_channel_url, resolve_channel_id, video_id_from_url


class SourceUrlBaselineTests(unittest.TestCase):
    def test_supported_video_url_forms(self):
        expected = "AbCdEfGhI12"
        self.assertEqual(video_id_from_url(f"https://www.youtube.com/watch?v={expected}"), expected)
        self.assertEqual(video_id_from_url(f"https://youtu.be/{expected}?feature=share"), expected)
        self.assertEqual(video_id_from_url(f"https://youtube.com/shorts/{expected}"), expected)
        self.assertEqual(video_id_from_url(f"https://youtube.com/embed/{expected}"), expected)

    def test_malformed_id_is_rejected_but_parser_does_not_validate_host(self):
        self.assertIsNone(video_id_from_url("https://www.youtube.com/@creator"))
        self.assertIsNone(video_id_from_url("https://www.youtube.com/watch?v=too-short"))
        # Characterize current behavior: host validation is left to callers.
        self.assertEqual(video_id_from_url("https://example.com/watch?v=AbCdEfGhI12"), "AbCdEfGhI12")

    def test_channel_url_recognition_is_limited_to_youtube_channel_paths(self):
        self.assertTrue(is_channel_url("https://www.youtube.com/@creator"))
        self.assertTrue(is_channel_url("https://youtube.com/channel/UC123"))
        self.assertTrue(is_channel_url("https://youtube.com/user/creator"))
        self.assertFalse(is_channel_url("https://youtu.be/AbCdEfGhI12"))
        self.assertFalse(is_channel_url("https://example.com/@creator"))

    def test_channel_id_url_resolves_without_an_api_request(self):
        channel_id = "UC" + "a" * 22
        self.assertEqual(
            resolve_channel_id(object(), f"https://www.youtube.com/channel/{channel_id}"),
            channel_id,
        )


if __name__ == "__main__":
    unittest.main()
