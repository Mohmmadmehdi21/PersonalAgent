import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch

import retry
from retry import AccountUploadLimitError, UploadOutcomeUnknownError, UploadCommittedInterruptedError


def workspace_tempdir():
    return tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "tests")


@contextmanager
def approved_control_task():
    from upload_auth import issue_upload_approval

    def checkpoint(*, clip=None, ask_upload=False):
        return issue_upload_approval(clip["id"], "123456789") if ask_upload else "continue"

    with patch.dict(os.environ, {"AGENT_CONTROL_TASK_ID": "test-task"}), \
         patch("telegram_bot.control.checkpoint", side_effect=checkpoint), \
         patch("telegram_bot.control.associate_clips"):
        yield


class StructuredYouTubeError(Exception):
    def __init__(self, reason, status=400, domain="youtube.video", message="Account upload limit reached"):
        super().__init__(f"HTTP {status}")
        self.error_details = [{"domain": domain, "reason": reason, "message": message}]
        self.resp = types.SimpleNamespace(status=status)
        self.content = json.dumps({"error": {"status": "LIMIT_EXCEEDED", "message": message,
                                               "errors": [{"domain": domain, "reason": reason,
                                                           "message": message}]}}).encode()


def load_main_with_stubs():
    """Load main.py with offline stubs so no external API/dependency is used."""
    modules = {}

    def module(name, **attrs):
        value = types.ModuleType(name)
        for key, item in attrs.items():
            setattr(value, key, item)
        modules[name] = value
        return value

    module("config", WORK="work", CLIPS_PER_DAY=2, DRY_RUN=False, REVIEW_DIR="review",
           HOOK_MIN_SCORE=5, OWN_CONTENT_ENABLED=False, OWN_RATIO=1, CLEANUP=False,
           validate_startup_config=lambda *a: [], ConfigurationError=ValueError)
    module("download", download=lambda *a, **k: None)
    module("transcribe", transcribe=lambda *a, **k: None)
    module("find_clips", find_clips=lambda *a, **k: [])
    module("clip", make_clip=lambda *a, **k: None, write_srt=lambda *a, **k: None)
    module("thumb", make_thumb=lambda *a, **k: None)
    module("upload", get_service=lambda: object(), upload=lambda *a, **k: None)
    module("commentary", apply_commentary=lambda *a, **k: None)
    module("own_content", get_pending_own_video=lambda: None, archive_own_video=lambda *a: None)
    module("source", find_popular_video=lambda *a: None, is_channel_url=lambda *a: False,
           video_id_from_url=lambda *a: None)
    module("lock", acquire_lock=lambda: None, release_lock=lambda: None)
    module("state_manager", load_state=lambda: {}, save_state_atomic=lambda state: None,
           init_video_state=lambda *a, **k: None, update_step_status=lambda *a, **k: None,
           init_clip_state=lambda *a, **k: None, is_clip_already_uploaded=lambda *a: False,
           is_valid_json_file=lambda *a: False, is_valid_media_file=lambda *a: False,
           are_all_clips_complete=lambda *a: False,
           update_clip_step=lambda state, video, key, step, status, **kw: _update_state(state, video, key, step, status, kw))
    module("notify", notify_run_success=lambda *a, **k: None,
           notify_run_error=lambda *a, **k: None, notify_quota_or_auth=lambda *a, **k: None,
           notify_queue_empty=lambda *a, **k: None, notify_upload_summary=lambda *a, **k: None)
    module("cleanup", run_cleanup=lambda *a, **k: (0, None))
    module("render_cache", cache_matches=lambda *a: True, write_cache=lambda *a: None)

    spec = importlib.util.spec_from_file_location(
        "main_upload_test", Path(__file__).resolve().parents[1] / "main.py"
    )
    main = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(main)
    return main


def _update_state(state, video, key, step, status, kwargs):
    clip = state.setdefault("videos", {}).setdefault(video, {}).setdefault("clips", {}).setdefault(key, {})
    clip.setdefault(step, {}).update(status=status, **kwargs)


