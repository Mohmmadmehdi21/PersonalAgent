"""Allowlisted command routing and safe, low-disclosure status handlers."""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from telegram_bot.access import is_authorized
from telegram_bot import monitoring

DATA_DIR = Path("data")
QUEUE_FILE = Path(os.getenv("REVIEW_DIR", "review")) / "queue.json"
COMMAND_NAMES = ("start", "help", "commands", "status", "health", "queue", "logs", "task", "history",
                 "showanalytics", "showviews", "showlikes", "showcomments", "videos",
                 "review", "prepare", "upload", "pause", "resume", "cancel", "retry", "confirm", "deny")


def _commands(_message):
    return ("Available commands: /start, /help, /commands, /status, /health, /queue, /review, /logs, "
            "/task TASK_ID, /history, /showanalytics 7d|28d, /showviews VIDEO_ID, "
            "/showlikes VIDEO_ID, /showcomments VIDEO_ID, /videos. You can also ask in plain language, "
            "for example: 'last 7 days analytics', 'count upload-ready clips', 'explain latest error', "
            "'prepare clips but do not upload', or 'pause the queue'.")


def _start(_message):
    return ("PersonalAgent monitoring menu. Choose an option to view the current task, queue, health, "
            "or clips that need review. These controls are for monitoring and read-only review.")


def _menu_keyboard():
    return {"inline_keyboard": [
        [{"text": "Task Status", "callback_data": "pa1:status"}, {"text": "Queue", "callback_data": "pa1:queue:0"}],
        [{"text": "Health", "callback_data": "pa1:health"}, {"text": "Review Clips", "callback_data": "pa1:review:0"}],
        [{"text": "Prepare Clips", "callback_data": "pa2:prepare"}],
        [{"text": "Help", "callback_data": "pa1:help"}],
    ]}


def _page_keyboard(kind, page, total):
    row = []
    if page > 0:
        row.append({"text": "← Previous", "callback_data": f"pa1:{kind}:{page - 1}"})
    if (page + 1) * 5 < total:
        row.append({"text": "Next →", "callback_data": f"pa1:{kind}:{page + 1}"})
    rows = [row] if row else []
    rows.append([{"text": "Main Menu", "callback_data": "pa1:home"}])
    return {"inline_keyboard": rows}


def _callback_action(data):
    """Parse only the small, explicit Phase 1 callback allowlist."""
    if not isinstance(data, str) or len(data) > 64:
        return None
    if data in {"pa1:status", "pa1:health", "pa1:help", "pa1:home", "pa2:prepare"}:
        return data.split(":", 1)[1], 0
    match = re.fullmatch(r"pa1:(queue|review):(0|[1-9][0-9]{0,3})", data)
    return (match.group(1), int(match.group(2))) if match else None


def _callback_response(action, page, *, queue_reader=None, heartbeat_reader=None, max_hours=None,
                       now=None, monitor_reader=None, integrations_reader=None, message=None):
    if action == "home":
        return _start(None), _menu_keyboard()
    if action == "status":
        return _status(None, queue_reader, monitor_reader), _home_keyboard()
    if action == "health":
        return _health(heartbeat_reader, max_hours, now, monitor_reader,
                       integrations_reader, storage_checker=_safe_storage_snapshot), _home_keyboard()
    if action == "help":
        return _menu_help(), _home_keyboard()
    if action == "prepare":
        from telegram_bot.control import DuplicateTaskRequest
        try:
            request_key = f"menu-prepare:{message.get('from', {}).get('id')}:{message.get('message_id')}"
            return _control_launch("prepare", message, request_key=request_key), _home_keyboard()
        except DuplicateTaskRequest as duplicate:
            return (f"This Prepare button was already used. Task {duplicate.task_id} is saved as "
                    f"{duplicate.status}; use a new /start menu to request another run."), _home_keyboard()
        except RuntimeError as exc:
            if "already active" in str(exc):
                return "A controlled task is already active. Check Task Status before starting another run.", _home_keyboard()
            raise
    if action == "queue":
        items = (queue_reader or _read_queue)()
        text, count = _render_queue_page(items, page)
        if page and page * 5 >= count:
            return "This queue page is no longer available. Return to Main Menu and reopen Queue.", _home_keyboard()
        return text, _page_keyboard("queue", page, count)
    if action == "review":
        items = (queue_reader or _read_queue)()
        review_items = [item for item in items if _eligible_for_review(item)]
        text, count = _render_review_page(review_items, page)
        if page and page * 5 >= count:
            return "This review page is no longer available. Return to Main Menu and reopen Review Clips.", _home_keyboard()
        return text, _page_keyboard("review", page, count)
    return "This menu action is unavailable. Send /start for a fresh menu.", None


def _home_keyboard():
    return {"inline_keyboard": [[{"text": "Main Menu", "callback_data": "pa1:home"}]]}


