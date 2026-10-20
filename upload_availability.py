"""YouTube Upload Availability Recovery Alert Manager.

Detects when YouTube's uploadLimitExceeded restriction occurs, persists the
restriction state safely, monitors availability via verified legitimate upload
outcomes, and delivers reliable, bounded Telegram recovery alerts.
"""
import copy
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_STATE_FILE = "data/upload_limit_state.json"
MAX_DELIVERY_ATTEMPTS = 5
RETRY_COOLDOWN_SECONDS = 60.0

_LOCK = threading.RLock()


def get_upload_limit_file_path(override_path=None):
    if override_path:
        return Path(override_path)
    return Path(os.getenv("YOUTUBE_UPLOAD_LIMIT_FILE", DEFAULT_STATE_FILE))


def _default_state():
    return {
        "restriction_active": False,
        "restricted_at": None,
        "restricted_iso": None,
        "reason": None,
        "http_status": None,
        "diagnostic_details": None,
        "clip_id": None,
        "clip_title": None,
        "recovery_detected": False,
        "recovered_at": None,
        "recovered_iso": None,
        "recovery_reason": None,
        "recovered_clip_id": None,
        "recovered_video_id": None,
        "alert_pending": False,
        "alert_delivered": False,
        "alert_delivered_at": None,
        "delivery_attempts": 0,
        "last_delivery_attempt_at": None,
        "last_delivery_error": None,
    }


def load_upload_limit_state(file_path=None):
    """Load persistent upload limit state safely."""
    path = get_upload_limit_file_path(file_path)
    with _LOCK:
        if not path.is_file():
            return _default_state()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return _default_state()
            state = _default_state()
            state.update(data)
            return state
        except Exception:
            return _default_state()


def save_upload_limit_state(state, file_path=None):
    """Atomically persist upload limit state."""
    path = get_upload_limit_file_path(file_path)
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="upload_limit.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, indent=2, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


def is_upload_limit_exceeded_error(err):
    """Accurately detect whether an error represents YouTube's uploadLimitExceeded.

    Strictly discriminates against generic 403, quota exhaustion, auth errors,
    analytics failures, or transient network issues.
    """
    if err is None:
        return False

    try:
        from retry import AccountUploadLimitError
        if isinstance(err, AccountUploadLimitError):
            return True
    except Exception:
        pass

    try:
        from retry import youtube_error_details
        details = youtube_error_details(err)
        for detail in details.get("details", []):
            if isinstance(detail, dict) and detail.get("reason") == "uploadLimitExceeded":
                return True
    except Exception:
        pass

    try:
        from retry import classify_error
        classification = classify_error(err, service="youtube", operation="upload")
        if classification.category == "account_upload_limit":
            err_str = (str(err) + " " + getattr(err, "content", "") if hasattr(err, "content") else str(err)).lower()
            if "uploadlimitexceeded" in err_str or "upload limit exceeded" in err_str:
                return True
    except Exception:
        pass

    return False


def record_upload_limit_exceeded(err=None, *, clip_id=None, clip_title=None, details=None, file_path=None):
    """Record an active uploadLimitExceeded restriction."""
    if err is not None and not is_upload_limit_exceeded_error(err):
        return False, "not_upload_limit_error"

    with _LOCK:
        state = load_upload_limit_state(file_path)
        now = time.time()
        now_iso = datetime.now(timezone.utc).isoformat()

        diag = "reason: uploadLimitExceeded"
        http_code = 400
        if isinstance(details, dict):
            http_code = details.get("http_status") or 400
            detail_items = details.get("details", [])
            if detail_items:
                diag = "; ".join(f"{d.get('domain', 'API')}/{d.get('reason', 'unknown')}" for d in detail_items)
            elif details.get("api_message"):
                diag = str(details.get("api_message"))[:150]
        elif err is not None:
            try:
                from retry import youtube_error_details
                dt = youtube_error_details(err)
                http_code = dt.get("http_status") or 400
                if dt.get("details"):
                    diag = "; ".join(f"{d.get('domain', 'API')}/{d.get('reason', 'unknown')}" for d in dt.get("details", []))
            except Exception:
                diag = str(err)[:150]

        try:
            from logger import mask_secrets
            diag = mask_secrets(diag)
        except Exception:
            pass

        state["restriction_active"] = True
        state["restricted_at"] = now
        state["restricted_iso"] = now_iso
        state["reason"] = "uploadLimitExceeded"
        state["http_status"] = http_code
        state["diagnostic_details"] = diag
        state["clip_id"] = str(clip_id) if clip_id is not None else None
        state["clip_title"] = str(clip_title) if clip_title is not None else None

        # Reset any past recovery/alert states for the new occurrence
        state["recovery_detected"] = False
        state["recovered_at"] = None
        state["recovered_iso"] = None
        state["recovery_reason"] = None
        state["recovered_clip_id"] = None
        state["recovered_video_id"] = None
        state["alert_pending"] = False
        state["alert_delivered"] = False
        state["delivery_attempts"] = 0
        state["last_delivery_attempt_at"] = None
        state["last_delivery_error"] = None

        save_upload_limit_state(state, file_path)
        try:
            from logger import log_warn
            log_warn(f"[Upload Availability] Recorded YouTube uploadLimitExceeded restriction for clip '{clip_title or clip_id}'.")
        except Exception:
            pass
        return True, "recorded"


