"""Opt-in top-level video comments; never attempts to pin comments."""
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .tracker import DATA_DIR, read_videos
from retry import create_groq_client, retry_groq, call_google_write, retry_google_read

COMMENTS_FILE = DATA_DIR / "comments.json"
TO_PIN_FILE = DATA_DIR / "to_pin.txt"


def _atomic_json(path, value):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.stem + "-", suffix=".tmp", dir=str(DATA_DIR))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _atomic_text(path, text):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.stem + "-", suffix=".tmp", dir=str(DATA_DIR))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _comment_for(row, generator=None, avoid=()):
    prompt = ("Write one brief, specific, non-spammy top-level YouTube comment ending in a natural question. "
              "No hashtags, promotional links, or generic engagement bait. Make the wording distinct from these recent comments: "
              + json.dumps(list(avoid)[-20:], ensure_ascii=False) + ". Video title: " + row.get("title", ""))
    if generator:
        return str(generator(prompt)).strip()
    from config import GROQ_MODEL
    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise RuntimeError("GROQ_API_KEY missing; cannot draft comments.")
    client = create_groq_client(key)
    response = retry_groq(lambda: client.chat.completions.create(
        model=GROQ_MODEL, messages=[{"role": "user", "content": prompt}]
    ))
    return (response.choices[0].message.content or "").strip()


def post_comments(youtube, dry_run=False, enabled=False, rows=None, generator=None):
    if not enabled:
        print("COMMENT_ENABLED=false; comments are disabled.")
        return []
    posted = json.loads(COMMENTS_FILE.read_text(encoding="utf-8")) if COMMENTS_FILE.exists() else {}
    rows = rows if rows is not None else read_videos()
    pending_ids = [row["video_id"] for row in rows if row.get("video_id") and row["video_id"] not in posted]
    public = {}
    for offset in range(0, len(pending_ids), 50):
        ids = pending_ids[offset:offset + 50]
        response = retry_google_read(
            lambda: youtube.videos().list(part="snippet,status", id=",".join(ids)).execute(),
            task="Comment eligibility lookup",
        )
        for item in response.get("items", []):
            if item.get("status", {}).get("privacyStatus") == "public":
                public[item["id"]] = item
    result = []
    pin_links = []
    for row in rows:
        vid = row.get("video_id")
        if not vid or vid not in public or vid in posted:
            continue
        comment = _comment_for(row, generator, [item.get("text", "") for item in posted.values()])
        if not comment:
            continue
        if dry_run:
            print(f"[DRY RUN] Would comment on https://youtu.be/{vid}: {comment}")
        else:
            call_google_write(lambda: youtube.commentThreads().insert(
                part="snippet",
                body={"snippet": {"videoId": vid, "topLevelComment": {"snippet": {"textOriginal": comment}}}},
            ).execute(), task=f"Comment insert {vid}")
            posted[vid] = {"text": comment, "posted_at": datetime.now(timezone.utc).isoformat()}
            _atomic_json(COMMENTS_FILE, posted)
            print(f"Comment posted on https://youtu.be/{vid}.")
        result.append({"video_id": vid, "text": comment})
        if not dry_run:
            pin_links.append(f"https://studio.youtube.com/video/{vid}/comments")
    new_pin_links = list(pin_links)
    if posted:
        pin_links = [f"https://studio.youtube.com/video/{video_id}/comments" for video_id in posted]
        _atomic_text(TO_PIN_FILE, "\n".join(pin_links) + "\n")
    if new_pin_links and not dry_run:
        try:
            from notify import broadcast_message
            broadcast_message("Comments ready for manual pinning", "\n".join(new_pin_links))
        except Exception as exc:
            print(f"[Growth] Pin reminder notification skipped: {exc}")
    return result

