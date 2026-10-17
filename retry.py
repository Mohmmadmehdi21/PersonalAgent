"""Central error classification, bounded retries, and API client timeouts."""
import json
import os
import random
import re
import socket
import subprocess
import time
from functools import lru_cache
from dataclasses import dataclass

from logger import log_error, log_event, log_warn


class FatalError(Exception):
    """Base for non-retryable errors that require operator attention."""


class QuotaExceededError(FatalError):
    """YouTube API project quota is exhausted."""


class ProviderQuotaError(FatalError):
    """An AI provider reports exhausted credits/quota (not a transient 429)."""


class AuthError(FatalError):
    """Authentication credentials are invalid, expired, or unauthorized."""


class ModelNotFoundError(FatalError):
    """The configured AI model is unavailable or does not exist."""


class ModelConfigurationError(FatalError):
    """The model is unavailable to the account or the request is not supported."""


class NonRetryableAPIError(FatalError):
    """An API rejected a request that retrying unchanged cannot fix."""


class AccountUploadLimitError(NonRetryableAPIError):
    """Account-level upload restriction; stop the whole upload batch."""


class UploadOutcomeUnknownError(FatalError):
    """An upload may have reached YouTube; retrying could create a duplicate."""


class ExternalWriteOutcomeUnknownError(FatalError):
    """A non-upload API write may have committed; do not issue it again blindly."""


class UploadCommittedInterruptedError(FatalError):
    """Upload is confirmed, but interruption occurred during a later side effect."""

    def __init__(self, video_id):
        self.video_id = video_id
        super().__init__(f"Upload {video_id} succeeded, then processing was interrupted.")


class PermanentSourceError(FatalError):
    """A video is private, deleted, copyright-blocked, or unavailable."""


class BotVerificationError(FatalError):
    """YouTube requires bot verification or authenticated browser cookies."""


class RateLimitedError(FatalError):
    """YouTube or API request is blocked due to HTTP 429 rate limiting."""


class RetryExhaustedError(RuntimeError):
    """A transient task failed after its finite retry budget."""


@dataclass(frozen=True)
class ErrorClassification:
    category: str
    retryable: bool
    stop_batch: bool = False
    outcome_unknown: bool = False
    retry_after: float | None = None


def _status_code(err):
    for value in (
        getattr(err, "status_code", None),
        getattr(getattr(err, "response", None), "status_code", None),
        getattr(getattr(err, "resp", None), "status", None),
        getattr(err, "status", None),
        getattr(err, "code", None),
    ):
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    match = re.search(r"\bHTTP(?:\s*Error)?\s*[: ]?\s*(\d{3})\b", str(err), re.IGNORECASE)
    return int(match.group(1)) if match else None


def youtube_error_reasons(err):
    """Extract structured YouTube API reasons from common HttpError shapes."""
    reasons = set()
    details = getattr(err, "error_details", None)
    if isinstance(details, (list, tuple)):
        for detail in details:
            if isinstance(detail, dict) and detail.get("reason"):
                reasons.add(str(detail["reason"]))

    content = getattr(err, "content", None)
    if content:
        try:
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
            payload = json.loads(content) if isinstance(content, str) else content
            for detail in payload.get("error", {}).get("errors", []):
                if detail.get("reason"):
                    reasons.add(str(detail["reason"]))
            status = payload.get("error", {}).get("status")
            if status:
                reasons.add(str(status))
        except (TypeError, ValueError, AttributeError):
            pass
    return reasons


def parse_retry_after(value):
    """Read Retry-After seconds or a human-readable retry interval."""
    if value is None:
        return None
    try:
        number = float(value)
        return max(0.0, number)
    except (TypeError, ValueError):
        match = re.search(r"(?:retry[-_ ]after|retry in|wait)[\s:]*([0-9]+(?:\.[0-9]+)?)", str(value), re.IGNORECASE)
        return float(match.group(1)) if match else None


def is_quota_error(err):
    result = classify_error(err, service="youtube", operation="upload")
    return result.category in {"youtube_quota", "provider_quota"}


def is_auth_error(err):
    return classify_error(err).category == "authentication"