class UploadRetryTests(unittest.TestCase):
    def test_draft_ids_are_stable_and_distinct_for_different_clip_ranges(self):
        main = load_main_with_stubs()
        first = main._draft_clip_id("source-id", 5.2, 38.9)
        second = main._draft_clip_id("source-id", 15.4, 43.7)
        self.assertNotEqual(first, second)
        self.assertEqual(first, main._draft_clip_id("source-id", 5.8, 38.1))
        self.assertNotIn(":", first)

    def test_upload_limit_reason_is_one_attempt_and_not_retryable(self):
        calls = []

        def insert():
            calls.append(1)
            raise StructuredYouTubeError("uploadLimitExceeded")

        with patch.object(retry.time, "sleep") as sleep:
            with self.assertRaises(AccountUploadLimitError):
                retry.retry_upload(insert)
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

    def test_upload_limit_retains_structured_api_context(self):
        error = StructuredYouTubeError("uploadLimitExceeded")
        self.assertEqual(retry.youtube_error_details(error), {
            "http_status": 400, "api_status": "LIMIT_EXCEEDED",
            "api_message": "Account upload limit reached",
            "details": [{"domain": "youtube.video", "reason": "uploadLimitExceeded",
                         "message": "Account upload limit reached"}],
        })

    def test_transient_http_server_error_keeps_bounded_retry(self):
        calls = []

        def insert():
            calls.append(1)
            raise StructuredYouTubeError("backendError", status=500)

        with patch.object(retry.time, "sleep") as sleep:
            with self.assertRaises(retry.RetryExhaustedError):
                retry.retry_upload(insert)
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

    def test_unrelated_permanent_http_error_remains_bounded(self):
        calls = []

        def insert():
            calls.append(1)
            raise StructuredYouTubeError("invalidTitle", status=400)

        with patch.object(retry.time, "sleep") as sleep:
            with self.assertRaises(retry.NonRetryableAPIError):
                retry.retry_upload(insert)
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

    def test_timeout_is_marked_ambiguous_without_retry(self):
        calls = []

        def insert():
            calls.append(1)
            raise TimeoutError("socket timed out")

        with patch.object(retry.time, "sleep"):
            with self.assertRaises(UploadOutcomeUnknownError):
                retry.retry_upload(insert)
        self.assertEqual(len(calls), 1)