def record_upload_success(*, clip_id=None, youtube_video_id=None, title=None, sender=None, file_path=None):
    """Record a verified legitimate upload success.

    If an uploadLimitExceeded restriction was active, this confirms recovery
    and triggers a reliable Telegram recovery alert.
    """
    with _LOCK:
        state = load_upload_limit_state(file_path)
        if not state.get("restriction_active"):
            return False, "no_active_restriction"

        # Confirmed recovery event!
        now = time.time()
        now_iso = datetime.now(timezone.utc).isoformat()

        state["restriction_active"] = False
        state["recovery_detected"] = True
        state["recovered_at"] = now
        state["recovered_iso"] = now_iso
        state["recovery_reason"] = "legitimate_upload_success"
        state["recovered_clip_id"] = str(clip_id) if clip_id is not None else None
        state["recovered_video_id"] = str(youtube_video_id) if youtube_video_id is not None else None
        state["alert_pending"] = True
        state["alert_delivered"] = False
        state["delivery_attempts"] = 0
        state["last_delivery_attempt_at"] = None
        state["last_delivery_error"] = None

        save_upload_limit_state(state, file_path)
        try:
            from logger import log
            log(f"[Upload Availability] YouTube upload availability confirmed restored by clip '{title or clip_id}' (ID: {youtube_video_id}).")
        except Exception:
            pass

        # Attempt immediate delivery of the recovery alert
        delivered, status = deliver_recovery_alert(sender=sender, file_path=file_path)
        return True, f"recovery_confirmed (delivered={delivered}, status={status})"


