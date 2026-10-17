"""Standalone CLI entry points, intentionally independent of pipeline modules."""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _stats():
    from .auth import get_service
    from .tracker import refresh_stats
    rows = refresh_stats(get_service("read"))
    print(f"Performance records: {len(rows)}")


def _slots():
    from .slots import suggest_slots
    from config import SLOT_HOURS_IST
    from config import MIN_SAMPLES_PER_SLOT, CANDIDATE_HOURS_IST, AUTO_SLOT_UPDATE
    suggest_slots(current_hours=SLOT_HOURS_IST, min_samples=MIN_SAMPLES_PER_SLOT,
                  candidates=CANDIDATE_HOURS_IST, auto_update=AUTO_SLOT_UPDATE)


def _learn():
    from .learnings import refresh_learnings
    refresh_learnings()


def _comments():
    from config import COMMENT_ENABLED, DRY_RUN
    if not COMMENT_ENABLED:
        print("COMMENT_ENABLED=false; comments are disabled.")
        return
    from .auth import get_service
    from .comments import post_comments
    post_comments(get_service("comments"), dry_run=DRY_RUN, enabled=COMMENT_ENABLED)


def _health():
    raw_hours = os.getenv("HEALTH_MAX_HOURS", "26")
    hours = max(1, int(raw_hours)) if raw_hours.isdigit() else 26
    path = Path("data/heartbeat.json")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        last = datetime.fromisoformat(payload["last_success"].replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except (OSError, ValueError, KeyError, TypeError):
        last = None
    if last is not None and datetime.now(timezone.utc) - last <= timedelta(hours=hours):
        print(f"Growth health OK: last successful run {last.isoformat()}.")
        return
    message = f"PersonalAgent health alert: last_success is missing or older than {hours} hours."
    try:
        from notify import broadcast_message
        broadcast_message("PersonalAgent health alert", message)
    except Exception as exc:
        print(f"[Growth Health] Notification unavailable ({exc}). {message}")
    else:
        print(message)


def dispatch(args):
    command = args[0] if args else ""
    commands = {"stats": _stats, "slots": _slots, "learn": _learn,
                "comments": _comments, "growth-health": _health}
    if command not in commands:
        return False
    commands[command]()
    return True