class QueueSafetyTests(unittest.TestCase):
    def test_queue_round_trip_is_atomic_and_preserves_metadata(self):
        main = load_main_with_stubs()
        queue = [{"id": "clip-1", "key": "clip-1", "status": "pending", "title": "Keep me", "thumbnail": "x.jpg"}]
        with workspace_tempdir() as temp:
            main.REVIEW_DIR = temp
            main.QUEUE_FILE = os.path.join(temp, "queue.json")
            main.save_queue(queue)
            self.assertEqual(main.load_queue(), queue)
            self.assertEqual(os.listdir(temp), ["queue.json"])

    def test_corrupt_queue_is_not_treated_as_empty_or_overwritten(self):
        main = load_main_with_stubs()
        with workspace_tempdir() as temp:
            main.REVIEW_DIR = temp
            main.QUEUE_FILE = os.path.join(temp, "queue.json")
            original = "{broken queue"
            Path(main.QUEUE_FILE).write_text(original, encoding="utf-8")
            with self.assertRaises(RuntimeError):
                main.load_queue()
            self.assertEqual(Path(main.QUEUE_FILE).read_text(encoding="utf-8"), original)

    def test_account_upload_limit_keeps_clip_pending_and_stops_batch(self):
        main = load_main_with_stubs()
        queue = [
            {"id": "clip-1", "key": "k1", "source_id": "src", "title": "Limit clip", "status": "pending",
             "source_video_path": "source.mp4", "caption": "", "hashtags": []},
            {"id": "clip-2", "key": "k2", "source_id": "src", "title": "Next clip", "status": "pending",
             "source_video_path": "source.mp4", "caption": "", "hashtags": []},
        ]
        state = {}
        upload_calls = []
        main.load_queue = lambda: queue
        saved_queues = []
        main.save_queue = lambda value: saved_queues.append(json.loads(json.dumps(value)))
        main.load_state = lambda: state
        main.save_state_atomic = lambda value: None
        main.get_service = lambda: object()
        main.is_clip_already_uploaded = lambda *a: False
        main.is_valid_media_file = lambda *a: True
        main.update_clip_step = lambda *a, **k: None
        with workspace_tempdir() as temp:
            main.WORK = temp
            for clip in queue:
                Path(temp, f"{clip['id']}.jpg").write_text("thumb")
                Path(temp, f"{clip['id']}.mp4").write_text("video")
            def insert():
                upload_calls.append(1)
                raise StructuredYouTubeError("uploadLimitExceeded")
            def limited_upload(*args, **kwargs):
                return retry.retry_upload(insert)
            main.upload = limited_upload
            events = []
            with patch.object(main, "log_event", side_effect=lambda event, **fields: events.append((event, fields))), \
                 patch.dict(os.environ, {"AGENT_CONTROL_CHAT_ID": "12345"}), \
                 patch("notify.send_telegram_to") as owner_notice, approved_control_task():
                main.render_stage()
        self.assertEqual(len(upload_calls), 1)
        owner_notice.assert_called_once()
        self.assertEqual(queue[0]["status"], "blocked")
        self.assertEqual(queue[0]["upload_error"], "uploadLimitExceeded")
        self.assertEqual(queue[1]["status"], "pending")
        self.assertNotIn("youtube_video_id", queue[0])
        self.assertEqual(queue[0]["upload_error_details"]["http_status"], 400)
        self.assertEqual(queue[0]["upload_error_details"]["details"][0]["reason"], "uploadLimitExceeded")
        self.assertNotIn("k1", state.get("clips", []))
        self.assertTrue(saved_queues)
        self.assertEqual([row["id"] for row in saved_queues[-1]], ["clip-1", "clip-2"])
        self.assertFalse(any(event == "task_result" and fields.get("task") == "clip:clip-1"
                             and fields.get("status") == "success" for event, fields in events))


