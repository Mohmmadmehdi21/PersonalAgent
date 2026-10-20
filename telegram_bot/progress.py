"""Centralized, real-time progress tracking system for PersonalAgent.

Provides:
1. Accurate, weighted stage progress calculation (no fake timers or arbitrary jumps).
2. Live Telegram progress message sender and message editor with rate-limit throttling.
3. Clean state tracking: task_id, status, stages, batch item progress, elapsed time, ETA.
4. Thread-safe updates and atomic persistence to data/progress/{task_id}.json.
5. Fallback safety: Telegram API errors never fail the underlying pipeline task.
"""
import copy
import json
import math
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from env_config import load_project_env

load_project_env()

PROGRESS_DIR = Path("data/progress")
_TRACKER_REGISTRY = {}
_TRACKER_LOCK = threading.RLock()

# Stage definitions and relative weights (sum = 100%)
STAGE_CONFIGS = {
    "prepare": [
        {"id": "source_validation", "name": "Source validation", "weight": 5},
        {"id": "download", "name": "Video download", "weight": 20},
        {"id": "transcribe", "name": "Audio extraction & transcription", "weight": 25},
        {"id": "ai_analysis", "name": "AI analysis and clip selection", "weight": 20},
        {"id": "clip_selection", "name": "Clip assessment & validation", "weight": 10},
        {"id": "queue_insertion", "name": "Queue insertion & persistence", "weight": 20},
    ],
    "upload": [
        {"id": "queue_assessment", "name": "Queue assessment", "weight": 5},
        {"id": "batch_processing", "name": "Clip rendering & upload", "weight": 90},
        {"id": "batch_finalization", "name": "Batch finalization", "weight": 5},
    ],
    "source_refresh": [
        {"id": "init_lock", "name": "Lock & initialization", "weight": 10},
        {"id": "channel_scan", "name": "Channel video discovery", "weight": 50},
        {"id": "filter_merge", "name": "Deduplication & merge", "weight": 30},
        {"id": "persist_record", "name": "Persistence & finalization", "weight": 10},
    ],
    "source_cleanup": [
        {"id": "scan_ledger", "name": "Scan sources & ledgers", "weight": 20},
        {"id": "backup", "name": "Create source backup", "weight": 20},
        {"id": "filter_sources", "name": "Filter used & duplicates", "weight": 40},
        {"id": "persist_sources", "name": "Persist updated sources", "weight": 20},
    ],
}


def render_progress_bar(percentage, length=16):
    """Render a visual ASCII progress bar [████████░░░░░░░░] 50%."""
    val = max(0, min(100, int(percentage)))
    filled = int(round((length * val) / 100.0))
    bar = "█" * filled + "░" * (length - filled)
    return f"[{bar}] {val}%"


