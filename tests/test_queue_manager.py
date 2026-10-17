import json
import os
import importlib.util
import tempfile
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from queue_manager import add_if_new, load_queue, save_queue

_config_stub = types.ModuleType("config")
_config_stub.WORK = "work"
_logger_stub = types.ModuleType("logger")
_logger_stub.log = _logger_stub.log_warn = lambda *a, **k: None
with patch.dict(sys.modules, {"config": _config_stub, "logger": _logger_stub}):
    _lock_spec = importlib.util.spec_from_file_location("phase2_lock_test", Path(__file__).resolve().parents[1] / "lock.py")
    lock = importlib.util.module_from_spec(_lock_spec)
    _lock_spec.loader.exec_module(lock)


def workspace_tempdir():
    return tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)


class QueueManagerTests(unittest.TestCase):
    def test_pending_survives_save_and_restart(self):
        with workspace_tempdir() as tmp:
            path = Path(tmp) / "review" / "queue.json"
            original = [{"id": "j1", "key": "src:1-2", "status": "pending", "custom": {"x": 1}}]
            save_queue(path, original)
            self.assertEqual(load_queue(path), original)

    def test_legacy_migration_keeps_exact_backup_and_queue_fields(self):
        with workspace_tempdir() as tmp:
            path = Path(tmp) / "review" / "queue.json"
            path.parent.mkdir()
            original = '[{"id":"j1","status":"approved","custom":"preserve"}]'
            path.write_text(original, encoding="utf-8")
            queue = load_queue(path)
            self.assertEqual(queue[0]["status"], "pending")
            self.assertEqual(queue[0]["custom"], "preserve")
            backups = list((path.parent / "backups").glob("queue-*.json"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), original)

    def test_interrupted_render_returns_pending_but_upload_requires_review(self):
        with workspace_tempdir() as tmp:
            path = Path(tmp) / "queue.json"
            path.write_text(json.dumps([
                {"id": "render", "key": "render", "status": "processing", "processing_step": "rendering"},
                {"id": "upload", "key": "upload", "status": "processing", "processing_step": "uploading"},
            ]), encoding="utf-8")
            queue = load_queue(path)
            self.assertEqual([item["status"] for item in queue], ["pending", "needs_review"])

    def test_confirmed_legacy_completion_migrates_to_uploaded_only_with_id(self):
        with workspace_tempdir() as tmp:
            path = Path(tmp) / "queue.json"
            path.write_text(json.dumps([
                {"id": "ok", "status": "completed", "youtube_video_id": "yt123"},
                {"id": "unknown", "status": "completed"},
            ]), encoding="utf-8")
            queue = load_queue(path)
            self.assertEqual(queue[0]["status"], "uploaded")
            self.assertEqual(queue[1]["status"], "needs_review")

    def test_duplicate_job_is_retained_but_held_for_review(self):
        queue = [{"id": "first", "key": "same", "status": "pending"}]
        self.assertFalse(add_if_new(queue, {"id": "second", "key": "same", "status": "pending"}))
        self.assertEqual(len(queue), 1)
        with workspace_tempdir() as tmp:
            path = Path(tmp) / "queue.json"
            path.write_text(json.dumps([
                {"id": "first", "key": "same", "status": "pending"},
                {"id": "second", "key": "same", "status": "pending"},
            ]), encoding="utf-8")
            migrated = load_queue(path)
            self.assertEqual(len(migrated), 2)
            self.assertEqual(migrated[1]["status"], "needs_review")

    def test_corrupt_queue_is_preserved(self):
        with workspace_tempdir() as tmp:
            path = Path(tmp) / "queue.json"
            path.write_text("{bad", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                load_queue(path)
            self.assertEqual(path.read_text(encoding="utf-8"), "{bad")

    def test_failed_atomic_replace_keeps_previous_queue(self):
        with workspace_tempdir() as tmp:
            path = Path(tmp) / "queue.json"
            old = [{"id": "old", "key": "old", "status": "pending"}]
            save_queue(path, old)
            with patch("queue_manager.os.replace", side_effect=OSError("simulated replace failure")):
                with self.assertRaises(OSError):
                    save_queue(path, [{"id": "new", "key": "new", "status": "pending"}])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), old)
            self.assertFalse(list(path.parent.glob("queue.json.*.tmp")))

    def test_live_run_lock_is_preserved_and_prevents_overlap(self):
        with workspace_tempdir() as tmp:
            lock_path = Path(tmp) / "run.lock"
            lock_path.write_text(json.dumps({"pid": 4321, "created_at": 1}), encoding="utf-8")
            with patch.object(lock, "LOCK_FILE", str(lock_path)), patch.object(lock, "is_pid_alive", return_value=True):
                with self.assertRaises(SystemExit) as stopped:
                    lock.acquire_lock()
            self.assertEqual(stopped.exception.code, 0)
            self.assertTrue(lock_path.exists())

    def test_dead_run_lock_is_recovered(self):
        with workspace_tempdir() as tmp:
            lock_path = Path(tmp) / "run.lock"
            lock_path.write_text(json.dumps({"pid": 4321, "created_at": 1}), encoding="utf-8")
            with patch.object(lock, "LOCK_FILE", str(lock_path)), patch.object(lock, "is_pid_alive", return_value=False):
                lock.acquire_lock()
                self.assertEqual(json.loads(lock_path.read_text(encoding="utf-8"))["pid"], os.getpid())
                lock.release_lock()
            self.assertFalse(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
