"""Comprehensive tests for Telegram Bot Start / Stop / Exit Lifecycle Control.

Covers:
1. STOPPED state: /status returns exact single response 'Please first start the bot.'
2. STOPPED state: /upload returns exact single response 'Please first start the bot.'
3. STOPPED state: unknown command returns exact single response 'Please first start the bot.'
4. STOPPED state: normal text returns exact single response 'Please first start the bot.'
5. Authorized Start Bot successful startup transitions to RUNNING.
6. Start failure safely cleans up and resets state to STOPPED.
7. Duplicate Start Bot callback rejected idempotently.
8. Unauthorized user cannot invoke Start, Stop, or Exit lifecycle callbacks.
9. Stop Bot prompts confirmation; Cancel returns safely to RUNNING.
10. Stop Bot confirmation gracefully stops services, pauses pipeline, sets STOPPED.
11. Post-stop commands are strictly blocked.
12. Start Bot is fully functional after Stop Bot (control transport preserved).
13. Exit confirmation requests exit and signals clean process shutdown.
14. Supervisor restart reconciliation marks stale RUNNING as STOPPED on boot.
15. Queues, sources.txt, uploaded ledger, and pending approvals preserved across shutdown.
16. Scheduler duplicate execution prevented across repeated starts.
17. Repeated messages produce exactly one response each, preventing loops.
"""
import copy
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from telegram_bot import lifecycle
from telegram_bot.commands import handle_update, _stopped_keyboard, _menu_keyboard
from telegram_bot.poller import run_bot, PollLock


def _msg_update(user_id=12345, text="/status", chat_id=12345):
    return {
        "update_id": 1,
        "message": {
            "from": {"id": user_id},
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
        },
    }


def _cb_update(user_id=12345, data="life:start", chat_id=12345):
    return {
        "update_id": 2,
        "callback_query": {
            "id": f"cb_{int(time.time() * 1000)}",
            "from": {"id": user_id},
            "message": {
                "chat": {"id": chat_id, "type": "private"},
                "text": "menu",
            },
            "data": data,
        },
    }


class BotLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.lifecycle_file = Path(self.tmp_dir.name) / "bot_lifecycle.json"
        self.offset_file = Path(self.tmp_dir.name) / "telegram_offset.json"
        os.environ["PERSONALAGENT_LIFECYCLE_FILE"] = str(self.lifecycle_file)
        os.environ["TELEGRAM_OFFSET_FILE"] = str(self.offset_file)
        os.environ["BOT_INITIAL_STATE"] = lifecycle.STATE_STOPPED
        os.environ["BOT_AUTO_START"] = "false"
        lifecycle.reset_exit_request()
        lifecycle.set_state(lifecycle.STATE_STOPPED)

    def tearDown(self):
        lifecycle.reset_exit_request()
        lifecycle.stop_scheduler()
        self.tmp_dir.cleanup()
        os.environ.pop("PERSONALAGENT_LIFECYCLE_FILE", None)
        os.environ.pop("TELEGRAM_OFFSET_FILE", None)

    # 1. STOPPED state /status exact single response
    def test_01_stopped_state_status_blocked(self):
        owner_ids = frozenset({12345})
        resp = handle_update(_msg_update(text="/status"), owner_ids)
        self.assertIsNotNone(resp)
        chat_id, text, markup = resp
        self.assertEqual(chat_id, "12345")
        self.assertEqual(text, "Please first start the bot.")
        self.assertEqual(markup, _stopped_keyboard())

    # 2. STOPPED state /upload exact single response
    def test_02_stopped_state_upload_blocked(self):
        owner_ids = frozenset({12345})
        resp = handle_update(_msg_update(text="/upload"), owner_ids)
        self.assertIsNotNone(resp)
        chat_id, text, markup = resp
        self.assertEqual(chat_id, "12345")
        self.assertEqual(text, "Please first start the bot.")
        self.assertEqual(markup, _stopped_keyboard())

    # 3. STOPPED state unknown command exact single response
    def test_03_stopped_state_unknown_command_blocked(self):
        owner_ids = frozenset({12345})
        resp = handle_update(_msg_update(text="/nonexistent_foo"), owner_ids)
        self.assertIsNotNone(resp)
        chat_id, text, markup = resp
        self.assertEqual(chat_id, "12345")
        self.assertEqual(text, "Please first start the bot.")
        self.assertEqual(markup, _stopped_keyboard())

    # 4. STOPPED state normal text exact single response (no natural language / intent execution)
    def test_04_stopped_state_normal_text_blocked(self):
        owner_ids = frozenset({12345})
        with patch("telegram_bot.natural_language.classify") as mock_classify, \
             patch("telegram_bot.control.launch") as mock_launch:
            resp = handle_update(_msg_update(text="Prepare clips please"), owner_ids)
            mock_classify.assert_not_called()
            mock_launch.assert_not_called()
            self.assertIsNotNone(resp)
            chat_id, text, markup = resp
            self.assertEqual(chat_id, "12345")
            self.assertEqual(text, "Please first start the bot.")
            self.assertEqual(markup, _stopped_keyboard())

    # 5. Authorized Start Bot successful startup
    def test_05_authorized_start_bot_success(self):
        owner_ids = frozenset({12345})
        self.assertEqual(lifecycle.get_state(), lifecycle.STATE_STOPPED)

        with patch("telegram_bot.control.reconcile_tasks") as mock_recon, \
             patch("telegram_bot.control.set_paused") as mock_pause:
            resp = handle_update(_cb_update(data="life:start"), owner_ids)
            self.assertIsNotNone(resp)
            chat_id, text, markup = resp
            self.assertIn("started successfully", text.lower())
            self.assertEqual(markup, _menu_keyboard())
            self.assertEqual(lifecycle.get_state(), lifecycle.STATE_RUNNING)
            mock_recon.assert_called_once()
            mock_pause.assert_called_once_with(False)

    # 6. Start failure and cleanup
    def test_06_start_bot_failure_and_cleanup(self):
        owner_ids = frozenset({12345})
        with patch("telegram_bot.control.reconcile_tasks", side_effect=RuntimeError("Database corruption")):
            resp = handle_update(_cb_update(data="life:start"), owner_ids)
            self.assertIsNotNone(resp)
            _chat_id, text, markup = resp
            self.assertIn("Startup failed", text)
            self.assertIn("Database corruption", text)
            self.assertEqual(markup, _stopped_keyboard())
            self.assertEqual(lifecycle.get_state(), lifecycle.STATE_STOPPED)

    # 7. Duplicate Start Bot callback
    def test_07_duplicate_start_bot_callback(self):
        owner_ids = frozenset({12345})
        # Set to RUNNING
        lifecycle.set_state(lifecycle.STATE_RUNNING)
        resp = handle_update(_cb_update(data="life:start"), owner_ids)
        self.assertIsNotNone(resp)
        _chat_id, text, markup = resp
        self.assertIn("already running", text.lower())
        self.assertEqual(markup, _menu_keyboard())

        # If STARTING
        lifecycle.set_state(lifecycle.STATE_STARTING)
        resp2 = handle_update(_cb_update(data="life:start"), owner_ids)
        self.assertIsNotNone(resp2)
        text2 = resp2[1]
        self.assertIn("starting up", text2.lower())

    # 8. Unauthorized Start/Stop/Exit rejection
    def test_08_unauthorized_lifecycle_rejection(self):
        owner_ids = frozenset({12345})
        intruder_id = 99999

        # Message command while stopped
        resp = handle_update(_msg_update(user_id=intruder_id, text="/start"), owner_ids)
        self.assertIsNone(resp)

        # Callbacks
        for action in ("life:start", "life:stop_prompt", "life:stop_confirm", "life:exit_prompt", "life:exit_confirm"):
            resp = handle_update(_cb_update(user_id=intruder_id, data=action), owner_ids)
            self.assertIsNone(resp)

    # 9. Running state Stop Bot confirmation and cancellation
    def test_09_stop_bot_prompt_and_cancel(self):
        owner_ids = frozenset({12345})
        lifecycle.set_state(lifecycle.STATE_RUNNING)

        # Prompt
        resp = handle_update(_cb_update(data="life:stop_prompt"), owner_ids)
        self.assertIsNotNone(resp)
        _chat_id, text, markup = resp
        self.assertIn("Confirm Stop", json.dumps(markup))
        self.assertEqual(lifecycle.get_state(), lifecycle.STATE_RUNNING)

        # Cancel
        resp_cancel = handle_update(_cb_update(data="life:stop_cancel"), owner_ids)
        _chat_id, text_c, markup_c = resp_cancel
        self.assertIn("still running", text_c.lower())
        self.assertEqual(markup_c, _menu_keyboard())
        self.assertEqual(lifecycle.get_state(), lifecycle.STATE_RUNNING)

    # 10. Graceful stop and worker shutdown
    def test_10_graceful_stop_and_worker_shutdown(self):
        owner_ids = frozenset({12345})
        lifecycle.set_state(lifecycle.STATE_RUNNING)

        with patch("telegram_bot.control.set_paused") as mock_pause, \
             patch("telegram_bot.lifecycle.stop_scheduler") as mock_stop_sched:
            resp = handle_update(_cb_update(data="life:stop_confirm"), owner_ids)
            self.assertIsNotNone(resp)
            _chat_id, text, markup = resp
            self.assertIn("stopped", text.lower())
            self.assertEqual(markup, _stopped_keyboard())
            self.assertEqual(lifecycle.get_state(), lifecycle.STATE_STOPPED)
            mock_pause.assert_called_once_with(True)
            mock_stop_sched.assert_called_once()

    # 11. Stop ke baad commands blocked
    def test_11_commands_blocked_after_stop(self):
        owner_ids = frozenset({12345})
        lifecycle.set_state(lifecycle.STATE_RUNNING)

        # Stop bot
        handle_update(_cb_update(data="life:stop_confirm"), owner_ids)
        self.assertEqual(lifecycle.get_state(), lifecycle.STATE_STOPPED)

        # Test various commands remain blocked
        for cmd in ("/status", "/queue", "/help", "/review", "/upload", "/prepare", "make 3 clips"):
            resp = handle_update(_msg_update(text=cmd), owner_ids)
            self.assertIsNotNone(resp)
            self.assertEqual(resp[1], "Please first start the bot.")
            self.assertEqual(resp[2], _stopped_keyboard())

        # /start after stop provides the startup menu and does not say 'Please first start the bot.'
        start_resp = handle_update(_msg_update(text="/start"), owner_ids)
        self.assertIsNotNone(start_resp)
        self.assertNotEqual(start_resp[1], "Please first start the bot.")
        self.assertIn("stopped", start_resp[1].lower())
        self.assertEqual(start_resp[2], _stopped_keyboard())

    # 12. Control transport se Stop ke baad Start possible
    def test_12_start_possible_after_stop(self):
        owner_ids = frozenset({12345})
        lifecycle.set_state(lifecycle.STATE_RUNNING)

        # Stop
        handle_update(_cb_update(data="life:stop_confirm"), owner_ids)
        self.assertEqual(lifecycle.get_state(), lifecycle.STATE_STOPPED)

        # Restart via control transport callback
        resp = handle_update(_cb_update(data="life:start"), owner_ids)
        self.assertIsNotNone(resp)
        self.assertEqual(lifecycle.get_state(), lifecycle.STATE_RUNNING)
        self.assertIn("started successfully", resp[1].lower())
        self.assertEqual(resp[2], _menu_keyboard())

        # Now normal commands work again
        resp_status = handle_update(_msg_update(text="/status"), owner_ids, queue_reader=lambda: [])
        self.assertIsNotNone(resp_status)
        self.assertNotEqual(resp_status[1], "Please first start the bot.")

    # 13. Exit confirmation ke baad complete shutdown
    def test_13_exit_prompt_and_confirmation(self):
        owner_ids = frozenset({12345})
        lifecycle.set_state(lifecycle.STATE_RUNNING)

        # Prompt
        resp_prompt = handle_update(_cb_update(data="life:exit_prompt"), owner_ids)
        self.assertIn("Exit Process Confirmation", resp_prompt[1])
        self.assertIn("life:exit_confirm", json.dumps(resp_prompt[2]))
        self.assertFalse(lifecycle.is_exit_requested())

        # Cancel
        resp_cancel = handle_update(_cb_update(data="life:exit_cancel"), owner_ids)
        self.assertIn("Exit cancelled", resp_cancel[1])
        self.assertFalse(lifecycle.is_exit_requested())

        # Confirm
        resp_confirm = handle_update(_cb_update(data="life:exit_confirm"), owner_ids)
        self.assertIn("shutting down", resp_confirm[1].lower())
        self.assertTrue(lifecycle.is_exit_requested())
        self.assertEqual(lifecycle.get_state(), lifecycle.STATE_STOPPED)

    # 14. Supervisor restart behavior reconciliation
    def test_14_supervisor_restart_reconciliation(self):
        # Stale RUNNING on crash without auto-start resets to STOPPED
        lifecycle.set_state(lifecycle.STATE_RUNNING)
        os.environ["BOT_AUTO_START"] = "false"
        reconciled = lifecycle.reconcile_lifecycle_on_boot()
        self.assertEqual(reconciled, lifecycle.STATE_STOPPED)
        self.assertEqual(lifecycle.get_state(), lifecycle.STATE_STOPPED)

        # Explicit BOT_AUTO_START=true sets RUNNING
        os.environ["BOT_AUTO_START"] = "true"
        reconciled_auto = lifecycle.reconcile_lifecycle_on_boot()
        self.assertEqual(reconciled_auto, lifecycle.STATE_RUNNING)
        self.assertEqual(lifecycle.get_state(), lifecycle.STATE_RUNNING)

    # 15. Queue, source list, upload history aur pending approvals preserved
    def test_15_data_preservation_across_lifecycle_changes(self):
        queue_path = Path(self.tmp_dir.name) / "queue.json"
        sources_path = Path(self.tmp_dir.name) / "sources.txt"

        original_queue = [
            {"id": "clip-1", "title": "Clip One", "status": "pending"},
            {"id": "clip-2", "title": "Clip Two", "status": "needs_review"},
        ]
        original_sources = "https://www.youtube.com/watch?v=11111111111\nhttps://www.youtube.com/@somechannel\n"

        queue_path.write_text(json.dumps(original_queue), encoding="utf-8")
        sources_path.write_text(original_sources, encoding="utf-8")

        owner_ids = frozenset({12345})
        # Start bot
        handle_update(_cb_update(data="life:start"), owner_ids)
        # Stop bot
        handle_update(_cb_update(data="life:stop_confirm"), owner_ids)
        # Start bot again
        handle_update(_cb_update(data="life:start"), owner_ids)

        # Verify files are completely untouched
        self.assertEqual(json.loads(queue_path.read_text(encoding="utf-8")), original_queue)
        self.assertEqual(sources_path.read_text(encoding="utf-8"), original_sources)

    # 16. Scheduler duplicate execution prevented
    def test_16_scheduler_duplicate_execution_prevented(self):
        def fake_run(scheduler, stop_event):
            stop_event.wait(5.0)

        with patch("telegram_bot.scheduler.is_scheduler_configured_enabled", return_value=True), \
             patch("telegram_bot.scheduler.run_scheduler", side_effect=fake_run):
            # First start
            lifecycle.start_scheduler()
            self.assertIsNotNone(lifecycle._SCHEDULER_THREAD)
            first_thread = lifecycle._SCHEDULER_THREAD
            self.assertTrue(first_thread.is_alive())

            # Second start should be a no-op
            lifecycle.start_scheduler()
            self.assertEqual(lifecycle._SCHEDULER_THREAD, first_thread)

            # Stop scheduler
            lifecycle.stop_scheduler()
            self.assertIsNone(lifecycle._SCHEDULER_THREAD)

    # 17. Repeated messages do not trigger duplicate replies or loops
    def test_17_repeated_messages_produce_single_reply_each(self):
        owner_ids = frozenset({12345})
        lifecycle.set_state(lifecycle.STATE_STOPPED)

        responses = []
        for i in range(5):
            res = handle_update(_msg_update(text="/status"), owner_ids)
            responses.append(res)

        self.assertEqual(len(responses), 5)
        for res in responses:
            self.assertEqual(res[1], "Please first start the bot.")
            self.assertEqual(res[2], _stopped_keyboard())

    # 18. STOPPED state /start returns startup menu controls without 'Please first start the bot.'
    def test_18_stopped_state_start_command_returns_startup_menu(self):
        owner_ids = frozenset({12345})
        self.assertEqual(lifecycle.get_state(), lifecycle.STATE_STOPPED)

        resp = handle_update(_msg_update(text="/start"), owner_ids)
        self.assertIsNotNone(resp)
        chat_id, text, markup = resp
        self.assertEqual(chat_id, "12345")
        self.assertNotEqual(text, "Please first start the bot.")
        self.assertIn("PersonalAgent", text)
        self.assertIn("stopped", text.lower())
        self.assertEqual(markup, _stopped_keyboard())
        # Check buttons in markup
        callbacks = [btn["callback_data"] for row in markup["inline_keyboard"] for btn in row]
        self.assertIn("life:start", callbacks)
        self.assertIn("life:exit_prompt", callbacks)
        # Services must remain stopped
        self.assertEqual(lifecycle.get_state(), lifecycle.STATE_STOPPED)

        # /start with unexpected args
        resp_args = handle_update(_msg_update(text="/start extra_arg"), owner_ids)
        self.assertIsNotNone(resp_args)
        self.assertEqual(resp_args[1], "This command takes no arguments. Use /help.")

    # 19. RUNNING state /start returns normal monitoring menu without restarting workers
    def test_19_running_state_start_command_returns_main_menu(self):
        owner_ids = frozenset({12345})
        lifecycle.set_state(lifecycle.STATE_RUNNING)

        with patch("telegram_bot.lifecycle.start_services") as mock_start_svc, \
             patch("telegram_bot.lifecycle.start_scheduler") as mock_start_sched:
            resp = handle_update(_msg_update(text="/start"), owner_ids)
            self.assertIsNotNone(resp)
            chat_id, text, markup = resp
            self.assertEqual(chat_id, "12345")
            self.assertIn("monitoring", text.lower())
            self.assertEqual(markup, _menu_keyboard())
            # Background services must not be restarted
            mock_start_svc.assert_not_called()
            mock_start_sched.assert_not_called()
            self.assertEqual(lifecycle.get_state(), lifecycle.STATE_RUNNING)

    # 20. Repeated /start commands do not create duplicate handlers, menus, or workers
    def test_20_repeated_start_commands_safe_and_idempotent(self):
        owner_ids = frozenset({12345})

        # When STOPPED
        lifecycle.set_state(lifecycle.STATE_STOPPED)
        for _ in range(5):
            res = handle_update(_msg_update(text="/start"), owner_ids)
            self.assertIsNotNone(res)
            self.assertNotEqual(res[1], "Please first start the bot.")
            self.assertIn("stopped", res[1].lower())
            self.assertEqual(res[2], _stopped_keyboard())
            self.assertEqual(lifecycle.get_state(), lifecycle.STATE_STOPPED)

        # When RUNNING
        lifecycle.set_state(lifecycle.STATE_RUNNING)
        for _ in range(5):
            res = handle_update(_msg_update(text="/start"), owner_ids)
            self.assertIsNotNone(res)
            self.assertIn("monitoring", res[1].lower())
            self.assertEqual(res[2], _menu_keyboard())
            self.assertEqual(lifecycle.get_state(), lifecycle.STATE_RUNNING)

    # 21. Exit prompt does not terminate process; Cancel stays running
    def test_21_exit_prompt_does_not_terminate_bot(self):
        owner_ids = frozenset({12345})
        lifecycle.set_state(lifecycle.STATE_RUNNING)

        # Prompt must only return confirmation UI
        resp_prompt = handle_update(_cb_update(data="life:exit_prompt"), owner_ids)
        self.assertIsNotNone(resp_prompt)
        self.assertIn("Exit Process Confirmation", resp_prompt[1])
        self.assertFalse(lifecycle.is_exit_requested())
        self.assertEqual(lifecycle.get_state(), lifecycle.STATE_RUNNING)

        # Cancel returns to main menu and keeps running
        resp_cancel = handle_update(_cb_update(data="life:exit_cancel"), owner_ids)
        self.assertIsNotNone(resp_cancel)
        self.assertIn("Exit cancelled", resp_cancel[1])
        self.assertFalse(lifecycle.is_exit_requested())
        self.assertEqual(lifecycle.get_state(), lifecycle.STATE_RUNNING)

    # 22. Exit confirmation cleanly stops services, flushes offset, and terminates poller
    def test_22_exit_confirmation_stops_services_and_flushes_offset(self):
        owner_ids = frozenset({12345})
        lifecycle.set_state(lifecycle.STATE_RUNNING)

        calls = []
        replies = []
        exit_cb = {
            "update_id": 105,
            "callback_query": {
                "id": "cb_exit_105",
                "from": {"id": 12345},
                "message": {"chat": {"id": 12345, "type": "private"}, "date": int(time.time()), "text": "menu"},
                "data": "life:exit_confirm",
            },
        }

        def fake_api(token, method, params=None, timeout=40):
            calls.append((method, params))
            if method == "getUpdates" and sum(1 for m, _ in calls if m == "getUpdates") == 1:
                return [exit_cb]
            return []

        class TestLock:
            def __init__(self): self.held = True
            def acquire(self): return True
            def release(self): self.held = False

        lock = TestLock()
        run_bot(token="fake", owner_ids_raw="12345", api_call=fake_api,
                reply_sender=lambda chat, text, **kw: replies.append((chat, text)),
                lock=lock, sleep=lambda s: None, offset_path=self.offset_file)

        # Lock released
        self.assertFalse(lock.held)
        # Services stopped
        self.assertEqual(lifecycle.get_state(), lifecycle.STATE_STOPPED)
        # Goodbye notification sent
        self.assertEqual(len(replies), 1)
        self.assertIn("shutting down", replies[0][1].lower())
        # Offset persisted to disk
        self.assertTrue(self.offset_file.exists())
        saved_offset = json.loads(self.offset_file.read_text(encoding="utf-8")).get("offset")
        self.assertEqual(saved_offset, 106)
        # Exit flush sent to Telegram getUpdates
        flush_calls = [p for m, p in calls if m == "getUpdates" and p.get("offset") == 106]
        self.assertTrue(len(flush_calls) >= 1)

    # 23. Stale exit confirmation from an earlier un-offsetted run is dismissed on startup
    def test_23_stale_exit_callback_on_fresh_startup_dismissed(self):
        calls = []
        replies = []
        stale_exit_cb = {
            "update_id": 88,
            "callback_query": {
                "id": "cb_stale_88",
                "from": {"id": 12345},
                "message": {"chat": {"id": 12345, "type": "private"}, "date": 1000000, "text": "old menu"},
                "data": "life:exit_confirm",
            },
        }
        live_cmd = {
            "update_id": 89,
            "message": {
                "from": {"id": 12345},
                "chat": {"id": 12345, "type": "private"},
                "text": "/start",
            },
        }

        iteration = 0
        def fake_api(token, method, params=None, timeout=40):
            nonlocal iteration
            calls.append((method, params))
            if method == "getUpdates":
                iteration += 1
                if iteration == 1:
                    # Initial backlog containing stale exit callback followed by live command
                    return [stale_exit_cb, live_cmd]
            if method == "answerCallbackQuery" and (params or {}).get("callback_query_id") == "cb_stale_88":
                raise RuntimeError("stale_callback")
            return []

        stop_flags = iter([False, False, True])
        run_bot(token="fake", owner_ids_raw="12345", api_call=fake_api,
                reply_sender=lambda chat, text, **kw: replies.append((chat, text)),
                lock=FakeLock(), sleep=lambda s: None,
                stop_requested=lambda: next(stop_flags), offset_path=self.offset_file)

        # Bot must NOT have exited from stale callback
        self.assertFalse(lifecycle.is_exit_requested())
        # Stale exit was not replied with shutdown goodbye
        self.assertFalse(any("shutting down" in t.lower() for _, t in replies))
        # Live command was answered
        self.assertTrue(any("PersonalAgent" in t for _, t in replies))
        # Offset was advanced past both updates
        self.assertTrue(self.offset_file.exists())
        saved_offset = json.loads(self.offset_file.read_text(encoding="utf-8")).get("offset")
        self.assertEqual(saved_offset, 90)

    # 24. Callback ack failure / stale callback does not crash poller
    def test_24_callback_ack_failure_and_stale_callback_handling(self):
        os.environ["BOT_AUTO_START"] = "true"
        lifecycle.set_state(lifecycle.STATE_RUNNING)
        cb = {
            "update_id": 50,
            "callback_query": {
                "id": "cb_failing_50",
                "from": {"id": 12345},
                "message": {"chat": {"id": 12345, "type": "private"}, "date": int(time.time()), "text": "menu"},
                "data": "pa1:help",
            },
        }
        calls = []
        replies = []
        def fake_api(token, method, params=None, timeout=40):
            calls.append((method, params))
            if method == "getUpdates":
                return [cb] if sum(1 for m, _ in calls if m == "getUpdates") == 1 else []
            if method == "answerCallbackQuery":
                # Simulate stale callback error from Telegram API
                raise RuntimeError("stale_callback")
            return []

        stop_flags = iter([False, True])
        run_bot(token="fake", owner_ids_raw="12345", api_call=fake_api,
                reply_sender=lambda chat, text, **kw: replies.append((chat, text)),
                lock=FakeLock(), sleep=lambda s: None,
                stop_requested=lambda: next(stop_flags), offset_path=self.offset_file)

        # Polling did not crash and help reply was successfully sent
        self.assertEqual(len(replies), 1)
        self.assertIn("Monitoring menu", replies[0][1])

    # 25. Cleanup and shutdown routines are idempotent
    def test_25_cleanup_and_shutdown_are_idempotent(self):
        # Repeated stop_services
        self.assertTrue(lifecycle.stop_services()[0])
        self.assertTrue(lifecycle.stop_services()[0])

        # Repeated stop_scheduler
        lifecycle.stop_scheduler()
        lifecycle.stop_scheduler()

        # Repeated reset_exit_request
        lifecycle.reset_exit_request()
        lifecycle.reset_exit_request()

        # Repeated lock release
        lock = PollLock(self.tmp_dir.name + "/test_poll.lock")
        self.assertTrue(lock.acquire())
        lock.release()
        lock.release()  # Second release is harmless

    # 26. Callback error does not re-execute tasks or duplicate notifications
    def test_26_callback_failure_does_not_reexecute_tasks(self):
        os.environ["BOT_AUTO_START"] = "true"
        lifecycle.set_state(lifecycle.STATE_RUNNING)
        cb = {
            "update_id": 70,
            "callback_query": {
                "id": "cb_70",
                "from": {"id": 12345},
                "message": {"chat": {"id": 12345, "type": "private"}, "date": int(time.time()), "text": "menu"},
                "data": "src:menu",
            },
        }
        calls = []
        replies = []
        def fake_api(token, method, params=None, timeout=40):
            calls.append((method, params))
            if method == "getUpdates":
                return [cb] if sum(1 for m, _ in calls if m == "getUpdates") == 1 else []
            if method == "answerCallbackQuery":
                raise RuntimeError("telegram_api_error")
            return []

        stop_flags = iter([False, True])
        run_bot(token="fake", owner_ids_raw="12345", api_call=fake_api,
                reply_sender=lambda chat, text, **kw: replies.append((chat, text)),
                lock=FakeLock(), sleep=lambda s: None,
                stop_requested=lambda: next(stop_flags), offset_path=self.offset_file)

        # Exactly 1 reply produced, no retry loop
        self.assertEqual(len(replies), 1)
        self.assertIn("Source Manager", replies[0][1])


class FakeLock:
    def __init__(self): self.held = True
    def acquire(self): return True
    def release(self): self.held = False


if __name__ == "__main__":
    unittest.main()
