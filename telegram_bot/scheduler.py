"""Persistent, once-per-day Telegram bot scheduler; never performs public uploads."""
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

STATE_FILE = Path("data/scheduler.json")
LEADER_FILE = Path("work/scheduler.lock")


def _enabled(name, default="false"):
    return os.getenv(name, default).strip().lower() in {"true", "1", "t", "yes"}


def _positive_int(name, default, maximum=100):
    raw = os.getenv(name, str(default)).strip()
    if not raw.isdigit() or not 1 <= int(raw) <= maximum:
        raise ValueError(f"{name} must be an integer from 1 to {maximum}.")
    return int(raw)


def _clock(name, default):
    raw = os.getenv(name, default).strip()
    try:
        hour, minute = (int(part) for part in raw.split(":", 1))
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError
        return hour, minute
    except (ValueError, TypeError):
        raise ValueError(f"{name} must use 24-hour HH:MM format.") from None


def _load_state():
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {"jobs": {}}
    except FileNotFoundError:
        return {"jobs": {}}


def _save_state(state):
    import tempfile
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="scheduler.", suffix=".tmp", dir=str(STATE_FILE.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, STATE_FILE)
    finally:
        if os.path.exists(name):
            os.remove(name)


class Scheduler:
    """Small polling scheduler with persistent daily idempotency keys and bounded retries."""

    def __init__(self, *, launch_job=None, analytics_job=None, now=None):
        self.launch_job = launch_job or self._launch_job
        self.analytics_job = analytics_job or self._analytics_job
        self.now = now or (lambda: datetime.now(ZoneInfo(os.getenv("SCHEDULER_TIMEZONE", "Asia/Kolkata"))))
        self.timezone = os.getenv("SCHEDULER_TIMEZONE", "Asia/Kolkata").strip()
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            raise ValueError("SCHEDULER_TIMEZONE must be a valid IANA timezone (for example Asia/Kolkata).") from None
        self.prepare_time = _clock("SCHEDULER_PREPARE_TIME", "09:00")
        self.prepare_enabled = _enabled("SCHEDULER_PREPARE_ENABLED", "true")
        self.source_refresh_enabled = _enabled("SCHEDULER_SOURCE_REFRESH_ENABLED")
        self.source_refresh_time = _clock("SCHEDULER_SOURCE_REFRESH_TIME", "08:00")
        self.analytics_time = _clock("SCHEDULER_ANALYTICS_TIME", "10:00")
        self.analytics_enabled = _enabled("SCHEDULER_ANALYTICS_ENABLED")
        self.daily_draft_limit = _positive_int("DAILY_DRAFT_LIMIT", 2, 4)
        self.max_attempts = _positive_int("SCHEDULER_MAX_ATTEMPTS", 2, 3)
        self.retry_delay = _positive_int("SCHEDULER_RETRY_DELAY_MINUTES", 30, 1440) * 60
        self.analytics_range = os.getenv("SCHEDULER_ANALYTICS_RANGE", "7d").strip()
        if self.analytics_range not in {"7d", "28d"}:
            raise ValueError("SCHEDULER_ANALYTICS_RANGE must be 7d or 28d.")
        if _enabled("SCHEDULER_ENABLED") and not os.getenv("TELEGRAM_CHAT_ID", "").strip():
            raise ValueError("TELEGRAM_CHAT_ID is required when SCHEDULER_ENABLED=true.")
        self._leader_fd = None

    def acquire_leader(self):
        LEADER_FILE.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                self._leader_fd = os.open(str(LEADER_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(self._leader_fd, "w", encoding="utf-8") as stream:
                    self._leader_fd = None
                    json.dump({"pid": os.getpid(), "started_at": time.time()}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                return True
            except FileExistsError:
                try:
                    data = json.loads(LEADER_FILE.read_text(encoding="utf-8"))
                    from lock import is_pid_alive
                    if isinstance(data.get("pid"), int) and not is_pid_alive(data["pid"]):
                        LEADER_FILE.unlink()
                        continue
                except (OSError, ValueError, TypeError):
                    try:
                        if time.time() - LEADER_FILE.stat().st_mtime > 60:
                            LEADER_FILE.unlink()
                            continue
                    except OSError:
                        continue
                return False
        return False

    def release_leader(self):
        try:
            data = json.loads(LEADER_FILE.read_text(encoding="utf-8"))
            if data.get("pid") == os.getpid():
                LEADER_FILE.unlink()
        except (OSError, ValueError, TypeError):
            pass

    def _launch_job(self, kind, key, draft_limit):
        from telegram_bot.access import get_owner_ids
        from telegram_bot.control import launch
        owners = get_owner_ids()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not chat_id:
            raise RuntimeError("TELEGRAM_CHAT_ID is required for scheduled tasks and reports.")
        return launch(kind, next(iter(owners)), chat_id,
                      draft_limit=draft_limit if kind == "prepare" else None,
                      scheduler_key=key)

    def _analytics_job(self, period):
        from telegram_bot.commands import _analytics_report
        from notify import send_telegram_to
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not chat_id:
            raise RuntimeError("TELEGRAM_CHAT_ID is required for scheduled analytics reports.")
        summary = _analytics_report([period])
        if not send_telegram_to(chat_id, f"Scheduled YouTube analytics ({period})\n{summary}"):
            raise RuntimeError("Telegram analytics report could not be delivered.")
        return "sent"

    def _source_refresh_job(self):
        """Refresh the curated long-video dataset, then append only unseen sources."""
        commands = (
            (sys.executable, "make_sources.py"),
            (sys.executable, "merge_sources.py"),
        )
        outputs = []
        for command in commands:
            try:
                result = subprocess.run(
                    command, check=False, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=60 * 60,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"{command[1]} timed out after one hour.") from exc
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "No details returned.").strip()
                raise RuntimeError(f"{command[1]} exited with code {result.returncode}: {detail[-1200:]}")
            outputs.append((result.stdout or "").strip())
        return "\n".join(line for line in outputs if line)

    def _paused(self):
        try:
            from telegram_bot.control import _read as read_control
            return bool(read_control().get("paused"))
        except Exception:
            return False

    def _notify_failure(self, key, error):
        try:
            from notify import send_telegram_to
            chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
            if chat_id:
                send_telegram_to(chat_id, f"Scheduled task {key} failed. Check /health and /logs.")
        except Exception:
            pass
        try:
            from logger import log_error, mask_secrets
            log_error(f"[Scheduler] {key} failed: {mask_secrets(error)}")
        except Exception:
            pass

    def _finalize_worker_tasks(self, state, now_ts):
        from telegram_bot.control import task_info
        for key, row in state.setdefault("jobs", {}).items():
            if row.get("status") not in {"running", "starting"} or not row.get("task_id"):
                continue
            task = task_info(row["task_id"])
            if not task or task.get("status") in {"queued", "running", "waiting_confirmation", "paused", "cancelling"}:
                continue
            status = task.get("status")
            row.update(status="completed" if status == "completed" else "failed",
                       finished_at=now_ts, error=task.get("error"))
            if row.get("kind") == "prepare":
                row["draft_ids"] = list(dict.fromkeys(row.get("draft_ids", []) + task.get("clip_ids", [])))
            if row["status"] == "failed":
                row["failed_at"] = now_ts
                row["retryable"] = _retryable(task.get("error", status), row.get("kind"))
                self._notify_failure(key, task.get("error", status))

    def _run_due(self, state, key, kind, period, now_ts):
        row = state["jobs"].get(key)
        if row and row.get("status") in {"starting", "running", "completed", "disabled"}:
            return
        if self._paused():
            return
        attempt = int(row.get("attempts", 0)) + 1 if row else 1
        if row and (attempt > self.max_attempts or now_ts - float(row.get("failed_at", 0)) < self.retry_delay):
            return
        next_row = dict(row or {})
        next_row.update(kind=kind, status="starting", attempts=attempt,
                        started_at=now_ts, scheduled_key=key)
        state["jobs"][key] = next_row
        _save_state(state)  # Durable claim before external work starts.
        try:
            if kind == "analytics":
                result = self.analytics_job(period)
                state["jobs"][key].update(status="completed", result=str(result), finished_at=now_ts)
            elif kind == "source_refresh":
                result = self._source_refresh_job()
                state["jobs"][key].update(status="completed", result=str(result)[-2000:], finished_at=now_ts)
            else:
                remaining = max(0, self.daily_draft_limit - len(state["jobs"][key].get("draft_ids", [])))
                if remaining == 0:
                    state["jobs"][key].update(status="failed", retryable=False,
                                               error="Daily draft limit was already reached.")
                    _save_state(state)
                    return
                task_id = self.launch_job(kind, key, remaining)
                state["jobs"][key].update(status="running", task_id=task_id)
        except Exception as exc:
            state["jobs"][key].update(status="failed", failed_at=now_ts, error=str(exc)[:300],
                                       retryable=_retryable(exc, kind))
            self._notify_failure(key, exc)
        _save_state(state)

    def _retry_or_recover(self, state, now_ts):
        for key, row in list(state.setdefault("jobs", {}).items()):
            if row.get("status") == "starting" and not row.get("task_id") and row.get("kind") == "prepare":
                try:
                    from telegram_bot.control import _read as read_control
                    task = next((t for t in read_control().get("tasks", {}).values()
                                 if t.get("scheduler_key") == key), None)
                except Exception:
                    task = None
                if task:
                    row.update(task_id=task["id"], status="running")
                elif now_ts - float(row.get("started_at", now_ts)) > 120:
                    row.update(status="failed", failed_at=now_ts, retryable=True,
                               error="Interrupted before task ID was recorded; no matching worker task exists.")
            if (row.get("status") == "starting" and row.get("kind") == "source_refresh"
                    and now_ts - float(row.get("started_at", now_ts)) > 3600):
                row.update(status="failed", failed_at=now_ts, retryable=True,
                           error="Interrupted during source refresh; it is safe to retry.")
            if row.get("status") == "failed" and row.get("retryable"):
                scheduled = row.get("scheduled_key", "")
                scheduled_date = scheduled.split(":", 1)[-1]
                # Only retry when this scheduled day is still current, avoiding stale catch-up storms.
                today_str = datetime.fromtimestamp(now_ts, ZoneInfo(self.timezone)).strftime("%Y-%m-%d")
                if scheduled_date == today_str:
                    try:
                        kind = row.get("kind")
                        period = os.getenv("SCHEDULER_ANALYTICS_RANGE", "7d").strip()
                        self._run_due(state, key, kind, period, now_ts)
                    except Exception as exc:
                        self._notify_failure(key, exc)

    def tick(self, now=None):
        instant = now or self.now()
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=ZoneInfo(self.timezone))
        local = instant.astimezone(ZoneInfo(self.timezone))
        timestamp = instant.timestamp()
        state = _load_state()
        self._finalize_worker_tasks(state, timestamp)
        if not _enabled("SCHEDULER_ENABLED"):
            _save_state(state)
            return
        if not self._paused():
            date_key = local.strftime("%Y-%m-%d")
            if self.source_refresh_enabled and (local.hour, local.minute) >= self.source_refresh_time:
                self._run_due(state, f"sources:{date_key}", "source_refresh", None, timestamp)
            prepare_due = (local.hour, local.minute) >= self.prepare_time
            if self.prepare_enabled and prepare_due:
                refresh = state["jobs"].get(f"sources:{date_key}")
                if not self.source_refresh_enabled or (refresh and refresh.get("status") == "completed"):
                    self._run_due(state, f"prepare:{date_key}", "prepare", None, timestamp)
            if self.analytics_enabled and (local.hour, local.minute) >= self.analytics_time:
                period = os.getenv("SCHEDULER_ANALYTICS_RANGE", "7d").strip()
                if period in {"7d", "28d"}:
                    self._run_due(state, f"analytics:{date_key}", "analytics", period, timestamp)
        self._retry_or_recover(state, timestamp)
        _save_state(state)


def run_scheduler(*, scheduler=None, stop_event=None, sleep=time.sleep, interval=15):
    """Run schedule polling in a daemon thread owned by the Telegram poller."""
    scheduler = scheduler or Scheduler()
    if not scheduler.acquire_leader():
        return
    try:
        while not (stop_event and stop_event.is_set()):
            try:
                scheduler.tick()
            except Exception as exc:
                try:
                    from logger import log_error, mask_secrets
                    log_error(f"[Scheduler] Tick failed safely: {mask_secrets(exc)}")
                except Exception:
                    pass
            sleep(interval)
    finally:
        scheduler.release_leader()


def _retryable(error, kind):
    """Retry only known transient read/preparation failures, never auth/quota/config errors."""
    try:
        from retry import classify_error
        if kind == "analytics":
            service, operation = "youtube", "read"
        elif kind == "source_refresh":
            service, operation = "download", "download"
        else:
            service, operation = "groq", "generate"
        classification = classify_error(RuntimeError(str(error)), service=service, operation=operation)
        return classification.retryable and classification.category in {
            "network", "timeout", "transient_http", "rate_limited"
        }
    except Exception:
        return False