def format_progress_message(record):
    """Format full human-readable Telegram progress message."""
    task_name = record.get("task_name") or record.get("task_type", "Task").replace("_", " ").title()
    task_id = record.get("task_id", "N/A")
    status = str(record.get("status", "RUNNING")).upper()
    overall = max(0, min(100, int(record.get("overall_progress", 0))))

    # Safety: non-completed tasks must never display 100%
    if status != "COMPLETED" and overall >= 100:
        overall = 99

    lines = [
        f"**PersonalAgent — {task_name}**",
        "",
        f"Task: {task_name}",
        f"Task ID: `{task_id}`",
        "",
        f"Overall Progress: {overall}%",
        "",
        render_progress_bar(overall, 16),
        "",
    ]

    # Batch item progress where applicable
    total_items = record.get("total_items")
    if total_items and total_items > 0:
        processed = int(record.get("processed_items", 0))
        lines.append(f"Clips: {processed} / {total_items} completed")
        if status == "RUNNING" and processed < total_items:
            current_idx = processed + 1
            cur_title = record.get("current_item_title") or f"Clip {current_idx}"
            lines.append(f"Current Clip: {current_idx} / {total_items} ({cur_title[:35]})")
            clip_prog = int(record.get("current_item_progress", 0))
            lines.append(f"Current Clip Progress: {clip_prog}%")
        lines.append("")

    current_stage = record.get("current_stage_name") or record.get("current_stage") or "Processing"
    lines.append(f"Current Stage: {current_stage}")

    detail = record.get("stage_detail")
    if detail:
        lines.append(f"Detail: {detail}")
    lines.append("")

    completed_stages = record.get("completed_stages", [])
    if completed_stages:
        lines.append("Completed:")
        for st in completed_stages:
            lines.append(f"✓ {st}")
        lines.append("")

    if status == "RUNNING":
        lines.append("In Progress:")
        lines.append(f"⏳ {current_stage}")
        lines.append("")
    elif status in {"WAITING_CONFIRMATION", "WAITING_RETRY", "PAUSED", "NEEDS_REVIEW"}:
        lines.append("Waiting State:")
        lines.append(f"⏸️ {status}")
        lines.append("")

    remaining_stages = record.get("remaining_stages", [])
    if remaining_stages and status not in {"COMPLETED", "FAILED", "CANCELLED", "NO_ELIGIBLE_CLIPS"}:
        lines.append("Remaining:")
        for st in remaining_stages:
            lines.append(f"○ {st}")
        lines.append("")

    total_stages = record.get("total_stages", 0)
    if total_stages > 0:
        lines.append(f"Stages: {len(completed_stages)} / {total_stages} completed")

    # Elapsed time and calculable ETA
    started_at = record.get("started_at")
    if started_at:
        elapsed_sec = max(0, int(time.time() - started_at))
        lines.append(f"Elapsed: {elapsed_sec // 60:02d}:{elapsed_sec % 60:02d}")

        eta_sec = record.get("estimated_remaining_seconds")
        if eta_sec is not None and eta_sec > 0 and status == "RUNNING":
            lines.append(f"Estimated Remaining: ~{int(eta_sec) // 60:02d}:{int(eta_sec) % 60:02d}")
        elif status == "RUNNING":
            if overall > 5 and elapsed_sec > 4:
                est_rem = (elapsed_sec / (overall / 100.0)) - elapsed_sec
                if 0 < est_rem < 86400:
                    lines.append(f"Estimated Remaining: ~{int(est_rem) // 60:02d}:{int(est_rem) % 60:02d}")
                else:
                    lines.append("Estimated Remaining: Calculating...")
            else:
                lines.append("Estimated Remaining: Calculating...")

    lines.append("")
    lines.append(f"Status: {status}")

    if record.get("error"):
        from logger import mask_secrets
        err_msg = mask_secrets(str(record["error"]))[:180]
        lines.append(f"Error: {err_msg}")

    updated_at = record.get("updated_at") or time.time()
    t_struct = time.localtime(updated_at)
    lines.append(f"Last Updated: {time.strftime('%H:%M:%S', t_struct)}")

    return "\n".join(lines)


