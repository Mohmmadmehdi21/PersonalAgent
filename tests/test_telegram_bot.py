import tempfile
import unittest
import os
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("PERSONALAGENT_MONITOR_FILE", "tests/.monitoring-test.json")

from telegram_bot.access import AccessConfigurationError, get_owner_ids, is_authorized, parse_owner_ids
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


def callback_update(user_id=12345, data="/confirm abc12345", chat_type="private", chat_id=12345, update_id=2):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "cb_1",
            "from": {"id": user_id},
            "message": {
                "chat": {"id": chat_id, "type": chat_type},
                "text": "Please confirm",
            },
            "data": data,
        },
    }


class TelegramAccessTests(unittest.TestCase):
    def setUp(self):
        os.environ["BOT_AUTO_START"] = "true"
        from telegram_bot import lifecycle
        lifecycle.set_state(lifecycle.STATE_RUNNING)

    def test_single_valid_owner_id(self):
        self.assertEqual(parse_owner_ids("123456789"), frozenset({123456789}))
        self.assertEqual(parse_owner_ids(123456789), frozenset({123456789}))
        for raw in (True, 123.45):
            with self.subTest(raw=raw), self.assertRaises(AccessConfigurationError):
                parse_owner_ids(raw)

    def test_multiple_comma_separated_owner_ids(self):
        self.assertEqual(parse_owner_ids("123456789,987654321"), frozenset({123456789, 987654321}))
        self.assertEqual(parse_owner_ids("111,222,333,444"), frozenset({111, 222, 333, 444}))

    def test_whitespace_around_ids(self):
        self.assertEqual(
            parse_owner_ids("  123456789  , \t 987654321 \n ,  55555  "),
            frozenset({123456789, 987654321, 55555})
        )

    def test_missing_configuration(self):
        with self.assertRaises(AccessConfigurationError):
            parse_owner_ids(None)
        with patch.dict(os.environ, {}, clear=True):
            with patch("telegram_bot.access.load_project_env", return_value={}):
                with self.assertRaises(AccessConfigurationError):
                    get_owner_ids()
        with patch.dict(os.environ, {"TELEGRAM_OWNER_IDS": "   "}), \
             patch("telegram_bot.access.load_project_env", return_value={}):
            with self.assertRaises(AccessConfigurationError):
                get_owner_ids()

    def test_empty_malformed_or_non_numeric_ids(self):
        malformed_inputs = [
            "", "   ", ",,,", "abc", "123,nope", "-123", "0", "123, 0", "123.45", "!@#$", "123, -456", "¹²³"
        ]
        for raw in malformed_inputs:
            with self.subTest(raw=raw), self.assertRaises(AccessConfigurationError):
                parse_owner_ids(raw)

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

    def test_authorized_owner_command_execution(self):
        owner_ids = frozenset({12345, 67890})
        self.assertIn("PersonalAgent", handle_update(update(user_id=12345, text="/start"), owner_ids)[1])
        self.assertIn("Commands:", handle_update(update(user_id=12345, text="/help"), owner_ids)[1])
        with patch("telegram_bot.monitoring.pid_is_alive", return_value=False):
            res = handle_update(update(user_id=67890, text="/status"), owner_ids, queue_reader=lambda: [])
            self.assertIn("Queue:", res[1])

    def test_unauthorized_user_command_rejection(self):
        owner_ids = frozenset({12345})
        for cmd in ("/start", "/help", "/status", "/health", "/queue", "/upload", "/prepare", "/pause", "/resume", "/cancel 1", "/retry 1", "/confirm 1", "/deny 1"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(handle_update(update(user_id=99999, text=cmd), owner_ids))

    def test_callback_and_inline_button_authorization(self):
        owner_ids = frozenset({12345})

        with patch("telegram_bot.control.confirm", return_value="Upload approved for clip c1.") as mock_confirm:
            cb_auth = callback_update(user_id=12345, data="/confirm code1")
            self.assertTrue(is_authorized(cb_auth, owner_ids))
            res = handle_update(cb_auth, owner_ids)
            self.assertIsNotNone(res)
            self.assertEqual(res[0], "12345")
            self.assertEqual(res[1], "Upload approved for clip c1.")
            mock_confirm.assert_called_once()

        cb_unauth = callback_update(user_id=99999, data="/confirm code1")
        self.assertFalse(is_authorized(cb_unauth, owner_ids))
        self.assertIsNone(handle_update(cb_unauth, owner_ids))

        cb_group = callback_update(user_id=12345, data="/confirm code1", chat_type="group", chat_id=-100)
        self.assertFalse(is_authorized(cb_group, owner_ids))
        self.assertIsNone(handle_update(cb_group, owner_ids))

        self.assertFalse(is_authorized({"callback_query": {}}, owner_ids))

    def test_unauthorized_requests_cannot_trigger_side_effects(self):
        owner_ids = frozenset({12345})
        unauthorized_id = 99999
        with patch("telegram_bot.control.launch") as mock_launch, \
             patch("telegram_bot.control.set_paused") as mock_pause, \
             patch("telegram_bot.control.cancel") as mock_cancel, \
             patch("telegram_bot.control.confirm") as mock_confirm, \
             patch("telegram_bot.control.retry_task") as mock_retry:

            for cmd in ("/upload", "/prepare", "/pause", "/resume", "/cancel task-1", "/retry task-1", "/confirm code1", "/deny code1", "prepare clips", "pause the queue"):
                res = handle_update(update(user_id=unauthorized_id, text=cmd), owner_ids)
                self.assertIsNone(res)

            cb_res = handle_update(callback_update(user_id=unauthorized_id, data="/confirm code1"), owner_ids)
            self.assertIsNone(cb_res)

            mock_launch.assert_not_called()
            mock_pause.assert_not_called()
            mock_cancel.assert_not_called()
            mock_confirm.assert_not_called()
            mock_retry.assert_not_called()

    def test_multi_owner_control_actions(self):
        from telegram_bot.control import _is_authorized_owner
        with patch.dict(os.environ, {"TELEGRAM_OWNER_IDS": "12345,67890"}):
            self.assertTrue(_is_authorized_owner(12345, 12345))
            self.assertTrue(_is_authorized_owner(67890, 12345))
            self.assertFalse(_is_authorized_owner(99999, 12345))

    def test_allowlisted_commands_and_unknown_input(self):
        self.assertEqual(set(COMMAND_NAMES), {"start", "help", "commands", "status", "health", "queue", "logs", "task", "history",
                                              "showanalytics", "showviews", "showlikes", "showcomments", "videos",
                                              "review",
                                              "prepare", "upload", "pause", "resume", "cancel", "retry", "confirm", "deny",
                                              "sources", "sourcemanager"})
        self.assertIn("/health", handle_update(update(text="/commands@agent"), {12345})[1])
        self.assertEqual(handle_update(update(text="/missing"), {12345})[1], "Unknown command. Use /help.")
        self.assertEqual(handle_update(update(text="/status extra"), {12345})[1], "This command takes no arguments. Use /help.")
        queue = [{"status": "pending"}, {"status": "uploaded"}, {"status": "failed"}]
        with patch("telegram_bot.monitoring.pid_is_alive", return_value=True), \
             patch("telegram_bot.control._read", return_value={"tasks": {}}):
            result = handle_update(update(text="/status"), {12345}, queue_reader=lambda: queue,
                                   monitor_reader=lambda: {"running": True, "pid": 12345,
                                                           "stage": "render", "current_task": "clip-1"})[1]
        self.assertIn("Running: stage=render; task=clip-1.", result)
        self.assertIn("pending=1", result)

    def test_review_route_shows_existing_manual_review_items(self):
        queue = [
            {"id": "clip-review", "status": "needs_review", "title": "Needs check", "review_reason": "verify source"},
            {"id": "clip-pending", "status": "pending", "title": "Not for review"},
        ]
        result = handle_update(update(text="/review"), {12345}, queue_reader=lambda: queue)[1]
        self.assertIn("Manual review required: 1", result)
        self.assertIn("clip-review", result)
        self.assertNotIn("clip-pending", result)
        self.assertEqual(handle_update(update(text="/review extra"), {12345})[1],
                         "This command takes no arguments. Use /help.")

    def test_phase1_start_menu_is_owner_only_and_contains_monitoring_actions(self):
        authorized = handle_update(update(text="/start"), {12345})
        self.assertEqual(len(authorized), 3)
        self.assertIn("monitoring", authorized[1])
        callbacks = [button["callback_data"] for row in authorized[2]["inline_keyboard"] for button in row]
        self.assertEqual(set(callbacks), {"pa1:status", "pa1:queue:0", "pa1:health", "pa1:review:0",
                                          "pa1:help", "pa2:prepare", "src:menu", "life:stop_prompt", "life:exit_prompt"})
        self.assertFalse(any("upload" in value for value in callbacks))
        self.assertIsNone(handle_update(update(user_id=99999, text="/start"), {12345}))

    def test_phase1_callbacks_auth_allowlist_and_main_menu(self):
        queue = [{"id": "candidate", "status": "needs_review", "title": "Check me"}]
        result = handle_update(callback_update(data="pa1:review:0"), {12345}, queue_reader=lambda: queue)
        self.assertEqual(result[0], "12345")
        self.assertIn("candidate", result[1])
        self.assertEqual(result[2]["inline_keyboard"][-1][0]["callback_data"], "pa1:home")
        self.assertIsNone(handle_update(callback_update(user_id=99999, data="pa1:queue:0"), {12345},
                                         queue_reader=lambda: self.fail("unauthorized callback read queue")))
        for invalid in ("pa1:upload", "pa1:queue:-1", "pa1:queue:99999", "pa1:review:0:extra", "unknown"):
            with self.subTest(data=invalid):
                response = handle_update(callback_update(data=invalid), {12345})
                self.assertIn("invalid or expired", response[1])
        home = handle_update(callback_update(data="pa1:home"), {12345})
        self.assertIn("monitoring", home[1])
        self.assertIn("pa1:status", str(home[2]))

    def test_phase1_queue_and_review_pagination_are_read_only(self):
        from telegram_bot import commands
        records = [{"id": f"clip-{i}", "status": "needs_review", "title": f"Clip {i}"} for i in range(12)]
        with tempfile.TemporaryDirectory(dir="tests") as directory:
            path = Path(directory) / "queue.json"
            original = json.dumps(records, separators=(",", ":"))
            path.write_text(original, encoding="utf-8")
            with patch.object(commands, "QUEUE_FILE", path):
                page0 = handle_update(callback_update(data="pa1:review:0"), {12345})
                page1 = handle_update(callback_update(data="pa1:review:1"), {12345})
                queue_view = handle_update(callback_update(data="pa1:queue:0"), {12345})
                self.assertIn("clip-0", page0[1])
                self.assertNotIn("clip-6", page0[1])
                self.assertIn("clip-5", page1[1])
                self.assertIn("Next →", str(page0[2]))
                self.assertIn("pending=0", queue_view[1])
                self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_phase1_queue_reader_reports_malformed_data_instead_of_empty(self):
        from telegram_bot.commands import _read_queue
        with tempfile.TemporaryDirectory(dir="tests") as directory:
            path = Path(directory) / "queue.json"
            path.write_text('{"not": "a queue"}', encoding="utf-8")
            with patch("telegram_bot.commands.QUEUE_FILE", path), self.assertRaisesRegex(ValueError, "not a JSON list"):
                _read_queue()
            path.write_text('[{"id":"ok"}, null]', encoding="utf-8")
            with patch("telegram_bot.commands.QUEUE_FILE", path), self.assertRaisesRegex(ValueError, "malformed record"):
                _read_queue()

    def test_phase1_empty_queue_and_queue_read_failure_are_truthful(self):
        empty = handle_update(callback_update(data="pa1:queue:0"), {12345}, queue_reader=lambda: [])
        self.assertIn("pending=0", empty[1])
        self.assertIn("Items 0-0 of 0", empty[1])
        failed = handle_update(callback_update(data="pa1:queue:0"), {12345},
                               queue_reader=lambda: (_ for _ in ()).throw(OSError("storage unavailable")))
        self.assertIn("Queue information is unavailable", failed[1])
        self.assertNotIn("pending=0", failed[1])

    def test_phase1_status_distinguishes_unknown_live_state_and_recorded_stage(self):
        control = {"tasks": {"t-live": {"id": "t-live", "kind": "prepare", "status": "running",
                                          "created_at": "today"}}}
        with patch("telegram_bot.control._read", return_value=control):
            text = handle_update(update(text="/status"), {12345}, queue_reader=lambda: [],
                                 monitor_reader=lambda: {"last_run_status": "completed"})[1]
        self.assertIn("live status unknown", text)
        self.assertIn("Processing stage: not recorded", text)
        self.assertIn("separate from YouTube publication", text)

    def test_phase1_status_reports_corrupt_task_state_without_fabricating_idle(self):
        from telegram_bot import commands
        with tempfile.TemporaryDirectory(dir="tests") as directory:
            path = Path(directory) / "telegram_control.json"
            path.write_text("{broken", encoding="utf-8")
            with patch.object(commands, "_read_queue", return_value=[]), \
                 patch("telegram_bot.control.CONTROL_FILE", path), \
                 patch("telegram_bot.control._read", return_value={}):
                response = handle_update(update(text="/status"), {12345})
        self.assertIn("Task status is unavailable", response[1])
        self.assertNotIn("Not running", response[1])

    def test_phase1_health_does_not_probe_or_expose_credentials(self):
        response = handle_update(callback_update(data="pa1:health"), {12345}, heartbeat_reader=lambda: {},
                                 monitor_reader=lambda: {"last_run_status": "not recorded"},
                                 integrations_reader=lambda: {"youtube": "credentials present; connectivity not tested",
                                                              "google_cloud_adc": "not required"})
        self.assertIn("connectivity not tested", response[1])
        self.assertIn("google_cloud_adc: not required", response[1])
        self.assertNotIn("token.json value", response[1])

    def test_phase1_health_shows_missing_config_without_secret_values(self):
        with patch.dict(os.environ, {}, clear=True), patch("env_config.load_project_env", return_value={}), \
             patch("telegram_bot.commands._safe_storage_snapshot", return_value={"data_writable": False,
                                                                                  "review_writable": False,
                                                                                  "free_gb": 0}):
            response = handle_update(callback_update(data="pa1:health"), {12345}, heartbeat_reader=lambda: {},
                                     monitor_reader=lambda: {"last_run_status": "not recorded"},
                                     integrations_reader=lambda: {"youtube": "credentials missing",
                                                                  "google_growth": "not configured",
                                                                  "google_cloud_adc": "not required"})
        self.assertIn("Telegram bot token=missing", response[1])
        self.assertIn("owner IDs=missing", response[1])
        self.assertIn("youtube: credentials missing", response[1])

    def test_status_does_not_call_dead_control_process_running(self):
        control_data = {"tasks": {"t-dead": {"id": "t-dead", "kind": "upload", "status": "running",
                                                    "pid": 99999, "error": "Worker stopped"}}}
        with patch("telegram_bot.control._read", return_value=control_data), \
             patch("telegram_bot.monitoring.pid_is_alive", return_value=False):
            result = handle_update(update(text="/status"), {12345}, queue_reader=lambda: [],
                                   monitor_reader=lambda: {"running": False, "last_run_status": "failed"})[1]
        self.assertIn("is not running", result)
        self.assertNotIn("Telegram task t-dead (upload): running.", result)

    def test_finish_confirmation_state_is_null_and_malformed_safe(self):
        from telegram_bot.control import finish
        cases = [
            ({"tasks": {"t1": {"status": "running"}}}, None),
            ({"tasks": {"t1": {"status": "running"}}, "confirmation": None}, None),
            ({"tasks": {"t1": {"status": "running"}}, "confirmation": "malformed"}, "malformed"),
            ({"tasks": {"t1": {"status": "running"}}, "confirmation": {"id": "c", "task_id": "t1"}}, None),
            ({"tasks": {"t1": {"status": "running"}}, "confirmation": {"id": "c", "task_id": "t2"}},
             {"id": "c", "task_id": "t2"}),
        ]
        for state, expected in cases:
            with self.subTest(confirmation=state.get("confirmation", "<missing>")), \
                 patch("telegram_bot.control._read", return_value=state), patch("telegram_bot.control._write"):
                finish("t1", "failed")
            self.assertEqual(state.get("confirmation"), expected)
            self.assertEqual(state["tasks"]["t1"]["status"], "failed")

    def test_late_concurrent_finish_is_idempotent_and_keeps_other_confirmation(self):
        from telegram_bot import control
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "tests") as directory:
            control_file = Path(directory) / "control.json"
            state = {"tasks": {"t1": {"id": "t1", "status": "running"},
                               "t2": {"id": "t2", "status": "waiting_confirmation"}},
                     "confirmation": {"id": "c2", "task_id": "t2", "clip_id": "clip2", "status": "pending"}}
            with patch.object(control, "CONTROL_FILE", control_file):
                control._write(state)
                control.finish("t1", "failed")
                self.assertEqual(control._read()["confirmation"]["id"], "c2")
                errors = []
                threads = [threading.Thread(target=lambda: self._capture_finish(control, errors)) for _ in range(6)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
                saved = control._read()
        self.assertEqual(errors, [])
        self.assertIsNone(saved["confirmation"])
        self.assertEqual(saved["tasks"]["t2"]["status"], "completed")

    @staticmethod
    def _capture_finish(control, errors):
        try:
            control.finish("t2", "completed")
        except Exception as exc:
            errors.append(exc)

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

    def test_health_distinguishes_preparation_from_publication_and_missing_marker(self):
        from telegram_bot.commands import _health
        response = _health(heartbeat_reader=lambda: {}, now=datetime(2026, 10, 20, tzinfo=timezone.utc),
                           monitor_reader=lambda: {"last_run_status": "completed", "last_run_publications": 0,
                                                   "last_error": None},
                           integrations_reader=lambda: {"youtube": "credentials present; connectivity not tested",
                                                        "google_cloud_adc": "not required"},
                           storage_checker=lambda: {"data_writable": True, "review_writable": True, "free_gb": 10})
        self.assertIn("Last run result: completed", response)
        self.assertIn("none confirmed", response)
        self.assertIn("Last public publication marker: not recorded", response)
        self.assertIn("connectivity not tested", response)
        self.assertIn("google_cloud_adc: not required", response)

    def test_monitor_registry_tracks_confirmed_upload_separately_from_task_completion(self):
        with tempfile.TemporaryDirectory(dir="tests") as directory:
            path = Path(directory) / "monitoring.json"
            monitoring.begin_run(path, pid=12345)
            monitoring.record_event("task_result", {"task": "prepare:transcribe", "status": "success"}, path)
            state = monitoring.finish_run(path=path)
            self.assertEqual(state["last_run_status"], "completed")
            self.assertEqual(state["last_run_publications"], 0)
            self.assertNotIn("last_publication_at", state)
            monitoring.begin_run(path, pid=12345)
            monitoring.record_event("task_result", {"task": "clip:c1", "status": "success",
                                                       "youtube_video_id": "public-id",
                                                       "privacy_status": "public"}, path)
            state = monitoring.finish_run(path=path)
            self.assertEqual(state["last_run_publications"], 1)
            self.assertTrue(state.get("last_publication_at"))
            publication_marker = state["last_publication_at"]

            monitoring.begin_run(path, pid=12345)
            monitoring.record_event("task_result", {"task": "clip:c2", "status": "success",
                                                       "youtube_video_id": "private-id",
                                                       "privacy_status": "private"}, path)
            state = monitoring.finish_run(path=path)
            self.assertEqual(state["last_run_publications"], 0)
            self.assertEqual(state["last_publication_at"], publication_marker)

            from telegram_bot import commands
            response = commands._health(heartbeat_reader=lambda: {}, monitor_reader=lambda: {
                "last_run_status": "completed", "last_run_publications": 2,
                "last_run_publication_visibility": "none",
                "last_publication_at": publication_marker,
            })
            self.assertIn("public visibility is unverified (legacy state)", response)

    def test_integration_health_separates_credentials_from_connectivity_and_optional_adc(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(monitoring.Path, "is_file", new=lambda path: path.name == "token.json"):
            integrations = monitoring.integration_status()
        self.assertEqual(integrations["youtube"], "credentials present; connectivity not tested")
        self.assertEqual(integrations["google_cloud_adc"], "not required (Agent Platform not configured)")
        self.assertIn("no external API probes", integrations["note"])
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(monitoring.Path, "is_file", new=lambda path: False):
            integrations = monitoring.integration_status()
        self.assertEqual(integrations["youtube"], "credentials missing")

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

    def test_poller_answers_callbacks_and_deduplicates_repeated_callback_id(self):
        class FakeLock:
            def acquire(self): return True
            def release(self): pass
        cb = callback_update(data="pa1:help", update_id=10)
        calls, replies = [], []
        def fake_api(token, method, params, timeout):
            calls.append((method, params))
            return [cb, cb] if method == "getUpdates" and sum(name == "getUpdates" for name, _ in calls) == 1 else []
        stop = iter([False, True])
        run_bot(token="fake", owner_ids_raw="12345", api_call=fake_api,
                reply_sender=lambda chat, text, **kwargs: replies.append((chat, text, kwargs)),
                lock=FakeLock(), sleep=lambda _seconds: None, stop_requested=lambda: next(stop))
        answers = [params for method, params in calls if method == "answerCallbackQuery"]
        self.assertEqual(len(replies), 1)
        self.assertEqual(len(answers), 2)
        self.assertEqual(answers[0], {"callback_query_id": "cb_1"})
        self.assertIn("already handled", answers[1]["text"])

    def test_poll_lock_refuses_existing_unknown_lock(self):
        with tempfile.TemporaryDirectory(dir="tests") as directory:
            path = Path(directory) / "poll.lock"
            path.write_text("not-json", encoding="utf-8")
            lock = PollLock(path)
            self.assertFalse(lock.acquire())
            self.assertTrue(path.exists())


class TelegramPipelineControlTests(unittest.TestCase):
    def setUp(self):
        from telegram_bot import lifecycle
        lifecycle.set_state(lifecycle.STATE_RUNNING)
        self.tmp_dir = tempfile.TemporaryDirectory(dir="tests")
        self.task_state_path = Path(self.tmp_dir.name) / "tasks.json"
        self.patch_state = patch("telegram_bot.control.CONTROL_FILE", self.task_state_path)
        self.patch_state.start()

    def tearDown(self):
        self.patch_state.stop()
        self.tmp_dir.cleanup()

    def test_pipeline_control_commands_via_handle_update(self):
        owner_ids = frozenset({12345})
        with patch("telegram_bot.control.launch", return_value="task123") as mock_launch, \
             patch("telegram_bot.control.set_paused") as mock_pause, \
             patch("telegram_bot.control.cancel", return_value="Cancellation requested.") as mock_cancel, \
             patch("telegram_bot.control.retry_task", return_value="Requeued clip.") as mock_retry, \
             patch("telegram_bot.control.confirm", return_value="Upload approved.") as mock_confirm:

            # /prepare
            res = handle_update(update(user_id=12345, text="/prepare"), owner_ids)
            self.assertIn("Started background prepare task task123", res[1])
            mock_launch.assert_called_with("prepare", 12345, 12345)

            # /upload
            res = handle_update(update(user_id=12345, text="/upload"), owner_ids)
            self.assertIn("Started background upload task task123", res[1])
            mock_launch.assert_called_with("upload", 12345, 12345)

            # /pause
            res = handle_update(update(user_id=12345, text="/pause"), owner_ids)
            self.assertIn("Paused", res[1])
            mock_pause.assert_called_with(True)

            # /resume
            res = handle_update(update(user_id=12345, text="/resume"), owner_ids)
            self.assertIn("Resumed", res[1])
            mock_pause.assert_called_with(False)

            # /cancel
            res = handle_update(update(user_id=12345, text="/cancel task123"), owner_ids)
            self.assertEqual(res[1], "Cancellation requested.")
            mock_cancel.assert_called_with("task123", 12345)

            # /retry
            res = handle_update(update(user_id=12345, text="/retry task123"), owner_ids)
            self.assertEqual(res[1], "Requeued clip.")
            mock_retry.assert_called_with("task123", 12345, 12345)

            # /confirm
            res = handle_update(update(user_id=12345, text="/confirm code1"), owner_ids)
            self.assertEqual(res[1], "Upload approved.")
            mock_confirm.assert_called_with("code1", 12345, True)

            # /deny
            res = handle_update(update(user_id=12345, text="/deny code1"), owner_ids)
            self.assertEqual(res[1], "Upload approved.")
            mock_confirm.assert_called_with("code1", 12345, False)

    def test_reconcile_tasks_recovers_dead_worker(self):
        from telegram_bot.control import reconcile_tasks
        state = {
            "tasks": {
                "t1": {
                    "id": "t1",
                    "status": "running",
                    "pid": 9999999,
                    "owner_id": "12345"
                },
                "t2": {
                    "id": "t2",
                    "status": "cancelling",
                    "cancel_requested": True,
                    "pid": 9999998,
                    "owner_id": "12345"
                }
            },
            "confirmation": {"id": "c1", "task_id": "t1", "clip_id": "clip1", "status": "pending"}
        }
        with patch("lock.is_pid_alive", return_value=False):
            changed = reconcile_tasks(state)
            self.assertTrue(changed)
            self.assertEqual(state["tasks"]["t1"]["status"], "failed")
            self.assertIn("terminated unexpectedly", state["tasks"]["t1"]["error"])
            self.assertEqual(state["tasks"]["t2"]["status"], "cancelled")
            self.assertIsNone(state["confirmation"])

    def test_confirmation_lifecycle_and_expiration(self):
        from telegram_bot.control import confirm
        state = {
            "tasks": {"t1": {"id": "t1", "status": "waiting_confirmation", "owner_id": "12345"}},
            "confirmation": {
                "id": "conf1",
                "task_id": "t1",
                "clip_id": "clipA",
                "owner_id": "12345",
                "action": "public_upload",
                "status": "pending",
                "expires_at": 100.0
            }
        }
        with patch("telegram_bot.control._read", return_value=state), \
             patch("telegram_bot.control._write"), \
             patch("telegram_bot.control._is_authorized_owner", return_value=True), \
             patch("time.time", return_value=200.0):

            res = confirm("conf1", 12345, approved=True)
            self.assertIn("expired", res)
            self.assertEqual(state["confirmation"]["status"], "expired")

        state["confirmation"]["expires_at"] = 500.0
        state["confirmation"]["status"] = "pending"
        with patch("telegram_bot.control._read", return_value=state), \
             patch("telegram_bot.control._write"), \
             patch("telegram_bot.control._is_authorized_owner", return_value=True), \
             patch("time.time", return_value=200.0):

            res = confirm("conf1", 12345, approved=True)
            self.assertEqual(res, "Upload approved for clip clipA.")
            self.assertEqual(state["confirmation"]["status"], "approved")

            res2 = confirm("conf1", 12345, approved=True)
            self.assertIn("not found or already used", res2)

    def test_confirmation_rejects_mismatched_task_clip_or_owner(self):
        from telegram_bot.control import confirm
        state = {
            "tasks": {"t1": {"id": "t1", "status": "waiting_confirmation", "owner_id": "12345",
                              "clip_ids": ["clip-1"]}},
            "confirmation": {"id": "code1", "task_id": "t1", "clip_id": "clip-2", "owner_id": "12345",
                             "status": "pending", "expires_at": 500.0},
        }
        with patch("telegram_bot.control._read", return_value=state), patch("telegram_bot.control._write"), \
             patch("telegram_bot.control._is_authorized_owner", return_value=True), patch("time.time", return_value=200.0):
            result = confirm("code1", 12345, True)
        self.assertIn("no longer attached", result)
        self.assertEqual(state["confirmation"]["status"], "pending")
        state["confirmation"]["clip_id"] = "clip-1"
        with patch("telegram_bot.control._read", return_value=state), patch("telegram_bot.control._write"), \
             patch("telegram_bot.control._is_authorized_owner", return_value=False), patch("time.time", return_value=200.0):
            result = confirm("code1", 99999, True)
        self.assertIn("not found or already used", result)
        self.assertEqual(state["confirmation"]["status"], "pending")

    def test_checkpoint_sends_inline_keyboard(self):
        from telegram_bot.control import checkpoint
        clip = {"id": "clip-99", "title": "Test Title"}
        state = {
            "tasks": {"task-test": {"id": "task-test", "status": "running"}},
            "confirmation": None,
            "paused": False
        }
        sent_messages = []
        def fake_send(chat_id, text, reply_markup=None):
            sent_messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
            state["confirmation"]["status"] = "approved"
            return True

        with patch.dict(os.environ, {"AGENT_CONTROL_TASK_ID": "task-test", "AGENT_CONTROL_CHAT_ID": "12345"}), \
             patch("telegram_bot.control._read", side_effect=lambda: dict(state)), \
             patch("telegram_bot.control._write"), \
             patch("telegram_bot.control._mutate", side_effect=lambda fn: fn(state)), \
             patch("notify.send_telegram_to", side_effect=fake_send), \
             patch("time.sleep", side_effect=lambda s: None):

            outcome = checkpoint(clip=clip, ask_upload=True)
            from upload_auth import UploadApproval
            self.assertIsInstance(outcome, UploadApproval)
            self.assertEqual(outcome.clip_id, "clip-99")
            self.assertEqual(len(sent_messages), 1)
            self.assertIn("Public upload confirmation required", sent_messages[0]["text"])
            keyboard = sent_messages[0]["reply_markup"]["inline_keyboard"][0]
            self.assertEqual(keyboard[0]["text"], "✅ Confirm Upload")
            self.assertEqual(keyboard[1]["text"], "❌ Deny Upload")

    def test_direct_clip_id_retry(self):
        from telegram_bot.control import retry_task
        queue = [{"id": "failed-clip", "status": "failed", "last_error": "timeout"}]
        with patch("telegram_bot.control.task_info", return_value=None), \
             patch("queue_manager.load_queue", return_value=queue), \
             patch("queue_manager.save_queue") as mock_save, \
             patch("telegram_bot.control.launch", return_value="new_task_1"):

            msg = retry_task("failed-clip", 12345, 12345)
            self.assertIn("Requeued clip failed-clip; started upload task new_task_1", msg)
            self.assertEqual(queue[0]["status"], "pending")
            self.assertNotIn("last_error", queue[0])
            mock_save.assert_called_once()

    def test_prepare_menu_uses_existing_background_command_and_deduplicates_button(self):
        from types import SimpleNamespace
        from telegram_bot import control
        popen_calls = []
        class FakeThread:
            def __init__(self, *args, **kwargs): pass
            def start(self): pass
        def fake_popen(command, **kwargs):
            popen_calls.append((command, kwargs))
            return SimpleNamespace(pid=454545)
        cb = callback_update(data="pa2:prepare")
        with patch.object(control, "LOG_DIR", Path(self.tmp_dir.name) / "logs"), \
             patch("telegram_bot.control.subprocess.Popen", side_effect=fake_popen), \
             patch("telegram_bot.control.threading.Thread", FakeThread):
            first = handle_update(cb, {12345})
            second = handle_update(cb, {12345})
        self.assertIn("Started background prepare task", first[1])
        self.assertIn("already used", second[1])
        self.assertEqual(len(popen_calls), 1)
        self.assertEqual(popen_calls[0][0][-2:], ["main.py", "prepare"])
        state = control._read()
        task = next(iter(state["tasks"].values()))
        self.assertEqual(task["status"], "running")
        self.assertIn("created_at", task)
        self.assertIn("started_at", task)
        self.assertIn("updated_at", task)
        self.assertEqual(task["clip_ids"], [])

    def test_prepare_launch_rejects_second_incompatible_active_task(self):
        from types import SimpleNamespace
        from telegram_bot import control
        class FakeThread:
            def __init__(self, *args, **kwargs): pass
            def start(self): pass
        with patch.object(control, "LOG_DIR", Path(self.tmp_dir.name) / "logs"), \
             patch("telegram_bot.control.subprocess.Popen", return_value=SimpleNamespace(pid=os.getpid())), \
             patch("telegram_bot.control.threading.Thread", FakeThread):
            control.launch("prepare", 12345, 12345, request_key="button-A")
            with self.assertRaisesRegex(RuntimeError, "already active"):
                control.launch("upload", 12345, 12345, request_key="button-B")

    def test_prepare_menu_explains_missing_configuration_without_starting_task(self):
        from config import ConfigurationError
        with patch("config.validate_startup_config", side_effect=ConfigurationError("GROQ_MODEL is invalid")), \
             patch("telegram_bot.control.launch") as launch:
            response = handle_update(callback_update(data="pa2:prepare"), {12345})
        self.assertIn("configuration is incomplete", response[1])
        self.assertIn("GROQ_MODEL", response[1])
        launch.assert_not_called()

    def test_prepare_completion_notification_uses_verified_result_once(self):
        from telegram_bot import control
        from types import SimpleNamespace
        task = {"id": "prepare-1", "kind": "prepare", "chat_id": "12345", "status": "completed",
                "created_at": 1, "finished_at": 2,
                "result": {"verified_draft_count": 2, "clip_ids": ["draft-a", "draft-b"]}}
        control._write({"tasks": {"prepare-1": task}})
        fake_process = SimpleNamespace(wait=lambda: 0)
        with patch("notify.send_telegram_to", return_value=True) as send:
            control._watch_process("prepare-1", fake_process)
            control._watch_process("prepare-1", fake_process)
        send.assert_called_once()
        self.assertIn("Verified 2 draft(s)", send.call_args.args[1])
        self.assertIn("No upload or publication", send.call_args.args[1])
        saved = control._read()["tasks"]["prepare-1"]
        self.assertEqual(saved["status"], "completed")
        self.assertTrue(saved["completion_notification_sent"])

    def test_prepare_notification_failure_does_not_change_task_result(self):
        from telegram_bot import control
        task = {"id": "prepare-2", "kind": "prepare", "chat_id": "12345", "status": "completed",
                "result": {"verified_draft_count": 0, "clip_ids": []}}
        control._write({"tasks": {"prepare-2": task}})
        with patch("notify.send_telegram_to", return_value=False):
            control._watch_process("prepare-2", type("Process", (), {"wait": lambda self: 0})())
        saved = control._read()["tasks"]["prepare-2"]
        self.assertEqual(saved["status"], "completed")
        self.assertFalse(saved["completion_notification_sent"])

    def test_failure_notification_delivery_metadata_does_not_change_failure_state(self):
        from telegram_bot.control import record_notification_result
        state = {"tasks": {"failed-prepare": {"id": "failed-prepare", "status": "failed",
                                                  "error": "transcription failed"}}}
        with patch("telegram_bot.control._read", return_value=state), patch("telegram_bot.control._write"):
            record_notification_result("failed-prepare", "preparation_failure", False, "token=SECRET")
        saved = state["tasks"]["failed-prepare"]
        self.assertEqual(saved["status"], "failed")
        self.assertFalse(saved["preparation_failure_notification_sent"])
        self.assertNotIn("SECRET", saved["preparation_failure_notification_error"])

    def test_scheduler_independent_prepare_flag(self):
        from telegram_bot.scheduler import Scheduler
        from datetime import datetime
        from zoneinfo import ZoneInfo
        state = {"jobs": {}}
        launched = []

        with patch.dict(os.environ, {
            "SCHEDULER_ENABLED": "true",
            "TELEGRAM_CHAT_ID": "12345",
            "SCHEDULER_PREPARE_ENABLED": "false",
            "SCHEDULER_PREPARE_TIME": "09:00"
        }), patch("telegram_bot.scheduler._load_state", return_value=state), \
           patch("telegram_bot.scheduler._save_state"):

            sched = Scheduler(launch_job=lambda kind, k, rem: launched.append((kind, k)))
            sched.prepare_enabled = False
            at_9am = datetime(2026, 10, 20, 9, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
            sched.tick(now=at_9am)
            self.assertEqual(launched, [])

            sched.prepare_enabled = True
            sched.tick(now=at_9am)
            self.assertEqual(len(launched), 1)
            self.assertEqual(launched[0][0], "prepare")

    def test_scheduler_can_refresh_sources_without_preparing_or_publishing(self):
        from telegram_bot.scheduler import Scheduler
        from datetime import datetime
        from zoneinfo import ZoneInfo
        state = {"jobs": {}}
        launched = []
        with patch.dict(os.environ, {
            "SCHEDULER_ENABLED": "true",
            "TELEGRAM_CHAT_ID": "12345",
            "SCHEDULER_PREPARE_ENABLED": "false",
            "SCHEDULER_SOURCE_REFRESH_ENABLED": "true",
            "SCHEDULER_SOURCE_REFRESH_TIME": "08:00",
        }), patch("telegram_bot.scheduler._load_state", return_value=state), \
             patch("telegram_bot.scheduler._save_state"), \
             patch.object(Scheduler, "_source_refresh_job", return_value="refreshed"):
            scheduler = Scheduler(launch_job=lambda *args: launched.append(args))
            scheduler.tick(now=datetime(2026, 10, 20, 9, 30, tzinfo=ZoneInfo("Asia/Kolkata")))
        self.assertEqual(state["jobs"]["sources:2026-10-20"]["status"], "completed")
        self.assertEqual(launched, [])

    def test_storage_health_check(self):
        from telegram_bot.commands import _check_storage
        storage = _check_storage()
        self.assertIn("data_writable", storage)
        self.assertIn("review_writable", storage)
        self.assertIn("free_gb", storage)
        self.assertTrue(storage["free_gb"] > 0)

    def test_natural_language_router_safety(self):
        owner_ids = frozenset({12345})
        with patch("telegram_bot.control.launch", return_value="nl-task"):
            res = handle_update(update(user_id=12345, text="prepare clips"), owner_ids)
            self.assertIn("Started background prepare task", res[1])

        from telegram_bot.natural_language import classify
        with patch.dict(os.environ, {"GROQ_API_KEY": "gsk_test_key_that_should_never_be_used_1234567890"}), \
             patch("retry.create_groq_client", side_effect=AssertionError("local phrase must not call Groq")) as groq, \
             patch("retry.retry_groq", side_effect=AssertionError("local phrase must not call network")) as network:
            self.assertEqual(classify("prepare clips"), {"intent": "prepare", "range": None, "confidence": 1.0})
            groq.assert_not_called()
            network.assert_not_called()

        dangerous = ("import os; os.system('rm -rf')", "cat /etc/passwd", "run bash", "eval('1+1')", "drop table users")
        with patch("telegram_bot.natural_language.classify", return_value={"intent": "clarify", "range": None, "confidence": 0.0}):
            for text in dangerous:
                res_d = handle_update(update(user_id=12345, text=text), owner_ids)
                self.assertTrue(
                    "I need a little more detail" in res_d[1] or "/help" in res_d[1],
                    f"Unexpected response for dangerous input: {res_d[1]}"
                )

        with patch("telegram_bot.natural_language.classify") as classifier:
            self.assertIsNone(handle_update(update(user_id=99999, text="prepare clips"), owner_ids))
            classifier.assert_not_called()

    def test_natural_language_ambiguous_request_uses_injected_classifier(self):
        owner_ids = frozenset({12345})
        classifier = lambda text: {"intent": "clarify", "range": None, "confidence": 0.0}
        response = handle_update(update(user_id=12345, text="do something with those clips"), owner_ids,
                                 intent_classifier=classifier)
        self.assertIn("couldn't confidently identify", response[1])


if __name__ == "__main__":
    unittest.main()
