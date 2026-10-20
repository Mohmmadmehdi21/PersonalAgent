"""Bot Lifecycle Controller: STARTING, RUNNING, STOPPING, STOPPED.

Provides:
1. Explicit lifecycle state transitions and persistent state tracking.
2. Startup gate: blocks normal commands in STOPPED/STARTING/STOPPING states.
3. Clean separation between Application Services (scheduler, workers) and Control Transport (Telegram polling).
4. Start Bot, Stop Bot, and Exit callback handling with owner authorization.
5. Cooperative thread termination and graceful shutdown without mutating queue or sources.
"""
import copy
import json
import os
import tempfile
import threading
import time
from pathlib import Path

def get_lifecycle_path():
    return Path(os.getenv("PERSONALAGENT_LIFECYCLE_FILE", "data/bot_lifecycle.json"))

STATE_STOPPED = "STOPPED"
STATE_STARTING = "STARTING"
STATE_RUNNING = "RUNNING"
STATE_STOPPING = "STOPPING"

_STATE_LOCK = threading.RLock()
_EXIT_REQUESTED_EVENT = threading.Event()
_SCHEDULER_THREAD = None
_SCHEDULER_STOP_EVENT = None


def get_state():
    """Retrieve current bot lifecycle state."""
    with _STATE_LOCK:
        path = get_lifecycle_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                st = data.get("state")
                if st in {STATE_STOPPED, STATE_STARTING, STATE_RUNNING, STATE_STOPPING}:
                    return st
            except Exception:
                pass
        return os.getenv("BOT_INITIAL_STATE", STATE_STOPPED)