def is_permanent_source_error(err):
    return classify_error(err, service="youtube", operation="download").category == "permanent_source"


def is_bot_verification_error(err):
    return classify_error(err, service="download", operation="download").category == "bot_verification"


def is_rate_limited_error(err):
    return classify_error(err, service="download", operation="download").category == "rate_limited"


def is_download_cooldown_active(state, now=None):
    if not isinstance(state, dict):
        return False
    until = state.get("download_rate_limited_until")
    if until is None:
        return False
    try:
        now_ts = time.time() if now is None else float(now)
        return now_ts < float(until)
    except (TypeError, ValueError):
        return False


def get_download_cooldown_remaining(state, now=None):
    if not isinstance(state, dict):
        return 0.0
    until = state.get("download_rate_limited_until")
    if until is None:
        return 0.0
    try:
        now_ts = time.time() if now is None else float(now)
        return max(0.0, float(until) - now_ts)
    except (TypeError, ValueError):
        return 0.0


def set_download_cooldown(state, cooldown_seconds, now=None):
    if not isinstance(state, dict):
        return
    now_ts = time.time() if now is None else float(now)
    state["download_rate_limited_until"] = now_ts + max(0.0, float(cooldown_seconds))


def classify_error(err, service="generic", operation="api"):
    """Classify errors once so every retry caller shares the same policy.

    `service` accepts `groq`, `youtube`, `download`, or `generic`; operation
    distinguishes an upload whose result may be ambiguous from a safe read.
    """
    if isinstance(err, AccountUploadLimitError):
        return ErrorClassification("account_upload_limit", False, stop_batch=True)
    if isinstance(err, UploadOutcomeUnknownError):
        return ErrorClassification("upload_outcome_unknown", False, stop_batch=True, outcome_unknown=True)
    if isinstance(err, ExternalWriteOutcomeUnknownError):
        return ErrorClassification("external_write_outcome_unknown", False, stop_batch=True, outcome_unknown=True)
    if isinstance(err, UploadCommittedInterruptedError):
        return ErrorClassification("upload_committed_interrupted", False, stop_batch=True)
    if isinstance(err, AuthError):
        return ErrorClassification("authentication", False, stop_batch=True)
    if isinstance(err, ModelNotFoundError):
        return ErrorClassification("model_not_found", False)
    if isinstance(err, QuotaExceededError):
        return ErrorClassification("youtube_quota", False, stop_batch=True)
    if isinstance(err, ProviderQuotaError):
        return ErrorClassification("provider_quota", False)
    if isinstance(err, PermanentSourceError):
        return ErrorClassification("permanent_source", False)
    if isinstance(err, BotVerificationError):
        return ErrorClassification("bot_verification", False)
    if isinstance(err, RateLimitedError):
        return ErrorClassification("rate_limited", False, stop_batch=True)
    if isinstance(err, FatalError):
        return ErrorClassification("non_retryable", False)

    text = str(err).lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    reasons = {reason.lower().replace("’", "'").replace("‘", "'") for reason in youtube_error_reasons(err)}
    status = _status_code(err)
    combined = " ".join((text, " ".join(reasons)))

    # Bot verification / Captcha / Authenticated session challenge
    is_bot_verification = any(token in combined for token in (
        "confirm you're not a bot",
        "confirm you are not a bot",
        "use --cookies-from-browser",
        "use --cookies for the authentication",
        "use --cookies",
        "bot-detection",
        "bot detection",
        "captcha",
    )) or ("sign in" in combined and "bot" in combined)
    if is_bot_verification:
        return ErrorClassification("bot_verification", False)

    if (
        "uploadlimitexceeded" in combined
        or "upload limit exceeded" in combined
        or "exceeded the number of videos" in combined
        or "uploadlimit" in combined
    ):
        return ErrorClassification("account_upload_limit", False, stop_batch=True)

    if any(token in combined for token in (
        "invalid_grant", "invalid api key", "incorrect api key", "api key not valid", "api_key_invalid", "authentication required",
        "unauthorized", "unauthenticated", "invalid credentials", "expired or revoked",
        "keyinvalid", "autherror", "insufficientpermissions", "accountdelegationforbidden",
        "youtubesignuprequired",
    )) or (status == 401 and service != "download") or (status == 403 and service == "generic"):
        return ErrorClassification("authentication", False, stop_batch=True)

    model_missing = any(token in combined for token in (
        "model_not_found", "model not found", "model does not exist", "model_decommissioned",
        "model_decommission", "unknown model", "model is not available",
    )) or ("model" in text and any(token in text for token in (
        "does not exist", "not found", "decommissioned", "unavailable",
    )))
    if service in {"groq", "ai"} and (model_missing or status == 404):
        return ErrorClassification("model_not_found", False)
    if service == "groq" and status == 403:
        return ErrorClassification("model_configuration", False)
    if service == "groq" and status == 400:
        return ErrorClassification("invalid_request", False)

    if any(token in combined for token in (
        "quotaexceeded", "dailylimitexceeded", "quota exceeded", "insufficient_quota",
        "billing hard limit", "credits exhausted",
    )) or (service == "youtube" and status == 403 and "quota" in combined):
        category = "youtube_quota" if service == "youtube" or operation == "upload" else "provider_quota"
        return ErrorClassification(category, False, stop_batch=(category == "youtube_quota"))
    if service in {"groq", "ai"} and status == 402:
        return ErrorClassification("provider_quota", False)

    headers = getattr(getattr(err, "response", None), "headers", {}) or {}
    retry_after = parse_retry_after(headers.get("retry-after") or headers.get("Retry-After"))
    if retry_after is None:
        retry_after = parse_retry_after(text)

    is_rate_limit = any(token in combined for token in (
        "ratelimitexceeded", "userratelimitexceeded", "rate limit exceeded",
        "too many requests", "rate-limited by youtube",
    )) or status == 429

    if is_rate_limit:
        if operation == "write":
            return ErrorClassification("external_write_outcome_unknown", False, stop_batch=True, outcome_unknown=True)
        if service == "download" or operation == "download":
            return ErrorClassification("rate_limited", False, stop_batch=True, retry_after=retry_after)
        return ErrorClassification("rate_limited", True, retry_after=retry_after)

    if operation == "download" and any(pattern in text for pattern in (
        "private video", "video unavailable", "this video has been removed", "has been terminated",
        "sign in to confirm your age", "members-only content", "copyright claim",
        "this video is not available", "inappropriate for some users",
        "premieres in", "live event will begin in", "this live stream has ended",
        "requires purchase", "paid content",
    )):
        return ErrorClassification("permanent_source", False)
    if (service == "download" or operation == "download") and status in (400, 401, 403, 404, 410, 451):
        return ErrorClassification("permanent_source", False)

    exc_name = type(err).__name__.lower()
    module_name = type(err).__module__.lower()
    is_timeout = isinstance(err, (TimeoutError, socket.timeout)) or "timeout" in exc_name
    is_connection = isinstance(err, (ConnectionError, socket.gaierror)) or any(token in exc_name for token in (
        "connectionerror", "connecterror", "networkerror", "servernotfound",
    ))
    if operation in {"upload", "write"} and (is_timeout or is_connection or module_name.startswith("httplib2")):
        category = "upload_outcome_unknown" if operation == "upload" else "external_write_outcome_unknown"
        return ErrorClassification(category, False, stop_batch=True, outcome_unknown=True)
    if is_timeout:
        return ErrorClassification("timeout", True)
    if is_connection or "network" in module_name:
        return ErrorClassification("network", True)
    if status in (408, 425, 500, 502, 503, 504):
        if operation == "write":
            return ErrorClassification("external_write_outcome_unknown", False, stop_batch=True, outcome_unknown=True)
        return ErrorClassification("transient_http", True, retry_after=retry_after)
    if status is not None and 400 <= status < 500:
        return ErrorClassification("permanent_http", False)

    # Unknown/programming errors are not assumed transient; this avoids loops.
    return ErrorClassification("unexpected", False)