class ProgressTracker:
    """Thread-safe progress tracking and live Telegram updater for a single task."""

    def __init__(self, task_id, task_type="prepare", *, total_items=None,
                 chat_id=None, task_name=None, min_interval=2.0):
        self.task_id = str(task_id)
        self.task_type = str(task_type)
        self.task_name = task_name or self._derive_task_name(task_type)
        self.chat_id = str(chat_id) if chat_id else None
        self.message_id = None
        self.min_interval = min_interval
        self.last_telegram_update_time = 0.0
        self.last_sent_text = None
        self.lock = threading.RLock()

        # Load stage configuration
        stages = STAGE_CONFIGS.get(self.task_type)
        if not stages:
            stages = [{"id": "executing", "name": "Executing task", "weight": 100}]
        self.stage_configs = copy.deepcopy(stages)
        self.total_stages = len(self.stage_configs)
        self.stage_weights = {s["id"]: float(s.get("weight", 100.0 / self.total_stages))
                              for s in self.stage_configs}

        # Normalize weights to exactly 100
        w_sum = sum(self.stage_weights.values()) or 100.0
        for k in self.stage_weights:
            self.stage_weights[k] = (self.stage_weights[k] / w_sum) * 100.0

        now = time.time()
        self.record = {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "task_name": self.task_name,
            "status": "RUNNING",
            "current_stage": self.stage_configs[0]["id"] if self.stage_configs else "init",
            "current_stage_name": self.stage_configs[0]["name"] if self.stage_configs else "Initializing",
            "stage_index": 0,
            "total_stages": self.total_stages,
            "stage_progress": 0.0,
            "overall_progress": 0.0,
            "completed_stages": [],
            "remaining_stages": [s["name"] for s in self.stage_configs[1:]],
            "stage_detail": None,
            "total_items": total_items,
            "processed_items": 0 if total_items is not None else None,
            "current_item_title": None,
            "current_item_progress": None,
            "started_at": now,
            "updated_at": now,
            "finished_at": None,
            "error": None,
            "telegram_chat_id": self.chat_id,
            "telegram_message_id": None,
        }

        # Check existing persisted state if restoring
        self.progress_file = PROGRESS_DIR / f"{self.task_id}.json"
        self._load_persisted()

    def _derive_task_name(self, task_type):
        names = {
            "prepare": "Prepare Clips",
            "upload": "Upload Clips",
            "batch_upload": "Batch Upload",
            "source_refresh": "Source Refresh",
            "source_cleanup": "Source Cleanup",
        }
        return names.get(task_type, task_type.replace("_", " ").title())

    def _load_persisted(self):
        if self.progress_file.exists():
            try:
                data = json.loads(self.progress_file.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("task_id") == self.task_id:
                    self.record.update(data)
                    self.message_id = self.record.get("telegram_message_id")
            except Exception:
                pass

    def _persist(self):
        PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=f"prog-{self.task_id}-", suffix=".tmp", dir=str(PROGRESS_DIR))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.record, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.progress_file)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def _calculate_overall_progress(self):
        status = self.record.get("status")
        if status == "COMPLETED":
            return 100.0
        if status in {"FAILED", "CANCELLED"}:
            return min(99.0, self.record.get("overall_progress", 0.0))

        completed_ids = set()
        for s in self.stage_configs:
            if s["name"] in self.record.get("completed_stages", []):
                completed_ids.add(s["id"])

        completed_weight = sum(self.stage_weights.get(sid, 0.0) for sid in completed_ids)
        current_id = self.record.get("current_stage")
        if current_id not in completed_ids:
            cur_weight = self.stage_weights.get(current_id, 0.0)
            cur_progress = max(0.0, min(1.0, float(self.record.get("stage_progress", 0.0))))
            calculated = completed_weight + (cur_weight * cur_progress)
        else:
            calculated = completed_weight

        # Prevent backward jumps in normal forward execution
        prev = self.record.get("overall_progress", 0.0)
        val = max(prev, calculated)
        return min(99.0, val)

    def send_initial_telegram_message(self):
        """Send first Telegram message and store message_id for subsequent edits."""
        with self.lock:
            if not self.chat_id:
                return None
            try:
                text = format_progress_message(self.record)
                msg_id = _api_send_message(self.chat_id, text)
                if msg_id:
                    self.message_id = msg_id
                    self.record["telegram_message_id"] = msg_id
                    self.last_telegram_update_time = time.time()
                    self.last_sent_text = text
                    self._persist()
                return msg_id
            except Exception:
                return None

    def _edit_telegram_message(self, force=False):
        """Edit existing Telegram progress message with throttling."""
        with self.lock:
            if not self.chat_id or not self.message_id:
                return

            now = time.time()
            if not force and (now - self.last_telegram_update_time < self.min_interval):
                return

            text = format_progress_message(self.record)
            if text == self.last_sent_text:
                return

            try:
                success, new_msg_id = _api_edit_message(self.chat_id, self.message_id, text)
                if success:
                    self.last_telegram_update_time = now
                    self.last_sent_text = text
                    if new_msg_id and new_msg_id != self.message_id:
                        self.message_id = new_msg_id
                        self.record["telegram_message_id"] = new_msg_id
                        self._persist()
            except Exception:
                pass

    def start_stage(self, stage_id, detail=None, force_telegram=True):
        """Transition into a new stage checkpoint."""
        with self.lock:
            # Find matching stage
            match = next((s for s in self.stage_configs if s["id"] == stage_id), None)
            stage_name = match["name"] if match else stage_id.replace("_", " ").title()

            self.record["current_stage"] = stage_id
            self.record["current_stage_name"] = stage_name
            self.record["stage_progress"] = 0.0
            self.record["stage_detail"] = detail
            self.record["updated_at"] = time.time()

            # Update remaining list
            found_cur = False
            remaining = []
            for s in self.stage_configs:
                if s["id"] == stage_id:
                    found_cur = True
                    continue
                if found_cur and s["name"] not in self.record["completed_stages"]:
                    remaining.append(s["name"])
            self.record["remaining_stages"] = remaining

            self.record["overall_progress"] = self._calculate_overall_progress()
            self._persist()
            self._edit_telegram_message(force=force_telegram)

    def update_stage(self, stage_id=None, progress=None, detail=None, force_telegram=False):
        """Update progress percentage (0.0 to 1.0) and operation detail within current stage."""
        with self.lock:
            if stage_id and stage_id != self.record.get("current_stage"):
                self.start_stage(stage_id, detail=detail, force_telegram=force_telegram)
                return

            if progress is not None:
                self.record["stage_progress"] = max(0.0, min(1.0, float(progress)))

            if detail is not None:
                self.record["stage_detail"] = str(detail)

            self.record["updated_at"] = time.time()
            self.record["overall_progress"] = self._calculate_overall_progress()
            self._persist()
            self._edit_telegram_message(force=force_telegram)

    def complete_stage(self, stage_id, detail=None, force_telegram=True):
        """Mark a stage as successfully completed."""
        with self.lock:
            match = next((s for s in self.stage_configs if s["id"] == stage_id), None)
            stage_name = match["name"] if match else stage_id.replace("_", " ").title()

            if stage_name not in self.record["completed_stages"]:
                self.record["completed_stages"].append(stage_name)

            self.record["stage_progress"] = 1.0
            if detail is not None:
                self.record["stage_detail"] = str(detail)

            # Remove from remaining
            self.record["remaining_stages"] = [r for r in self.record["remaining_stages"] if r != stage_name]
            self.record["updated_at"] = time.time()
            self.record["overall_progress"] = self._calculate_overall_progress()
            self._persist()
            self._edit_telegram_message(force=force_telegram)

    def update_batch_items(self, processed, total=None, current_title=None,
                           current_progress=None, detail=None, force_telegram=False):
        """Update batch counts and current item progress."""
        with self.lock:
            if total is not None:
                self.record["total_items"] = int(total)
            self.record["processed_items"] = int(processed)
            if current_title is not None:
                self.record["current_item_title"] = str(current_title)
            if current_progress is not None:
                self.record["current_item_progress"] = max(0, min(100, int(current_progress)))
            if detail is not None:
                self.record["stage_detail"] = str(detail)

            # If inside batch_processing stage, compute fractional progress from items
            if self.record.get("current_stage") == "batch_processing" and self.record.get("total_items"):
                t = float(self.record["total_items"])
                p = float(self.record.get("processed_items", 0))
                c_prog = float(self.record.get("current_item_progress", 0) or 0) / 100.0
                stage_frac = (p + c_prog) / t if t > 0 else 0.0
                self.record["stage_progress"] = max(0.0, min(1.0, stage_frac))

            self.record["updated_at"] = time.time()
            self.record["overall_progress"] = self._calculate_overall_progress()
            self._persist()
            self._edit_telegram_message(force=force_telegram)

    def set_status(self, status, detail=None, force_telegram=True):
        """Set task state (WAITING_CONFIRMATION, PAUSED, CANCELLED, etc.)."""
        with self.lock:
            self.record["status"] = str(status).upper()
            if detail:
                self.record["stage_detail"] = str(detail)
            self.record["updated_at"] = time.time()
            self.record["overall_progress"] = self._calculate_overall_progress()
            self._persist()
            self._edit_telegram_message(force=force_telegram)

    def finish(self, status="COMPLETED", error=None, force_telegram=True):
        """Finalize the task with terminal status."""
        with self.lock:
            clean_status = str(status).upper()
            self.record["status"] = clean_status
            self.record["finished_at"] = time.time()
            self.record["updated_at"] = time.time()
            if error:
                self.record["error"] = str(error)

            if clean_status == "COMPLETED":
                # Terminal status must not manufacture completion of work that
                # never ran (for example, an upload task with an empty queue).
                completed = set(self.record.get("completed_stages", []))
                all_stages_completed = all(s["name"] in completed for s in self.stage_configs)
                if all_stages_completed:
                    self.record["remaining_stages"] = []
                    self.record["overall_progress"] = 100.0
                    self.record["stage_progress"] = 1.0
                    if self.record.get("total_items"):
                        self.record["processed_items"] = self.record["total_items"]
                else:
                    # Keep the last evidence-backed percentage and stage list.
                    self.record["overall_progress"] = min(
                        99.0, float(self.record.get("overall_progress", 0.0))
                    )
            elif clean_status == "NO_ELIGIBLE_CLIPS":
                self.record["overall_progress"] = min(
                    99.0, float(self.record.get("overall_progress", 0.0))
                )
            else:
                # Failed or cancelled must never be 100%
                self.record["overall_progress"] = min(99.0, self.record.get("overall_progress", 0.0))

            self._persist()
            self._edit_telegram_message(force=force_telegram)

    def snapshot(self):
        """Return a copy of the current progress record."""
        with self.lock:
            return copy.deepcopy(self.record)