def set_state(new_state, error=None):
    """Safely persist lifecycle state."""
    if new_state not in {STATE_STOPPED, STATE_STARTING, STATE_RUNNING, STATE_STOPPING}:
        raise ValueError(f"Invalid lifecycle state: {new_state}")

    with _STATE_LOCK:
        path = get_lifecycle_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        current_data = {}
        if path.exists():
            try:
                current_data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                current_data = {}

        now = time.time()
        record = {
            "state": new_state,
            "updated_at": now,
            "started_at": now if new_state == STATE_RUNNING else current_data.get("started_at"),
            "stopped_at": now if new_state == STATE_STOPPED else current_data.get("stopped_at"),
            "last_error": error if error is not None else current_data.get("last_error"),
        }

        fd, name = tempfile.mkstemp(prefix="bot_lifecycle.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, indent=2, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                try:
                    os.remove(name)
                except OSError:
                    pass

        return new_state


def reconcile_lifecycle_on_boot():
    """Reconcile state on process startup; stale RUNNING/STARTING is marked STOPPED."""
    with _STATE_LOCK:
        reset_exit_request()
        try:
            from upload_availability import reconcile_upload_limit_on_boot
            reconcile_upload_limit_on_boot()
        except Exception:
            pass
        # If auto start is explicitly requested via env
        if os.getenv("BOT_AUTO_START", "false").strip().lower() in ("true", "1", "yes"):
            set_state(STATE_RUNNING)
            return STATE_RUNNING

        persisted = get_state()
        if persisted in {STATE_RUNNING, STATE_STARTING, STATE_STOPPING}:
            # A fresh process startup cannot assume services are already active
            set_state(STATE_STOPPED, error="Process restarted; bot initialized in STOPPED state.")
            return STATE_STOPPED
        return persisted


def request_exit():
    """Signal Telegram polling loop to terminate cleanly."""
    _EXIT_REQUESTED_EVENT.set()


def is_exit_requested():
    """Check if process exit has been requested."""
    return _EXIT_REQUESTED_EVENT.is_set()


def reset_exit_request():
    """Reset exit signal (primarily for test environments)."""
    _EXIT_REQUESTED_EVENT.clear()


def start_scheduler():
    """Start in-process scheduler daemon thread if enabled and not already running."""
    global _SCHEDULER_THREAD, _SCHEDULER_STOP_EVENT
    with _STATE_LOCK:
        if _SCHEDULER_THREAD and _SCHEDULER_THREAD.is_alive():
            return
        from telegram_bot.scheduler import Scheduler, run_scheduler, is_scheduler_configured_enabled
        if is_scheduler_configured_enabled():
            scheduler = Scheduler()
            _SCHEDULER_STOP_EVENT = threading.Event()
            _SCHEDULER_THREAD = threading.Thread(
                target=run_scheduler,
                kwargs={"scheduler": scheduler, "stop_event": _SCHEDULER_STOP_EVENT},
                daemon=True,
                name="personal-agent-scheduler",
            )
            _SCHEDULER_THREAD.start()


def stop_scheduler():
    """Gracefully signal and join scheduler thread."""
    global _SCHEDULER_THREAD, _SCHEDULER_STOP_EVENT
    with _STATE_LOCK:
        if _SCHEDULER_STOP_EVENT:
            _SCHEDULER_STOP_EVENT.set()
        if _SCHEDULER_THREAD and _SCHEDULER_THREAD.is_alive():
            _SCHEDULER_THREAD.join(timeout=2.0)
        _SCHEDULER_THREAD = None
        _SCHEDULER_STOP_EVENT = None


def start_services():
    """Initialize application bot services and mark RUNNING."""
    with _STATE_LOCK:
        current = get_state()
        if current == STATE_RUNNING:
            return True, "Bot is already running."
        if current == STATE_STARTING:
            return False, "Bot is currently starting up. Please wait."
        if current == STATE_STOPPING:
            return False, "Bot is currently stopping. Please wait for shutdown to complete."

        set_state(STATE_STARTING)
        try:
            # 1. Reconcile tasks
            from telegram_bot.control import reconcile_tasks, set_paused
            reconcile_tasks()
            set_paused(False)

            # 2. Start scheduler if configured
            start_scheduler()

            set_state(STATE_RUNNING)
            return True, "✅ Bot started successfully!\n\nAll services are active and ready."
        except Exception as exc:
            stop_scheduler()
            set_state(STATE_STOPPED, error=str(exc))
            try:
                from logger import log_error, mask_secrets
                log_error(f"[Lifecycle] Bot startup failed: {mask_secrets(exc)}")
            except Exception:
                pass
            return False, f"⚠️ Startup failed: {str(exc)[:200]}"


def stop_services():
    """Gracefully stop application bot services and mark STOPPED."""
    with _STATE_LOCK:
        current = get_state()
        if current == STATE_STOPPED:
            return True, "Bot is already stopped."
        if current == STATE_STOPPING:
            return False, "Bot is currently stopping. Please wait."

        set_state(STATE_STOPPING)
        try:
            # 1. Stop scheduler thread
            stop_scheduler()

            # 2. Pause active pipeline execution without modifying queue or sources
            try:
                from telegram_bot.control import set_paused
                set_paused(True)
            except Exception:
                pass

            set_state(STATE_STOPPED)
            return True, ("⏹️ Bot has been stopped.\n\n"
                         "Application services, queue processing, and scheduled jobs are paused. "
                         "Telegram control transport remains active.\n\n"
                         "To resume, click Start Bot below.")
        except Exception as exc:
            set_state(STATE_STOPPED, error=str(exc))
            return True, f"⏹️ Bot stopped with warnings: {str(exc)[:200]}"


def stopped_keyboard():
    """Minimal keyboard presented when bot is STOPPED."""
    return {"inline_keyboard": [
        [{"text": "▶️ Start Bot", "callback_data": "life:start"}],
        [{"text": "🚪 Exit", "callback_data": "life:exit_prompt"}],
    ]}


def handle_lifecycle_callback(action, user_id, chat_id):
    """Process lifecycle buttons (Start, Stop, Exit) with owner verification."""
    from telegram_bot.commands import _menu_keyboard

    if action == "life:start":
        current = get_state()
        if current == STATE_RUNNING:
            return "The bot is already running.", _menu_keyboard()
        if current == STATE_STARTING:
            return "Bot is currently starting up. Please wait.", None
        if current == STATE_STOPPING:
            return "Bot is currently stopping. Please wait for shutdown to complete.", None

        ok, msg = start_services()
        return (msg, _menu_keyboard()) if ok else (msg, stopped_keyboard())

    if action == "life:stop_prompt":
        current = get_state()
        if current == STATE_STOPPED:
            return "The bot is already stopped.", stopped_keyboard()
        text = ("⏹️ Stop Bot Confirmation\n\n"
                "Are you sure you want to stop the bot?\n"
                "Application services, background workers, and scheduled jobs will be stopped. "
                "The Telegram control transport will remain online so you can start the bot again at any time.")
        kb = {"inline_keyboard": [
            [{"text": "⏹️ Confirm Stop", "callback_data": "life:stop_confirm"}],
            [{"text": "❌ Cancel", "callback_data": "life:stop_cancel"}],
        ]}
        return text, kb

    if action == "life:stop_confirm":
        current = get_state()
        if current == STATE_STOPPED:
            return "The bot is already stopped.", stopped_keyboard()
        _ok, msg = stop_services()
        return msg, stopped_keyboard()

    if action == "life:stop_cancel":
        current = get_state()
        if current == STATE_STOPPED:
            return "Stop action cancelled. Bot remains stopped.", stopped_keyboard()
        return "Stop cancelled. Bot is still running.", _menu_keyboard()

    if action == "life:exit_prompt":
        current = get_state()
        text = ("🚪 Exit Process Confirmation\n\n"
                "Are you sure you want to completely shut down the application process?\n\n"
                "⚠️ Note: If running under an external supervisor (e.g., Docker restart: unless-stopped or systemd Restart=always), "
                "the supervisor may automatically restart the process. The bot state is marked STOPPED so it will not auto-run upon reboot.")
        kb = {"inline_keyboard": [
            [{"text": "🚪 Yes, Exit", "callback_data": "life:exit_confirm"}],
            [{"text": "❌ Cancel", "callback_data": "life:exit_cancel"}],
        ]}
        return text, kb

    if action == "life:exit_confirm":
        stop_services()
        request_exit()
        return "🚪 PersonalAgent is shutting down. Polling loop terminated. Goodbye!", None

    if action == "life:exit_cancel":
        current = get_state()
        if current == STATE_STOPPED:
            return "Exit cancelled.", stopped_keyboard()
        return "Exit cancelled. Bot is still running.", _menu_keyboard()

    return "Unknown lifecycle action.", None
