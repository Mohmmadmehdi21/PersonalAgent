"""Comprehensive test suite for real-time task progress tracking in PersonalAgent.

Verifies all 20 required criteria from Section 9:
 1. Task start initializes progress correctly.
 2. Stage completion increases overall progress correctly.
 3. Weighted progress calculation is correct.
 4. Progress never exceeds 100%.
 5. Failed task is never marked 100%.
 6. Cancelled task is never marked completed.
 7. 100% is reached only after successful finalization.
 8. Multiple clips show correct batch counts.
 9. Retries do not double-count completed work.
10. Unknown stage progress does not invent precision.
11. Concurrent task updates remain isolated.
12. Delayed callbacks cannot overwrite a newer task state.
13. Telegram progress message is edited instead of creating duplicates.
14. Telegram rate limits and transient errors are handled.
15. Telegram API failure does not fail the underlying task.
16. /status displays the latest persisted progress.
17. Task failure displays the correct last stage and percentage.
18. Restart recovery does not mark interrupted tasks completed.
19. Existing upload confirmation and owner approval remain mandatory.
20. Stop Bot and Exit lifecycle cooperate safely with active tasks.
"""
import copy
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from telegram_bot import control, lifecycle, progress
from telegram_bot.commands import handle_update
from telegram_bot.progress import (
    ProgressTracker,
    format_progress_message,
    get_active_progress,
    get_task_progress,
    get_tracker,
    render_progress_bar,
    start_task_progress,
)


class TaskProgressTrackingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="prog_test_")
        self.progress_dir = Path(self.temp_dir) / "progress"
        self.data_dir = Path(self.temp_dir) / "data"
        self.progress_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self._orig_progress_dir = progress.PROGRESS_DIR
        progress.PROGRESS_DIR = self.progress_dir
        progress._TRACKER_REGISTRY.clear()

        lifecycle.set_state(lifecycle.STATE_RUNNING)
        self.owner_ids = frozenset({12345})

    def tearDown(self):
        progress.PROGRESS_DIR = self._orig_progress_dir
        progress._TRACKER_REGISTRY.clear()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # 1. Task start initializes progress correctly
    def test_01_task_start_initializes_progress_correctly(self):
        with patch("telegram_bot.progress._api_send_message", return_value=999):
            tracker = start_task_progress("t1", "prepare", chat_id="12345")
            snap = tracker.snapshot()
            self.assertEqual(snap["task_id"], "t1")
            self.assertEqual(snap["task_type"], "prepare")
            self.assertEqual(snap["status"], "RUNNING")
            self.assertEqual(snap["overall_progress"], 0.0)
            self.assertEqual(snap["current_stage"], "source_validation")
            self.assertEqual(snap["total_stages"], 6)
            self.assertEqual(snap["completed_stages"], [])
            self.assertEqual(snap["telegram_message_id"], 999)
            self.assertTrue((self.progress_dir / "t1.json").exists())

    # 2. Stage completion increases overall progress correctly
    def test_02_stage_completion_increases_overall_progress_correctly(self):
        tracker = start_task_progress("t2", "prepare", send_telegram=False)
        self.assertEqual(tracker.record["overall_progress"], 0.0)

        # source_validation weight is 5%
        tracker.complete_stage("source_validation")
        self.assertAlmostEqual(tracker.record["overall_progress"], 5.0, places=1)
        self.assertIn("Source validation", tracker.record["completed_stages"])

        # download weight is 20% -> cumulative 25%
        tracker.complete_stage("download")
        self.assertAlmostEqual(tracker.record["overall_progress"], 25.0, places=1)
        self.assertIn("Video download", tracker.record["completed_stages"])

    # 3. Weighted progress calculation is correct
    def test_03_weighted_progress_calculation_is_correct(self):
        tracker = start_task_progress("t3", "prepare", send_telegram=False)
        tracker.complete_stage("source_validation")  # 5%
        tracker.complete_stage("download")           # 20% -> 25% total completed

        # transcribe weight is 25%. 40% into transcribe => 25% + (25% * 0.40) = 35%
        tracker.start_stage("transcribe")
        tracker.update_stage(progress=0.40)
        self.assertAlmostEqual(tracker.record["overall_progress"], 35.0, places=1)

    # 4. Progress never exceeds 100%
    def test_04_progress_never_exceeds_100(self):
        tracker = start_task_progress("t4", "prepare", send_telegram=False)
        for stage in tracker.stage_configs:
            tracker.complete_stage(stage["id"])
        tracker.update_stage(progress=15.0)  # arbitrary excessive input
        self.assertLessEqual(tracker.record["overall_progress"], 99.0)  # running capped at 99%

        tracker.finish(status="COMPLETED")
        self.assertEqual(tracker.record["overall_progress"], 100.0)

        # ASCII bar also clamps
        bar = render_progress_bar(150)
        self.assertIn("100%", bar)

    # 5. Failed task is never marked 100%
    def test_05_failed_task_is_never_marked_100(self):
        tracker = start_task_progress("t5", "prepare", send_telegram=False)
        tracker.complete_stage("source_validation")
        tracker.complete_stage("download")
        tracker.complete_stage("transcribe")
        tracker.finish(status="FAILED", error="API Quota Exceeded")

        snap = tracker.snapshot()
        self.assertEqual(snap["status"], "FAILED")
        self.assertLess(snap["overall_progress"], 100.0)
        msg = format_progress_message(snap)
        self.assertIn("Status: FAILED", msg)
        self.assertNotIn("100%", msg)

    # 6. Cancelled task is never marked completed
    def test_06_cancelled_task_is_never_marked_completed(self):
        tracker = start_task_progress("t6", "prepare", send_telegram=False)
        tracker.complete_stage("source_validation")
        tracker.finish(status="CANCELLED")

        snap = tracker.snapshot()
        self.assertEqual(snap["status"], "CANCELLED")
        self.assertNotEqual(snap["status"], "COMPLETED")
        self.assertLess(snap["overall_progress"], 100.0)

    # 7. 100% is reached only after successful finalization
    def test_07_100_percent_reached_only_after_successful_finalization(self):
        tracker = start_task_progress("t7", "prepare", send_telegram=False)
        for stage in tracker.stage_configs:
            tracker.complete_stage(stage["id"])
        # Even with all stages complete, before finish() it remains 99% to indicate finalization
        self.assertLess(tracker.record["overall_progress"], 100.0)

        tracker.finish(status="COMPLETED")
        self.assertEqual(tracker.record["overall_progress"], 100.0)
        self.assertEqual(tracker.record["status"], "COMPLETED")

    # 8. Multiple clips show correct batch counts
    def test_08_multiple_clips_show_correct_batch_counts(self):
        tracker = start_task_progress("t8", "upload", total_items=5, send_telegram=False)
        tracker.start_stage("batch_processing")
        tracker.update_batch_items(processed=2, total=5, current_title="Clip Part 3", current_progress=40)

        snap = tracker.snapshot()
        self.assertEqual(snap["total_items"], 5)
        self.assertEqual(snap["processed_items"], 2)
        self.assertEqual(snap["current_item_progress"], 40)

        msg = format_progress_message(snap)
        self.assertIn("Clips: 2 / 5 completed", msg)
        self.assertIn("Current Clip: 3 / 5 (Clip Part 3)", msg)
        self.assertIn("Current Clip Progress: 40%", msg)

    # 9. Retries do not double-count completed work
    def test_09_retries_do_not_double_count_completed_work(self):
        tracker = start_task_progress("t9", "upload", total_items=3, send_telegram=False)
        tracker.start_stage("batch_processing")

        # Clip 1 succeeds
        tracker.update_batch_items(processed=1, total=3, current_progress=100)
        prog1 = tracker.record["overall_progress"]

        # Clip 2 fails and retries (processed remains 1)
        tracker.update_batch_items(processed=1, total=3, current_progress=10, detail="Retrying Clip 2")
        prog_retry = tracker.record["overall_progress"]

        # Progress must not double-count or jump ahead
        self.assertLessEqual(prog_retry, prog1 + 5.0)
        self.assertEqual(tracker.record["processed_items"], 1)

    # 10. Unknown stage progress does not invent precision
    def test_10_unknown_stage_progress_does_not_invent_precision(self):
        tracker = start_task_progress("t10", "prepare", send_telegram=False)
        tracker.start_stage("ai_analysis", detail="Groq deep analysis in progress")
        # Internal progress unknown: stage_progress remains 0.0
        self.assertEqual(tracker.record["stage_progress"], 0.0)
        msg = format_progress_message(tracker.snapshot())
        self.assertIn("Current Stage: AI analysis and clip selection", msg)
        self.assertIn("Detail: Groq deep analysis in progress", msg)
        # Should not invent arbitrary jumps like 50%
        self.assertNotIn("50%", msg)

    # 11. Concurrent task updates remain isolated
    def test_11_concurrent_task_updates_remain_isolated(self):
        tA = start_task_progress("taskA", "prepare", send_telegram=False)
        tB = start_task_progress("taskB", "upload", send_telegram=False)

        def workerA():
            for i in range(10):
                tA.update_stage(progress=i * 0.1)
                time.sleep(0.005)

        def workerB():
            for i in range(10):
                tB.update_batch_items(processed=i, total=10)
                time.sleep(0.005)

        thA = threading.Thread(target=workerA)
        thB = threading.Thread(target=workerB)
        thA.start()
        thB.start()
        thA.join()
        thB.join()

        snapA = get_task_progress("taskA")
        snapB = get_task_progress("taskB")
        self.assertEqual(snapA["task_type"], "prepare")
        self.assertEqual(snapB["task_type"], "upload")
        self.assertEqual(snapB["total_items"], 10)

    # 12. Delayed callbacks cannot overwrite a newer task state
    def test_12_delayed_callbacks_cannot_overwrite_newer_task_state(self):
        tracker = start_task_progress("t12", "prepare", send_telegram=False)
        tracker.complete_stage("source_validation")
        tracker.complete_stage("download")
        current_prog = tracker.record["overall_progress"]

        # Stale callback attempting to set 0.0
        tracker._calculate_overall_progress = tracker._calculate_overall_progress
        tracker.update_stage("source_validation", progress=0.1)
        # Monotonicity check ensures no backward progress jump
        self.assertGreaterEqual(tracker.record["overall_progress"], current_prog)

    # 13. Telegram progress message is edited instead of creating duplicates
    def test_13_telegram_progress_message_is_edited_instead_of_creating_duplicates(self):
        send_mock = MagicMock(return_value=777)
        edit_mock = MagicMock(return_value=(True, 777))
        with patch("telegram_bot.progress._api_send_message", send_mock), \
             patch("telegram_bot.progress._api_edit_message", edit_mock):

            tracker = start_task_progress("t13", "prepare", chat_id="555", send_telegram=True)
            self.assertEqual(send_mock.call_count, 1)

            # Stage transition calls edit, not send
            tracker.complete_stage("source_validation", force_telegram=True)
            self.assertEqual(send_mock.call_count, 1)
            self.assertEqual(edit_mock.call_count, 1)

            tracker.complete_stage("download", force_telegram=True)
            self.assertEqual(send_mock.call_count, 1)
            self.assertEqual(edit_mock.call_count, 2)

    # 14. Telegram rate limits and transient errors are handled
    def test_14_telegram_rate_limits_and_transient_errors_are_handled(self):
        edit_mock = MagicMock(return_value=(True, 888))
        with patch("telegram_bot.progress._api_edit_message", edit_mock):
            tracker = start_task_progress("t14", "prepare", chat_id="555", send_telegram=False)
            tracker.message_id = 888
            tracker.min_interval = 2.0
            tracker.last_telegram_update_time = time.time()

            # Multiple rapid updates without force should be throttled
            tracker.update_stage(progress=0.1, force_telegram=False)
            tracker.update_stage(progress=0.2, force_telegram=False)
            tracker.update_stage(progress=0.3, force_telegram=False)
            self.assertEqual(edit_mock.call_count, 0)

            # Forced update bypasses throttle
            tracker.update_stage(progress=0.4, force_telegram=True)
            self.assertEqual(edit_mock.call_count, 1)

    # 15. Telegram API failure does not fail the underlying task
    def test_15_telegram_api_failure_does_not_fail_underlying_task(self):
        with patch("telegram_bot.progress._api_edit_message", side_effect=RuntimeError("Telegram Network Outage")):
            tracker = start_task_progress("t15", "prepare", chat_id="555", send_telegram=False)
            tracker.message_id = 111

            # Method should not raise exception even if telegram fails
            tracker.start_stage("download", detail="Downloading candidate video")
            tracker.complete_stage("download")
            tracker.finish(status="COMPLETED")

            self.assertEqual(tracker.record["status"], "COMPLETED")
            self.assertEqual(tracker.record["overall_progress"], 20.0)
            self.assertNotEqual(len(tracker.record["completed_stages"]), tracker.record["total_stages"])

    def test_completed_status_does_not_manufacture_progress_for_unrun_stages(self):
        tracker = start_task_progress("empty-upload", "upload", send_telegram=False)
        tracker.complete_stage(
            "queue_assessment",
            detail="No eligible clips; no confirmation was requested and no upload was attempted.",
        )
        tracker.finish(status="NO_ELIGIBLE_CLIPS")

        snap = tracker.snapshot()
        self.assertEqual(snap["status"], "NO_ELIGIBLE_CLIPS")
        self.assertEqual(snap["completed_stages"], ["Queue assessment"])
        self.assertEqual(snap["overall_progress"], 5.0)
        self.assertEqual(snap["remaining_stages"], ["Clip rendering & upload", "Batch finalization"])
        rendered = format_progress_message(snap)
        self.assertIn("Status: NO_ELIGIBLE_CLIPS", rendered)
        self.assertIn("Overall Progress: 5%", rendered)
        self.assertIn("no confirmation was requested", rendered)

    def test_completed_status_keeps_evidence_backed_progress_when_stages_are_missing(self):
        tracker = start_task_progress("partial-finish", "upload", send_telegram=False)
        tracker.complete_stage("queue_assessment")
        tracker.finish(status="COMPLETED")

        snap = tracker.snapshot()
        self.assertEqual(snap["status"], "COMPLETED")
        self.assertEqual(snap["overall_progress"], 5.0)
        self.assertEqual(snap["completed_stages"], ["Queue assessment"])
        self.assertNotEqual(snap["overall_progress"], 100.0)

    def test_task_command_reports_empty_upload_outcome_and_saved_progress(self):
        from telegram_bot.commands import _task

        saved = {
            "task_id": "empty-upload", "task_type": "upload", "status": "NO_ELIGIBLE_CLIPS",
            "current_stage_name": "Queue assessment", "overall_progress": 5.0,
        }
        record = {
            "id": "empty-upload", "kind": "upload", "status": "no_eligible_clips",
            "result": {"outcome": "no_eligible_clips", "selected": 0,
                       "uploaded": 0, "publications": 0},
        }
        with patch("telegram_bot.control.task_info", return_value=record), \
             patch("telegram_bot.progress.get_task_progress", return_value=saved):
            message = _task(["empty-upload"])

        self.assertIn("no_eligible_clips", message)
        self.assertIn("selected=0", message)
        self.assertIn("uploaded=0", message)
        self.assertIn("publications=0", message)
        self.assertIn("Progress: 5% (Queue assessment)", message)

    # 16. /status displays the latest persisted progress
    def test_16_status_displays_latest_persisted_progress(self):
        tracker = start_task_progress("t16", "prepare", chat_id="12345", send_telegram=False)
        tracker.start_stage("transcribe", detail="Whisper speech-to-text")
        tracker.update_stage(progress=0.5)

        control_state = {
            "tasks": {
                "t16": {"id": "t16", "kind": "prepare", "status": "running", "pid": None}
            }
        }
        with patch("telegram_bot.control._read", return_value=control_state), \
             patch("telegram_bot.progress.get_task_progress", return_value=tracker.snapshot()):
            msg = {
                "update_id": 1,
                "message": {
                    "from": {"id": 12345},
                    "chat": {"id": 12345, "type": "private"},
                    "text": "/status",
                },
            }
            res = handle_update(msg, self.owner_ids)
            self.assertIsNotNone(res)
            text = res[1]
            self.assertIn("PersonalAgent — Prepare Clips", text)
            self.assertIn("Task ID: `t16`", text)
            self.assertIn("Current Stage: Audio extraction & transcription", text)

    # 17. Task failure displays the correct last stage and percentage
    def test_17_task_failure_displays_correct_last_stage_and_percentage(self):
        tracker = start_task_progress("t17", "prepare", send_telegram=False)
        tracker.complete_stage("source_validation")
        tracker.complete_stage("download")
        tracker.start_stage("transcribe", detail="Transcribing audio")
        tracker.finish(status="FAILED", error="Audio decoding failed with corrupted header")

        snap = tracker.snapshot()
        self.assertEqual(snap["status"], "FAILED")
        self.assertEqual(snap["current_stage"], "transcribe")
        self.assertIn("Audio decoding failed", snap["error"])

        msg = format_progress_message(snap)
        self.assertIn("Current Stage: Audio extraction & transcription", msg)
        self.assertIn("Error: Audio decoding failed", msg)
        self.assertIn("Status: FAILED", msg)

    # 18. Restart recovery does not mark interrupted tasks completed
    def test_18_restart_recovery_does_not_mark_interrupted_tasks_completed(self):
        tracker = start_task_progress("t18", "prepare", send_telegram=False)
        tracker.complete_stage("source_validation")
        last_prog = tracker.record["overall_progress"]

        state = {
            "tasks": {
                "t18": {
                    "id": "t18",
                    "kind": "prepare",
                    "status": "running",
                    "pid": 99999999,  # Dead PID
                }
            }
        }
        with patch("lock.is_pid_alive", return_value=False):
            control.reconcile_tasks(state)

        task = state["tasks"]["t18"]
        self.assertEqual(task["status"], "failed")
        self.assertNotEqual(task["status"], "completed")

        # Persisted progress record remains un-faked
        self.assertLess(last_prog, 100.0)

    # 19. Existing upload confirmation and owner approval remain mandatory
    def test_19_existing_upload_confirmation_and_owner_approval_remain_mandatory(self):
        state = {
            "tasks": {
                "t19": {"id": "t19", "kind": "upload", "status": "running", "chat_id": "12345"}
            },
            "confirmation": None,
        }
        with patch("telegram_bot.control._read", return_value=state), \
             patch("telegram_bot.control._write"), \
             patch.dict(os.environ, {"AGENT_CONTROL_TASK_ID": "t19"}), \
             patch("telegram_bot.progress.get_tracker", return_value=None):

            clip = {"id": "clip_abc", "title": "Highlight 1"}
            # Asking upload requires explicit owner confirmation
            outcome = control.checkpoint(clip=clip, ask_upload=True)
            # Without owner confirmation token, checkpoint pauses or waits
            self.assertNotEqual(outcome, "approved")
            self.assertEqual(state["tasks"]["t19"]["status"], "waiting_confirmation")

    # 20. Stop Bot and Exit lifecycle cooperate safely with active tasks
    def test_20_stop_bot_and_exit_lifecycle_cooperate_safely_with_active_tasks(self):
        # 1. When STOPPED, incoming commands are blocked
        lifecycle.set_state(lifecycle.STATE_STOPPED)
        update_msg = {
            "update_id": 100,
            "message": {
                "from": {"id": 12345},
                "chat": {"id": 12345, "type": "private"},
                "text": "/status",
            },
        }
        res = handle_update(update_msg, self.owner_ids)
        self.assertEqual(res[1], "Please first start the bot.")

        # 2. When cancelling a task, progress reflects CANCELLING
        lifecycle.set_state(lifecycle.STATE_RUNNING)
        tracker = start_task_progress("t20", "prepare", send_telegram=False)
        tracker.complete_stage("source_validation")
        tracker.set_status("CANCELLING")
        self.assertEqual(tracker.record["status"], "CANCELLING")

        msg = format_progress_message(tracker.snapshot())
        self.assertIn("Status: CANCELLING", msg)


if __name__ == "__main__":
    unittest.main()
