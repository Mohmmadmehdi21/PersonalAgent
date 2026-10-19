"""Persistent, single-worker controls for Telegram-triggered pipeline runs."""
import json
import copy
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

CONTROL_FILE = Path("data/telegram_control.json")
LOG_DIR = Path("logs")
_MUTATE_THREAD_LOCK = threading.RLock()


class DuplicateTaskRequest(RuntimeError):
    """A callback request key has already created a task."""
    def __init__(self, task_id, status):
        self.task_id = str(task_id)
        self.status = str(status)
        super().__init__("This Telegram action was already accepted.")


def _read():
    try:
        value = json.loads(CONTROL_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _confirmation(data):
    """Return only a structurally usable confirmation record."""
    value = data.get("confirmation") if isinstance(data, dict) else None
    return value if isinstance(value, dict) else None


def _confirmation_belongs_to(data, task_id):
    value = _confirmation(data)
    return bool(value and str(value.get("task_id", "")) == str(task_id))


def _write(data):
    CONTROL_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="telegram_control.", suffix=".tmp", dir=str(CONTROL_FILE.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, CONTROL_FILE)
    finally:
        if os.path.exists(name):
            os.remove(name)


def _mutate(callback):
    # The file lock protects other processes; this lock prevents same-process
    # threads from contending on Windows' O_EXCL lock-file semantics.
    with _MUTATE_THREAD_LOCK:
        return _mutate_with_file_lock(callback)


def _mutate_with_file_lock(callback):
    lock = CONTROL_FILE.with_suffix(".lock")
    deadline = time.time() + 10
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"pid": os.getpid(), "at": time.time()}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 30:
                    lock.unlink()
                    continue
            except OSError:
                continue
            if time.time() >= deadline:
                raise RuntimeError("Telegram task state is busy; retry shortly.")
            time.sleep(.05)
    try:
        data = _read()
        before_tasks = copy.deepcopy(data.get("tasks", {})) if isinstance(data, dict) else {}
        result = callback(data)
        tasks = data.get("tasks", {}) if isinstance(data, dict) else {}
        if isinstance(tasks, dict):
            now = time.time()
            for task_id, task in tasks.items():
                if isinstance(task, dict) and task != before_tasks.get(task_id):
                    task["updated_at"] = now
        _write(data)
        return result
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def reconcile_tasks(data=None):
    """Reconcile active tasks whose processes are no longer alive."""
    def _do_reconcile(state):
        tasks = state.setdefault("tasks", {})
        changed = False
        now_ts = time.time()
        for task in tasks.values():
            if task.get("status") in {"queued", "running", "waiting_confirmation", "paused", "cancelling"}:
                pid = task.get("pid")
                try:
                    from lock import is_pid_alive
                    alive = bool(pid and is_pid_alive(pid))
                except Exception:
                    alive = False
                if not alive:
                    task["status"] = "cancelled" if task.get("cancel_requested") else "failed"
                    task["error"] = "Worker process terminated unexpectedly; reconciled on restart."
                    task["finished_at"] = now_ts
                    changed = True
                    if _confirmation_belongs_to(state, task.get("id")):
                        state["confirmation"] = None
        return changed

    if data is not None:
        return _do_reconcile(data)
    return _mutate(_do_reconcile)