def build_recovery_alert_text(state):
    """Format clear, user-facing recovery alert message."""
    recovered_at = state.get("recovered_at")
    time_str = "just now"
    if recovered_at:
        try:
            time_str = datetime.fromtimestamp(recovered_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception:
            time_str = str(state.get("recovered_iso") or "recently")

    clip_info = ""
    clip_title = state.get("clip_title") or state.get("recovered_clip_id")
    if clip_title:
        clip_info = f"\nConfirmed by successful upload at {time_str}."

    return (
        "✅ <b>YouTube Upload Availability Restored</b>\n\n"
        "Your previous YouTube upload limit restriction appears to have cleared.\n"
        f"{clip_info}\n\n"
        "You can now try uploading a new clip.\n\n"
        "Send /upload or use the existing upload workflow to start. "
        "Normal upload confirmation and safety checks still apply.\n\n"
        "<i>Note: Upload availability does not guarantee that every upload will succeed. "
        "Normal YouTube API limits, channel restrictions, and upload confirmations still apply.</i>"
    )


def deliver_recovery_alert(*, sender=None, file_path=None, max_attempts=MAX_DELIVERY_ATTEMPTS):
    """Deliver the recovery alert to Telegram with bounded retries."""
    with _LOCK:
        state = load_upload_limit_state(file_path)
        if not state.get("alert_pending"):
            return False, "no_pending_alert"
        if state.get("alert_delivered"):
            return False, "already_delivered"

        attempts = int(state.get("delivery_attempts", 0))
        if attempts >= max_attempts:
            return False, "max_attempts_exhausted"

        state["delivery_attempts"] = attempts + 1
        state["last_delivery_attempt_at"] = time.time()

        message = build_recovery_alert_text(state)
        chat_id = os.getenv("AGENT_CONTROL_CHAT_ID", "").strip() or os.getenv("TELEGRAM_CHAT_ID", "").strip()

        success = False
        try:
            if sender is not None:
                success = bool(sender(chat_id, message))
            else:
                if not chat_id:
                    try:
                        from logger import log_warn
                        log_warn("[Upload Availability] Cannot deliver recovery alert: neither AGENT_CONTROL_CHAT_ID nor TELEGRAM_CHAT_ID is set.")
                    except Exception:
                        pass
                    state["last_delivery_error"] = "missing_chat_id"
                    save_upload_limit_state(state, file_path)
                    return False, "missing_chat_id"

                from notify import send_telegram_to
                success = bool(send_telegram_to(chat_id, message, parse_mode="HTML"))
        except Exception as exc:
            state["last_delivery_error"] = str(exc)[:200]
            try:
                from logger import log_warn
                log_warn(f"[Upload Availability] Recovery alert delivery threw exception: {exc}")
            except Exception:
                pass
            success = False

        if success:
            state["alert_delivered"] = True
            state["alert_pending"] = False
            state["alert_delivered_at"] = time.time()
            state["last_delivery_error"] = None
            save_upload_limit_state(state, file_path)
            try:
                from logger import log
                log("[Upload Availability] Telegram recovery alert successfully delivered.")
            except Exception:
                pass
            return True, "delivered"
        else:
            state["alert_delivered"] = False
            state["alert_pending"] = True
            if not state.get("last_delivery_error"):
                state["last_delivery_error"] = "Telegram send returned False"
            save_upload_limit_state(state, file_path)
            try:
                from logger import log_warn
                log_warn(f"[Upload Availability] Telegram recovery alert delivery failed (attempt {state['delivery_attempts']}/{max_attempts}).")
            except Exception:
                pass
            return False, "delivery_failed"


def check_and_deliver_pending_recovery_alert(*, sender=None, file_path=None, cooldown=RETRY_COOLDOWN_SECONDS):
    """Check if a pending alert needs retry after cooldown (used by scheduler/tick)."""
    with _LOCK:
        state = load_upload_limit_state(file_path)
        if not state.get("alert_pending") or state.get("alert_delivered"):
            return False, "no_pending_alert"

        attempts = int(state.get("delivery_attempts", 0))
        if attempts >= MAX_DELIVERY_ATTEMPTS:
            return False, "max_attempts_exhausted"

        last_attempt = float(state.get("last_delivery_attempt_at") or 0.0)
        if (time.time() - last_attempt) < cooldown:
            return False, "in_cooldown"

        return deliver_recovery_alert(sender=sender, file_path=file_path)


def reconcile_upload_limit_on_boot(*, sender=None, file_path=None):
    """Reconcile upload limit state on agent/bot boot.

    Preserves active restrictions without assuming unverified recovery,
    and flushes any unconfirmed pending recovery alerts.
    """
    with _LOCK:
        state = load_upload_limit_state(file_path)
        if state.get("restriction_active"):
            try:
                from logger import log
                log(f"[Upload Availability] Active YouTube uploadLimitExceeded restriction preserved from {state.get('restricted_iso')}. Awaiting confirmed recovery.")
            except Exception:
                pass

        if state.get("alert_pending") and not state.get("alert_delivered"):
            deliver_recovery_alert(sender=sender, file_path=file_path)

        return state


def get_upload_limit_summary(file_path=None):
    """Return concise status for health/status diagnostics."""
    state = load_upload_limit_state(file_path)
    if state.get("restriction_active"):
        t_iso = state.get("restricted_iso") or "unknown time"
        return f"RESTRICTED (uploadLimitExceeded recorded at {t_iso}; awaiting confirmed recovery)"
    if state.get("recovery_detected"):
        t_iso = state.get("recovered_iso") or "unknown time"
        return f"AVAILABLE (recovered at {t_iso})"
    return "AVAILABLE (no active upload limit restriction)"
