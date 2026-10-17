import tempfile
import unittest
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("PERSONALAGENT_MONITOR_FILE", "tests/.monitoring-test.json")

from telegram_bot.access import AccessConfigurationError, is_authorized, parse_owner_ids
from telegram_bot.commands import COMMAND_NAMES, handle_update
from telegram_bot.poller import PollLock, run_bot
from telegram_bot import monitoring


def update(user_id=12345, text="/help", chat_type="private", chat_id=12345, update_id=1):
    return {
        "update_id": update_id,
        "message": {
            "from": {"id": user_id}, "chat": {"id": chat_id, "type": chat_type}, "text": text,
        },
    }


class TelegramAccessTests(unittest.TestCase):
    def test_owner_ids_fail_closed(self):
        self.assertEqual(parse_owner_ids("123, 456"), frozenset({123, 456}))
        for raw in ("", "abc", "123,nope"):
            with self.subTest(raw=raw), self.assertRaises(AccessConfigurationError):
                parse_owner_ids(raw)

    def test_only_owner_in_private_chat_authorized(self):
        self.assertTrue(is_authorized(update(), {12345}))
        self.assertFalse(is_authorized(update(user_id=9), {12345}))
        self.assertFalse(is_authorized(update(chat_type="group", chat_id=-100), {12345}))
        self.assertFalse(is_authorized({}, {12345}))

    def test_unauthorized_never_reads_protected_state(self):
        called = []
        result = handle_update(update(user_id=8, text="/status"), {12345}, queue_reader=lambda: called.append("queue"))
        self.assertIsNone(result)
        self.assertEqual(called, [])

    def test_allowlisted_commands_and_unknown_input(self):
        self.assertEqual(set(COMMAND_NAMES), {"start", "help", "commands", "status", "health", "queue", "logs", "task", "history",
                                              "showanalytics", "showviews", "showlikes", "showcomments", "videos",
                                              "prepare", "upload", "pause", "resume", "cancel", "retry", "confirm", "deny"})
        self.assertIn("/health", handle_update(update(text="/commands@agent"), {12345})[1])
        self.assertEqual(handle_update(update(text="/missing"), {12345})[1], "Unknown command. Use /help.")
        self.assertEqual(handle_update(update(text="/status extra"), {12345})[1], "This command takes no arguments. Use /help.")
        queue = [{"status": "pending"}, {"status": "uploaded"}, {"status": "failed"}]
        with patch("telegram_bot.monitoring.pid_is_alive", return_value=True):
            result = handle_update(update(text="/status"), {12345}, queue_reader=lambda: queue,
                                   monitor_reader=lambda: {"running": True, "pid": 12345,
                                                           "stage": "render", "current_task": "clip-1"})[1]
        self.assertIn("Running: stage=render; task=clip-1.", result)
        self.assertIn("pending=1", result)

    def test_queue_task_logs_and_history_commands(self):
        queue = [{"id": "clip-7", "status": "failed", "title": "Example", "last_error": "broken"}]
        registry = {"task_results": [{"at": "now", "task": "YouTube upload", "status": "failure", "error_category": "quota"}]}
        call = lambda text: handle_update(update(text=text), {12345}, queue_reader=lambda: queue,
                                           monitor_reader=lambda: registry,
                                           logs_reader=lambda count: ["safe log line"])[1]
        self.assertIn("failed: clip-7", call("/queue"))
        self.assertEqual(call("/task clip-7"), "Task clip-7: failed; error=broken")
        self.assertIn("YouTube upload: failure", call("/history"))
        self.assertEqual(call("/logs"), "Recent safe logs:\nsafe log line")
        self.assertEqual(call("/task"), "Usage: /task TASK_ID")

    def test_health_response_only_reports_age_and_state(self):
        now = datetime(2026, 10, 17, tzinfo=timezone.utc)
        response = handle_update(update(text="/health"), {12345}, heartbeat_reader=lambda: {
            "last_success": "2026-10-16T00:00:00+00:00", "private": "must-not-leak",
        }, max_hours=26, now=now,
           monitor_reader=lambda: {"last_run_status": "failed", "last_error": {"task": "render", "category": "upload", "message": "denied"},
                                   "last_api_response": {"task": "YouTube upload", "status": "failure", "category": "quota"}},
           integrations_reader=lambda: {"telegram": "online", "youtube": "configured"})[1]
        self.assertIn("Last success marker: 24.0 hours ago", response)
        self.assertIn("does not prove end-to-end success", response)
        self.assertIn("Last run result: failed", response)
        self.assertIn("YouTube upload=failure", response)
        self.assertIn("render / upload: denied", response)
        self.assertNotIn("must-not-leak", response)

    def test_monitor_registry_preserves_failure_and_never_calls_failed_run_success(self):
        with tempfile.TemporaryDirectory(dir="tests") as directory:
            path = Path(directory) / "monitoring.json"
            monitoring.begin_run(path, pid=12345)
            monitoring.record_event("pipeline_progress", {"stage": "render", "task": "clip-1"}, path)
            monitoring.record_event("task_result", {"task": "YouTube upload", "status": "failure",
                                                      "error_category": "account_upload_limit", "error": "uploadLimitExceeded"}, path)
            state = monitoring.finish_run(path=path)
            self.assertEqual(state["last_run_status"], "failed")
            self.assertEqual(state["last_api_response"]["status"], "failure")
            self.assertEqual(state["last_error"]["category"], "account_upload_limit")
            self.assertFalse(state["running"])

    def test_poller_uses_single_lock_and_fixed_command_registry(self):
        class FakeLock:
            held = False
            def acquire(self): self.held = True; return True
            def release(self): self.held = False

        calls, replies = [], []
        def fake_api(token, method, params, timeout):
            calls.append((method, params))
            if len(calls) == 1:
                return [update(text="/commands", update_id=4), update(user_id=99, text="/status", update_id=5)]
            return []
        stops = iter([False, False, True])
        lock = FakeLock()
        run_bot(token="fake", owner_ids_raw="12345", api_call=fake_api,
                reply_sender=lambda chat, text: replies.append((chat, text)), lock=lock,
                sleep=lambda _seconds: None, stop_requested=lambda: next(stops))
        self.assertFalse(lock.held)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(replies), 1)
        self.assertIn("/health", replies[0][1])
        self.assertEqual(calls[1][1]["offset"], 6)

    def test_poll_lock_refuses_existing_unknown_lock(self):
        with tempfile.TemporaryDirectory(dir="tests") as directory:
            path = Path(directory) / "poll.lock"
            path.write_text("not-json", encoding="utf-8")
            lock = PollLock(path)
            self.assertFalse(lock.acquire())
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
