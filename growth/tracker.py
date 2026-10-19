"""Atomic CSV tracking and due YouTube performance snapshots."""
import csv
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from zoneinfo import ZoneInfo
from retry import retry_google_read

DATA_DIR = Path("data")
VIDEOS_FILE = DATA_DIR / "videos.csv"
FIELDS = [
    "video_id", "source_video_id", "title", "publish_at_ist", "publish_hour_ist",
    "weekday", "hook_score", "template", "uploaded_on", "clip_length_seconds",
    "views_1d", "likes_1d", "comments_1d", "snapshot_1d_at",
    "views_3d", "likes_3d", "comments_3d", "snapshot_3d_at",
    "views_7d", "likes_7d", "comments_7d", "snapshot_7d_at",
]
IST = ZoneInfo("Asia/Kolkata")


def _atomic_csv(rows):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix="videos-", suffix=".tmp", dir=str(DATA_DIR))
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(10):
            try:
                os.replace(temp_path, VIDEOS_FILE)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def read_videos():
    if not VIDEOS_FILE.exists():
        return []
    with VIDEOS_FILE.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def record_upload(video_id, source_video_id, title, publish_at=None, hook_score=None,
                  template=None, clip_length_seconds=None):
    """Record a confirmed upload once; accepts test clients through the stats API."""
    now = datetime.now(IST)
    publish = _parse_datetime(publish_at, default=now)
    publish_ist = publish.astimezone(IST)
    rows = read_videos()
    for row in rows:
        if row.get("video_id") == str(video_id):
            return row
    row = {field: "" for field in FIELDS}
    row.update({
        "video_id": str(video_id),
        "source_video_id": str(source_video_id or ""),
        "title": str(title or ""),
        "publish_at_ist": publish_ist.isoformat(timespec="seconds"),
        "publish_hour_ist": str(publish_ist.hour),
        "weekday": publish_ist.strftime("%A"),
        "hook_score": "" if hook_score is None else str(hook_score),
        "template": str(template or ""),
        "uploaded_on": now.isoformat(timespec="seconds"),
        "clip_length_seconds": "" if clip_length_seconds is None else str(clip_length_seconds),
    })
    rows.append(row)
    _atomic_csv(rows)
    return row


def record_success(now=None):
    """Atomically write the most recent successful pipeline timestamp."""
    now = now or datetime.now(timezone.utc)
    path = DATA_DIR / "heartbeat.json"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix="heartbeat-", suffix=".tmp", dir=str(DATA_DIR))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            import json
            json.dump({"last_success": now.astimezone(timezone.utc).isoformat()}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _parse_datetime(value, default=None):
    if not value:
        return default or datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return default or datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed


def _snapshot_due(row, now, days):
    column = f"views_{days}d"
    if row.get(column):
        return False
    published = _parse_datetime(row.get("publish_at_ist"))
    return published.astimezone(timezone.utc) <= now - timedelta(days=days)


def refresh_stats(youtube, now=None):
    """Fetch due public-video snapshots, in batches of at most 50 ids."""
    now = now or datetime.now(timezone.utc)
    rows = read_videos()
    due_by_video = {}
    for row in rows:
        due = [days for days in (1, 3, 7) if _snapshot_due(row, now, days)]
        if due:
            due_by_video[row["video_id"]] = due
    ids = list(due_by_video)
    changed = False
    for offset in range(0, len(ids), 50):
        batch = ids[offset:offset + 50]
        statuses = retry_google_read(
            lambda: youtube.videos().list(part="status", id=",".join(batch)).execute(),
            task="Growth video visibility lookup",
        )
        public_ids = [item.get("id") for item in statuses.get("items", [])
                      if item.get("status", {}).get("privacyStatus") == "public"]
        for stats_offset in range(0, len(public_ids), 50):
            statistics = retry_google_read(lambda: youtube.videos().list(
                part="statistics", id=",".join(public_ids[stats_offset:stats_offset + 50])
            ).execute(), task="Growth video statistics")
            stats_by_id = {item.get("id"): item.get("statistics", {}) for item in statistics.get("items", [])}
            for video_id in public_ids[stats_offset:stats_offset + 50]:
                item_stats = stats_by_id.get(video_id)
                if item_stats is None:
                    continue
                row = next((r for r in rows if r.get("video_id") == video_id), None)
                if row is None:
                    continue
                stamp = now.astimezone(IST).isoformat(timespec="seconds")
                for days in due_by_video[video_id]:
                    row.update({
                        f"views_{days}d": item_stats.get("viewCount", ""),
                        f"likes_{days}d": item_stats.get("likeCount", ""),
                        f"comments_{days}d": item_stats.get("commentCount", ""),
                        f"snapshot_{days}d_at": stamp,
                    })
                changed = True
    if changed:
        _atomic_csv(rows)
    _sync_sheet_if_configured(rows)
    return rows


def _sync_sheet_if_configured(rows):
    """Optional Sheets sync; kept off unless SHEET_ID is explicitly supplied."""
    sheet_id = os.getenv("SHEET_ID", "").strip()
    if not sheet_id:
        return
    try:
        from .auth import get_service
        service = get_service("sheets")
        values = [FIELDS] + [[row.get(field, "") for field in FIELDS] for row in rows]
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id, range="Videos!A1", valueInputOption="RAW",
            body={"values": values},
        ).execute()
    except Exception as exc:
        print(f"[Growth] Optional Google Sheet sync failed: {exc}")

