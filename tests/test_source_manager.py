"""Comprehensive test suite for YouTube Daily Source Refresh and Telegram Source Manager.

Covers:
- SourceLock concurrency and stale lock recovery.
- Authoritative state cross-referencing and queue protection.
- Read-only preview, hash validation, stale detection, and atomic cleanup with backups.
- Safe YouTube URL validation, deduplication, and additions.
- Scheduler refresh outcomes (success_new_sources, success_no_new_sources, partial_results, failed).
- Dependent scheduled preparation blocking on failed refresh.
- Telegram menu navigation, buttons, callbacks, and owner authorization.
"""
import copy
import hashlib
import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from telegram_bot import source_manager
from telegram_bot.commands import handle_update
from telegram_bot.scheduler import Scheduler, is_scheduler_configured_enabled


def make_update(user_id=12345, text="/start", chat_id=12345):
    return {
        "update_id": 1,
        "message": {
            "from": {"id": user_id},
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
        },
    }


def make_callback(user_id=12345, data="src:menu", chat_id=12345):
    return {
        "update_id": 2,
        "callback_query": {
            "id": "cb_test_1",
            "from": {"id": user_id},
            "message": {
                "message_id": 99,
                "chat": {"id": chat_id, "type": "private"},
                "text": "menu",
            },
            "data": data,
        },
    }


class SourceLockTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.lock_file = Path(self.temp_dir) / "sources.lock"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_acquire_and_release(self):
        lock = source_manager.SourceLock(path=self.lock_file, timeout=2)
        self.assertTrue(lock.acquire())
        self.assertTrue(self.lock_file.exists())
        lock.release()
        self.assertFalse(self.lock_file.exists())

    def test_context_manager(self):
        with source_manager.SourceLock(path=self.lock_file, timeout=2) as lock:
            self.assertTrue(lock.held)
            self.assertTrue(self.lock_file.exists())
        self.assertFalse(self.lock_file.exists())

    def test_mutual_exclusion(self):
        lock1 = source_manager.SourceLock(path=self.lock_file, timeout=0.2)
        lock2 = source_manager.SourceLock(path=self.lock_file, timeout=0.2)
        self.assertTrue(lock1.acquire())
        self.assertFalse(lock2.acquire())
        lock1.release()
        self.assertTrue(lock2.acquire())
        lock2.release()

    def test_stale_pid_recovery(self):
        # Write dead PID
        self.lock_file.write_text(json.dumps({"pid": 99999999, "at": time.time() - 100}), encoding="utf-8")
        with patch("lock.is_pid_alive", return_value=False):
            lock = source_manager.SourceLock(path=self.lock_file, timeout=1)
            self.assertTrue(lock.acquire())
            lock.release()


class AuthoritativeStateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.state_file = Path(self.temp_dir) / "state.json"
        self.used_file = Path(self.temp_dir) / "used_videos.json"
        self.queue_file = Path(self.temp_dir) / "queue.json"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_state_reconciliation_and_queue_protection(self):
        state_data = {
            "done_videos": ["video_a", "video_b"],
            "done": ["https://www.youtube.com/watch?v=video_c"],
        }
        used_data = {
            "video_d": {"status": "consumed", "clips": ["c1"]},
            "video_e": {"status": "consumed", "clips": ["c2"]},
        }
        # video_b has an unfinished/blocked clip in queue, so it MUST be protected!
        queue_data = [
            {"id": "video_b_01", "source_id": "video_b", "status": "blocked"},
            {"id": "video_c_01", "source_id": "video_c", "status": "uploaded"},
            {"id": "video_d_01", "source_id": "video_d", "status": "pending"},
        ]

        self.state_file.write_text(json.dumps(state_data), encoding="utf-8")
        self.used_file.write_text(json.dumps(used_data), encoding="utf-8")
        self.queue_file.write_text(json.dumps(queue_data), encoding="utf-8")

        with patch("telegram_bot.source_manager.STATE_FILE", self.state_file), \
             patch("telegram_bot.source_manager.USED_VIDEOS_FILE", self.used_file), \
             patch("telegram_bot.source_manager.QUEUE_FILE", self.queue_file):
            auth = source_manager.get_authoritative_state()

        # Protected because unfinished in queue
        self.assertIn("video_b", auth["protected_queue_ids"])
        self.assertIn("video_d", auth["protected_queue_ids"])
        # Conclusively used should NOT contain protected videos
        self.assertNotIn("video_b", auth["conclusively_used"])
        self.assertNotIn("video_d", auth["conclusively_used"])
        # Conclusively used should contain video_a, video_c, and video_e
        self.assertIn("video_a", auth["conclusively_used"])
        self.assertIn("video_c", auth["conclusively_used"])
        self.assertIn("video_e", auth["conclusively_used"])


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.sources_file = Path(self.temp_dir) / "sources.txt"
        initial_lines = [
            "# Curated sources",
            "https://www.youtube.com/@MrBeast/videos",
            "https://www.youtube.com/watch?v=used_vid_01 | Consumed video",
            "https://www.youtube.com/watch?v=pend_vid_01 | Keep this",
            "https://www.youtube.com/watch?v=bloc_vid_01 | Protected blocked",
            "https://www.youtube.com/watch?v=pend_vid_01 | Duplicate line",
            "",
        ]
        self.sources_file.write_text("\n".join(initial_lines), encoding="utf-8")

        self.auth_state = {
            "conclusively_used": {"used_vid_01"},
            "protected_queue_ids": {"bloc_vid_01"},
            "state_done": {"used_vid_01"},
            "consumed_ids": set(),
        }

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_cleanup_preview_and_execute(self):
        with patch("telegram_bot.source_manager.get_authoritative_state", return_value=self.auth_state), \
             patch("telegram_bot.source_manager.LOCK_FILE", Path(self.temp_dir) / "lock"):
            proposal = source_manager.preview_cleanup(sources_path=self.sources_file)

            self.assertEqual(proposal["eligible_used_count"], 1)
            self.assertEqual(proposal["duplicate_count"], 1)
            self.assertEqual(proposal["eligible_removals"][0]["vid"], "used_vid_01")

            # Execute cleanup
            result = source_manager.execute_cleanup(proposal["preview_id"], sources_path=self.sources_file)

            self.assertEqual(result["removed_used"], 1)
            self.assertEqual(result["removed_duplicates"], 1)
            self.assertTrue(Path(result["backup_path"]).exists())

            # Verify contents of sources.txt
            content = self.sources_file.read_text(encoding="utf-8")
            self.assertIn("@MrBeast/videos", content)
            self.assertIn("pend_vid_01", content)
            self.assertIn("bloc_vid_01", content)
            self.assertNotIn("used_vid_01", content)
            # Duplicate line removed, exactly 1 pend_vid_01 left
            self.assertEqual(content.count("pend_vid_01"), 1)

    def test_cleanup_cancelled_leaves_file_intact(self):
        with patch("telegram_bot.source_manager.get_authoritative_state", return_value=self.auth_state):
            original = self.sources_file.read_text(encoding="utf-8")
            proposal = source_manager.preview_cleanup(sources_path=self.sources_file)
            source_manager.cancel_cleanup(proposal["preview_id"])
            self.assertEqual(self.sources_file.read_text(encoding="utf-8"), original)

    def test_stale_preview_rejected(self):
        with patch("telegram_bot.source_manager.get_authoritative_state", return_value=self.auth_state), \
             patch("telegram_bot.source_manager.LOCK_FILE", Path(self.temp_dir) / "lock"):
            proposal = source_manager.preview_cleanup(sources_path=self.sources_file)

            # Mutate file after preview
            with open(self.sources_file, "a", encoding="utf-8") as f:
                f.write("https://www.youtube.com/watch?v=dQw4w9WgXcQ\n")

            with self.assertRaises(ValueError) as ctx:
                source_manager.execute_cleanup(proposal["preview_id"], sources_path=self.sources_file)
            self.assertIn("changed since the preview", str(ctx.exception))


class AddSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.sources_file = Path(self.temp_dir) / "sources.txt"
        self.sources_file.write_text("https://www.youtube.com/watch?v=existing123\n", encoding="utf-8")
        self.auth_state = {
            "conclusively_used": {"used9991234"},
            "protected_queue_ids": {"blocked8881"},
            "state_done": set(),
            "consumed_ids": set(),
        }

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_valid_video_url(self):
        with patch("telegram_bot.source_manager.get_authoritative_state", return_value=self.auth_state), \
             patch("telegram_bot.source_manager.LOCK_FILE", Path(self.temp_dir) / "lock"):
            cand = source_manager.validate_new_source("https://youtu.be/newvid45678", sources_path=self.sources_file)
            self.assertEqual(cand["source_type"], "video")
            self.assertEqual(cand["video_id"], "newvid45678")
            self.assertFalse(cand["already_exists"])
            self.assertFalse(cand["already_used"])

            res = source_manager.append_new_source(cand["candidate_id"], sources_path=self.sources_file)
            self.assertEqual(res["added_url"], "https://www.youtube.com/watch?v=newvid45678")
            content = self.sources_file.read_text(encoding="utf-8")
            self.assertIn("https://www.youtube.com/watch?v=newvid45678", content)

    def test_duplicate_rejected(self):
        with patch("telegram_bot.source_manager.get_authoritative_state", return_value=self.auth_state):
            cand = source_manager.validate_new_source("https://www.youtube.com/watch?v=existing123", sources_path=self.sources_file)
            self.assertTrue(cand["already_exists"])
            with self.assertRaises(ValueError):
                source_manager.append_new_source(cand["candidate_id"], sources_path=self.sources_file)

    def test_used_video_flagged(self):
        with patch("telegram_bot.source_manager.get_authoritative_state", return_value=self.auth_state):
            cand = source_manager.validate_new_source("https://www.youtube.com/watch?v=used9991234", sources_path=self.sources_file)
            self.assertTrue(cand["already_used"])

    def test_invalid_domain_rejected(self):
        for bad in ("https://vimeo.com/12345678901", "http://malicious.com/test", "not a url"):
            with self.assertRaises(ValueError):
                source_manager.validate_new_source(bad, sources_path=self.sources_file)


class RefreshOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.sources_file = Path(self.temp_dir) / "sources.txt"
        self.sources_file.write_text("https://www.youtube.com/@Channel/videos\n", encoding="utf-8")
        self.sched_file = Path(self.temp_dir) / "scheduler.json"
        Path("mrbeast_15min_plus.txt").unlink(missing_ok=True)

    def tearDown(self):
        Path("mrbeast_15min_plus.txt").unlink(missing_ok=True)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_refresh_success_new_sources(self):
        dataset_path = Path("mrbeast_15min_plus.txt")
        dataset_path.write_text("https://www.youtube.com/watch?v=dQw4w9WgXcQ\n", encoding="utf-8")
        try:
            with patch("telegram_bot.source_manager.SCHEDULER_FILE", self.sched_file), \
                 patch("telegram_bot.source_manager.LOCK_FILE", Path(self.temp_dir) / "lock"), \
                 patch("make_sources.main"):
                rec = source_manager.run_source_refresh(dest=self.sources_file)
                self.assertIn(rec["status"], {"success_new_sources", "success_no_new_sources"})
                self.assertIn("status", rec)
                self.assertIn("duration_seconds", rec)
        finally:
            dataset_path.unlink(missing_ok=True)

    def test_refresh_failed_status(self):
        Path("mrbeast_15min_plus.txt").unlink(missing_ok=True)
        with patch("telegram_bot.source_manager.SCHEDULER_FILE", self.sched_file), \
             patch("telegram_bot.source_manager.LOCK_FILE", Path(self.temp_dir) / "lock"), \
             patch("make_sources.main", side_effect=RuntimeError("Scraper connection failed")):
            rec = source_manager.run_source_refresh(dest=self.sources_file)
            self.assertEqual(rec["status"], "failed")
            self.assertIsNotNone(rec["error"])



class SchedulerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.sched_file = Path(self.temp_dir) / "scheduler.json"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_failed_refresh_blocks_dependent_prepare(self):
        state = {"jobs": {}}
        launched = []
        with patch.dict(os.environ, {
            "SCHEDULER_ENABLED": "true",
            "TELEGRAM_CHAT_ID": "12345",
            "SCHEDULER_PREPARE_ENABLED": "true",
            "SCHEDULER_PREPARE_TIME": "09:00",
            "SCHEDULER_SOURCE_REFRESH_ENABLED": "true",
            "SCHEDULER_SOURCE_REFRESH_TIME": "08:00",
        }), patch("telegram_bot.scheduler._load_state", return_value=state), \
             patch("telegram_bot.scheduler._save_state"), \
             patch.object(Scheduler, "_source_refresh_job", side_effect=RuntimeError("Channel fetch failed")):

            scheduler = Scheduler(launch_job=lambda *args: launched.append(args))
            # Tick at 09:30 when both refresh and prepare are due
            scheduler.tick(now=datetime(2026, 10, 20, 9, 30, tzinfo=ZoneInfo("Asia/Kolkata")))

        # Source refresh failed
        self.assertEqual(state["jobs"]["sources:2026-10-20"]["status"], "failed")
        # Prepare was BLOCKED because source refresh failed!
        self.assertNotIn("prepare:2026-10-20", state["jobs"])
        self.assertEqual(launched, [])

    def test_successful_refresh_allows_prepare(self):
        state = {"jobs": {}}
        launched = []
        with patch.dict(os.environ, {
            "SCHEDULER_ENABLED": "true",
            "TELEGRAM_CHAT_ID": "12345",
            "SCHEDULER_PREPARE_ENABLED": "true",
            "SCHEDULER_PREPARE_TIME": "09:00",
            "SCHEDULER_SOURCE_REFRESH_ENABLED": "true",
            "SCHEDULER_SOURCE_REFRESH_TIME": "08:00",
        }), patch("telegram_bot.scheduler._load_state", return_value=state), \
             patch("telegram_bot.scheduler._save_state"), \
             patch.object(Scheduler, "_source_refresh_job", return_value={"status": "success_new_sources", "appended": 1}):

            scheduler = Scheduler(launch_job=lambda *args: launched.append(args))
            scheduler.tick(now=datetime(2026, 10, 20, 9, 30, tzinfo=ZoneInfo("Asia/Kolkata")))

        self.assertEqual(state["jobs"]["sources:2026-10-20"]["status"], "completed")
        self.assertEqual(state["jobs"]["prepare:2026-10-20"]["status"], "running")
        self.assertEqual(len(launched), 1)

    def test_persistent_config_override_enables_refresh(self):
        state = {
            "jobs": {},
            "config": {"source_refresh_enabled": True, "scheduler_enabled": True}
        }
        with patch.dict(os.environ, {
            "SCHEDULER_ENABLED": "false",
            "SCHEDULER_SOURCE_REFRESH_ENABLED": "false",
            "TELEGRAM_CHAT_ID": "12345",
        }), patch("telegram_bot.scheduler._load_state", return_value=state):
            scheduler = Scheduler()
            self.assertTrue(scheduler.is_scheduler_enabled(state))
            self.assertTrue(scheduler.is_source_refresh_enabled(state))
            self.assertTrue(is_scheduler_configured_enabled())