# Telegram API helpers with graceful error suppression
def _api_send_message(chat_id, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or not chat_id:
        return None
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": str(chat_id), "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                return data["result"]["message_id"]
    except Exception:
        pass
    return None


def _api_edit_message(chat_id, message_id, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or not chat_id or not message_id:
        return False, None
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    payload = json.dumps({
        "chat_id": str(chat_id),
        "message_id": int(message_id),
        "text": text,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return bool(data.get("ok")), message_id
    except urllib.error.HTTPError as exc:
        # Ignore Telegram's "message is not modified" error
        try:
            err_body = exc.read().decode("utf-8")
            if "message is not modified" in err_body:
                return True, message_id
            if "message to edit not found" in err_body or exc.code == 400:
                # Fallback: re-send a new progress message
                new_id = _api_send_message(chat_id, text)
                return bool(new_id), new_id
        except Exception:
            pass
    except Exception:
        pass
    return False, None


# Public registry functions
def start_task_progress(task_id, task_type="prepare", *, total_items=None,
                        chat_id=None, task_name=None, send_telegram=True):
    """Initialize a new progress tracker for a task."""
    with _TRACKER_LOCK:
        tracker = ProgressTracker(
            task_id=task_id,
            task_type=task_type,
            total_items=total_items,
            chat_id=chat_id,
            task_name=task_name,
        )
        _TRACKER_REGISTRY[str(task_id)] = tracker
        tracker._persist()
        if send_telegram and chat_id:
            tracker.send_initial_telegram_message()
        return tracker


def get_tracker(task_id):
    """Retrieve an active or registered progress tracker."""
    with _TRACKER_LOCK:
        task_id = str(task_id)
        if task_id in _TRACKER_REGISTRY:
            return _TRACKER_REGISTRY[task_id]
        # Check persisted file
        p_file = PROGRESS_DIR / f"{task_id}.json"
        if p_file.exists():
            try:
                data = json.loads(p_file.read_text(encoding="utf-8"))
                tracker = ProgressTracker(
                    task_id=task_id,
                    task_type=data.get("task_type", "prepare"),
                    chat_id=data.get("telegram_chat_id"),
                    task_name=data.get("task_name"),
                )
                _TRACKER_REGISTRY[task_id] = tracker
                return tracker
            except Exception:
                pass
        return None


def get_task_progress(task_id):
    """Retrieve raw progress dictionary for a task."""
    tracker = get_tracker(task_id)
    if tracker:
        return tracker.snapshot()
    p_file = PROGRESS_DIR / f"{task_id}.json"
    if p_file.exists():
        try:
            return json.loads(p_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def get_active_progress():
    """Retrieve the progress dictionary of the most recently active non-terminal task."""
    with _TRACKER_LOCK:
        for tid, tracker in reversed(list(_TRACKER_REGISTRY.items())):
            snap = tracker.snapshot()
            if snap.get("status") in {"RUNNING", "WAITING_CONFIRMATION", "PAUSED", "QUEUED"}:
                return snap

    if PROGRESS_DIR.exists():
        try:
            files = sorted(PROGRESS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
            for f in files:
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("status") in {"RUNNING", "WAITING_CONFIRMATION", "PAUSED", "QUEUED"}:
                    return data
        except Exception:
            pass
    return None