def _menu_help():
    return ("Monitoring menu: Task Status shows saved task state and verified process activity; Queue shows "
            "read-only queue counts and entries; Health shows configuration, recorded checks, and local storage; "
            "Review Clips lists items currently marked needs_review. Prepare Clips starts the existing background "
            "preparation pipeline and does not publish. Use Main Menu to return. Supported monitoring "
            "commands: /start, /help, /status, /queue, /health, /review. Other existing bot commands remain "
            "available through /help. Upload remains outside this menu action.")


def _safe_storage_snapshot():
    """Read-only storage check; avoid the existing write-probe helper for menu views."""
    from notify import get_disk_free_gb
    data_path = DATA_DIR
    queue_path = QUEUE_FILE
    return {
        "data_writable": None,
        "review_writable": None,
        "data_write_permission": os.access(data_path, os.W_OK) if data_path.exists() else os.access(data_path.parent, os.W_OK),
        "review_write_permission": os.access(queue_path.parent, os.W_OK) if queue_path.parent.exists() else os.access(queue_path.parent.parent, os.W_OK),
        "free_gb": get_disk_free_gb("."),
    }


def _help(_message):
    return ("Commands:\n/start, /help, /commands\n/status — current stage/task and queue counts"
            "\n/health — integration/config status and last error\n/queue — queue items\n/logs — recent redacted logs"
            "\n/task TASK_ID — task state\n/history — recent task results"
            "\n/showanalytics 7d|28d — channel Analytics report\n/showviews VIDEO_ID /showlikes VIDEO_ID /showcomments VIDEO_ID"
            "\n/videos — latest channel uploads and current views"
            "\n/review — inspect clips that need manual review"
            "\n/prepare, /upload — start background work; /upload asks before public upload"
            "\n/pause, /resume — pause or continue at safe checkpoints"
            "\n/cancel TASK_ID, /retry TASK_ID — cancel safely or retry eligible failed work"
            "\n/confirm CODE, /deny CODE — decide a pending public upload"
            "\nYou can ask naturally too: 'last 7 days analytics', 'count upload-ready clips', "
            "'explain latest error', 'prepare clips but do not upload', or 'pause the queue'.")


