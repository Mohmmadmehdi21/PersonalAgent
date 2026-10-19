import os
import json
import tempfile
import shutil
import unittest
from unittest.mock import patch

import guard


class TestDuplicateGuard(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.old_cwd = os.getcwd()
        os.chdir(self.test_dir)
        os.environ["GUARD_ENABLED"] = "true"
        os.environ["MAX_CLIPS_PER_VIDEO"] = "4"
        os.environ["MIN_GAP_SEC"] = "10"
        os.environ["GUARD_COUNT_DRY_RUN"] = "false"

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_video_id_normalization_across_formats(self):
        expected_id = "1WEAJ-DFkHE"
        urls = [
            "https://youtu.be/1WEAJ-DFkHE?si=rsDsae426Qe_rTG2",
            "https://youtu.be/1WEAJ-DFkHE?si=57iNTRsctXuM4Ymp",
            "https://www.youtube.com/watch?v=1WEAJ-DFkHE&t=30s&list=PL123",
            "https://m.youtube.com/watch?v=1WEAJ-DFkHE",
            "https://music.youtube.com/watch?v=1WEAJ-DFkHE",
            "https://www.youtube.com/shorts/1WEAJ-DFkHE",
            "https://www.youtube.com/embed/1WEAJ-DFkHE",
            "https://www.youtube.com/live/1WEAJ-DFkHE?feature=share",
        ]
        for u in urls:
            extracted = guard.video_id_from_url(u)
            self.assertEqual(extracted, expected_id, f"Failed extracting from {u}")

    def test_non_youtube_url_sha1_fallback(self):
        non_yt = "https://example.com/custom/video/stream.mp4"
        vid = guard.video_id_from_url(non_yt)
        self.assertIsNotNone(vid)
        self.assertEqual(len(vid), 40)  # sha1 hex digest length

    def test_bootstrap_from_state_json(self):
        # Create a mock state.json with done videos and clips
        state_data = {
            "done": ["https://youtu.be/1WEAJ-DFkHE?si=abc"],
            "done_videos": ["rqsRdfdN4uI"],
            "clips": ["rqsRdfdN4uI:10-45", "1WEAJ-DFkHE:20-55"],
            "videos": {}
        }
        with open("state.json", "w", encoding="utf-8") as f:
            json.dump(state_data, f)

        # Run bootstrap against the temporary working directory only.
        guard.bootstrap()

        ledger = guard.load_ledger()
        self.assertIn("1WEAJ-DFkHE", ledger)
        self.assertIn("rqsRdfdN4uI", ledger)
        self.assertEqual(ledger["1WEAJ-DFkHE"]["status"], "consumed")
        self.assertEqual(ledger["rqsRdfdN4uI"]["status"], "consumed")
        self.assertTrue(len(ledger["rqsRdfdN4uI"]["clips"]) >= 1)

    def test_sync_state_marks_all_source_variants_as_done(self):
        # Ledger has video '1WEAJ-DFkHE' consumed
        os.makedirs("data", exist_ok=True)
        ledger = {
            "1WEAJ-DFkHE": {
                "url": "https://youtu.be/1WEAJ-DFkHE",
                "status": "consumed",
                "clips": [{"start": 10.0, "end": 45.0, "uploaded": True}]
            }
        }
        with open("data/used_videos.json", "w", encoding="utf-8") as f:
            json.dump(ledger, f)

        # sources.txt contains multiple URL variants of the same video
        with open("sources.txt", "w", encoding="utf-8") as f:
            f.write("https://youtu.be/1WEAJ-DFkHE?si=variant1 | Note 1\n")
            f.write("https://www.youtube.com/watch?v=1WEAJ-DFkHE&si=variant2\n")
            f.write("https://youtu.be/ANOTHER_VID?si=123\n")

        state = {"done": [], "done_videos": []}
        guard.sync_state(state)

        # Both variants of 1WEAJ-DFkHE should now be in state["done"]
        self.assertIn("https://youtu.be/1WEAJ-DFkHE?si=variant1", state["done"])
        self.assertIn("https://www.youtube.com/watch?v=1WEAJ-DFkHE&si=variant2", state["done"])
        self.assertIn("1WEAJ-DFkHE", state["done_videos"])
        # The other unprocessed video must not be marked done
        self.assertNotIn("https://youtu.be/ANOTHER_VID?si=123", state["done"])

    def test_claim_and_record_lifecycle(self):
        vid = "TEST_VID_001"
        url = f"https://www.youtube.com/watch?v={vid}"

        # First claim should succeed
        self.assertTrue(guard.claim(vid, url))

        # Recording the first clip marks it consumed
        guard.record_clip(vid, 10.0, 45.0, uploaded=True, dry_run=False)

        ledger = guard.load_ledger()
        self.assertEqual(ledger[vid]["status"], "consumed")
        self.assertEqual(len(ledger[vid]["clips"]), 1)

        # Subsequent claim on consumed video must return False
        self.assertFalse(guard.claim(vid, url))

    def test_filter_clips_overlap_and_gap(self):
        vid = "TEST_VID_002"
        # Pre-record a clip at [20.0 - 55.0]
        guard.record_clip(vid, 20.0, 55.0, uploaded=True, dry_run=False)

        candidates = [
            {"start": 25.0, "end": 50.0, "hook_score": 9, "title": "Inside Overlap"},
            {"start": 58.0, "end": 90.0, "hook_score": 8, "title": "Too Close to End (<10s gap)"},
            {"start": 66.0, "end": 100.0, "hook_score": 8, "title": "Valid Separated Clip (>10s gap)"},
        ]

        filtered = guard.filter_clips(vid, candidates)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["title"], "Valid Separated Clip (>10s gap)")

    def test_filter_clips_intra_list_overlap_prefers_higher_hook_score(self):
        vid = "TEST_VID_003"
        candidates = [
            {"start": 10.0, "end": 45.0, "hook_score": 6, "title": "Lower Score Clashing"},
            {"start": 15.0, "end": 50.0, "hook_score": 9, "title": "Higher Score Clashing"},
            {"start": 70.0, "end": 105.0, "hook_score": 7, "title": "Independent Clip"},
        ]

        filtered = guard.filter_clips(vid, candidates)
        self.assertEqual(len(filtered), 2)
        titles = [c["title"] for c in filtered]
        self.assertIn("Higher Score Clashing", titles)
        self.assertNotIn("Lower Score Clashing", titles)
        self.assertIn("Independent Clip", titles)

    def test_filter_clips_respects_max_limit(self):
        vid = "TEST_VID_004"
        os.environ["MAX_CLIPS_PER_VIDEO"] = "3"

        # Pre-record 2 clips
        guard.record_clip(vid, 10.0, 45.0, uploaded=True, dry_run=False)
        guard.record_clip(vid, 60.0, 95.0, uploaded=True, dry_run=False)

        # 3 candidate clips provided; only 1 slot remains
        candidates = [
            {"start": 110.0, "end": 145.0, "hook_score": 9, "title": "Slot 3 Candidate A"},
            {"start": 160.0, "end": 195.0, "hook_score": 8, "title": "Slot 4 Exceeding Candidate B"},
        ]

        filtered = guard.filter_clips(vid, candidates)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["title"], "Slot 3 Candidate A")

    def test_corrupt_ledger_recovery(self):
        os.makedirs("data", exist_ok=True)
        # Write corrupted garbage bytes to used_videos.json
        with open("data/used_videos.json", "w", encoding="utf-8") as f:
            f.write("{invalid json corrupt content...!!!")

        # Loading must not crash, renames corrupt file, returns empty dict
        ledger = guard.load_ledger()
        self.assertEqual(ledger, {})

        # Corrupt backup file must exist
        files = os.listdir("data")
        corrupt_files = [fn for fn in files if "corrupt" in fn]
        self.assertTrue(len(corrupt_files) >= 1)

    def test_guard_disabled_bypass(self):
        os.environ["GUARD_ENABLED"] = "false"
        vid = "TEST_VID_005"
        # Claim always True
        self.assertTrue(guard.claim(vid, "url"))

        clips = [
            {"start": 10.0, "end": 45.0, "hook_score": 9},
            {"start": 12.0, "end": 47.0, "hook_score": 8},
        ]
        # Filter returns original list unchanged
        filtered = guard.filter_clips(vid, clips)
        self.assertEqual(len(filtered), 2)


if __name__ == "__main__":
    unittest.main()
