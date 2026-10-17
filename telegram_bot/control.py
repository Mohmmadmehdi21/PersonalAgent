"""Persistent, single-worker controls for Telegram-triggered pipeline runs."""
import json
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


def _read():
    try:
        value = json.loads(CONTROL_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


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
        result = callback(data)
        _write(data)
        return result
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def launch(kind, owner_id, chat_id, *, draft_limit=None, scheduler_key=None):
    if kind not in {"prepare", "upload"}:
        raise ValueError("Unsupported task type.")
    task_id = uuid.uuid4().hex[:12]
    def add(data):
        tasks = data.setdefault("tasks", {})
        for task in tasks.values():
            if task.get("status") in {"queued", "running", "waiting_confirmation", "paused", "cancelling"}:
                raise RuntimeError(f"Task {task.get('id')} is already active; only one controlled pipeline task may run at a time.")
        tasks[task_id] = {"id": task_id, "kind": kind, "owner_id": str(owner_id), "chat_id": str(chat_id),
                          "status": "queued", "created_at": time.time(), "clip_ids": []}
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
        data["tasks"][task_id].update(status="running", pid=process.pid, log=str(log_path))
    _mutate(started)
    threading.Thread(target=_watch_process, args=(task_id, process), daemon=True,
                     name=f"telegram-task-{task_id}").start()
    return task_id


def _watch_process(task_id, process):
    code = process.wait()
    task = task_info(task_id) or {}
    if task.get("status") in {"queued", "running", "waiting_confirmation", "paused", "cancelling"}:
        finish(task_id, "failed" if code else "completed", error=f"Worker exited with code {code}." if code else None)


def set_paused(paused):
    _mutate(lambda data: data.update(paused=bool(paused)))


def cancel(task_id, owner_id):
    def apply(data):
        task = data.get("tasks", {}).get(task_id)
        if not task or task.get("owner_id") != str(owner_id):
            return "Task not found."
        if task.get("status") not in {"queued", "running", "waiting_confirmation", "paused", "cancelling"}:
            return f"Task already finished: {task.get('status')}."
        task["cancel_requested"] = True
        task["status"] = "cancelling"
        return "Cancellation requested. It will stop at the next safe checkpoint; an in-flight YouTube upload cannot be interrupted."
    return _mutate(apply)


def confirm(confirm_id, owner_id, approved=True):
    def apply(data):
        confirmation = data.get("confirmation")
        if not confirmation or confirmation.get("id") != confirm_id:
            return "Confirmation not found or already used."
        if confirmation.get("owner_id") != str(owner_id):
            return "Confirmation not found or already used."
        if confirmation.get("expires_at", 0) < time.time():
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
        return "continue"
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
        existing = state.get("confirmation")
        if not existing or existing.get("task_id") != task_id or existing.get("clip_id") != str(clip.get("id")):
            confirmation = {"id": uuid.uuid4().hex[:10], "task_id": task_id, "clip_id": str(clip.get("id")),
                            "owner_id": str(owner_id), "action": "public_upload", "status": "pending",
                            "expires_at": time.time() + 300}
            _mutate(lambda data: (data.update(confirmation=confirmation), data.get("tasks", {}).get(task_id, {}).update(status="waiting_confirmation")))
            def deny_if_pending(data):
                current = data.get("confirmation") or {}
                if current.get("id") == confirmation["id"] and current.get("status") == "pending":
                    current["status"] = "denied"
            try:
                from notify import send_telegram_to
                sent = send_telegram_to(os.getenv("AGENT_CONTROL_CHAT_ID", ""),
                                        f"Public upload confirmation required\nClip: {clip.get('title', clip.get('id'))}\nTask: {task_id}\nReply /confirm {confirmation['id']} or /deny {confirmation['id']} within 5 minutes.")
                if not sent:
                    _mutate(deny_if_pending)
            except Exception:
                _mutate(deny_if_pending)
        while True:
            current = _read()
            task = current.get("tasks", {}).get(task_id, {})
            conf = current.get("confirmation", {})
            if task.get("cancel_requested"):
                return "cancel"
            if current.get("paused"):
                break
            if conf.get("task_id") == task_id and conf.get("clip_id") == str(clip.get("id")):
                if conf.get("status") == "approved":
                    _mutate(lambda data: data.update(confirmation=None))
                    return "continue"
                if conf.get("status") in {"denied", "expired"} or conf.get("expires_at", 0) < time.time():
                    _mutate(lambda data: data.update(confirmation=None))
                    return "denied"
            time.sleep(.5)
            if state.get("paused"):
                break
        # Pause clears the in-memory confirmation only after a final state check; resume reissues if needed.


def finish(task_id, status, *, error=None):
    def apply(data):
        task = data.get("tasks", {}).get(task_id)
        if task:
            task.update(status=status, finished_at=time.time())
            if error:
                task["error"] = str(error)[:400]
        if data.get("confirmation", {}).get("task_id") == task_id:
            data["confirmation"] = None
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
    if not task or task.get("owner_id") != str(owner_id):
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