def api_timeout_seconds():
    try:
        return min(300.0, max(1.0, float(os.getenv("API_TIMEOUT_SECONDS", "30"))))
    except (TypeError, ValueError):
        return 30.0


@lru_cache(maxsize=4)
def create_groq_client(api_key):
    """Disable SDK-internal retries so the shared retry budget is authoritative."""
    # pyrefly: ignore[missing-import]
    from groq import Groq
    return Groq(api_key=api_key, timeout=api_timeout_seconds(), max_retries=0)


def build_google_service(api_name, version, credentials):
    """Build Google API clients with one explicit network timeout."""
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build
    http = AuthorizedHttp(credentials, http=httplib2.Http(timeout=api_timeout_seconds()))
    return build(api_name, version, http=http, cache_discovery=False)


def _retry_delay(classification, delays, attempt):
    configured_max = api_timeout_seconds() * 10
    max_wait = min(300.0, configured_max)
    if classification.retry_after is not None:
        base = min(max_wait, classification.retry_after)
    else:
        base = min(max_wait, delays[min(attempt - 1, len(delays) - 1)])
    return max(0.0, base + random.uniform(0.0, min(2.0, base * 0.1)))


def _log_result(task, status, attempt, classification=None, error=None, **fields):
    log_event(
        "task_result", task=task, status=status, attempt=attempt,
        error_category=classification.category if classification else None,
        retryable=classification.retryable if classification else False,
        error=str(error)[:500] if error is not None else None,
        **fields,
    )


