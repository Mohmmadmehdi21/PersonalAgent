"""Single-instance Telegram long poller; command parsing never evaluates input."""
import json
import os
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path

from env_config import load_project_env
from telegram_bot.access import AccessConfigurationError, get_owner_ids, parse_owner_ids
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


OFFSET_PATH = Path(os.getenv("TELEGRAM_OFFSET_FILE", "data/telegram_offset.json"))


def _load_offset(path=OFFSET_PATH):
    try:
        p = Path(path)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            off = data.get("offset")
            if isinstance(off, int) and off > 0:
                return off
    except Exception:
        pass
    return None


def _save_offset(offset, path=OFFSET_PATH):
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"offset": offset, "updated_at": time.time()}), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        pass


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
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
            err_json = json.loads(err_body)
            desc = err_json.get("description", "").lower()
            if "query is too old" in desc or "query id is invalid" in desc:
                raise RuntimeError("stale_callback") from None
        except RuntimeError:
            raise
        except Exception:
            pass
        raise RuntimeError("telegram_api_error") from None
    except Exception:
        raise RuntimeError("telegram_api_error") from None
    if not payload.get("ok"):
        raise RuntimeError("telegram_api_error")
    return payload.get("result")


def run_bot(*, token=None, owner_ids_raw=None, api_call=None, reply_sender=None, lock=None,
            sleep=time.sleep, stop_requested=None, offset_path=None):
    """Run until interrupted; injected API and sender allow offline tests."""
    load_project_env()
    token = (token if token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
    if not token:
        raise AccessConfigurationError("Set TELEGRAM_BOT_TOKEN before starting the Telegram bot.")
    owner_ids = parse_owner_ids(owner_ids_raw) if owner_ids_raw is not None else get_owner_ids()
    api_call = api_call or _api_call
    if reply_sender is None:
        from notify import send_telegram_to
        reply_sender = send_telegram_to
    lock = lock or PollLock()
    if not lock.acquire():
        raise RuntimeError("Another Telegram polling process is already active, or its lock cannot be verified.")
    try:
        from telegram_bot.control import reconcile_tasks
        reconcile_tasks()
    except Exception:
        pass

    offset_file = Path(offset_path) if offset_path else Path(os.getenv("TELEGRAM_OFFSET_FILE", "data/telegram_offset.json"))
    offset = _load_offset(offset_file)
    initial_poll = (offset is None)

    answered_callbacks = set()
    callback_order = deque()
    try:
        from telegram_bot.lifecycle import (
            reconcile_lifecycle_on_boot,
            get_state,
            start_scheduler,
            stop_scheduler,
            is_exit_requested,
            reset_exit_request,
            STATE_RUNNING,
        )
        reset_exit_request()
        reconcile_lifecycle_on_boot()
        if get_state() == STATE_RUNNING:
            start_scheduler()

        while not ((stop_requested and stop_requested()) or is_exit_requested()):
            try:
                params = {"timeout": 30, "allowed_updates": json.dumps(["message", "edited_message", "callback_query"])}
                if offset is not None:
                    params["offset"] = offset
                updates = api_call(token, "getUpdates", params, 40) or []
                for update in updates:
                    offset = int(update.get("update_id", 0)) + 1
                    _save_offset(offset, offset_file)

                    callback = update.get("callback_query") if isinstance(update, dict) else None
                    callback_id = callback.get("id") if isinstance(callback, dict) else None
                    cb_data = callback.get("data") if callback else ""

                    if callback_id and callback_id in answered_callbacks:
                        try:
                            api_call(token, "answerCallbackQuery", {"callback_query_id": callback_id,
                                                                      "text": "This button was already handled."}, 10)
                        except Exception:
                            pass
                        continue

                    is_stale = False
                    if callback_id:
                        answered_callbacks.add(callback_id)
                        callback_order.append(callback_id)
                        if len(callback_order) > 512:
                            answered_callbacks.discard(callback_order.popleft())
                        try:
                            api_call(token, "answerCallbackQuery", {"callback_query_id": callback_id}, 10)
                        except RuntimeError as exc:
                            if str(exc) == "stale_callback":
                                is_stale = True
                            else:
                                try:
                                    from logger import log_warn
                                    log_warn("[Telegram] Could not acknowledge an inline button callback.")
                                except Exception:
                                    pass
                        except Exception:
                            try:
                                from logger import log_warn
                                log_warn("[Telegram] Could not acknowledge an inline button callback.")
                            except Exception:
                                pass

                    # Dismiss stale exit confirmation from an old session if expired on Telegram
                    if is_stale and cb_data == "life:exit_confirm":
                        try:
                            from logger import log
                            log("[Telegram] Dismissed stale exit confirmation from previous session.")
                        except Exception:
                            pass
                        continue

                    try:
                        reply = handle_update(update, owner_ids)
                        if reply:
                            chat_id, text = reply[:2]
                            markup = reply[2] if len(reply) > 2 else None
                            if markup:
                                try:
                                    reply_sender(chat_id, text, reply_markup=markup)
                                except TypeError:
                                    reply_sender(chat_id, text)
                            else:
                                reply_sender(chat_id, text)
                    except Exception:
                        # One bad update cannot stop polling or leak the incoming payload.
                        if callback_id:
                            try:
                                api_call(token, "answerCallbackQuery", {"callback_query_id": callback_id,
                                                                          "text": "Unable to process this button."}, 10)
                            except Exception:
                                pass
                        continue

                    if is_exit_requested():
                        # Acknowledge offset to Telegram immediately on clean exit
                        if offset is not None:
                            try:
                                api_call(token, "getUpdates", {"offset": offset, "limit": 1, "timeout": 0}, timeout=5)
                            except Exception:
                                pass
                        break

                initial_poll = False
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
        # If exit was requested, ensure offset was confirmed to Telegram so it is never re-delivered
        if is_exit_requested() and offset is not None:
            try:
                api_call(token, "getUpdates", {"offset": offset, "limit": 1, "timeout": 0}, timeout=5)
            except Exception:
                pass
        try:
            from telegram_bot.lifecycle import stop_scheduler
            stop_scheduler()
        except Exception:
            pass
        lock.release()
