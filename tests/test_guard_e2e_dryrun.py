"""Simulates an end-to-end pipeline run demonstrating duplicate guard behavior."""
import os
import json
import tempfile
import shutil
import unittest

import guard


class TestGuardEndToEndDryRun(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.old_cwd = os.getcwd()
        os.chdir(self.test_dir)
        os.environ["GUARD_ENABLED"] = "true"
        os.environ["MAX_CLIPS_PER_VIDEO"] = "4"
        os.environ["MIN_GAP_SEC"] = "10"
        os.environ["GUARD_COUNT_DRY_RUN"] = "true"

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_end_to_end_dedupe_lifecycle(self):
        vid = "TEST_E2E_01"
        url = f"https://www.youtube.com/watch?v={vid}"

        # Setup sources.txt with 2 entries
        with open("sources.txt", "w", encoding="utf-8") as f:
            f.write(f"https://youtu.be/{vid}?si=sample1 | Permission\n")
            f.write(f"https://youtu.be/ANOTHER_VIDEO?si=sample2\n")

        state = {"done": [], "done_videos": []}

        # === SIMULATED RUN 1 ===
        # Step 1: Bootstrap & Sync
        guard.bootstrap()
        guard.sync_state(state)
        self.assertNotIn(vid, state["done_videos"])

        # Step 2: Claim source video
        can_proceed = guard.claim(vid, url)
        self.assertTrue(can_proceed)

        # Step 3: Candidate clips generator produces 6 candidates (including overlapping ones)
        raw_candidates = [
            {"start": 10.0, "end": 45.0, "hook_score": 8, "title": "Clip 1"},
            {"start": 20.0, "end": 55.0, "hook_score": 9, "title": "Clip 2 (Overlaps Clip 1)"},
            {"start": 70.0, "end": 100.0, "hook_score": 7, "title": "Clip 3"},
            {"start": 115.0, "end": 145.0, "hook_score": 8, "title": "Clip 4"},
            {"start": 160.0, "end": 195.0, "hook_score": 9, "title": "Clip 5"},
            {"start": 210.0, "end": 245.0, "hook_score": 7, "title": "Clip 6 (Exceeds Max Limit)"},
        ]

        # Step 4: Filter clips via guard
        filtered = guard.filter_clips(vid, raw_candidates)

        # Assertions:
        # - Overlapping Clip 1 vs Clip 2: Clip 2 has hook_score 9 > 8, so Clip 1 was suppressed
        # - Max 4 clips strictly enforced (Clip 6 trimmed)
        self.assertEqual(len(filtered), 4)
        filtered_titles = [c["title"] for c in filtered]
        self.assertIn("Clip 2 (Overlaps Clip 1)", filtered_titles)
        self.assertNotIn("Clip 1", filtered_titles)
        self.assertNotIn("Clip 6 (Exceeds Max Limit)", filtered_titles)

        # Step 5: Finalize & Record clips
        for c in filtered:
            guard.record_clip(vid, c["start"], c["end"], uploaded=False, dry_run=True)

        ledger = guard.load_ledger()
        self.assertEqual(ledger[vid]["status"], "consumed")
        self.assertEqual(len(ledger[vid]["clips"]), 4)

        # === SIMULATED RUN 2 (Rerun pipeline) ===
        state_run2 = {"done": [], "done_videos": []}
        guard.sync_state(state_run2)

        # The consumed video MUST be in state_run2["done"] and state_run2["done_videos"]
        self.assertIn(f"https://youtu.be/{vid}?si=sample1", state_run2["done"])
        self.assertIn(vid, state_run2["done_videos"])

        # Claiming this video again must be rejected immediately
        can_proceed_run2 = guard.claim(vid, url)
        self.assertFalse(can_proceed_run2)

        # If any clips were somehow evaluated, filter_clips returns empty
        filtered_run2 = guard.filter_clips(vid, raw_candidates)
        self.assertEqual(filtered_run2, [])


if __name__ == "__main__":
    unittest.main()
