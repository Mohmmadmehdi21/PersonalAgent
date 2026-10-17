"""Crash-safe persistence helpers for the existing list-based review queue."""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

TASK_STATES = {"pending", "processing", "uploaded", "failed", "blocked", "needs_review"}


def _backup(path, content):
    path = Path(path)
    backup_dir = path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = backup_dir / f"{path.stem}-{stamp}-{os.getpid()}.json"
    # Exclusive creation prevents a same-microsecond collision from overwriting history.
    with backup.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    return backup


def save_queue(path, queue, *, backup=True):
    """Write queue atomically; archive the prior bytes before replacing them."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(queue, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if path.exists() and backup:
        previous = path.read_bytes()
        if previous != encoded:
            _backup(path, previous)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.remove(temp_name)


def _migrate(queue):
    changed = False
    legacy_map = {
        "approved": "pending", "rendered_dry_run": "pending",
        "completed": "uploaded", "skipped_unusable": "failed", "skipped": "failed",
        "upload_uncertain": "needs_review",
    }
    for item in queue:
        status = item.get("status")
        if not item.get("key") or not item.get("id"):
            if status in (None, "pending", "processing"):
                item["legacy_status"] = status
                item["status"] = "needs_review"
                item["recovery_note"] = "Queue item is missing a stable id or key; preserved for manual repair."
                changed = True
                continue
        if status is None:
            item["status"] = "pending"
            changed = True
        elif status == "processing":
            phase = item.get("processing_step") or item.get("task_phase")
            if item.get("youtube_video_id"):
                item["status"] = "uploaded"
            elif phase in ("uploading", "upload", "upload_started"):
                item["status"] = "needs_review"
                item["recovery_note"] = "Upload was interrupted after request start; verify YouTube Studio before retrying."
            else:
                item["status"] = "pending"
                item.pop("processing_step", None)
                item.pop("task_phase", None)
            changed = True
        elif status in legacy_map:
            new_status = legacy_map[status]
            if status == "completed" and not item.get("youtube_video_id"):
                new_status = "needs_review"
                item["recovery_note"] = "Legacy completed item has no confirmed YouTube video ID; verify manually."
            item["legacy_status"] = status
            item["status"] = new_status
            changed = True
        elif status not in TASK_STATES:
            item["legacy_status"] = status
            item["status"] = "needs_review"
            item["recovery_note"] = "Unknown queue status preserved for manual review."
            changed = True
        elif status == "uploaded" and not item.get("youtube_video_id"):
            item["status"] = "needs_review"
            item["recovery_note"] = "Uploaded status has no confirmed YouTube video ID."
            changed = True

    # Preserve every duplicate record, but prevent later copies from uploading automatically.
    seen = {}
    for index, item in enumerate(queue):
        identity = item.get("key") or item.get("id")
        if not identity:
            continue
        if identity in seen and item.get("status") in ("pending", "processing"):
            item["status"] = "needs_review"
            item["duplicate_queue_index"] = seen[identity]
            item["recovery_note"] = "Duplicate job identity preserved and held to prevent duplicate upload."
            changed = True
        else:
            seen[identity] = index
    return changed


def load_queue(path):
    path = Path(path)
    if not path.exists():
        return []
    try:
        original = path.read_bytes()
        queue = json.loads(original.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Queue file could not be read safely; preserving it: {exc}") from exc
    if not isinstance(queue, list) or any(not isinstance(item, dict) for item in queue):
        raise RuntimeError("Queue file has an invalid structure; preserving it without processing.")
    if _migrate(queue):
        # Archive the exact original bytes before committing any migration.
        _backup(path, original)
        save_queue(path, queue, backup=False)
    return queue


def add_if_new(queue, item):
    """Append only when the stable job key/id is not already present."""
    identity = item.get("key") or item.get("id")
    if identity and any((old.get("key") or old.get("id")) == identity for old in queue):
        return False
    queue.append(item)
    return True