def _retry_call(fn, task, service, operation, max_attempts, delays, *args, **kwargs):
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = fn(*args, **kwargs)
            _log_result(task, "success", attempt)
            return result
        except KeyboardInterrupt as exc:
            classification = ErrorClassification("interrupted", False, stop_batch=True,
                                                  outcome_unknown=operation in {"upload", "write"})
            _log_result(task, "interrupted", attempt, classification, exc)
            if operation == "upload":
                raise UploadOutcomeUnknownError(
                    "Upload interrupted while the insert may be in flight; verify YouTube Studio."
                ) from exc
            if operation == "write":
                raise ExternalWriteOutcomeUnknownError(
                    f"{task} interrupted while the request may be in flight; verify before retrying."
                ) from exc
            raise
        except Exception as exc:
            last_err = exc
            classification = classify_error(exc, service=service, operation=operation)
            if classification.outcome_unknown:
                log_error(f"[{task}] Write outcome is unknown; automatic retry stopped: {exc}")
                _log_result(task, "failure", attempt, classification, exc)
                if operation == "upload":
                    raise UploadOutcomeUnknownError(
                        f"Upload outcome is unknown; verify YouTube Studio before retrying: {exc}"
                    ) from exc
                raise ExternalWriteOutcomeUnknownError(
                    f"{task} outcome is unknown; check the service before retrying: {exc}"
                ) from exc
            if not classification.retryable:
                _log_result(task, "failure", attempt, classification, exc)
                _raise_non_retryable(task, classification, exc)
            if attempt >= max_attempts:
                log_error(f"[{task}] Retry budget exhausted after {max_attempts} attempts: {exc}")
                _log_result(task, "failure", attempt, classification, exc)
                raise RetryExhaustedError(f"{task} failed after {max_attempts} attempts: {exc}") from exc
            wait = _retry_delay(classification, delays, attempt)
            log_event("retry_scheduled", task=task, attempt=attempt, max_attempts=max_attempts,
                      category=classification.category, wait_seconds=round(wait, 2))
            log_warn(f"[{task}] Attempt {attempt}/{max_attempts} failed ({classification.category}); retrying in {wait:.1f}s.")
            time.sleep(wait)
    raise RetryExhaustedError(f"{task} failed after {max_attempts} attempts: {last_err}") from last_err