class PreparationDiagnosticsTests(unittest.TestCase):
    def test_failure_stage_task_and_queue_are_preserved_before_idle_cleanup(self):
        main = load_main_with_stubs()
        from telegram_bot import control, monitoring
        original = TypeError("open() got an unexpected keyword argument 'metadata_errors'")
        reports, errors = [], []
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            root = Path(directory)
            task_file = root / "telegram_control.json"
            queue_file = root / "queue.json"
            queue_before = [
                {"id": "existing-upload", "key": "uploaded-1", "status": "uploaded", "youtube_video_id": "saved-public-id"},
                {"id": "keep-pending", "key": "pending-1", "status": "pending"},
            ]
            queue_bytes = (json.dumps(queue_before, indent=2) + "\n").encode("utf-8")
            queue_file.write_bytes(queue_bytes)
            main.QUEUE_FILE = str(queue_file)
            registry = {"stage": "starting", "current_task": "initializing"}
            def finish_run(outcome=None, error=None, **kwargs):
                registry["last_failed_stage"] = registry.get("current_task")
                registry.update(stage="idle", current_task=None, last_run_status=outcome)
            def fail_prepare():
                registry.update(stage="prepare", current_task="transcribe source")
                raise original
            with patch.object(control, "CONTROL_FILE", task_file), \
                 patch.dict(os.environ, {"AGENT_CONTROL_TASK_ID": "task-regression", "AGENT_CONTROL_CHAT_ID": "12345"}), \
                 patch.object(main, "acquire_lock"), patch.object(main, "release_lock"), \
                 patch.object(main, "prepare_stage", side_effect=fail_prepare), \
                 patch.object(main, "log_event"), patch.object(main, "log_error", side_effect=errors.append), \
                 patch.object(monitoring, "begin_run", side_effect=lambda: registry.update(stage="prepare", current_task="initializing")), \
                 patch.object(monitoring, "read_registry", side_effect=lambda: dict(registry)), \
                 patch.object(monitoring, "finish_run", side_effect=finish_run), \
                 patch("notify.send_telegram_to", side_effect=lambda chat, message: (reports.append((chat, message)), True)[1]), \
                 patch("sys.argv", ["main.py", "prepare"]):
                control._write({"tasks": {"task-regression": {
                    "id": "task-regression", "kind": "prepare", "status": "running",
                    "owner_id": "owner-test", "chat_id": "12345", "clip_ids": [],
                }}})
                with self.assertRaises(TypeError) as raised:
                    main.main()
                task = control.task_info("task-regression")
                self.assertEqual(queue_file.read_bytes(), queue_bytes)

        self.assertIs(raised.exception, original)
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["clip_ids"], [])
        self.assertEqual(registry["last_failed_stage"], "transcribe source")
        self.assertEqual(registry["stage"], "idle")
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0][0], "12345")
        self.assertIn("Stage: transcribe source", reports[0][1])
        self.assertNotIn("Stage: idle", reports[0][1])
        self.assertIn("task-regression", reports[0][1])
        self.assertTrue(any("Traceback (most recent call last)" in line and "metadata_errors" in line for line in errors))

    def test_render_batch_passes_each_approved_clip_grant_to_upload(self):
        main = load_main_with_stubs()
        from upload_auth import consume_upload_approval
        from telegram_bot import control
        queue = [
            {"id": "clip-1", "key": "k1", "status": "pending", "title": "One", "caption": "one",
             "hashtags": [], "thumbnail_text": "one", "source_video_path": "source.mp4", "source_id": "src"},
            {"id": "clip-2", "key": "k2", "status": "pending", "title": "Two", "caption": "two",
             "hashtags": [], "thumbnail_text": "two", "source_video_path": "source.mp4", "source_id": "src"},
        ]
        main.load_queue = lambda: queue
        main.save_queue = lambda _queue: None
        main.load_state = lambda: {}
        main.save_state_atomic = lambda _state: None
        main.is_clip_already_uploaded = lambda *_args: False
        main.get_service = lambda: object()
        main.update_clip_step = lambda *a, **k: None
        main.build_description = lambda *a, **k: "description"
        work_tmp = workspace_tempdir()
        self.addCleanup(work_tmp.cleanup)
        main.WORK = work_tmp.name
        main.CLEANUP = False
        main.DRY_RUN = False
        for clip in queue:
            (Path(main.WORK) / f"{clip['id']}.mp4").write_text("video")
            (Path(main.WORK) / f"{clip['id']}.jpg").write_text("thumbnail")
        checkpoint_calls, duplicate_results, upload_calls = [], [], []
        original_confirm = control.confirm
        def reply_with_confirmation(_chat_id, message, **_kwargs):
            import re
            token = re.search(r"/confirm ([a-f0-9]+)", message).group(1)
            checkpoint_calls.append(token)
            duplicate_results.append(original_confirm(token, 12345, True))
            duplicate_results.append(original_confirm(token, 12345, True))
            return True
        def upload(_yt, *_args, approval=None, clip_id=None, **_kwargs):
            consume_upload_approval(approval, clip_id)
            upload_calls.append(clip_id)
            return f"video-{clip_id}"
        main.upload = upload
        control_file = Path(work_tmp.name) / "telegram_control.json"
        with patch.object(control, "CONTROL_FILE", control_file):
            control._write({"tasks": {"task-batch": {"id": "task-batch", "kind": "upload", "status": "running",
                                                          "owner_id": "12345", "chat_id": "12345", "clip_ids": []}},
                            "confirmation": None})
            with patch.dict(os.environ, {"AGENT_CONTROL_TASK_ID": "task-batch", "AGENT_CONTROL_OWNER_ID": "12345",
                                         "AGENT_CONTROL_CHAT_ID": "12345"}), \
             patch("notify.send_telegram_to", side_effect=reply_with_confirmation), \
             patch.object(main, "log_event"), patch.object(main, "log"), patch.object(main, "log_warn"), \
             patch.object(main, "notify_run_success"), patch.object(main, "notify_run_error"), \
             patch.object(main, "notify_quota_or_auth"), patch.object(main, "save_queue"):
                main.render_stage()
            control.finish("task-batch", "completed")
        self.assertEqual(len(checkpoint_calls), 2)
        self.assertNotEqual(checkpoint_calls[0], checkpoint_calls[1])
        self.assertEqual(duplicate_results, ["Upload approved for clip clip-1.",
                                             "Confirmation not found or already used.",
                                             "Upload approved for clip clip-2.",
                                             "Confirmation not found or already used."])
        self.assertEqual(upload_calls, ["clip-1", "clip-2"])
        self.assertEqual([item["status"] for item in queue], ["uploaded", "uploaded"])

    def test_denied_clip_stays_pending_and_is_never_uploaded(self):
        main = load_main_with_stubs()
        from upload_auth import issue_upload_approval, consume_upload_approval
        queue = [
            {"id": "clip-denied", "key": "k1", "status": "pending", "title": "Denied", "caption": "x",
             "hashtags": [], "thumbnail_text": "x", "source_video_path": "source.mp4", "source_id": "src"},
            {"id": "clip-approved", "key": "k2", "status": "pending", "title": "Approved", "caption": "y",
             "hashtags": [], "thumbnail_text": "y", "source_video_path": "source.mp4", "source_id": "src"},
        ]
        main.load_queue = lambda: queue
        main.save_queue = lambda _queue: None
        main.load_state = lambda: {}
        main.save_state_atomic = lambda _state: None
        main.is_clip_already_uploaded = lambda *_args: False
        main.get_service = lambda: object()
        main.update_clip_step = lambda *a, **k: None
        main.build_description = lambda *a, **k: "description"
        work_tmp = workspace_tempdir()
        self.addCleanup(work_tmp.cleanup)
        main.WORK = work_tmp.name
        main.CLEANUP = False
        main.DRY_RUN = False
        for clip in queue:
            (Path(main.WORK) / f"{clip['id']}.mp4").write_text("video")
            (Path(main.WORK) / f"{clip['id']}.jpg").write_text("thumbnail")
        requested, published = [], []
        def checkpoint(*, clip=None, ask_upload=False):
            if not ask_upload:
                return "continue"
            requested.append(clip["id"])
            return "denied" if clip["id"] == "clip-denied" else issue_upload_approval(clip["id"], "owner")
        def upload(_yt, *_args, approval=None, clip_id=None, **_kwargs):
            consume_upload_approval(approval, clip_id)
            published.append(clip_id)
            return f"video-{clip_id}"
        main.upload = upload
        with patch.dict(os.environ, {"AGENT_CONTROL_TASK_ID": "task-batch", "AGENT_CONTROL_CHAT_ID": "12345"}), \
             patch("telegram_bot.control.checkpoint", side_effect=checkpoint), \
             patch("telegram_bot.control.associate_clips"), patch.object(main, "log_event"), \
             patch.object(main, "log"), patch.object(main, "log_warn"), \
             patch.object(main, "notify_run_success"), patch.object(main, "notify_run_error"), \
             patch.object(main, "notify_quota_or_auth"), patch.object(main, "save_queue"):
            main.render_stage()
        self.assertEqual(requested, ["clip-denied", "clip-approved"])
        self.assertEqual(published, ["clip-approved"])
        self.assertEqual(queue[0]["status"], "pending")
        self.assertEqual(queue[0]["last_result"], "upload_confirmation_denied")
        self.assertEqual(queue[1]["status"], "uploaded")

    def test_rejected_upload_approval_stops_batch_and_keeps_later_clips_pending(self):
        main = load_main_with_stubs()
        from upload_auth import UploadApprovalRequiredError, issue_upload_approval
        queue = [
            {"id": "clip-rejected", "key": "k1", "status": "pending", "title": "Rejected", "caption": "x",
             "hashtags": [], "thumbnail_text": "x", "source_video_path": "source.mp4", "source_id": "src"},
            {"id": "clip-later", "key": "k2", "status": "pending", "title": "Later", "caption": "y",
             "hashtags": [], "thumbnail_text": "y", "source_video_path": "source.mp4", "source_id": "src"},
        ]
        main.load_queue = lambda: queue
        main.save_queue = lambda _queue: None
        main.load_state = lambda: {}
        main.save_state_atomic = lambda _state: None
        main.is_clip_already_uploaded = lambda *_args: False
        main.get_service = lambda: object()
        main.update_clip_step = lambda *a, **k: None
        main.build_description = lambda *a, **k: "description"
        work_tmp = workspace_tempdir()
        self.addCleanup(work_tmp.cleanup)
        main.WORK = work_tmp.name
        main.CLEANUP = False
        main.DRY_RUN = False
        for clip in queue:
            (Path(main.WORK) / f"{clip['id']}.mp4").write_text("video")
            (Path(main.WORK) / f"{clip['id']}.jpg").write_text("thumbnail")
        upload_calls, events = [], []
        def upload(*_args, **_kwargs):
            upload_calls.append("reached shared upload layer")
            raise UploadApprovalRequiredError("Public upload requires owner confirmation through the Telegram bot.")
        main.upload = upload
        def checkpoint(*, clip=None, ask_upload=False):
            return issue_upload_approval(clip["id"], "owner") if ask_upload else "continue"
        with patch.dict(os.environ, {"AGENT_CONTROL_TASK_ID": "task-batch", "AGENT_CONTROL_CHAT_ID": "12345"}), \
             patch("telegram_bot.control.checkpoint", side_effect=checkpoint), \
             patch("telegram_bot.control.associate_clips"), patch.object(main, "log_event", side_effect=lambda event, **fields: events.append((event, fields))), \
             patch.object(main, "log"), patch.object(main, "log_warn"), patch.object(main, "log_error"), \
             patch.object(main, "notify_run_success"), patch.object(main, "notify_run_error"), \
             patch.object(main, "notify_quota_or_auth"), patch.object(main, "save_queue"):
            main.render_stage()
        self.assertEqual(upload_calls, ["reached shared upload layer"])
        self.assertEqual(queue[0]["status"], "failed")
        self.assertEqual(queue[0]["last_result"], "upload_approval_required")
        self.assertEqual(queue[1]["status"], "pending")
        self.assertTrue(any(fields.get("error_category") == "approval_required" for _, fields in events))

    def test_failure_report_is_specific_redacted_and_notification_failure_is_safe(self):
        main = load_main_with_stubs()
        fake_notify = types.ModuleType("notify")
        sent = []
        fake_notify.send_telegram_to = lambda chat, message: (sent.append((chat, message)), False)[1]
        fake_notify.notify_run_error = lambda *a, **k: None
        cases = [
            ("analyze transcript", StructuredYouTubeError("authError", status=401), "authentication"),
            ("download source", StructuredYouTubeError("userRateLimitExceeded", status=429), "rate_limited"),
            ("download source", TimeoutError("network timed out"), "timeout"),
            ("transcribe source", ModuleNotFoundError("No module named 'whisper'"), "missing_dependency"),
            ("render media", __import__("subprocess").CalledProcessError(1, "ffmpeg", stderr="encoder failed"), "media_processing_failure"),
            ("analyze transcript", RuntimeError("unexpected token=SECRET123"), "unexpected"),
        ]
        with patch.dict(os.environ, {"AGENT_CONTROL_TASK_ID": "task-test", "AGENT_CONTROL_CHAT_ID": "12345"}), \
             patch("telegram_bot.monitoring.read_registry", side_effect=lambda: {"current_task": stage}), \
             patch.dict(sys.modules, {"notify": fake_notify}):
            for stage, error, category in cases:
                with self.subTest(category=category):
                    sent.clear()
                    original = error
                    main._report_preparation_failure(error)
                    self.assertEqual(len(sent), 1)
                    chat, message = sent[0]
                    self.assertEqual(chat, "12345")
                    self.assertIn("task-test", message)
                    self.assertIn(stage, message)
                    self.assertIn(category, message)
                    self.assertNotIn("SECRET123", message)
                    self.assertIs(error, original)

    def test_preparation_failure_cause_is_not_hidden_by_owner_notification(self):
        main = load_main_with_stubs()
        original = TimeoutError("request timed out")
        fake_notify = types.ModuleType("notify")
        fake_notify.send_telegram_to = lambda *a, **k: False
        fake_notify.notify_run_error = lambda *a, **k: None
        with patch.dict(os.environ, {"AGENT_CONTROL_CHAT_ID": "12345"}), \
             patch("telegram_bot.monitoring.read_registry", return_value={"current_task": "download source"}), \
             patch.dict(sys.modules, {"notify": fake_notify}):
            with self.assertRaises(TimeoutError) as raised:
                try:
                    raise original
                except TimeoutError as exc:
                    main._report_preparation_failure(exc)
                    raise
        self.assertIs(raised.exception, original)

    def test_pipeline_boundary_reports_once_and_keeps_original_preparation_failure(self):
        main = load_main_with_stubs()
        original = RuntimeError("prepare failed")
        original.__cause__ = ModuleNotFoundError("No module named 'whisper'")
        with patch.dict(os.environ, {"AGENT_CONTROL_TASK_ID": "task-test", "AGENT_CONTROL_CHAT_ID": "12345"}), \
             patch.object(main, "acquire_lock"), patch.object(main, "release_lock"), \
             patch.object(main, "prepare_stage", side_effect=original), \
             patch("telegram_bot.monitoring.begin_run"), patch("telegram_bot.monitoring.finish_run") as finish_run, \
             patch("telegram_bot.monitoring.read_registry", return_value={"current_task": "transcribe source"}), \
             patch("notify.send_telegram_to") as send_owner, patch("sys.argv", ["main.py", "prepare"]):
            with self.assertRaises(RuntimeError) as raised:
                main.main()
        self.assertIs(raised.exception, original)
        send_owner.assert_called_once()
        self.assertIn("missing_dependency", send_owner.call_args.args[1])
        finish_run.assert_called_once_with("failed", error=original)

    def test_ambiguous_upload_is_not_retried_or_processed_as_pending(self):
        main = load_main_with_stubs()
        calls = []
        with patch.object(retry.time, "sleep"):
            with self.assertRaises(UploadOutcomeUnknownError):
                retry.retry_upload(lambda: (calls.append(1), (_ for _ in ()).throw(TimeoutError("timeout")))[1])
        self.assertEqual(len(calls), 1)
        queue = [
            {"id": "clip-uncertain", "key": "ku", "source_id": "src", "title": "Uncertain", "status": "pending",
             "source_video_path": "source.mp4", "caption": "", "hashtags": []},
            {"id": "clip-next", "key": "kn", "source_id": "src", "title": "Next", "status": "pending",
             "source_video_path": "source.mp4", "caption": "", "hashtags": []},
        ]
        main.load_queue = lambda: queue
        main.save_queue = lambda value: None
        main.load_state = lambda: {}
        main.save_state_atomic = lambda value: None
        main.get_service = lambda: object()
        main.is_clip_already_uploaded = lambda *a: False
        main.is_valid_media_file = lambda *a: True
        main.update_clip_step = lambda *a, **k: None
        upload_calls = []
        with workspace_tempdir() as temp:
            main.WORK = temp
            for clip in queue:
                Path(temp, f"{clip['id']}.jpg").write_text("thumb")
                Path(temp, f"{clip['id']}.mp4").write_text("video")
            def uncertain_upload(*args, **kwargs):
                upload_calls.append(args)
                raise UploadOutcomeUnknownError("verify YouTube Studio")
            main.upload = uncertain_upload
            with approved_control_task():
                main.render_stage()
        self.assertEqual(len(upload_calls), 1)
        self.assertEqual(queue[0]["status"], "needs_review")
        self.assertIn("YouTube Studio", queue[0]["upload_error"])
        self.assertEqual(queue[1]["status"], "pending")
        self.assertNotIn("youtube_video_id", queue[0])

    def test_main_releases_run_lock_after_render_returns(self):
        main = load_main_with_stubs()
        calls = []
        main.acquire_lock = lambda: calls.append("acquire")
        main.release_lock = lambda: calls.append("release")
        main.render_stage = lambda: calls.append("render")
        with patch.object(main.sys, "argv", ["main.py", "render"]):
            main.main()
        self.assertEqual(calls, ["acquire", "render", "release"])

    def test_main_releases_lock_and_returns_interrupt_code(self):
        main = load_main_with_stubs()
        calls = []
        main.acquire_lock = lambda: calls.append("acquire")
        main.release_lock = lambda: calls.append("release")
        main.render_stage = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        with patch.object(main.sys, "argv", ["main.py", "render"]):
            with self.assertRaises(SystemExit) as raised:
                main.main()
        self.assertEqual(raised.exception.code, 130)
        self.assertEqual(calls, ["acquire", "release"])

    def test_confirmed_upload_survives_interruption_and_stops_batch(self):
        main = load_main_with_stubs()
        queue = [
            {"id": "clip-1", "key": "k1", "source_id": "src", "title": "Committed",
             "status": "pending", "source_video_path": "source.mp4", "caption": "", "hashtags": []},
            {"id": "clip-2", "key": "k2", "source_id": "src", "title": "Next",
             "status": "pending", "source_video_path": "source.mp4", "caption": "", "hashtags": []},
        ]
        state = {}
        main.load_queue = lambda: queue
        main.save_queue = lambda value: None
        main.load_state = lambda: state
        main.save_state_atomic = lambda value: None
        main.get_service = lambda: object()
        main.is_clip_already_uploaded = lambda *a: False
        main.is_valid_media_file = lambda *a: True
        main.update_clip_step = lambda *a, **k: None
        upload_calls = []

        def interrupted_upload(*args, **kwargs):
            upload_calls.append(args)
            raise UploadCommittedInterruptedError("yt-confirmed")

        main.upload = interrupted_upload
        with workspace_tempdir() as temp:
            main.WORK = temp
            for item in queue:
                Path(temp, f"{item['id']}.jpg").write_text("thumb")
                Path(temp, f"{item['id']}.mp4").write_text("video")
            with approved_control_task():
                main.render_stage()

        self.assertEqual(len(upload_calls), 1)
        self.assertEqual(queue[0]["status"], "uploaded")
        self.assertEqual(queue[0]["youtube_video_id"], "yt-confirmed")
        self.assertEqual(queue[1]["status"], "pending")
        self.assertIn("k1", state.get("clips", []))

    def test_direct_live_render_without_telegram_approval_cannot_publish(self):
        main = load_main_with_stubs()
        queue = [{"id": "clip-1", "key": "k1", "status": "pending"}]
        service_calls = []
        upload_calls = []
        main.load_queue = lambda: queue
        main.load_state = lambda: {}
        main.get_service = lambda: service_calls.append("service")
        main.upload = lambda *args, **kwargs: upload_calls.append("upload")
        with patch.dict(os.environ, {}, clear=True):
            main.render_stage()
        self.assertEqual(service_calls, [])
        self.assertEqual(upload_calls, [])
        self.assertEqual(queue[0]["status"], "pending")

    def test_needs_review_clip_is_never_automatically_reprocessed(self):
        main = load_main_with_stubs()
        queue = [{"id": "uncertain", "key": "k", "status": "needs_review"}]
        main.DRY_RUN = True
        main.load_queue = lambda: queue
        main.save_queue = lambda value: None
        main.load_state = lambda: {}
        main.render_stage()
        self.assertEqual(queue[0]["status"], "needs_review")


if __name__ == "__main__":
    unittest.main()