class TelegramBotSourceManagerTests(unittest.TestCase):
    def setUp(self):
        self.owner_ids = frozenset({12345})
        from telegram_bot import lifecycle
        lifecycle.set_state(lifecycle.STATE_RUNNING)

    def test_start_menu_contains_source_manager_button(self):
        res = handle_update(make_update(12345, "/start"), self.owner_ids)
        self.assertIsNotNone(res)
        text, markup = res[1], res[2]
        all_callbacks = [btn["callback_data"] for row in markup["inline_keyboard"] for btn in row]
        self.assertIn("src:menu", all_callbacks)

    def test_sources_command_opens_source_manager(self):
        res = handle_update(make_update(12345, "/sources"), self.owner_ids)
        self.assertIsNotNone(res)
        text, markup = res[1], res[2]
        self.assertIn("Source Manager", text)
        all_callbacks = [btn["callback_data"] for row in markup["inline_keyboard"] for btn in row]
        self.assertIn("src:status", all_callbacks)
        self.assertIn("src:refresh_confirm", all_callbacks)
        self.assertIn("src:clean_preview", all_callbacks)
        self.assertIn("src:add_prompt", all_callbacks)
        self.assertIn("src:schedule", all_callbacks)

    def test_unauthorized_user_blocked(self):
        # Non-owner 99999
        res = handle_update(make_update(99999, "/sources"), self.owner_ids)
        self.assertIsNone(res)
        res_cb = handle_update(make_callback(99999, "src:status"), self.owner_ids)
        self.assertIsNone(res_cb)

    def test_source_status_view(self):
        fake_status = {
            "total_lines": 50,
            "active_entries": 45,
            "direct_video_count": 43,
            "channel_count": 2,
            "duplicate_count": 0,
            "used_count": 5,
            "pending_count": 38,
            "last_attempt": time.time(),
            "last_success": time.time(),
            "last_result": "success_new_sources",
            "last_error": None,
            "scheduler_enabled": True,
            "refresh_enabled": True,
            "timezone": "Asia/Kolkata",
            "refresh_time": "08:00",
        }
        with patch("telegram_bot.source_manager.get_sources_status", return_value=fake_status):
            res = handle_update(make_callback(12345, "src:status"), self.owner_ids)
            self.assertIn("YouTube Sources Status", res[1])
            self.assertIn("Total source lines: 50", res[1])
            self.assertIn("Unique direct video URLs: 43", res[1])
            self.assertIn("Used / consumed links: 5", res[1])

    def test_source_summary_view(self):
        fake_summary = {
            "total_active": 10,
            "pending_sample": [{"id": "v1", "url": "https://youtu.be/v1"}],
            "used_sample": [{"id": "v2", "url": "https://youtu.be/v2"}],
            "channels": ["https://youtube.com/@MrBeast/videos"],
            "total_pending": 8,
            "total_used": 2,
        }
        with patch("telegram_bot.source_manager.get_source_summary", return_value=fake_summary):
            res = handle_update(make_callback(12345, "src:summary"), self.owner_ids)
            self.assertIn("Source Summary Overview", res[1])
            self.assertIn("@MrBeast/videos", res[1])
            self.assertIn("v1", res[1])

    def test_refresh_confirmation_and_execution(self):
        # 1. Confirmation prompt
        res1 = handle_update(make_callback(12345, "src:refresh_confirm"), self.owner_ids)
        self.assertIn("Refresh Sources Now", res1[1])
        all_cb = [btn["callback_data"] for row in res1[2]["inline_keyboard"] for btn in row]
        self.assertIn("src:refresh_start", all_cb)

        # 2. Execution
        fake_record = {
            "status": "success_new_sources",
            "duration_seconds": 1.25,
            "timezone": "Asia/Kolkata",
            "sources_scanned": 15,
            "new_links_found": 2,
            "links_appended": 2,
            "duplicates_skipped": 1,
            "used_skipped": 0,
            "error": None,
        }
        with patch("telegram_bot.source_manager.run_source_refresh", return_value=fake_record):
            res2 = handle_update(make_callback(12345, "src:refresh_start"), self.owner_ids)
            self.assertIn("Source Refresh Complete", res2[1])
            self.assertIn("Links appended: 2", res2[1])

    def test_clean_preview_and_execute_flow(self):
        fake_proposal = {
            "preview_id": "prev123",
            "total_lines": 20,
            "eligible_used_count": 2,
            "duplicate_count": 1,
            "retained_count": 17,
            "eligible_removals": [{"vid": "vid1", "reason": "Conclusively used"}],
            "duplicate_removals": [],
        }
        with patch("telegram_bot.source_manager.preview_cleanup", return_value=fake_proposal):
            res_prev = handle_update(make_callback(12345, "src:clean_preview"), self.owner_ids)
            self.assertIn("Clean Used Links — Preview", res_prev[1])
            self.assertIn("src:clean_confirm:prev123", [b["callback_data"] for row in res_prev[2]["inline_keyboard"] for b in row])

        with patch("telegram_bot.source_manager.execute_cleanup", return_value={
            "removed_used": 2, "removed_duplicates": 1, "remaining_lines": 17, "backup_path": "sources.txt.bak_test"
        }):
            res_conf = handle_update(make_callback(12345, "src:clean_confirm:prev123"), self.owner_ids)
            self.assertIn("Cleanup Completed Successfully", res_conf[1])
            self.assertIn("Removed used links: 2", res_conf[1])

    def test_conversational_add_source_flow(self):
        # 1. User clicks Add Source button
        res_prompt = handle_update(make_callback(12345, "src:add_prompt"), self.owner_ids)
        self.assertIn("Please paste the YouTube URL", res_prompt[1])
        self.assertEqual(source_manager.get_pending_input(12345), "add_source")

        # 2. User replies with URL
        fake_candidate = {
            "candidate_id": "cand999",
            "normalized_url": "https://www.youtube.com/watch?v=cand999",
            "source_type": "video",
            "video_id": "cand999",
            "already_exists": False,
            "already_used": False,
            "is_blocked_active": False,
        }
        with patch("telegram_bot.source_manager.validate_new_source", return_value=fake_candidate):
            res_reply = handle_update(make_update(12345, "https://youtu.be/cand999"), self.owner_ids)
            self.assertIn("Confirm New YouTube Source", res_reply[1])
            self.assertIn("src:add_confirm:cand999", [b["callback_data"] for row in res_reply[2]["inline_keyboard"] for b in row])

        # 3. User clicks Confirm
        with patch("telegram_bot.source_manager.append_new_source", return_value={
            "added_url": "https://www.youtube.com/watch?v=cand999",
            "source_type": "video",
            "total_sources": 51,
        }):
            res_final = handle_update(make_callback(12345, "src:add_confirm:cand999"), self.owner_ids)
            self.assertIn("YouTube Source Added", res_final[1])

    def test_conversational_add_cancelled(self):
        source_manager.set_pending_input(12345, "add_source")
        res = handle_update(make_update(12345, "/cancel"), self.owner_ids)
        self.assertIn("Source addition cancelled", res[1])
        self.assertIsNone(source_manager.get_pending_input(12345))

    def test_schedule_toggle(self):
        with patch("telegram_bot.source_manager.set_scheduler_config") as mock_set, \
             patch("telegram_bot.source_manager.get_scheduler_config", return_value={
                 "scheduler_enabled": True, "source_refresh_enabled": True, "timezone": "Asia/Kolkata",
                 "source_refresh_time": "08:00", "prepare_time": "09:00"
             }), \
             patch("telegram_bot.source_manager.get_sources_status", return_value={
                 "last_attempt": None, "last_result": "none recorded"
             }):
            res = handle_update(make_callback(12345, "src:sched_enable"), self.owner_ids)
            self.assertIn("Daily source refresh enabled", res[1])
            mock_set.assert_any_call("source_refresh_enabled", True)
            mock_set.assert_any_call("scheduler_enabled", True)


if __name__ == "__main__":
    unittest.main()
