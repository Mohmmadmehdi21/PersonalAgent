import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import retry
from retry import AccountUploadLimitError, UploadOutcomeUnknownError, UploadCommittedInterruptedError


def workspace_tempdir():
    return tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "tests")


class StructuredYouTubeError(Exception):
    def __init__(self, reason, status=400):
        super().__init__(f"HTTP {status}")
        self.error_details = [{"reason": reason}]
        self.resp = types.SimpleNamespace(status=status)


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
           notify_queue_empty=lambda *a, **k: None)
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
        main.save_queue = lambda value: None
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
            def limited_upload(*args):
                upload_calls.append(args)
                raise AccountUploadLimitError("uploadLimitExceeded")
            main.upload = limited_upload
            main.render_stage()
        self.assertEqual(len(upload_calls), 1)
        self.assertEqual(queue[0]["status"], "blocked")
        self.assertEqual(queue[0]["upload_error"], "uploadLimitExceeded")
        self.assertEqual(queue[1]["status"], "pending")
        self.assertNotIn("youtube_video_id", queue[0])

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
            def uncertain_upload(*args):
                upload_calls.append(args)
                raise UploadOutcomeUnknownError("verify YouTube Studio")
            main.upload = uncertain_upload
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

        def interrupted_upload(*args):
            upload_calls.append(args)
            raise UploadCommittedInterruptedError("yt-confirmed")

        main.upload = interrupted_upload
        with workspace_tempdir() as temp:
            main.WORK = temp
            for item in queue:
                Path(temp, f"{item['id']}.jpg").write_text("thumb")
                Path(temp, f"{item['id']}.mp4").write_text("video")
            main.render_stage()

        self.assertEqual(len(upload_calls), 1)
        self.assertEqual(queue[0]["status"], "uploaded")
        self.assertEqual(queue[0]["youtube_video_id"], "yt-confirmed")
        self.assertEqual(queue[1]["status"], "pending")
        self.assertIn("k1", state.get("clips", []))


if __name__ == "__main__":
    unittest.main()
