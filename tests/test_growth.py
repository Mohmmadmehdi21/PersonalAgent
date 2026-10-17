"""Pytest-discoverable offline tests (also runnable with stdlib unittest)."""
import json
import importlib
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from growth import comments, learnings, slots, tracker


class FakeRequest:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class FakeVideos:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return FakeRequest({"items": [self.responses[item] for item in kwargs["id"].split(",")
                                     if item in self.responses]})


class FakeComments:
    def __init__(self):
        self.calls = []

    def insert(self, **kwargs):
        self.calls.append(kwargs)
        return FakeRequest({"id": "comment"})


class FakeYoutube:
    def __init__(self, responses):
        self.video_api = FakeVideos(responses)
        self.comment_api = FakeComments()

    def videos(self):
        return self.video_api

    def commentThreads(self):
        return self.comment_api


class GrowthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.data_dir = Path(self.temp.name) / "data"
        self.data_dir.mkdir()
        self.patches = [
            patch.dict(os.environ, {"SHEET_ID": ""}),
            patch.object(tracker, "DATA_DIR", self.data_dir),
            patch.object(tracker, "VIDEOS_FILE", self.data_dir / "videos.csv"),
            patch.object(slots, "DATA_DIR", self.data_dir),
            patch.object(slots, "SLOTS_FILE", self.data_dir / "slots.json"),
            patch.object(slots, "LOG_FILE", self.data_dir / "slot_changes.log"),
            patch.object(slots, "EXPERIMENT_SLOTS_FILE", self.data_dir / "experiment_slots.json"),
            patch.object(learnings, "DATA_DIR", self.data_dir),
            patch.object(learnings, "LEARNINGS_FILE", self.data_dir / "learnings.txt"),
            patch.object(learnings, "REFRESH_FILE", self.data_dir / "learnings_refreshed_at.txt"),
            patch.object(comments, "DATA_DIR", self.data_dir),
            patch.object(comments, "COMMENTS_FILE", self.data_dir / "comments.json"),
            patch.object(comments, "TO_PIN_FILE", self.data_dir / "to_pin.txt"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_upload_record_unique_and_csv_atomic(self):
        row = tracker.record_upload("v1", "src", "Title", "2026-10-16T10:00:00+05:30", 9, "BREAKDOWN", 31.5)
        tracker.record_upload("v1", "src", "Changed", hook_score=1)
        rows = tracker.read_videos()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Title")
        self.assertEqual(rows[0]["publish_hour_ist"], "10")
        self.assertEqual(rows[0]["clip_length_seconds"], "31.5")
        self.assertEqual(row["weekday"], "Friday")
        self.assertEqual(list(self.data_dir.glob("*.tmp")), [])

    def test_learning_prompt_is_exactly_empty_when_disabled_or_missing(self):
        with patch.dict(os.environ, {"LEARNING_ENABLED": "false"}):
            self.assertEqual(learnings.prompt_block(), "")
        with patch.dict(os.environ, {"LEARNING_ENABLED": "true"}):
            self.assertEqual(learnings.prompt_block(), "")

    def test_config_slot_override_valid_file_only(self):
        fake_dotenv = type(sys)("dotenv")
        fake_dotenv.load_dotenv = lambda: None
        old_config = sys.modules.pop("config", None)
        old_cwd = os.getcwd()
        slots_path = self.data_dir / "slots.json"
        try:
            os.chdir(self.data_dir.parent)
            with patch.dict(sys.modules, {"dotenv": fake_dotenv}), \
                 patch.dict(os.environ, {"AUTO_SLOT_UPDATE": "true", "SLOT_HOURS_IST": "9,13,17,21"}):
                slots_path.write_text(json.dumps({"hours": [7, 11, 18, 22]}))
                valid = importlib.import_module("config")
                self.assertEqual(valid.SLOT_HOURS_IST, [7, 11, 18, 22])
                del sys.modules["config"]
                slots_path.write_text("not json")
                invalid = importlib.import_module("config")
                self.assertEqual(invalid.SLOT_HOURS_IST, [9, 13, 17, 21])
        finally:
            os.chdir(old_cwd)
            sys.modules.pop("config", None)
            if old_config is not None:
                sys.modules["config"] = old_config

    def test_stats_batches_and_fetches_statistics_only_for_public(self):
        now = datetime(2026, 10, 17, tzinfo=timezone.utc)
        for index in range(51):
            tracker.record_upload(f"v{index}", "source", f"Video {index}", now - timedelta(days=8))
        response = {
            f"v{index}": {"id": f"v{index}", "status": {"privacyStatus": "public"},
                           "statistics": {"viewCount": "123", "likeCount": "7", "commentCount": "2"}}
            for index in range(50)
        }
        response["v50"] = {"id": "v50", "status": {"privacyStatus": "private"},
                           "statistics": {"viewCount": "99"}}
        youtube = FakeYoutube(response)
        rows = tracker.refresh_stats(youtube, now=now)
        self.assertEqual([(call["part"], len(call["id"].split(","))) for call in youtube.video_api.calls],
                         [("status", 50), ("statistics", 50), ("status", 1)])
        self.assertEqual(rows[0]["views_1d"], "123")
        self.assertEqual(rows[0]["views_7d"], "123")
        self.assertEqual(rows[-1]["views_7d"], "")

    def test_slots_require_samples_and_update_only_one_slot(self):
        rows = [{"publish_hour_ist": str(hour), "views_7d": str(100 + i), "likes_7d": str(i)}
                for hour in (9, 13, 17, 21) for i in range(8)]
        insufficient = slots.suggest_slots(rows[:7], [9], min_samples=8, candidates=[7])
        self.assertFalse(insufficient["changed"])
        result = slots.suggest_slots(rows, [9, 13, 17, 21], min_samples=8,
                                     candidates=[7], auto_update=True)
        self.assertTrue(result["changed"])
        self.assertEqual(len(result["hours"]), 4)
        self.assertEqual(len(set(result["hours"]) - {9, 13, 17, 21}), 1)
        self.assertEqual(json.loads((self.data_dir / "slots.json").read_text())["hours"], result["hours"])

    def test_learnings_sample_and_weekly_limit(self):
        now = datetime(2026, 10, 17, tzinfo=timezone.utc)
        rows = [{"title": f"T{i}", "hook_score": "8", "template": "BREAKDOWN",
                 "clip_length_seconds": "35", "views_7d": str(i)} for i in range(15)]
        self.assertFalse(learnings.refresh_learnings(rows[:14], generator=lambda _: "summary", now=now))
        self.assertTrue(learnings.refresh_learnings(rows, generator=lambda prompt: "Useful patterns", now=now))
        text = learnings.LEARNINGS_FILE.read_text(encoding="utf-8")
        self.assertIn("TOP", text)
        self.assertIn("BOTTOM", text)
        self.assertIn("small", text)
        self.assertFalse(learnings.refresh_learnings(rows, generator=lambda _: "again", now=now + timedelta(days=2)))

    def test_comments_preview_public_only_without_posting_or_recording(self):
        rows = [{"video_id": "public", "title": "A specific moment"},
                {"video_id": "private", "title": "Not public"}]
        response = {
            "public": {"id": "public", "status": {"privacyStatus": "public"}},
            "private": {"id": "private", "status": {"privacyStatus": "private"}},
        }
        youtube = FakeYoutube(response)
        result = comments.post_comments(youtube, dry_run=True, enabled=True, rows=rows,
                                        generator=lambda _: "What would you have done here?")
        self.assertEqual([item["video_id"] for item in result], ["public"])
        self.assertEqual(youtube.comment_api.calls, [])
        self.assertFalse(comments.COMMENTS_FILE.exists())
        self.assertFalse(comments.TO_PIN_FILE.exists())

    def test_comments_are_deduplicated(self):
        rows = [{"video_id": "public", "title": "A specific moment"}]
        youtube = FakeYoutube({"public": {"id": "public", "status": {"privacyStatus": "public"}}})
        comments.post_comments(youtube, enabled=True, rows=rows,
                               generator=lambda _: "What would you have done here?")
        comments.post_comments(youtube, enabled=True, rows=rows,
                               generator=lambda _: "Another question?")
        self.assertEqual(len(youtube.comment_api.calls), 1)
        self.assertEqual(set(json.loads(comments.COMMENTS_FILE.read_text())), {"public"})
        self.assertIn("studio.youtube.com/video/public/comments", comments.TO_PIN_FILE.read_text())

    def test_cli_dispatch_uses_mock_services_for_growth_commands(self):
        from growth import cli
        calls = []
        fake_youtube = object()
        fake_config = types.SimpleNamespace(SLOT_HOURS_IST=[9, 13, 17, 21], MIN_SAMPLES_PER_SLOT=8,
                                            CANDIDATE_HOURS_IST=[7, 8], AUTO_SLOT_UPDATE=False,
                                            COMMENT_ENABLED=True, DRY_RUN=True)
        heartbeat = self.data_dir / "heartbeat.json"
        heartbeat.write_text(json.dumps({"last_success": datetime.now(timezone.utc).isoformat()}))
        old_cwd = os.getcwd()
        with patch("growth.auth.get_service", return_value=fake_youtube), \
             patch("growth.tracker.refresh_stats", side_effect=lambda yt: calls.append(("stats", yt)) or []), \
             patch("growth.slots.suggest_slots", side_effect=lambda **kw: calls.append(("slots", kw))), \
             patch("growth.learnings.refresh_learnings", side_effect=lambda: calls.append(("learn",))), \
             patch("growth.comments.post_comments", side_effect=lambda *a, **kw: calls.append(("comments", kw))), \
             patch.dict("sys.modules", {"config": fake_config}), \
             patch.dict(os.environ, {"COMMENT_ENABLED": "true", "DRY_RUN": "true"}):
            os.chdir(self.data_dir.parent)
            try:
                for command in ("stats", "slots", "learn", "comments", "growth-health"):
                    self.assertTrue(cli.dispatch([command]))
            finally:
                os.chdir(old_cwd)
        self.assertEqual([entry[0] for entry in calls], ["stats", "slots", "learn", "comments"])
        self.assertTrue(calls[-1][1]["dry_run"])


if __name__ == "__main__":
    unittest.main()