def launch(kind, owner_id, chat_id, *, draft_limit=None, scheduler_key=None, request_key=None):
    if kind not in {"prepare", "upload"}:
        raise ValueError("Unsupported task type.")
    if request_key is not None and (not isinstance(request_key, str) or not request_key or len(request_key) > 256):
        raise ValueError("Invalid task request key.")
    task_id = uuid.uuid4().hex[:12]
    def add(data):
        tasks = data.setdefault("tasks", {})
        if request_key:
            prior = next((task for task in tasks.values()
                          if isinstance(task, dict) and task.get("request_key") == request_key), None)
            if prior:
                raise DuplicateTaskRequest(prior.get("id", "unknown"), prior.get("status", "unknown"))
        reconcile_tasks(data)
        tasks = data.setdefault("tasks", {})
        for task in tasks.values():
            if task.get("status") in {"queued", "running", "waiting_confirmation", "paused", "cancelling"}:
                raise RuntimeError(f"Task {task.get('id')} is already active; only one controlled pipeline task may run at a time.")
        tasks[task_id] = {"id": task_id, "kind": kind, "owner_id": str(owner_id), "chat_id": str(chat_id),
                          "status": "queued", "created_at": time.time(), "updated_at": time.time(), "clip_ids": []}
        if request_key:
            tasks[task_id]["request_key"] = request_key
        if scheduler_key:
            tasks[task_id]["scheduler_key"] = scheduler_key
        return tasks[task_id]
    _mutate(add)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"telegram_task_{task_id}.log"
    stream = log_path.open("ab")
    env = os.environ.copy()
    env.update({"AGENT_CONTROL_TASK_ID": task_id, "AGENT_CONTROL_OWNER_ID": str(owner_id),
                "AGENT_CONTROL_CHAT_ID": str(chat_id)})
    if draft_limit is not None:
        env["CLIPS_PER_DAY"] = str(max(1, min(4, int(draft_limit))))
    command = [sys.executable, "main.py", "prepare" if kind == "prepare" else "render"]
    options = {"cwd": str(Path.cwd()), "env": env, "stdin": subprocess.DEVNULL,
               "stdout": stream, "stderr": subprocess.STDOUT, "close_fds": True}
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        options["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **options)
    except Exception:
        stream.close()
        def fail(data):
            data["tasks"][task_id]["status"] = "failed"
            data["tasks"][task_id]["finished_at"] = time.time()
        _mutate(fail)
        raise
    stream.close()
    def started(data):
        data["tasks"][task_id].update(status="running", pid=process.pid, started_at=time.time(), log=str(log_path))
    _mutate(started)
    threading.Thread(target=_watch_process, args=(task_id, process), daemon=True,
                     name=f"telegram-task-{task_id}").start()
    return task_id


def _watch_process(task_id, process):
    code = process.wait()
    task = task_info(task_id) or {}
    if task.get("status") in {"queued", "running", "waiting_confirmation", "paused", "cancelling"}:
        if task.get("cancel_requested"):
            status = "cancelled"
            err = None
        elif code != 0:
            status = "failed"
            err = f"Worker exited with code {code}."
        else:
            status = "failed"
            err = "Worker exited without recording a final task result."
        finish(task_id, status, error=err)
        chat_id = task.get("chat_id")
        if chat_id and not (task.get("kind") == "prepare" and status in {"completed", "failed"}):
            try:
                from notify import send_telegram_to
                kind = task.get("kind", "task")
                if status == "cancelled":
                    send_telegram_to(chat_id, f"🛑 Task {task_id} ({kind}) was cancelled.")
                elif status == "completed":
                    send_telegram_to(chat_id, f"✅ Task {task_id} ({kind}) completed successfully.")
                else:
                    send_telegram_to(chat_id, f"❌ Task {task_id} ({kind}) failed ({err or 'unknown error'}). Use /logs or /task {task_id} for details.")
            except Exception:
                pass
    task = task_info(task_id) or {}
    if task.get("kind") == "prepare":
        if task.get("status") == "completed":
            _notify_preparation_completion(task)
        elif task.get("status") == "failed" and not task.get("preparation_failure_notification_attempted_at"):
            _notify_preparation_worker_failure(task)


def _claim_prepare_notification(task_id):
    claimed = {}
    def apply(data):
        task = data.get("tasks", {}).get(task_id)
        if not task or task.get("completion_notification_attempted_at"):
            return
        task["completion_notification_attempted_at"] = time.time()
        claimed.update(task)
    _mutate(apply)
    return claimed or None


def _finish_prepare_notification(task_id, sent, error=None):
    def apply(data):
        task = data.get("tasks", {}).get(task_id)
        if not task:
            return
        task["completion_notification_sent"] = bool(sent)
        if error:
            try:
                from logger import mask_secrets
                task["completion_notification_error"] = mask_secrets(str(error))[:200]
            except Exception:
                task["completion_notification_error"] = "Notification delivery failed."
    _mutate(apply)


def _notify_preparation_completion(task):
    claimed = _claim_prepare_notification(str(task.get("id", "")))
    if not claimed:
        return
    result = claimed.get("result") if isinstance(claimed.get("result"), dict) else {}
    try:
        from logger import mask_secrets
        count = int(result.get("verified_draft_count", 0) or 0)
        text = (f"Preparation task {claimed.get('id')} completed. Verified {count} draft(s) in the queue. "
                "No upload or publication was started.")
        ids = result.get("clip_ids") or []
        if ids:
            text += "\nDraft IDs: " + ", ".join(mask_secrets(str(item)[:70]) for item in ids[:8])
        from notify import send_telegram_to
        sent = bool(send_telegram_to(claimed.get("chat_id"), text))
        _finish_prepare_notification(str(claimed.get("id")), sent,
                                     None if sent else "Telegram did not confirm delivery.")
    except Exception as exc:
        _finish_prepare_notification(str(claimed.get("id")), False, exc)
        try:
            from logger import log_warn, mask_secrets
            log_warn(f"[Telegram] Preparation completion notification failed: {mask_secrets(exc)}")
        except Exception:
            pass


def _notify_preparation_worker_failure(task):
    claimed = _claim_prepare_notification(str(task.get("id", "")))
    if not claimed:
        return
    try:
        from logger import mask_secrets
        detail = mask_secrets(str(claimed.get("error", "Worker failed unexpectedly."))[:200])
        text = f"Preparation task {claimed.get('id')} failed. Reason: {detail}. Check /logs."
        from notify import send_telegram_to
        sent = bool(send_telegram_to(claimed.get("chat_id"), text))
        _finish_prepare_notification(str(claimed.get("id")), sent,
                                     None if sent else "Telegram did not confirm delivery.")
    except Exception as exc:
        _finish_prepare_notification(str(claimed.get("id")), False, exc)
        try:
            from logger import log_warn, mask_secrets
            log_warn(f"[Telegram] Preparation failure notification failed: {mask_secrets(exc)}")
        except Exception:
            pass


def _is_authorized_owner(sender_id, recorded_owner_id=None):
    if recorded_owner_id is not None and str(sender_id) == str(recorded_owner_id):
        return True
    try:
        from telegram_bot.access import get_owner_ids
        owners = get_owner_ids()
        return int(sender_id) in owners
    except Exception:
        return False


def set_paused(paused):
    _mutate(lambda data: data.update(paused=bool(paused)))


def cancel(task_id, owner_id):
    def apply(data):
        task = data.get("tasks", {}).get(task_id)
        if not task or not _is_authorized_owner(owner_id, task.get("owner_id")):
            return "Task not found."
        if task.get("status") not in {"queued", "running", "waiting_confirmation", "paused", "cancelling"}:
            return f"Task already finished: {task.get('status')}."
        task["cancel_requested"] = True
        task["status"] = "cancelling"
        return "Cancellation requested. It will stop at the next safe checkpoint; an in-flight YouTube upload cannot be interrupted."
    return _mutate(apply)


def confirm(confirm_id, owner_id, approved=True):
    def apply(data):
        confirmation = _confirmation(data)
        if not confirmation or str(confirmation.get("id", "")) != str(confirm_id):
            return "Confirmation not found or already used."
        task_id = confirmation.get("task_id")
        tasks = data.get("tasks")
        task = tasks.get(task_id) if isinstance(tasks, dict) and isinstance(task_id, str) else None
        clip_id = confirmation.get("clip_id")
        recorded_owner = confirmation.get("owner_id")
        if (not isinstance(task, dict) or not clip_id or not recorded_owner
                or str(task.get("owner_id", "")) != str(recorded_owner)
                or (task.get("clip_ids") and str(clip_id) not in {str(value) for value in task["clip_ids"]})
                or task.get("status") != "waiting_confirmation"):
            return "Confirmation is no longer attached to a waiting task and clip."
        if not _is_authorized_owner(owner_id, recorded_owner):
            return "Confirmation not found or already used."
        try:
            expires_at = float(confirmation.get("expires_at"))
        except (TypeError, ValueError, OverflowError):
            expires_at = 0
        if not math.isfinite(expires_at) or expires_at <= time.time():
            confirmation["status"] = "expired"
            return "Confirmation expired; the clip remains queued."
        if confirmation.get("status") != "pending":
            return "Confirmation not found or already used."
        confirmation["status"] = "approved" if approved else "denied"
        return f"Upload {confirmation['status']} for clip {confirmation['clip_id']}."
    return _mutate(apply)


def checkpoint(*, clip=None, ask_upload=False):
    task_id = os.getenv("AGENT_CONTROL_TASK_ID", "").strip()
    if not task_id:
        return "denied" if ask_upload else "continue"
    owner_id = os.getenv("AGENT_CONTROL_OWNER_ID", "")
    while True:
        state = _read()
        task = state.get("tasks", {}).get(task_id)
        if not task:
            return "cancel"
        if task.get("cancel_requested"):
            return "cancel"
        if state.get("paused"):
            _mutate(lambda data: data.get("tasks", {}).get(task_id, {}).update(status="paused"))
            time.sleep(1)
            continue
        if task.get("status") != "running":
            _mutate(lambda data: data.get("tasks", {}).get(task_id, {}).update(status="running"))
        if not ask_upload:
            return "continue"
        clip_id = str((clip or {}).get("id", ""))
        if not clip_id:
            return "approval_unavailable"
        existing = _confirmation(state)
        raw_confirmation = state.get("confirmation")
        if raw_confirmation is not None and existing is None:
            return "approval_unavailable"
        created = False
        if existing and (str(existing.get("task_id", "")), str(existing.get("clip_id", ""))) == (task_id, clip_id):
            confirmation = existing
        else:
            if existing:
                # Never overwrite a different task/clip's still-live authorization token.
                return "approval_unavailable"
            candidate = {"id": uuid.uuid4().hex[:10], "task_id": task_id, "clip_id": clip_id,
                         "owner_id": str(owner_id), "action": "public_upload", "status": "pending",
                         "expires_at": time.time() + 300}
            def create_confirmation(data):
                raw_current = data.get("confirmation")
                current = _confirmation(data)
                if raw_current is not None and current is None:
                    return None, False
                if current:
                    if (str(current.get("task_id", "")), str(current.get("clip_id", ""))) == (task_id, clip_id):
                        return current, False
                    return None, False
                current_task = data.get("tasks", {}).get(task_id)
                if (not isinstance(current_task, dict) or current_task.get("cancel_requested")
                        or current_task.get("status") not in {"running", "waiting_confirmation"}):
                    return None, False
                data["confirmation"] = candidate
                current_task["status"] = "waiting_confirmation"
                return candidate, True
            confirmation, created = _mutate(create_confirmation)
            if confirmation is None:
                return "approval_unavailable"
        if created:
            def deny_if_pending(data):
                current = _confirmation(data)
                if current and current.get("id") == confirmation["id"] and current.get("status") == "pending":
                    current["status"] = "denied"
                    current["reason"] = "notification_failed"
            try:
                from notify import send_telegram_to
                reply_markup = {
                    "inline_keyboard": [
                        [
                            {"text": "✅ Confirm Upload", "callback_data": f"/confirm {confirmation['id']}"},
                            {"text": "❌ Deny Upload", "callback_data": f"/deny {confirmation['id']}"}
                        ]
                    ]
                }
                sent = send_telegram_to(os.getenv("AGENT_CONTROL_CHAT_ID", ""),
                                        f"Public upload confirmation required\nClip: {clip.get('title', clip.get('id'))}\nTask: {task_id}\nReply /confirm {confirmation['id']} or /deny {confirmation['id']} within 5 minutes.",
                                        reply_markup=reply_markup)
                if not sent:
                    _mutate(deny_if_pending)
            except Exception:
                _mutate(deny_if_pending)
        while True:
            current = _read()
            task = current.get("tasks", {}).get(task_id, {})
            task = task if isinstance(task, dict) else {}
            conf = _confirmation(current) or {}
            if task.get("cancel_requested"):
                return "cancel"
            if task.get("status") not in {"running", "waiting_confirmation", "paused"}:
                return "approval_unavailable"
            if current.get("paused"):
                break
            if str(conf.get("task_id", "")) == task_id and str(conf.get("clip_id", "")) == clip_id:
                if conf.get("status") == "approved":
                    def claim_approval(data):
                        current_conf = _confirmation(data)
                        if (not current_conf or current_conf.get("id") != conf.get("id")
                                or current_conf.get("status") != "approved"
                                or current_conf.get("task_id") != task_id
                                or str(current_conf.get("clip_id")) != clip_id):
                            return None
                        current_conf["status"] = "consumed"
                        return current_conf.get("owner_id", owner_id)
                    approved_owner = _mutate(claim_approval)
                    if approved_owner is None:
                        return "denied"
                    from upload_auth import issue_upload_approval
                    approval = issue_upload_approval(clip_id, approved_owner)
                    def clear_consumed(data):
                        current_conf = _confirmation(data)
                        if current_conf and current_conf.get("id") == conf.get("id") and current_conf.get("status") == "consumed":
                            data["confirmation"] = None
                    _mutate(clear_consumed)
                    return approval
                try:
                    expires_at = float(conf.get("expires_at"))
                    expired = not math.isfinite(expires_at) or expires_at <= time.time()
                except (TypeError, ValueError, OverflowError):
                    expired = True
                if conf.get("status") in {"denied", "expired"} or expired:
                    notification_failed = conf.get("status") == "denied" and conf.get("reason") == "notification_failed"
                    def mark_expired(data):
                        current_conf = _confirmation(data)
                        if current_conf and current_conf.get("id") == conf.get("id") and current_conf.get("status") == "pending":
                            current_conf["status"] = "expired"
                    _mutate(mark_expired)
                    def clear_resolved(data):
                        current_conf = _confirmation(data)
                        if current_conf and current_conf.get("id") == conf.get("id") and current_conf.get("status") in {"denied", "expired"}:
                            data["confirmation"] = None
                    _mutate(clear_resolved)
                    return "approval_unavailable" if notification_failed else "denied"
            elif not conf:
                return "approval_unavailable"
            time.sleep(.5)
            if state.get("paused"):
                break
        # Pause clears the in-memory confirmation only after a final state check; resume reissues if needed.


def finish(task_id, status, *, error=None, result=None):
    def apply(data):
        task = data.get("tasks", {}).get(task_id)
        if task:
            task.update(status=status, finished_at=time.time())
            if error:
                task["error"] = str(error)[:400]
            if result is not None:
                task["result"] = result
        if _confirmation_belongs_to(data, task_id):
            data["confirmation"] = None
    _mutate(apply)


def record_notification_result(task_id, notification, sent, error=None):
    """Record delivery outcome without changing the pipeline task result."""
    def apply(data):
        task = data.get("tasks", {}).get(str(task_id))
        if not task:
            return
        task[f"{notification}_notification_attempted_at"] = time.time()
        task[f"{notification}_notification_sent"] = bool(sent)
        if error:
            try:
                from logger import mask_secrets
                task[f"{notification}_notification_error"] = mask_secrets(str(error))[:200]
            except Exception:
                task[f"{notification}_notification_error"] = "Telegram notification failed."
    _mutate(apply)


def task_info(task_id):
    return _read().get("tasks", {}).get(task_id)


def associate_clips(task_id, clip_ids):
    def apply(data):
        task = data.get("tasks", {}).get(task_id)
        if task:
            task["clip_ids"] = list(dict.fromkeys(str(value) for value in clip_ids))
    _mutate(apply)


def retry_task(task_id, owner_id, chat_id):
    task = task_info(task_id)
    if not task:
        # Check if task_id is a direct clip ID in the queue
        queue_path = Path(os.getenv("REVIEW_DIR", "review")) / "queue.json"
        from queue_manager import load_queue, save_queue
        queue = load_queue(queue_path)
        matching = [item for item in queue if str(item.get("id")) == str(task_id) or str(item.get("key")) == str(task_id)]
        if matching:
            item = matching[0]
            if item.get("status") in {"failed", "blocked"} and item.get("processing_step") not in {"uploading", "upload"}:
                item["status"] = "pending"
                item.pop("processing_step", None)
                item.pop("processing_started_at", None)
                item.pop("last_error", None)
                save_queue(queue)
                new_task = launch("upload", owner_id, chat_id)
                return f"Requeued clip {item.get('id')}; started upload task {new_task}. Each public upload still requires confirmation."
            return f"Clip {task_id} is not eligible for retry (status: {item.get('status')})."
        return "Task not found."
    if not _is_authorized_owner(owner_id, task.get("owner_id")):
        return "Task not found."
    if task.get("status") not in {"failed", "cancelled", "blocked"}:
        return f"Task is not eligible for retry (status: {task.get('status')})."
    queue_path = Path(os.getenv("REVIEW_DIR", "review")) / "queue.json"
    from queue_manager import load_queue, save_queue
    queue = load_queue(queue_path)
    ids = set(task.get("clip_ids", []))
    retried = []
    for item in queue:
        if str(item.get("id")) not in ids:
            continue
        if item.get("status") == "failed" and item.get("processing_step") not in {"uploading", "upload"}:
            item["status"] = "pending"
            item.pop("processing_step", None)
            item.pop("processing_started_at", None)
            item.pop("last_error", None)
            retried.append(item.get("id"))
    if not retried:
        return "No safely retryable failed clips found. Blocked and needs_review items are preserved."
    save_queue(queue)
    new_task = launch("upload", owner_id, chat_id)
    return f"Requeued {len(retried)} eligible clip(s); started upload task {new_task}. Each public upload still requires confirmation."
