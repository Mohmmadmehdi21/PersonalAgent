"""Performance summaries and conservative publishing-slot suggestions."""
import json
import os
import statistics
import tempfile
from pathlib import Path

from .tracker import DATA_DIR, read_videos

SLOTS_FILE = DATA_DIR / "slots.json"
LOG_FILE = DATA_DIR / "slot_changes.log"
EXPERIMENT_SLOTS_FILE = DATA_DIR / "experiment_slots.json"
DEFAULT_HOURS = [9, 13, 17, 21]


def slot_summary(rows=None):
    rows = rows if rows is not None else read_videos()
    result = {}
    for row in rows:
        if not row.get("views_7d") or not row.get("publish_hour_ist", "").isdigit():
            continue
        hour = int(row["publish_hour_ist"])
        bucket = result.setdefault(hour, {"views": [], "likes": []})
        bucket["views"].append(int(row["views_7d"]))
        if row.get("likes_7d", "").isdigit():
            bucket["likes"].append(int(row["likes_7d"]))
    return {
        hour: {"count": len(values["views"]), "median_views": statistics.median(values["views"]),
               "median_likes": statistics.median(values["likes"]) if values["likes"] else None}
        for hour, values in sorted(result.items())
    }


def _atomic_json(path, value):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.stem + "-", suffix=".tmp", dir=str(DATA_DIR))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def suggest_slots(rows=None, current_hours=None, min_samples=8, candidates=None, auto_update=False):
    rows = rows if rows is not None else read_videos()
    current_hours = list(current_hours or DEFAULT_HOURS)
    candidates = list(candidates or [7, 8, 11, 12, 15, 16, 18, 19, 20, 22])
    summary = slot_summary(rows)
    print("Hour | Videos | Median 7d views | Median 7d likes")
    for hour, stats in summary.items():
        print(f"{hour:02d}:00 | {stats['count']:>6} | {stats['median_views']:>15} | {stats['median_likes']}")
    sample_counts = [summary.get(hour, {}).get("count", 0) for hour in current_hours]
    if not current_hours or min(sample_counts, default=0) < min_samples:
        print("Data kam hai, slots nahi badle.")
        return {"hours": current_hours, "changed": False, "reason": "insufficient samples"}

    experiments = []
    if EXPERIMENT_SLOTS_FILE.exists():
        try:
            experiments = json.loads(EXPERIMENT_SLOTS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            experiments = []
    retained = [hour for hour in current_hours if hour in experiments]
    if not retained and len(current_hours) >= 4:
        retained = [current_hours[-1]]
    eligible = [hour for hour in current_hours if hour not in retained]
    worst = min(eligible or current_hours, key=lambda hour: summary.get(hour, {}).get("median_views", float("inf")))
    tested_hours = set(summary)
    new_hour = next((hour for hour in candidates if hour not in tested_hours and hour not in current_hours), None)
    if new_hour is None:
        print("Koi untested candidate hour nahi mila; slots nahi badle.")
        return {"hours": current_hours, "changed": False, "reason": "no untested candidate hour"}
    proposed = list(current_hours)
    proposed[proposed.index(worst)] = new_hour
    reason = (f"Slot {worst}:00 had the lowest median 7-day views among tested slots; "
              f"replace with untested candidate {new_hour}:00. Kept experiment slot(s): {retained}.")
    print(f"Suggestion: {proposed}. {reason}")
    if auto_update:
        _atomic_json(SLOTS_FILE, {"hours": proposed})
        _atomic_json(EXPERIMENT_SLOTS_FILE, [new_hour])
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(reason + "\n")
        try:
            from notify import broadcast_message
            broadcast_message("Growth slot suggestion applied", reason)
        except Exception as exc:
            print(f"[Growth] Slot notification skipped: {exc}")
        return {"hours": proposed, "changed": True, "reason": reason}
    print("Suggestion only; AUTO_SLOT_UPDATE=false.")
    return {"hours": proposed, "changed": False, "reason": reason}

