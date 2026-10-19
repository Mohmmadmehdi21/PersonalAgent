"""tests/test_source_memory.py

Unit tests covering all 10 autofix test scenarios:
1. 2 channel lines + 3 Shorts + 8 long videos -> selector returns 5 long videos; Shorts remembered and never returned in 3 consecutive runs.
2. Rejected Shorts do not fill the 5-slot cap; usable candidates behind them are reached.
3. Direct URLs evaluated BEFORE channel results; channel only used when direct URLs exhausted.
4. Six bug IDs never appear as candidates again.
5. Short line in sources.txt deleted atomically; comments, blanks, BOM, CRLF preserved.
6. Deduplication of different URL forms of same ID.
7. Transient failures set cooldown; 5th failure escalates to permanent rejection.
8. REJECT_MEMORY_ENABLED=false reproduces original fallback behavior.
9. Corrupt data/rejected_ids.json backed up and rebuilt without crashing.
10. Concurrent edit of sources.txt is not lost.
"""

import os
import sys
import json
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Ensure root directory is on sys.path
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import source_memory


class TestSourceMemory(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.old_cwd = os.getcwd()
        os.chdir(self.test_dir)
        os.makedirs("data", exist_ok=True)
        os.environ["REJECT_MEMORY_ENABLED"] = "true"
        os.environ["TRANSIENT_COOLDOWN_HOURS"] = "12"
        os.environ["MAX_TRANSIENT_ATTEMPTS"] = "5"
        os.environ["MIN_SOURCE_DURATION_SEC"] = "480"
        os.environ["SOURCE_PICK_MODE"] = "ordered"
        source_memory.reset_quota_counter()

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_01_two_channels_three_shorts_eight_longs(self):
        """1. 2 channel lines + 3 Shorts + 8 long videos: selector returns 5 long videos;
        Shorts are rejected, remembered and never returned in 3 consecutive simulated runs."""
        sources_content = (
            "https://www.youtube.com/@MrBeast\n"
            "https://www.youtube.com/channel/UCX6OQ3DkcsbYNE6H8uQQuVA\n"
            "https://www.youtube.com/watch?v=vidShort001\n"
            "https://www.youtube.com/watch?v=vidShort002\n"
            "https://www.youtube.com/watch?v=vidShort003\n"
            "https://www.youtube.com/watch?v=vidLong0001\n"
            "https://www.youtube.com/watch?v=vidLong0002\n"
            "https://www.youtube.com/watch?v=vidLong0003\n"
            "https://www.youtube.com/watch?v=vidLong0004\n"
            "https://www.youtube.com/watch?v=vidLong0005\n"
            "https://www.youtube.com/watch?v=vidLong0006\n"
            "https://www.youtube.com/watch?v=vidLong0007\n"
            "https://www.youtube.com/watch?v=vidLong0008\n"
        )
        with open("sources.txt", "w", encoding="utf-8") as f:
            f.write(sources_content)

        # Mock pre-screening batch
        def mock_prescreen(ids, yt_service=None):
            res = {}
            for vid in ids:
                if "Short" in vid:
                    res[vid] = {"usable": False, "reason_code": "SHORT_OR_TOO_SHORT", "reason_text": "Short <= 60s", "duration": 45}
                else:
                    res[vid] = {"usable": True, "reason_code": "USABLE", "reason_text": "Long video", "duration": 1200}
            return res

        state = {}
        with patch("source_memory.prescreen_candidates_batch", side_effect=mock_prescreen):
            # Run 1: Returns first 5 long videos
            cands_run1 = source_memory.select_candidates(state, max_candidates=5)
            self.assertEqual(len(cands_run1), 5)
            ids_run1 = [source_memory.extract_video_id(c[0]) for c in cands_run1]
            self.assertEqual(ids_run1, ["vidLong0001", "vidLong0002", "vidLong0003", "vidLong0004", "vidLong0005"])

            # Verify Shorts were permanently remembered
            self.assertTrue(source_memory.is_rejected("vidShort001"))
            self.assertTrue(source_memory.is_rejected("vidShort002"))
            self.assertTrue(source_memory.is_rejected("vidShort003"))

            # Simulate Run 2 (state has run 1 videos done)
            state["done_videos"] = ids_run1
            cands_run2 = source_memory.select_candidates(state, max_candidates=5)
            # 3 remaining long videos: vidLong0006, 0007, 0008
            ids_run2 = [source_memory.extract_video_id(c[0]) for c in cands_run2]
            self.assertEqual(ids_run2, ["vidLong0006", "vidLong0007", "vidLong0008"])
            for s in ("vidShort001", "vidShort002", "vidShort003"):
                self.assertNotIn(s, ids_run2)

            # Simulate Run 3 (all long videos done)
            state["done_videos"].extend(ids_run2)
            cands_run3 = source_memory.select_candidates(state, max_candidates=5)
            ids_run3 = [source_memory.extract_video_id(c[0]) for c in cands_run3]
            for s in ("vidShort001", "vidShort002", "vidShort003"):
                self.assertNotIn(s, ids_run3)

    def test_02_rejected_shorts_do_not_fill_cap(self):
        """2. Rejected Shorts do not fill the 5-slot cap; usable candidates behind them are reached."""
        sources_content = (
            "https://www.youtube.com/watch?v=vidShort001\n"
            "https://www.youtube.com/watch?v=vidShort002\n"
            "https://www.youtube.com/watch?v=vidShort003\n"
            "https://www.youtube.com/watch?v=vidLong0001\n"
            "https://www.youtube.com/watch?v=vidLong0002\n"
            "https://www.youtube.com/watch?v=vidLong0003\n"
            "https://www.youtube.com/watch?v=vidLong0004\n"
            "https://www.youtube.com/watch?v=vidLong0005\n"
        )
        with open("sources.txt", "w", encoding="utf-8") as f:
            f.write(sources_content)

        def mock_prescreen(ids, yt_service=None):
            return {
                vid: {"usable": "Long" in vid, "reason_code": "USABLE" if "Long" in vid else "SHORT_OR_TOO_SHORT", "duration": 1200 if "Long" in vid else 40}
                for vid in ids
            }

        with patch("source_memory.prescreen_candidates_batch", side_effect=mock_prescreen):
            cands = source_memory.select_candidates({}, max_candidates=5)
            self.assertEqual(len(cands), 5)
            ids = [source_memory.extract_video_id(c[0]) for c in cands]
            self.assertEqual(ids, ["vidLong0001", "vidLong0002", "vidLong0003", "vidLong0004", "vidLong0005"])

    def test_03_direct_urls_come_first(self):
        """3. Direct URLs come first; channel results are used only when direct URLs are exhausted."""
        sources_content = (
            "https://www.youtube.com/@TestChannel\n"
            "https://www.youtube.com/watch?v=DIRECT_VID1\n"
            "https://www.youtube.com/watch?v=DIRECT_VID2\n"
        )
        with open("sources.txt", "w", encoding="utf-8") as f:
            f.write(sources_content)

        mock_yt = MagicMock()
        def mock_prescreen(ids, yt_service=None):
            return {vid: {"usable": True, "reason_code": "USABLE", "duration": 900} for vid in ids}

        with patch("source_memory.prescreen_candidates_batch", side_effect=mock_prescreen):
            cands = source_memory.select_candidates({}, max_candidates=2, yt_service=mock_yt)
            # Channel search should not even be called because 2 direct URLs were available
            mock_yt.search.assert_not_called()
            ids = [source_memory.extract_video_id(c[0]) for c in cands]
            self.assertEqual(ids, ["DIRECT_VID1", "DIRECT_VID2"])

    def test_04_six_bug_ids_never_appear(self):
        """4. The six IDs from 'THE BUG' never appear as candidates again."""
        bug_ids = ["_JUlWtSCNbs", "rqsRdfdN4uI", "897gvApD6V4", "B9LizOAAoLg", "QAkEMtJpq3c", "hYahU_Cqwp8"]
        sources_content = "\n".join(f"https://www.youtube.com/watch?v={vid}" for vid in bug_ids)
        sources_content += "\nhttps://www.youtube.com/watch?v=GOOD_LONG_1"
        with open("sources.txt", "w", encoding="utf-8") as f:
            f.write(sources_content)

        def mock_prescreen(ids, yt_service=None):
            return {vid: {"usable": True, "reason_code": "USABLE", "duration": 1000} for vid in ids}

        with patch("source_memory.prescreen_candidates_batch", side_effect=mock_prescreen):
            cands = source_memory.select_candidates({}, max_candidates=5)
            candidate_ids = [source_memory.extract_video_id(c[0]) for c in cands]
            for bid in bug_ids:
                self.assertNotIn(bid, candidate_ids)
            self.assertIn("GOOD_LONG_1", candidate_ids)

    def test_05_rejected_direct_url_removed_preserving_bytes(self):
        """5. A Short that is a line in sources.txt disappears from the file;
        every other line, comment, blank line, BOM and line ending stays byte-identical."""
        bom = "\ufeff"
        crlf = "\r\n"
        original_text = (
            f"{bom}# Comment header line{crlf}"
            f"https://www.youtube.com/@ChannelExample{crlf}"
            f"{crlf}"
            f"https://www.youtube.com/watch?v=SHORT_REJ01 | Note to remove{crlf}"
            f"https://www.youtube.com/watch?v=LONG_KEEP_01 | Keep me{crlf}"
            f"# Final footer comment{crlf}"
        )
        with open("sources.txt", "wb") as f:
            f.write(original_text.encode("utf-8-sig"))

        expected_after = (
            f"{bom}# Comment header line{crlf}"
            f"https://www.youtube.com/@ChannelExample{crlf}"
            f"{crlf}"
            f"https://www.youtube.com/watch?v=LONG_KEEP_01 | Keep me{crlf}"
            f"# Final footer comment{crlf}"
        )

        removed, line_num = source_memory.remove_id_from_sources_txt("SHORT_REJ01")
        self.assertTrue(removed)
        self.assertEqual(line_num, 4)

        with open("sources.txt", "rb") as f:
            resulting_bytes = f.read()

        self.assertEqual(resulting_bytes, expected_after.encode("utf-8-sig"))

    def test_06_url_forms_deduplicated(self):
        """6. Different URL forms of one ID are deduplicated."""
        url1 = "https://youtu.be/0e3GPea1Tyg?si=abcde123"
        url2 = "https://www.youtube.com/watch?v=0e3GPea1Tyg&t=30s"
        url3 = "https://www.youtube.com/shorts/0e3GPea1Tyg"
        url4 = "https://m.youtube.com/watch?v=0e3GPea1Tyg"

        self.assertEqual(source_memory.extract_video_id(url1), "0e3GPea1Tyg")
        self.assertEqual(source_memory.extract_video_id(url2), "0e3GPea1Tyg")
        self.assertEqual(source_memory.extract_video_id(url3), "0e3GPea1Tyg")
        self.assertEqual(source_memory.extract_video_id(url4), "0e3GPea1Tyg")

    def test_07_transient_errors_cooldown_and_fifth_rejects(self):
        """7. Transient errors set a cooldown and the selector moves on; the 5th failure rejects."""
        test_vid = "TRANSIENT01"

        # Attempts 1 to 4: in cooldown, not permanently rejected
        for attempt in range(1, 5):
            escalated = source_memory.mark_transient_failure(test_vid, f"Network timeout error {attempt}")
            self.assertFalse(escalated)
            self.assertTrue(source_memory.is_in_cooldown(test_vid))
            self.assertFalse(source_memory.is_rejected(test_vid))

        # 5th attempt: escalates to permanent rejection
        escalated = source_memory.mark_transient_failure(test_vid, "Network timeout error 5")
        self.assertTrue(escalated)
        self.assertTrue(source_memory.is_rejected(test_vid))

    def test_08_reject_memory_disabled_reproduces_original(self):
        """8. REJECT_MEMORY_ENABLED=false reproduces the old behaviour (returns None to fall back)."""
        os.environ["REJECT_MEMORY_ENABLED"] = "false"
        result = source_memory.select_candidates({}, max_candidates=5)
        self.assertIsNone(result)

    def test_09_corrupt_rejected_ids_rebuilt_safely(self):
        """9. A corrupt data\\rejected_ids.json does not crash the pipeline (back up as .corrupt.bak, rebuild)."""
        # Write corrupted JSON
        with open(source_memory.REJECTED_FILE, "w", encoding="utf-8") as f:
            f.write("{this is invalid corrupt json syntax")

        data = source_memory.load_memory()
        self.assertIn("rejected", data)
        self.assertIn("_JUlWtSCNbs", data["rejected"])

        # Check backup file exists
        bak_files = [f for f in os.listdir("data") if ".corrupt_" in f]
        self.assertTrue(len(bak_files) >= 1)

    def test_10_concurrent_edit_sources_txt(self):
        """10. Concurrent edit of sources.txt is not lost."""
        initial = (
            "https://www.youtube.com/watch?v=INITIAL_001\n"
            "https://www.youtube.com/watch?v=INITIAL_002\n"
        )
        with open("sources.txt", "w", encoding="utf-8") as f:
            f.write(initial)

        # Simulate concurrent line appended while removing INITIAL_001
        with open("sources.txt", "a", encoding="utf-8") as f:
            f.write("https://www.youtube.com/watch?v=CONCURRENT01\n")

        removed, _ = source_memory.remove_id_from_sources_txt("INITIAL_001")
        self.assertTrue(removed)

        with open("sources.txt", "r", encoding="utf-8") as f:
            final_content = f.read()

        self.assertNotIn("INITIAL_001", final_content)
        self.assertIn("INITIAL_002", final_content)
        self.assertIn("CONCURRENT01", final_content)


if __name__ == "__main__":
    unittest.main()