def _status(_message, queue_reader=None, monitor_reader=None):
    queue = (queue_reader or _read_queue)()
    counts = _queue_counts(queue)
    from telegram_bot.control import CONTROL_FILE, _read as read_control
    control_data = read_control()
    if not isinstance(control_data, dict) or not isinstance(control_data.get("tasks", {}), dict):
        raise ValueError("Telegram task-state storage is invalid; task status is unavailable.")
    if CONTROL_FILE.exists():
        try:
            persisted = json.loads(CONTROL_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("Telegram task-state storage could not be read; task status is unavailable.") from exc
        if not isinstance(persisted, dict) or not isinstance(persisted.get("tasks", {}), dict):
            raise ValueError("Telegram task-state storage is invalid; task status is unavailable.")
        if any(not isinstance(task, dict) for task in persisted.get("tasks", {}).values()):
            raise ValueError("Telegram task-state storage contains malformed records; task status is unavailable.")
    active_statuses = {"queued", "running", "waiting_confirmation", "paused", "cancelling"}
    active = [task for task in control_data.get("tasks", {}).values()
              if isinstance(task, dict) and task.get("status") in active_statuses]
    task_states = []
    for task in active:
        pid = task.get("pid")
        if task.get("status") == "queued" and not pid:
            live_state = "queued; process not started"
        elif not pid:
            live_state = "live status unknown (no process ID recorded)"
        else:
            try:
                alive = monitoring.pid_is_alive(pid)
                live_state = "process active" if alive else "is not running; saved status remains " + str(task.get("status", "unknown"))
            except Exception:
                alive = None
                live_state = "live status unknown"
        registry_data = (monitor_reader or monitoring.read_registry)()
        stage = registry_data.get("stage") if registry_data.get("pid") == pid else None
        task_states.append((task, live_state, stage))
    if task_states:
        task, live_state, stage = task_states[0]
        status = task.get("status", "unknown")
        lines = [f"Telegram task {task.get('id', 'unknown')} ({task.get('kind', 'task')}): {status}; {live_state}."]
        if stage:
            stage_label = "Current recorded processing stage" if live_state == "process active" else "Last recorded processing stage"
            lines.append(f"{stage_label}: {monitoring.mask_secrets(str(stage)[:100])}.")
        else:
            lines.append("Processing stage: not recorded for this task.")
        lines.append(f"Started: {task.get('created_at', 'not recorded')}; last update: {task.get('updated_at', 'not recorded')}.")
        if task.get("error"):
            lines.append("Saved failure: " + monitoring.mask_secrets(str(task["error"])[:180]))
            lines.append("Next step: inspect the saved error and resume only through the existing owner-controlled workflow.")
        elif status == "waiting_confirmation":
            lines.append("The task is waiting for owner confirmation.")
        lines.append("Last completed pipeline result: " + str(registry_data.get("last_run_status", "not recorded")) +
                     "; this is separate from YouTube publication.")
        lines.append("Queue: " + ", ".join(f"{state}={count}" for state, count in counts.items()))
        return "\n".join(lines)
    terminal_tasks = [task for task in control_data.get("tasks", {}).values()
                      if isinstance(task, dict) and task.get("status") in {"completed", "failed", "cancelled"}]
    if terminal_tasks:
        task = terminal_tasks[-1]
        registry_data = (monitor_reader or monitoring.read_registry)()
        status = str(task.get("status", "unknown"))
        lines = [f"No Telegram task is active. Last task {task.get('id', 'unknown')} ({task.get('kind', 'task')}): {status}.",
                 f"Started: {task.get('created_at', 'not recorded')}; last update: {task.get('updated_at', 'not recorded')}.",
                 "Last pipeline result: " + str(registry_data.get("last_run_status", status)) +
                 "; task completion does not by itself confirm YouTube publication."]
        if task.get("error"):
            lines.append("Failure: " + monitoring.mask_secrets(str(task["error"])[:180]))
            lines.append("Next step: inspect the saved failure detail and use the existing owner-controlled recovery path.")
        if not registry_data.get("last_publication_at"):
            lines.append("Last upload marker: not recorded.")
        elif registry_data.get("last_publication_visibility") == "public":
            lines.append("Last public publication was confirmed by the YouTube API.")
        else:
            lines.append("Last upload success marker is recorded; public visibility is unverified (legacy state).")
        lines.append("Queue: " + ", ".join(f"{state}={count}" for state, count in counts.items()))
        return "\n".join(lines)
    if control_data.get("paused"):
        return "Pipeline paused at safe checkpoints.\nQueue: " + ", ".join(f"{state}={count}" for state, count in counts.items())
    data = (monitor_reader or monitoring.read_registry)()
    if data.get("running"):
        task = data.get("current_task") or "unknown"
        stage = data.get("stage") or "unknown"
        try:
            if not monitoring.pid_is_alive(data.get("pid")):
                activity = f"Last recorded stage/task: {stage} / {task} (process is no longer active)."
            else:
                activity = f"Running: stage={stage}; task={task}."
        except Exception:
            activity = f"Last recorded stage/task: {stage} / {task}; process status unknown."
    else:
        status = data.get("last_run_status", "no run recorded")
        activity = f"Not running; last run status={status}."
    result = data.get("last_run_status", "not recorded")
    return (activity + f"\nLast completed pipeline result: {result}; this is separate from YouTube publication."
            + "\nQueue: " + ", ".join(f"{state}={count}" for state, count in counts.items()))


def _queue_counts(queue):
    counts = {state: 0 for state in ("pending", "processing", "uploaded", "failed", "blocked", "needs_review")}
    for item in queue:
        state = item.get("status") or "pending"
        if state in counts:
            counts[state] += 1
    return counts


def _queue(_message, queue_reader=None):
    items = (queue_reader or _read_queue)()
    return _render_queue_page(items, 0)[0]


def _render_queue_page(items, page):
    counts = _queue_counts(items)
    order = ("pending", "processing", "needs_review", "failed", "blocked", "uploaded")
    entries = [item for state in order for item in items if (item.get("status") or "pending") == state]
    unknown = [item for item in items if (item.get("status") or "pending") not in order]
    entries.extend(unknown)
    start = page * 5
    lines = ["Queue: " + ", ".join(f"{state}={counts[state]}" for state in order),
             f"Items {min(start + 1, len(entries)) if entries else 0}-{min(start + 5, len(entries))} of {len(entries)}."]
    if unknown:
        lines.append(f"Unmapped status records: {len(unknown)} (stored status retained; no category inferred).")
    for item in entries[start:start + 5]:
        state = item.get("status") or "pending"
        if state not in order:
            state = f"unmapped({monitoring.mask_secrets(str(state)[:40])})"
        label = monitoring.mask_secrets(str(item.get("id") or item.get("key") or "unknown")[:70])
        title = monitoring.mask_secrets(str(item.get("title") or item.get("filename") or "")[:70])
        lines.append(f"{state}: {label}" + (f" — {title}" if title else ""))
        reason = item.get("review_reason") or item.get("quality_reason") or item.get("last_error")
        if state in ("needs_review", "failed", "blocked") and reason:
            lines.append("  Reason: " + monitoring.mask_secrets(str(reason)[:180]))
    return "\n".join(lines), len(entries)


def _eligible_for_review(item):
    return (isinstance(item, dict) and
            (item.get("status") == "needs_review" or
             (item.get("quality_status") == "needs_review" and not item.get("quality_reviewed"))))


def _render_review_page(review_items, page):
    start = page * 5
    lines = [f"Manual review required: {len(review_items)} clip(s).",
             f"Items {min(start + 1, len(review_items)) if review_items else 0}-{min(start + 5, len(review_items))} of {len(review_items)}."]
    for item in review_items[start:start + 5]:
        label = monitoring.mask_secrets(str(item.get("id") or item.get("key") or "unknown")[:70])
        title = monitoring.mask_secrets(str(item.get("title") or item.get("filename") or "")[:70])
        reason = item.get("review_reason") or item.get("quality_reason") or item.get("upload_error") or item.get("last_error")
        lines.append(f"{label}" + (f" — {title}" if title else ""))
        if reason:
            lines.append("  Reason: " + monitoring.mask_secrets(str(reason)[:180]))
    return "\n".join(lines), len(review_items)


def _review(_message, queue_reader=None):
    items = (queue_reader or _read_queue)()
    review_items = [item for item in items if _eligible_for_review(item)]
    if not review_items:
        return "No clips currently need manual review."
    return _render_review_page(review_items, 0)[0]


def _read_queue():
    """Read queue JSON without queue_manager migration or any write side effects."""
    try:
        value = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(value, list):
        raise ValueError("Queue data is not a JSON list; monitoring was not able to inspect it safely.")
    if any(not isinstance(item, dict) for item in value):
        raise ValueError("Queue contains a malformed record; monitoring was not able to inspect it safely.")
    return value


def _check_storage():
    from notify import get_disk_free_gb
    res = {"data_writable": False, "review_writable": False, "free_gb": get_disk_free_gb(".")}
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        test_file = DATA_DIR / ".write_test.tmp"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
        res["data_writable"] = True
    except Exception:
        pass
    try:
        QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        test_file = QUEUE_FILE.parent / ".write_test.tmp"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
        res["review_writable"] = True
    except Exception:
        pass
    return res


def _health(heartbeat_reader=None, max_hours=None, now=None, monitor_reader=None, integrations_reader=None, storage_checker=None):
    from env_config import load_project_env
    load_project_env()
    data = (heartbeat_reader or _read_heartbeat)()
    last = data.get("last_success") if isinstance(data, dict) else None
    lines = ["Health Summary:"]
    lines.append(f"Configuration: Telegram bot token={'configured' if os.getenv('TELEGRAM_BOT_TOKEN', '').strip() else 'missing'}; "
                 f"owner IDs={'configured' if os.getenv('TELEGRAM_OWNER_IDS', '').strip() else 'missing'}; "
                 f"queue path={'configured' if str(QUEUE_FILE).strip() else 'missing'}.")
    check_storage = storage_checker or _check_storage
    storage = check_storage()
    def storage_result(name):
        if storage.get(name + "_writable") is not None:
            return "writable" if storage.get(name + "_writable") else "unwritable"
        permission = storage.get(name + "_write_permission")
        return "write not tested" + (" (permission appears available)" if permission else " (permission unavailable/unknown)")
    lines.append(f"Storage: data={storage_result('data')}, review={storage_result('review')}, free_space={storage.get('free_gb', 0):.1f}GB")
    integrations = (integrations_reader or monitoring.integration_status)()
    lines.extend(f"{name}: {value}" for name, value in integrations.items())
    try:
        from telegram_bot.control import _read as read_control
        control_state = read_control()
        if not isinstance(control_state, dict) or not isinstance(control_state.get("tasks", {}), dict):
            lines.append("Background task state: unavailable (invalid saved state).")
        else:
            lines.append(f"Background task state: available; {len(control_state.get('tasks', {}))} saved task(s).")
    except Exception:
        lines.append("Background task state: unavailable (read failed).")
    scheduler_path = DATA_DIR / "scheduler.json"
    if os.getenv("SCHEDULER_ENABLED", "false").strip().lower() in ("true", "1", "t", "yes"):
        if scheduler_path.is_file():
            try:
                scheduler_state = json.loads(scheduler_path.read_text(encoding="utf-8"))
                lines.append(f"Scheduler: configured enabled; saved state={'available' if isinstance(scheduler_state, dict) else 'invalid'}; not started or changed by this check.")
            except (OSError, ValueError):
                lines.append("Scheduler: configured enabled; saved state could not be read; not started or changed by this check.")
        else:
            lines.append("Scheduler: configured enabled; no saved state recorded; not started or changed by this check.")
    else:
        lines.append(f"Scheduler: disabled by configuration; saved state {'present' if scheduler_path.is_file() else 'not recorded'}; configuration was not changed.")
    if last:
        try:
            parsed = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            age_hours = ((now or datetime.now(timezone.utc)) - parsed.astimezone(timezone.utc)).total_seconds() / 3600
            threshold = int(max_hours if max_hours is not None else os.getenv("HEALTH_MAX_HOURS", "26"))
            lines.append(f"Last success marker: {age_hours:.1f} hours ago (threshold {threshold}h); this alone does not prove end-to-end success.")
        except (TypeError, ValueError, OverflowError):
            lines.append("Last success marker: invalid timestamp.")
    else:
        lines.append("Last success marker: not recorded.")
    registry = (monitor_reader or monitoring.read_registry)()
    lines.append(f"Last run result: {registry.get('last_run_status', 'not recorded')}")
    if registry.get("last_failed_stage"):
        lines.append(f"Last failed stage: {monitoring.mask_secrets(str(registry['last_failed_stage'])[:120])}")
    publications = registry.get("last_run_publications")
    if publications is None:
        lines.append("Last run publication: not recorded (no publication marker available).")
    elif int(publications or 0) == 0:
        lines.append("Last run publication: none confirmed; preparation/task completion is not publication.")
    elif registry.get("last_run_publication_visibility") == "public":
        lines.append(f"Last run publication: {int(publications)} public YouTube upload(s) confirmed by API.")
    else:
        lines.append(f"Last run: {int(publications)} YouTube upload(s) recorded; public visibility is unverified (legacy state).")
    if not registry.get("last_publication_at"):
        lines.append("Last public publication marker: not recorded.")
    api = registry.get("last_api_response")
    lines.append("Last API result: " + (f"{api.get('task')}={api.get('status')} ({api.get('category') or 'no error'})" if api else "not recorded"))
    error = registry.get("last_error")
    lines.append("Last error: " + (f"{error.get('task')} / {error.get('category')}: {error.get('message')}" if error else "none recorded"))
    return "\n".join(lines)


def _read_heartbeat():
    try:
        return json.loads((DATA_DIR / "heartbeat.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _parse_command(update, message=None):
    if message is None:
        message = update.get("message") or update.get("edited_message") or {}
        if not message and update.get("callback_query"):
            cb = update.get("callback_query") or {}
            cb_msg = cb.get("message") or {}
            if isinstance(cb_msg, dict):
                message = dict(cb_msg)
                if cb.get("data"):
                    message["text"] = cb.get("data")
                if cb.get("from"):
                    message["from"] = cb.get("from")
    text = message.get("text", "") if isinstance(message, dict) else ""
    parts = text.strip().split()
    if not parts or not parts[0].startswith("/"):
        return None, []
    command = parts[0][1:].split("@", 1)[0].lower()
    return (command if command in COMMAND_NAMES else None), parts[1:]


def _task(args, queue_reader=None, monitor_reader=None):
    if len(args) != 1:
        return "Usage: /task TASK_ID"
    task_id = args[0][:100]
    try:
        from telegram_bot.control import task_info
        controlled = task_info(task_id)
        if controlled:
            parts = [f"Task {task_id}: {controlled.get('status', 'unknown')} ({controlled.get('kind')})"]
            if controlled.get("pid"):
                parts.append(f"PID: {controlled['pid']}")
            if controlled.get("clip_ids"):
                parts.append(f"Clips: {', '.join(str(c) for c in controlled['clip_ids'][:5])}")
            if controlled.get("error"):
                parts.append(f"error={monitoring.mask_secrets(str(controlled.get('error'))[:200])}")
            return "; ".join(parts)
    except Exception:
        pass
    queue = (queue_reader or _read_queue)()
    for item in queue:
        if task_id in {str(item.get("id", "")), str(item.get("key", "")), str(item.get("youtube_video_id", ""))}:
            return f"Task {task_id}: {item.get('status', 'pending')}" + (f"; error={monitoring.mask_secrets(str(item.get('last_error'))[:250])}" if item.get("last_error") else "")
    registry = (monitor_reader or monitoring.read_registry)()
    found = next((item for item in reversed(registry.get("task_results", [])) if item.get("task") == task_id), None)
    if found:
        return f"Task {task_id}: {found.get('status', 'unknown')}" + (f"; {found.get('error_category')}: {monitoring.mask_secrets(found.get('error', ''))}" if found.get("error") else "")
    return "Task not found in the current queue or recent task history."


def _history(monitor_reader=None):
    lines = []
    try:
        from telegram_bot.control import _read as read_control
        tasks = list(read_control().get("tasks", {}).values())
        if tasks:
            for task in tasks[-5:]:
                tid = task.get("id", "unknown")
                kind = task.get("kind", "task")
                st = task.get("status", "unknown")
                lines.append(f"Telegram {kind} ({tid}): {st}" + (f" ({monitoring.mask_secrets(str(task.get('error'))[:100])})" if task.get("error") else ""))
    except Exception:
        pass
    results = (monitor_reader or monitoring.read_registry)().get("task_results", [])
    if results:
        for row in results[-5:]:
            lines.append(
                f"{row.get('at', '')} {monitoring.mask_secrets(row.get('task', 'unknown'))}: {row.get('status', 'unknown')}"
                + (f" ({row.get('error_category')})" if row.get("error_category") else "")
            )
    if not lines:
        return "No task results recorded yet."
    return "Recent task results:\n" + "\n".join(lines)


def _report_clients(client_factory=None, *, need_analytics=False):
    if client_factory:
        return client_factory()
    from growth.auth import get_service
    if need_analytics:
        from growth.auth import get_analytics_service
        return None, get_analytics_service()
    return get_service("read"), None


def _video_report(metric, args, client_factory=None):
    from telegram_bot.youtube_reports import ReportingError, video_metric
    if len(args) != 1:
        label = {"viewCount": "showviews", "likeCount": "showlikes", "commentCount": "showcomments"}[metric]
        raise ReportingError(f"Usage: /{label} VIDEO_ID")
    from telegram_bot.youtube_reports import VIDEO_ID_RE
    if not VIDEO_ID_RE.fullmatch(args[0]):
        raise ReportingError("Video ID must be the 11-character YouTube ID (not a full URL).")
    youtube, _analytics = _report_clients(client_factory)
    return video_metric(youtube, args[0], metric)


def _analytics_report(args, client_factory=None):
    from telegram_bot.youtube_reports import ReportingError, analytics_summary
    if len(args) != 1 or args[0] not in {"7d", "28d"}:
        raise ReportingError("Usage: /showanalytics 7d or /showanalytics 28d")
    _youtube, analytics = _report_clients(client_factory, need_analytics=True)
    return analytics_summary(analytics, args[0])


def _videos_report(args, client_factory=None):
    from telegram_bot.youtube_reports import ReportingError, list_recent_videos
    if args:
        raise ReportingError("Usage: /videos")
    youtube, _analytics = _report_clients(client_factory)
    return list_recent_videos(youtube)


def handle_update(update, owner_ids, *, queue_reader=None, heartbeat_reader=None, max_hours=None,
                  now=None, monitor_reader=None, logs_reader=None, integrations_reader=None,
                  report_client_factory=None, intent_classifier=None, audit_writer=None):
    """Return (chat_id, response) or None. Authorization precedes all protected reads."""
    if not is_authorized(update, owner_ids):
        return None
    callback = update.get("callback_query") if isinstance(update, dict) else None
    if isinstance(callback, dict):
        cb_message = callback.get("message") or {}
        chat_id = str((cb_message.get("chat") or {}).get("id", ""))
        legacy_approval = re.fullmatch(r"/(confirm|deny) ([A-Za-z0-9_-]{1,100})", callback.get("data", ""))
        if legacy_approval and isinstance(cb_message, dict):
            # Preserve the existing upload approval buttons through the original command path.
            approval_message = dict(cb_message)
            approval_message["from"] = callback.get("from") or {}
            approval_message["text"] = callback["data"]
            return handle_update({"message": approval_message}, owner_ids, queue_reader=queue_reader,
                                 heartbeat_reader=heartbeat_reader, max_hours=max_hours, now=now,
                                 monitor_reader=monitor_reader, logs_reader=logs_reader,
                                 integrations_reader=integrations_reader, report_client_factory=report_client_factory,
                                 intent_classifier=intent_classifier, audit_writer=audit_writer)
        parsed = _callback_action(callback.get("data"))
        if parsed is None:
            return chat_id, "This menu action is invalid or expired. Send /start for a fresh menu."
        action, page = parsed
        try:
            text, markup = _callback_response(
                action, page, queue_reader=queue_reader, heartbeat_reader=heartbeat_reader,
                max_hours=max_hours, now=now, monitor_reader=monitor_reader,
                integrations_reader=integrations_reader,
                message={**cb_message, "from": callback.get("from") or {}},
            )
        except Exception as exc:
            try:
                from logger import log_error, mask_secrets
                log_error(f"[Telegram] Menu callback '{action}' failed: {mask_secrets(exc)}")
            except Exception:
                pass
            failure_messages = {
                "status": "Task status is unavailable because its saved state could not be read. Check the local run log.",
                "queue": "Queue information is unavailable because the saved queue could not be read. Check the local run log.",
                "review": "Review list is unavailable because the saved queue could not be read. Check the local run log.",
                "health": "Health information is incomplete because a local diagnostic could not be read. Check the local run log.",
            }
            text, markup = failure_messages.get(action, "Unable to read this monitoring information. Check the local run log."), _home_keyboard()
        return (chat_id, text, markup) if markup else (chat_id, text)
    message = update.get("message") or update.get("edited_message") or {}
    command, args = _parse_command(update, message)
    if command is None:
        raw = (message.get("text") or "").strip()
        if raw.startswith("/"):
            return str(message["chat"]["id"]), "Unknown command. Use /help."
        return str(message["chat"]["id"]), _natural_language(
            raw, message, queue_reader=queue_reader, monitor_reader=monitor_reader,
            report_client_factory=report_client_factory,
            intent_classifier=intent_classifier, audit_writer=audit_writer,
        )
    if command in {"start", "help", "commands", "status", "health", "queue", "review", "logs", "history", "videos"} and args:
        return str(message["chat"]["id"]), "This command takes no arguments. Use /help."
    if command in {"showanalytics", "showviews", "showlikes", "showcomments"} and not args:
        return str(message["chat"]["id"]), f"Usage: /{command} {'7d or 28d' if command == 'showanalytics' else 'VIDEO_ID'}"
    if command in {"prepare", "upload", "pause", "resume"} and args:
        return str(message["chat"]["id"]), f"/{command} takes no arguments."
    if command in {"cancel", "retry", "confirm", "deny"} and len(args) != 1:
        suffix = "TASK_ID" if command in {"cancel", "retry"} else "CODE"
        return str(message["chat"]["id"]), f"Usage: /{command} {suffix}"
    registry = {
        "start": _start,
        "help": _help,
        "commands": _commands,
        "status": lambda msg: _status(msg, queue_reader, monitor_reader),
        "health": lambda msg: _health(heartbeat_reader, max_hours, now, monitor_reader, integrations_reader),
        "queue": lambda msg: _queue(msg, queue_reader),
        "review": lambda msg: _review(msg, queue_reader),
        "logs": lambda msg: "Recent safe logs:\n" + "\n".join((logs_reader or monitoring.recent_logs)(8)),
        "task": lambda msg: _task(args, queue_reader, monitor_reader),
        "history": lambda msg: _history(monitor_reader),
        "showanalytics": lambda msg: _analytics_report(args, report_client_factory),
        "showviews": lambda msg: _video_report("viewCount", args, report_client_factory),
        "showlikes": lambda msg: _video_report("likeCount", args, report_client_factory),
        "showcomments": lambda msg: _video_report("commentCount", args, report_client_factory),
        "videos": lambda msg: _videos_report(args, report_client_factory),
        "prepare": lambda msg: _control_launch("prepare", message),
        "upload": lambda msg: _control_launch("upload", message),
        "pause": lambda msg: _control_pause(True),
        "resume": lambda msg: _control_pause(False),
        "cancel": lambda msg: _control_cancel(args[0], message),
        "retry": lambda msg: _control_retry(args[0], message),
        "confirm": lambda msg: _control_confirm(args[0], message, True),
        "deny": lambda msg: _control_confirm(args[0], message, False),
    }
    try:
        response = registry[command](message)
    except Exception as exc:
        try:
            from logger import log_error, mask_secrets
            log_error(f"[Telegram] Allowlisted command '{command}' failed: {mask_secrets(exc)}")
        except Exception:
            pass
        try:
            from telegram_bot.youtube_reports import ReportingError
            if isinstance(exc, ReportingError):
                response = str(exc)
            elif command == "status":
                response = "Task status is unavailable because its saved state could not be read. Check the local run log."
            elif command in {"queue", "review"}:
                response = "Queue information is unavailable because the saved queue could not be read. Check the local run log."
            elif command == "health":
                response = "Health information is incomplete because a local diagnostic could not be read. Check the local run log."
            else:
                response = "Command failed. Check the local run log for details."
        except Exception:
            response = "Command failed. Check the local run log for details."
    if command == "start":
        return str(message["chat"]["id"]), response, _menu_keyboard()
    return str(message["chat"]["id"]), response


def _control_launch(kind, message, request_key=None):
    if kind == "prepare":
        from config import ConfigurationError, validate_startup_config
        try:
            validate_startup_config("prepare")
        except ConfigurationError as exc:
            return "Preparation cannot start because configuration is incomplete: " + monitoring.mask_secrets(str(exc))[:500]
    from telegram_bot.control import launch
    if request_key:
        task_id = launch(kind, message["from"]["id"], message["chat"]["id"], request_key=request_key)
    else:
        task_id = launch(kind, message["from"]["id"], message["chat"]["id"])
    return f"Started background {kind} task {task_id}. Use /task {task_id} for status."


def _control_pause(paused):
    from telegram_bot.control import set_paused
    set_paused(paused)
    return "Paused. Active work will stop at its next safe checkpoint." if paused else "Resumed. Eligible work will continue."


def _control_cancel(task_id, message):
    from telegram_bot.control import cancel
    return cancel(task_id, message["from"]["id"])


def _control_confirm(code, message, approved):
    from telegram_bot.control import confirm
    return confirm(code, message["from"]["id"], approved)


def _control_retry(task_id, message):
    from telegram_bot.control import retry_task
    return retry_task(task_id, message["from"]["id"], message["chat"]["id"])


def _natural_language(text, message, *, queue_reader=None, monitor_reader=None,
                      report_client_factory=None, intent_classifier=None, audit_writer=None):
    from telegram_bot.natural_language import classify, audit
    intent = {"intent": "clarify", "range": None, "confidence": 0.0}
    outcome = "Use /help for supported requests."
    try:
        intent = (intent_classifier or classify)(text)
        name = intent.get("intent") if isinstance(intent, dict) else "clarify"
        confidence = float(intent.get("confidence", 0)) if isinstance(intent, dict) else 0.0
        if name not in {"analytics", "pending_ready_count", "latest_error", "prepare", "pause", "resume", "help", "clarify"} or confidence < 0.72:
            outcome = "I couldn't confidently identify that request. Try /help or a fixed command."
        else:
            lowered = text.lower()
            # Guard side effects with a second, local intent/word check. Model output alone
            # cannot start jobs or change control state.
            if name == "prepare" and not any(word in lowered for word in ("prepare", "draft", "clip", "naye clips", "new clips")):
                outcome = "Did you mean to prepare new clips? Reply with a clearer request, or use /prepare."
            elif name == "pause" and not any(word in lowered for word in ("pause", "stop", "rok", "band")):
                outcome = "Please clarify whether you want to pause the pipeline."
            elif name == "resume" and not any(word in lowered for word in ("resume", "continue", "chalu", "jaari")):
                outcome = "Please clarify whether you want to resume the pipeline."
            elif name == "prepare" and ("upload" in lowered or "publish" in lowered) and not any(
                    phrase in lowered for phrase in ("don't upload", "do not upload", "without upload", "upload mat", "upload nahi", "not upload")):
                outcome = "This sounds like an upload request. Use /upload; each public clip will still need /confirm CODE."
            else:
                actions = {
                    "analytics": lambda: _analytics_report([intent.get("range")], report_client_factory),
                    "pending_ready_count": lambda: _pending_ready_summary(queue_reader),
                    "latest_error": lambda: _latest_error_summary(monitor_reader),
                    "prepare": lambda: _control_launch("prepare", message),
                    "pause": lambda: _control_pause(True),
                    "resume": lambda: _control_pause(False),
                    "help": lambda: _help(message),
                    "clarify": lambda: "I need a little more detail. Try /help or a fixed command.",
                }
                outcome = actions[name]()
    except Exception:
        outcome = "I couldn't process that request. Fixed commands are still available with /help."
    try:
        (audit_writer or audit)(user_id=message.get("from", {}).get("id", "unknown"),
                                text=text, intent=intent.get("intent", "clarify"),
                                confidence=intent.get("confidence", 0), outcome=outcome)
    except Exception:
        pass
    return outcome


def _pending_ready_summary(queue_reader=None):
    queue = (queue_reader or _read_queue)()
    pending = [item for item in queue if item.get("status", "pending") == "pending"]
    ready = [item for item in pending if all(item.get(field) for field in ("id", "key", "title", "source_video_path"))]
    rendered = 0
    work = Path(os.getenv("WORK_DIR", "work")).resolve()
    for item in ready:
        clip_id = str(item.get("id", ""))
        if not clip_id or Path(clip_id).name != clip_id:
            continue
        if (work / f"{clip_id}.mp4").is_file() and (work / f"{clip_id}.jpg").is_file():
            rendered += 1
    return (f"Pending queue: {len(pending)} clip(s); {len(ready)} eligible for the upload pipeline, "
            f"{rendered} already have rendered video and thumbnail files.")


def _latest_error_summary(monitor_reader=None):
    registry = (monitor_reader or monitoring.read_registry)()
    error = registry.get("last_error")
    if not error:
        return "No recent error is recorded."
    category = str(error.get("category", "unknown category"))
    explanations = {
        "authentication": "credentials were rejected or expired; reauthorize the affected service",
        "provider_quota": "the AI provider rejected the request because of quota or credits",
        "youtube_quota": "YouTube API quota was exhausted",
        "account_upload_limit": "YouTube blocked uploads at the account level",
        "model_not_found": "the configured AI model is unavailable or misspelled",
        "upload_outcome_unknown": "the upload result is uncertain; check YouTube Studio before retrying",
        "unhandled": "the pipeline stopped after an unexpected error",
    }
    detail = monitoring.mask_secrets(str(error.get("message", "No details"))[:400])
    return (f"Latest error: {error.get('task', 'unknown task')} ({category}). "
            f"{explanations.get(category, 'The saved error details may help identify the failing step.')}.\n"
            f"Details: {detail}")