def _raise_non_retryable(task, classification, exc):
    if classification.category == "account_upload_limit":
        msg = "[Upload Limit] YouTube channel ki daily video upload limit reach ho gayi hai. Upload sequence 24 hours ke liye pause ho raha hai."
        print(f"\n{msg}\n")
        log_warn(msg)
        raise AccountUploadLimitError(f"{msg}: {exc}") from exc
    if classification.category == "youtube_quota":
        raise QuotaExceededError(f"YouTube API quota exceeded: {exc}") from exc
    if classification.category == "authentication":
        raise AuthError(f"{task} authentication failed: {exc}") from exc
    if classification.category == "model_not_found":
        raise ModelNotFoundError(f"Configured AI model was not found or is unavailable: {exc}") from exc
    if classification.category == "model_configuration":
        raise ModelConfigurationError(f"Configured Groq model is not enabled for this account: {exc}") from exc
    if classification.category == "invalid_request":
        raise NonRetryableAPIError(f"Groq rejected the request as invalid or unsupported: {exc}") from exc
    if classification.category == "provider_quota":
        raise ProviderQuotaError(f"AI provider quota/credits are exhausted: {exc}") from exc
    if classification.category == "bot_verification":
        raise BotVerificationError(f"YouTube requires bot verification or browser authentication: {exc}") from exc
    if classification.category == "rate_limited":
        raise RateLimitedError(f"YouTube download rate limit reached (HTTP 429): {exc}") from exc
    if classification.category == "permanent_source":
        raise PermanentSourceError(f"Video unavailable or restricted: {exc}") from exc
    raise NonRetryableAPIError(f"{task} failed with non-retryable {classification.category}: {exc}") from exc


def retry_download(fn, *args, **kwargs):
    """Retry only transient download errors; stop immediately on permanent input, bot verification, or rate limits."""
    try:
        from config import YTDLP_MAX_RETRIES
        max_retries = YTDLP_MAX_RETRIES
    except Exception:
        max_retries = 2
    attempts = max(1, max_retries + 1)
    return _retry_call(fn, "Download", "download", "download", attempts, [15, 30, 60], *args, **kwargs)


def retry_groq(fn, *args, **kwargs):
    """Groq call: configurable bounded retries; config/auth errors are terminal."""
    from config import GROQ_MAX_RETRIES
    return _retry_call(fn, "Groq API", "groq", "generate", GROQ_MAX_RETRIES + 1,
                       [15, 30, 60], *args, **kwargs)


def retry_google_read(fn, task="YouTube API read", *args, **kwargs):
    """Three bounded attempts for safe, idempotent Google API reads."""
    return _retry_call(fn, task, "youtube", "read", 3, [2, 5], *args, **kwargs)


def call_google_write(fn, task="Google API write", *args, **kwargs):
    """Single attempt for non-idempotent writes; uncertain network results stay explicit."""
    return _retry_call(fn, task, "youtube", "write", 1, [], *args, **kwargs)


def retry_ffmpeg(fn, *args, **kwargs):
    """Retry a local FFmpeg task once; preserve the original callable contract."""
    last_err = None
    for attempt in range(1, 3):
        try:
            result = fn(*args, **kwargs)
            _log_result("FFmpeg", "success", attempt)
            return result
        except KeyboardInterrupt as exc:
            _log_result("FFmpeg", "interrupted", attempt, error=exc)
            raise
        except subprocess.CalledProcessError as exc:
            last_err = exc
            stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or str(exc))
            log_warn(f"[FFmpeg Retry] Attempt {attempt}/2 failed with exit code {exc.returncode}:\n{stderr[:500]}")
            if attempt == 2:
                _log_result("FFmpeg", "failure", attempt, error=exc)
            else:
                time.sleep(2)
        except Exception as exc:
            last_err = exc
            log_warn(f"[FFmpeg Retry] Attempt {attempt}/2 failed: {exc}")
            if attempt == 2:
                _log_result("FFmpeg", "failure", attempt, error=exc)
            else:
                time.sleep(2)
    raise RuntimeError(f"FFmpeg operation failed after 2 attempts: {last_err}") from last_err


def retry_upload(fn, *args, **kwargs):
    """Max 1 attempt for upload; no long 5-attempt retry loops."""
    try:
        return _retry_call(fn, "YouTube upload", "youtube", "upload", 1,
                           [], *args, **kwargs)
    except KeyboardInterrupt as exc:
        log_error("[YouTube upload] Interrupted while the insert outcome may be in flight; marking uncertain.")
        raise UploadOutcomeUnknownError(
            "Upload was interrupted while in flight; verify YouTube Studio before retrying."
        ) from exc
