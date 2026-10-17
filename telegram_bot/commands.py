"""Allowlisted command routing and safe, low-disclosure status handlers."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from telegram_bot.access import is_authorized
from telegram_bot import monitoring

DATA_DIR = Path("data")
QUEUE_FILE = Path(os.getenv("REVIEW_DIR", "review")) / "queue.json"
COMMAND_NAMES = ("start", "help", "commands", "status", "health", "queue", "logs", "task", "history",
                 "showanalytics", "showviews", "showlikes", "showcomments", "videos",
                 "prepare", "upload", "pause", "resume", "cancel", "retry", "confirm", "deny")


def _commands(_message):
    return ("Available commands: /start, /help, /commands, /status, /health, /queue, /logs, "
            "/task TASK_ID, /history, /showanalytics 7d|28d, /showviews VIDEO_ID, "
            "/showlikes VIDEO_ID, /showcomments VIDEO_ID, /videos. You can also ask in plain language, "
            "for example: 'last 7 days analytics', 'count upload-ready clips', 'explain latest error', "
            "'prepare clips but do not upload', or 'pause the queue'.")


def _start(_message):
    return "PersonalAgent control bot is ready. Use /help to see available commands."


def _help(_message):
    return ("Commands:\n/start, /help, /commands\n/status — current stage/task and queue counts"
            "\n/health — integration/config status and last error\n/queue — queue items\n/logs — recent redacted logs"
            "\n/task TASK_ID — task state\n/history — recent task results"
            "\n/showanalytics 7d|28d — channel Analytics report\n/showviews VIDEO_ID /showlikes VIDEO_ID /showcomments VIDEO_ID"
            "\n/videos — latest channel uploads and current views"
            "\n/prepare, /upload — start background work; /upload asks before public upload"
            "\n/pause, /resume — pause or continue at safe checkpoints"
            "\n/cancel TASK_ID, /retry TASK_ID — cancel safely or retry eligible failed work"
            "\n/confirm CODE, /deny CODE — decide a pending public upload"
            "\nYou can ask naturally too: 'last 7 days analytics', 'count upload-ready clips', "
            "'explain latest error', 'prepare clips but do not upload', or 'pause the queue'.")


def _status(_message, queue_reader=None, monitor_reader=None):
    queue = (queue_reader or _read_queue)()
    counts = _queue_counts(queue)
    try:
        from telegram_bot.control import _read as read_control
        control_data = read_control()
        active = [task for task in control_data.get("tasks", {}).values()
                  if task.get("status") in {"queued", "running", "waiting_confirmation", "paused", "cancelling"}]
        if active:
            task = active[0]
            return (f"Telegram task {task.get('id')} ({task.get('kind')}): {task.get('status')}.\nQueue: "
                    + ", ".join(f"{state}={count}" for state, count in counts.items()))
        if control_data.get("paused"):
            return "Pipeline paused at safe checkpoints.\nQueue: " + ", ".join(f"{state}={count}" for state, count in counts.items())
    except Exception:
        pass
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
    return activity + "\nQueue: " + ", ".join(f"{state}={count}" for state, count in counts.items())


def _queue_counts(queue):
    counts = {state: 0 for state in ("pending", "processing", "uploaded", "failed", "blocked", "needs_review")}
    for item in queue:
        state = item.get("status", "pending")
        if state in counts:
            counts[state] += 1
    return counts


def _queue(_message, queue_reader=None):
    items = (queue_reader or _read_queue)()
    counts = _queue_counts(items)
    lines = ["Queue: " + ", ".join(f"{state}={counts[state]}" for state in ("pending", "needs_review", "failed", "blocked", "uploaded"))]
    for state in ("pending", "needs_review", "failed", "blocked", "uploaded"):
        matches = [item for item in items if item.get("status", "pending") == state]
        for item in matches[:3]:
            label = str(item.get("id") or item.get("key") or "unknown")[:70]
            label = monitoring.mask_secrets(label)
            title = monitoring.mask_secrets(str(item.get("title") or "")[:70])
            lines.append(f"{state}: {label}" + (f" — {title}" if title else ""))
            reason = item.get("review_reason") or item.get("quality_reason")
            if state == "needs_review" and reason:
                lines.append("  Review reason: " + monitoring.mask_secrets(str(reason)[:180]))
        if len(matches) > 3:
            lines.append(f"{state}: +{len(matches) - 3} more")
    return "\n".join(lines)


def _read_queue():
    from queue_manager import load_queue
    return load_queue(QUEUE_FILE)


def _health(heartbeat_reader=None, max_hours=None, now=None, monitor_reader=None, integrations_reader=None):
    data = (heartbeat_reader or _read_heartbeat)()
    last = data.get("last_success") if isinstance(data, dict) else None
    lines = ["Health (configuration/recorded state; no live API probe):"]
    integrations = (integrations_reader or monitoring.integration_status)()
    lines.extend(f"{name}: {value}" for name, value in integrations.items())
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


def _parse_command(update):
    message = update.get("message") or update.get("edited_message") or {}
    text = message.get("text", "")
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
            return (f"Task {task_id}: {controlled.get('status', 'unknown')} ({controlled.get('kind')})"
                    + (f"; error={monitoring.mask_secrets(str(controlled.get('error'))[:200])}" if controlled.get("error") else ""))
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
    results = (monitor_reader or monitoring.read_registry)().get("task_results", [])
    if not results:
        return "No task results recorded yet."
    return "Recent task results:\n" + "\n".join(
        f"{row.get('at', '')} {monitoring.mask_secrets(row.get('task', 'unknown'))}: {row.get('status', 'unknown')}"
        + (f" ({row.get('error_category')})" if row.get("error_category") else "")
        for row in results[-5:]
    )


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
    message = update.get("message") or update.get("edited_message") or {}
    command, args = _parse_command(update)
    if command is None:
        raw = (message.get("text") or "").strip()
        if raw.startswith("/"):
            return str(message["chat"]["id"]), "Unknown command. Use /help."
        return str(message["chat"]["id"]), _natural_language(
            raw, message, queue_reader=queue_reader, monitor_reader=monitor_reader,
            report_client_factory=report_client_factory,
            intent_classifier=intent_classifier, audit_writer=audit_writer,
        )
    if command in {"start", "help", "commands", "status", "health", "queue", "logs", "history", "videos"} and args:
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
            response = str(exc) if isinstance(exc, ReportingError) else "Command failed. Check the local run log for details."
        except Exception:
            response = "Command failed. Check the local run log for details."
    return str(message["chat"]["id"]), response


def _control_launch(kind, message):
    from telegram_bot.control import launch
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
