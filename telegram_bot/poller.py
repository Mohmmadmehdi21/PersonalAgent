"""Single-instance Telegram long poller; command parsing never evaluates input."""
import json
import os
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from telegram_bot.access import AccessConfigurationError, parse_owner_ids
from telegram_bot.commands import handle_update

LOCK_PATH = Path("work/telegram_polling.lock")


def _pid_alive(pid):
    try:
        from lock import is_pid_alive
        return is_pid_alive(pid)
    except Exception:
        return True


class PollLock:
    def __init__(self, path=LOCK_PATH):
        self.path = Path(path)
        self.held = False

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump({"pid": os.getpid()}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                self.held = True
                return True
            except FileExistsError:
                try:
                    pid = json.loads(self.path.read_text(encoding="utf-8")).get("pid")
                    if isinstance(pid, int) and pid > 0 and not _pid_alive(pid):
                        self.path.unlink()
                        continue
                except (OSError, ValueError, TypeError):
                    pass
                return False
        return False

    def release(self):
        if self.held:
            try:
                if json.loads(self.path.read_text(encoding="utf-8")).get("pid") == os.getpid():
                    self.path.unlink()
            except (OSError, ValueError, TypeError):
                pass
            self.held = False


def _api_call(token, method, params=None, timeout=40):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode("utf-8") if params else None
    request = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Telegram 409 means another getUpdates consumer owns this bot token.
        if exc.code == 409:
            raise RuntimeError("polling_conflict") from None
        raise RuntimeError("telegram_api_error") from None
    except Exception:
        raise RuntimeError("telegram_api_error") from None
    if not payload.get("ok"):
        raise RuntimeError("telegram_api_error")
    return payload.get("result")


def run_bot(*, token=None, owner_ids_raw=None, api_call=None, reply_sender=None, lock=None,
            sleep=time.sleep, stop_requested=None):
    """Run until interrupted; injected API and sender allow offline tests."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    token = (token if token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
    if not token:
        raise AccessConfigurationError("Set TELEGRAM_BOT_TOKEN before starting the Telegram bot.")
    owner_ids = parse_owner_ids(owner_ids_raw if owner_ids_raw is not None else os.getenv("TELEGRAM_OWNER_IDS", ""))
    api_call = api_call or _api_call
    if reply_sender is None:
        from notify import send_telegram_to
        reply_sender = send_telegram_to
    lock = lock or PollLock()
    if not lock.acquire():
        raise RuntimeError("Another Telegram polling process is already active, or its lock cannot be verified.")
    offset = None
    scheduler_stop = threading.Event()
    scheduler_thread = None
    try:
        if os.getenv("SCHEDULER_ENABLED", "false").strip().lower() in ("true", "1", "t", "yes"):
            from telegram_bot.scheduler import Scheduler, run_scheduler
            scheduler = Scheduler()  # Validate timezone, times, limits and report destination before starting.
            scheduler_thread = threading.Thread(target=run_scheduler,
                                                kwargs={"scheduler": scheduler, "stop_event": scheduler_stop},
                                                daemon=True, name="personal-agent-scheduler")
            scheduler_thread.start()
        while not (stop_requested and stop_requested()):
            try:
                params = {"timeout": 30, "allowed_updates": json.dumps(["message", "edited_message"])}
                if offset is not None:
                    params["offset"] = offset
                updates = api_call(token, "getUpdates", params, 40) or []
                for update in updates:
                    offset = int(update.get("update_id", 0)) + 1
                    try:
                        reply = handle_update(update, owner_ids)
                        if reply:
                            chat_id, text = reply
                            reply_sender(chat_id, text)
                    except Exception:
                        # One bad update cannot stop polling or leak the incoming payload.
                        continue
            except RuntimeError as exc:
                if str(exc) == "polling_conflict":
                    raise RuntimeError("Telegram reports another polling process; stopping this bot.") from None
                try:
                    from logger import log_warn
                    log_warn("[Telegram] API request failed; polling will retry shortly.")
                except Exception:
                    pass
                sleep(3)
            except Exception:
                try:
                    from logger import log_warn
                    log_warn("[Telegram] Polling iteration failed; polling will retry shortly.")
                except Exception:
                    pass
                sleep(3)
    finally:
        scheduler_stop.set()
        if scheduler_thread and scheduler_thread.is_alive():
            scheduler_thread.join(timeout=2)
        lock.release()
