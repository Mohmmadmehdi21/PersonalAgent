import os
import json
import tempfile
import shutil
import sys
import types
import unittest
from unittest.mock import Mock, patch

import source_filter


class TestSourceFilter(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.old_cwd = os.getcwd()
        os.chdir(self.test_dir)
        os.environ["SOURCE_FILTER_ENABLED"] = "true"
        os.environ["MIN_SOURCE_DURATION_SEC"] = "480"

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_shorts_url_detection(self):
        self.assertTrue(source_filter.is_shorts_url("https://www.youtube.com/shorts/abcd1234efg"))
        self.assertTrue(source_filter.is_shorts_url("https://youtube.com/shorts/abcd1234efg?feature=share"))
        self.assertFalse(source_filter.is_shorts_url("https://www.youtube.com/watch?v=abcd1234efg"))
        self.assertFalse(source_filter.is_shorts_url("https://youtu.be/abcd1234efg"))

    def test_iso8601_duration_parsing(self):
        self.assertEqual(source_filter.parse_iso8601_duration("PT40S"), 40)
        self.assertEqual(source_filter.parse_iso8601_duration("PT1M15S"), 75)
        self.assertEqual(source_filter.parse_iso8601_duration("PT8M"), 480)
        self.assertEqual(source_filter.parse_iso8601_duration("PT20M"), 1200)
        self.assertEqual(source_filter.parse_iso8601_duration("PT1H2M30S"), 3750)
        self.assertIsNone(source_filter.parse_iso8601_duration("invalid"))
        self.assertIsNone(source_filter.parse_iso8601_duration(""))

    def test_duration_lookup_uses_noninteractive_metadata_without_oauth(self):
        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def extract_info(self, url, download=False):
                self.assert_no_download = download
                video_id = url.split("v=", 1)[1]
                return {"duration": {"LONG_VIDEO1": 600}.get(video_id)}

        ytdlp = types.ModuleType("yt_dlp")
        ytdlp.YoutubeDL = FakeYoutubeDL
        upload = types.ModuleType("upload")
        upload.get_service = Mock(side_effect=AssertionError("duration lookup must not start OAuth"))
        with patch.dict(sys.modules, {"yt_dlp": ytdlp, "upload": upload}):
            durations = source_filter.get_video_durations(["LONG_VIDEO1", "UNKNOWN_VID1"])
        self.assertEqual(durations, {"LONG_VIDEO1": 600.0, "UNKNOWN_VID1": None})
        upload.get_service.assert_not_called()

    def test_filter_candidates_skips_shorts_and_short_videos(self):
        candidates = [
            ("https://www.youtube.com/shorts/SHORT_URL_1", "Short URL Candidate"),
            ("https://www.youtube.com/watch?v=SHORT_DUR_1", "40-second video"),
            ("https://www.youtube.com/watch?v=LONG_VID_01", "20-minute video"),
            ("https://www.youtube.com/watch?v=UNKNOWN_DUR", "Unknown duration video"),
        ]

        mock_durations = {
            "SHORT_DUR_1": 40.0,    # <= 60s
            "LONG_VID_01": 1200.0,  # 20 minutes (>= 480s)
            "UNKNOWN_DUR": None,    # Fail-open
        }

        with patch("source_filter.get_video_durations", return_value=mock_durations):
            filtered = source_filter.filter_candidates(candidates)

        filtered_urls = [c[0] for c in filtered]

        # 40-sec video and Shorts URL must be skipped
        self.assertNotIn("https://www.youtube.com/shorts/SHORT_URL_1", filtered_urls)
        self.assertNotIn("https://www.youtube.com/watch?v=SHORT_DUR_1", filtered_urls)

        # 20-min video and unknown duration (fail-open) must pass
        self.assertIn("https://www.youtube.com/watch?v=LONG_VID_01", filtered_urls)
        self.assertIn("https://www.youtube.com/watch?v=UNKNOWN_DUR", filtered_urls)
        self.assertEqual(len(filtered), 2)

    def test_filter_candidates_disabled_flag(self):
        os.environ["SOURCE_FILTER_ENABLED"] = "false"
        candidates = [
            ("https://www.youtube.com/shorts/SHORT_URL_1", "Short URL Candidate"),
            ("https://www.youtube.com/watch?v=SHORT_DUR_1", "40-second video"),
        ]
        filtered = source_filter.filter_candidates(candidates)
        self.assertEqual(len(filtered), 2)

    def test_cached_clips_validation_invalid_start_renamed_to_bad(self):
        # Cache file with invalid negative start: start -4.0, end 26.0
        cache_path = os.path.join(self.test_dir, "test_invalid.clips.json")
        invalid_clips = [
            {"start": -4.0, "end": 26.0, "title": "Invalid Negative Start"},
            {"start": 10.0, "end": 45.0, "title": "Valid Clip"},
        ]
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(invalid_clips, f)

        # Validate cache
        result = source_filter.validate_cache_file(cache_path, video_duration=26.0)
        self.assertFalse(result)

        # Original file should no longer exist as .clips.json
        self.assertFalse(os.path.isfile(cache_path))

        # Must be renamed to .bad
        bad_path = cache_path + ".bad"
        self.assertTrue(os.path.isfile(bad_path))

        # Check content preserved in .bad file
        with open(bad_path, "r", encoding="utf-8") as f:
            preserved = json.load(f)
        self.assertEqual(len(preserved), 2)

    def test_cached_clips_validation_out_of_bounds_end_renamed(self):
        cache_path = os.path.join(self.test_dir, "test_oob.clips.json")
        oob_clips = [
            {"start": 10.0, "end": 500.0, "title": "Exceeds Source Duration"},
        ]
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(oob_clips, f)

        # Duration is only 300s, so end 500s is invalid
        result = source_filter.validate_cache_file(cache_path, video_duration=300.0)
        self.assertFalse(result)
        self.assertTrue(os.path.isfile(cache_path + ".bad"))

    def test_cached_clips_validation_valid_clips_kept(self):
        cache_path = os.path.join(self.test_dir, "test_valid.clips.json")
        valid_clips = [
            {"start": 10.0, "end": 45.0, "title": "Clip 1"},
            {"start": 60.0, "end": 95.0, "title": "Clip 2"},
        ]
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(valid_clips, f)

        result = source_filter.validate_cache_file(cache_path, video_duration=600.0)
        self.assertTrue(result)
        # File must remain intact
        self.assertTrue(os.path.isfile(cache_path))
        self.assertFalse(os.path.isfile(cache_path + ".bad"))


if __name__ == "__main__":
    unittest.main()
