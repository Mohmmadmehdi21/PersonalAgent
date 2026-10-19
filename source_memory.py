"""Persistent Rejection Memory and Robust Source Candidate Selection System.

Provides:
1. Persistent rejection memory (data/rejected_ids.json) for permanently rejected IDs
   (Shorts, too-short videos, unavailable, deleted, live streams, or max retried).
2. Direct URLs prioritization: evaluates direct long-form URLs from sources.txt BEFORE
   channel discovery fallback.
3. Accurate capacity counting: max_candidates cap only counts candidates that PASSED screening.
4. Quota-efficient pre-screening: batch metadata checks via YouTube Data API (up to 50 IDs / 1 quota unit).
5. Transient error tracking with cooldowns (TRANSIENT_COOLDOWN_HOURS default 12, MAX_TRANSIENT_ATTEMPTS default 5).
6. Atomic line deletion from sources.txt for rejected direct URLs, with audit logging to remove.txt.
7. Support for SOURCE_PICK_MODE=ordered (default) or random.
8. Controlled by REJECT_MEMORY_ENABLED=true (default); fail-open fallback if false or on error.
"""

import os
import sys
import re
import json
import time
import random
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, unquote

DATA_DIR = "data"
REJECTED_FILE = os.path.join(DATA_DIR, "rejected_ids.json")
SOURCES_FILE = "sources.txt"
REMOVE_FILE = "remove.txt"

# Default seed rejections
SEED_REJECTIONS = {
    "_JUlWtSCNbs": {
        "reason_code": "SHORT_OR_TOO_SHORT",
        "reason_text": "YouTube Short (duration <= 60s) from @MrBeast channel lookup; cannot make 25-60s clips",
        "url": "https://www.youtube.com/watch?v=_JUlWtSCNbs",
        "title": "MrBeast Short",
        "duration": "0:45",
        "views": "1000000000",
        "source_origin": "seed",
        "timestamp": "2026-10-18 16:15:00",
        "removed_from_sources": False
    },
    "rqsRdfdN4uI": {
        "reason_code": "SHORT_OR_TOO_SHORT",
        "reason_text": "YouTube Short (duration <= 60s) from @MrBeast channel lookup; cannot make 25-60s clips",
        "url": "https://www.youtube.com/watch?v=rqsRdfdN4uI",
        "title": "MrBeast Short",
        "duration": "0:50",
        "views": "800000000",
        "source_origin": "seed",
        "timestamp": "2026-10-18 16:15:00",
        "removed_from_sources": False
    },
    "897gvApD6V4": {
        "reason_code": "SHORT_OR_TOO_SHORT",
        "reason_text": "YouTube Short (duration <= 60s) from @MrBeast channel lookup; cannot make 25-60s clips",
        "url": "https://www.youtube.com/watch?v=897gvApD6V4",
        "title": "MrBeast Short",
        "duration": "0:35",
        "views": "600000000",
        "source_origin": "seed",
        "timestamp": "2026-10-18 16:15:00",
        "removed_from_sources": False
    },
    "B9LizOAAoLg": {
        "reason_code": "SHORT_OR_TOO_SHORT",
        "reason_text": "YouTube Short (duration <= 60s) from @MrBeast channel lookup; cannot make 25-60s clips",
        "url": "https://www.youtube.com/watch?v=B9LizOAAoLg",
        "title": "MrBeast Short",
        "duration": "0:58",
        "views": "700000000",
        "source_origin": "seed",
        "timestamp": "2026-10-18 16:15:00",
        "removed_from_sources": False
    },
    "QAkEMtJpq3c": {
        "reason_code": "SHORT_OR_TOO_SHORT",
        "reason_text": "YouTube Short (duration <= 60s) from @MrBeast channel lookup; cannot make 25-60s clips",
        "url": "https://www.youtube.com/watch?v=QAkEMtJpq3c",
        "title": "MrBeast Short",
        "duration": "0:42",
        "views": "650000000",
        "source_origin": "seed",
        "timestamp": "2026-10-18 16:15:00",
        "removed_from_sources": False
    },
    "hYahU_Cqwp8": {
        "reason_code": "SHORT_OR_TOO_SHORT",
        "reason_text": "YouTube Short (duration <= 60s) from @MrBeast channel lookup; cannot make 25-60s clips",
        "url": "https://www.youtube.com/watch?v=hYahU_Cqwp8",
        "title": "MrBeast Short",
        "duration": "0:55",
        "views": "600000000",
        "source_origin": "seed",
        "timestamp": "2026-10-18 16:15:00",
        "removed_from_sources": False
    },
    "good2222222": {
        "reason_code": "TEST_DATA",
        "reason_text": "Test fixture entry from guard duplicate ledger test suite",
        "url": "https://www.youtube.com/watch?v=good2222222",
        "title": "Test Fixture Video",
        "duration": "10:00",
        "views": "1000",
        "source_origin": "seed",
        "timestamp": "2026-10-18 16:30:00",
        "removed_from_sources": False
    }
}

_quota_units_used = 0
_last_exhaustion_summary = ""


def is_reject_memory_enabled():
    """Returns True if persistent rejection memory is enabled (default True)."""
    val = os.getenv("REJECT_MEMORY_ENABLED", "true").strip().lower()
    return val in ("true", "1", "yes")


def transient_cooldown_hours():
    try:
        return float(os.getenv("TRANSIENT_COOLDOWN_HOURS", "12"))
    except (ValueError, TypeError):
        return 12.0


def max_transient_attempts():
    try:
        return int(os.getenv("MAX_TRANSIENT_ATTEMPTS", "5"))
    except (ValueError, TypeError):
        return 5


def max_channel_pages():
    try:
        return int(os.getenv("MAX_CHANNEL_PAGES", "3"))
    except (ValueError, TypeError):
        return 3


def source_pick_mode():
    return os.getenv("SOURCE_PICK_MODE", "ordered").strip().lower()


def min_source_duration_sec():
    try:
        return float(os.getenv("MIN_SOURCE_DURATION_SEC", "480"))
    except (ValueError, TypeError):
        return 480.0


def get_quota_used():
    return _quota_units_used


def reset_quota_counter():
    global _quota_units_used
    _quota_units_used = 0


def get_exhaustion_summary():
    return _last_exhaustion_summary


def extract_video_id(url):
    """Canonical 11-char YouTube ID extraction from any URL form."""
    if not url or not isinstance(url, str):
        return None
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    host = (parsed.hostname or "").lower()
    path_parts = [p for p in unquote(parsed.path).split("/") if p]

    candidate = None
    if host in ("youtu.be", "www.youtu.be") and path_parts:
        candidate = path_parts[0]
    elif parsed.path == "/watch" or parsed.path.endswith("/watch"):
        candidate = parse_qs(parsed.query).get("v", [""])[0]
    elif len(path_parts) >= 2 and path_parts[0] in ("shorts", "embed", "live"):
        candidate = path_parts[1]
    elif len(path_parts) == 1 and host.endswith("youtube.com") and path_parts[0] not in ("channel", "c", "user"):
        candidate = path_parts[0]

    return candidate if candidate and re.fullmatch(r"[\w-]{11}", candidate) else None


def is_channel_url(url):
    if not url or not isinstance(url, str):
        return False
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if host != "youtube.com" and not host.endswith(".youtube.com"):
        return False
    path = unquote(parsed.path).strip("/")
    first = path.split("/", 1)[0]
    return first.startswith("@") or first in ("channel", "user", "c")


def load_memory():
    """
    Loads rejected_ids memory from data/rejected_ids.json.
    If corrupted or missing, backs up if possible and initializes with seed entries.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(REJECTED_FILE):
        data = {"rejected": dict(SEED_REJECTIONS), "transient": {}}
        save_memory(data)
        _sync_seeds_to_remove_txt()
        return data

    try:
        with open(REJECTED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and "rejected" in data:
                # Ensure seeds are present
                for seed_id, seed_info in SEED_REJECTIONS.items():
                    if seed_id not in data["rejected"]:
                        data["rejected"][seed_id] = seed_info
                data.setdefault("transient", {})
                return data
            raise ValueError("Root object must contain 'rejected' dict")
    except Exception as err:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        corrupt_backup = os.path.join(DATA_DIR, f"rejected_ids.json.corrupt_{stamp}.bak")
        print(f"[SourceMemory] WARNING: Rejection memory '{REJECTED_FILE}' is corrupt ({err})! Backing up to '{corrupt_backup}'.")
        try:
            if os.path.exists(REJECTED_FILE):
                os.replace(REJECTED_FILE, corrupt_backup)
        except Exception as ren_err:
            print(f"[SourceMemory] Could not rename corrupt memory file: {ren_err}")
        fresh_data = {"rejected": dict(SEED_REJECTIONS), "transient": {}}
        save_memory(fresh_data)
        _sync_seeds_to_remove_txt()
        return fresh_data


def save_memory(data):
    """Atomically writes memory to data/rejected_ids.json via temp file + os.replace."""
    os.makedirs(DATA_DIR, exist_ok=True)
    temp_fd, temp_path = tempfile.mkstemp(prefix="rejected_ids_", suffix=".tmp", dir=DATA_DIR)
    try:
        encoded = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        with os.fdopen(temp_fd, "wb") as f:
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, REJECTED_FILE)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def is_rejected(video_id):
    """Returns True if video_id is permanently rejected."""
    if not is_reject_memory_enabled() or not video_id:
        return False
    data = load_memory()
    return video_id in data.get("rejected", {})


def is_in_cooldown(video_id):
    """Returns True if video_id is currently in transient failure cooldown."""
    if not is_reject_memory_enabled() or not video_id:
        return False
    data = load_memory()
    transient = data.get("transient", {}).get(video_id)
    if not transient:
        return False
    cooldown_until = transient.get("cooldown_until", 0)
    return time.time() < cooldown_until


def get_rejected_ids():
    """Returns set of all permanently rejected video IDs."""
    if not is_reject_memory_enabled():
        return set()
    data = load_memory()
    return set(data.get("rejected", {}).keys())


def parse_iso8601_duration(duration_str):
    """Parses ISO 8601 duration (e.g. PT15M33S, PT1H2M10S, PT45S) into integer seconds."""
    if not duration_str or not isinstance(duration_str, str):
        return None
    pattern = r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?"
    match = re.fullmatch(pattern, duration_str.strip())
    if not match:
        return None
    parts = match.groupdict()
    days = int(parts.get("days") or 0)
    hours = int(parts.get("hours") or 0)
    minutes = int(parts.get("minutes") or 0)
    seconds = int(parts.get("seconds") or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _format_duration(seconds):
    if seconds is None:
        return "unknown"
    try:
        s = int(seconds)
        m = s // 60
        sec = s % 60
        return f"{m}:{sec:02d}"
    except (ValueError, TypeError):
        return str(seconds)


def append_remove_audit(video_id, reason_code, *, url=None, title=None, duration=None,
                        views=None, found_via=None, why_removed=None, why_not_again=None,
                        removed_from_sources_line=None):
    """
    Appends one block to remove.txt matching the required audit pattern.
    Header is created if file is new.
    """
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    full_url = url or f"https://www.youtube.com/watch?v={video_id}"
    v_title = title or "unknown"
    v_dur = duration if duration is not None else "unknown"
    v_views = str(views) if views is not None else "unknown"
    v_found = found_via or "channel lookup"
    v_why = why_removed or f"Rejected due to {reason_code}"
    v_why_not = why_not_again or "Candidate does not meet content quality, duration, or licensing requirements"

    if removed_from_sources_line:
        removed_str = f"YES (line {removed_from_sources_line})"
    else:
        removed_str = "NO (it was never a line in sources.txt)"

    block = (
        "----- REMOVE.TXT PATTERN -----\n"
        f"REMOVED {now_str} | {video_id} | {reason_code}\n"
        f"URL: {full_url}\n"
        f"Title: {v_title}\n"
        f"Duration: {v_dur}    Views: {v_views}\n"
        f"Found via: {v_found}\n"
        f"Why removed: {v_why}\n"
        f"Why it must NOT be used again: {v_why_not}\n"
        f"Removed from sources.txt?: {removed_str}\n"
        f"Re-add allowed?: NO (only via: python sources_manager.py restore {video_id})\n"
        "MY CHECK: [ ] agree   [ ] wrong, restore it\n"
        "-------------------------------\n"
    )

    header = "IDs listed here are never selected or added back automatically.\n\n"
    try:
        file_exists = os.path.exists(REMOVE_FILE)
        with open(REMOVE_FILE, "a", encoding="utf-8") as f:
            if not file_exists or os.path.getsize(REMOVE_FILE) == 0:
                f.write(header)
            f.write(block)
    except Exception as e:
        print(f"[SourceMemory] Warning: could not append to {REMOVE_FILE}: {e}")


def _sync_seeds_to_remove_txt():
    """Initializes remove.txt with seed entries if remove.txt does not exist."""
    if os.path.exists(REMOVE_FILE) and os.path.getsize(REMOVE_FILE) > 0:
        return
    for vid, info in SEED_REJECTIONS.items():
        append_remove_audit(
            vid,
            info["reason_code"],
            url=info.get("url"),
            title=info.get("title"),
            duration=info.get("duration"),
            views=info.get("views"),
            found_via=info.get("source_origin"),
            why_removed=info.get("reason_text"),
            why_not_again="Short video cannot make 25-60s commentary clips" if info["reason_code"] == "SHORT_OR_TOO_SHORT" else "Test fixture data",
            removed_from_sources_line=None
        )


def remove_id_from_sources_txt(video_id):
    """
    Atomically removes any direct URL line for video_id from sources.txt.
    Preserves comments, blank lines, CRLF/LF line endings, BOM, and ordering (Rule R6).
    Never removes channel URLs.
    Returns (removed_bool, line_number).
    """
    if not os.path.exists(SOURCES_FILE) or not video_id:
        return False, -1

    try:
        with open(SOURCES_FILE, "rb") as f:
            raw_bytes = f.read()
    except Exception as e:
        print(f"[SourceMemory] Warning: could not read {SOURCES_FILE}: {e}")
        return False, -1

    has_bom = raw_bytes.startswith(b"\xef\xbb\xbf")
    encoding = "utf-8-sig" if has_bom else "utf-8"
    crlf = b"\r\n" in raw_bytes
    newline = "\r\n" if crlf else "\n"

    try:
        lines = raw_bytes.decode(encoding).splitlines(keepends=True)
    except Exception as e:
        print(f"[SourceMemory] Warning: decode failed for {SOURCES_FILE}: {e}")
        return False, -1

    target_line_num = -1
    new_lines = []

    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue

        raw_url = stripped.split("|", 1)[0].strip() if "|" in stripped else stripped
        if is_channel_url(raw_url):
            # Channel lines are never deleted by code
            new_lines.append(line)
            continue

        vid = extract_video_id(raw_url)
        if vid == video_id and target_line_num == -1:
            target_line_num = idx
            # Exclude this line
            continue
        new_lines.append(line)

    if target_line_num == -1:
        return False, -1

    # Atomic write
    temp_dir = os.path.dirname(os.path.abspath(SOURCES_FILE)) or "."
    temp_fd, temp_path = tempfile.mkstemp(prefix="sources_", suffix=".tmp", dir=temp_dir)
    try:
        content_str = "".join(new_lines)
        encoded = content_str.encode("utf-8-sig" if has_bom else "utf-8")
        with os.fdopen(temp_fd, "wb") as f:
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, SOURCES_FILE)
        return True, target_line_num
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def mark_rejected(video_id, reason_code, reason_text, *, url=None, title=None,
                  duration=None, views=None, source_origin=None):
    """
    Permanently marks video_id as rejected in rejected_ids.json, removes from sources.txt
    if it was a direct URL line, and writes an audit block to remove.txt.
    """
    if not video_id:
        return
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    full_url = url or f"https://www.youtube.com/watch?v={video_id}"

    # Remove from sources.txt if present
    removed_from_src, line_num = remove_id_from_sources_txt(video_id)

    found_desc = source_origin or (f"sources.txt line {line_num}" if removed_from_src else "channel lookup")

    data = load_memory()
    data["rejected"][video_id] = {
        "video_id": video_id,
        "url": full_url,
        "title": title or "unknown",
        "duration": _format_duration(duration),
        "views": str(views) if views is not None else "unknown",
        "reason_code": reason_code,
        "reason_text": reason_text,
        "source_origin": found_desc,
        "timestamp": now_str,
        "removed_from_sources": removed_from_src
    }
    # Clear any transient record if permanently rejected
    if video_id in data.get("transient", {}):
        del data["transient"][video_id]

    save_memory(data)

    why_not = "It is a Short (<60s) or too short (<8m) and cannot make 25-60s commentary clips" if reason_code == "SHORT_OR_TOO_SHORT" else f"Video marked invalid ({reason_code})"
    append_remove_audit(
        video_id,
        reason_code,
        url=full_url,
        title=title,
        duration=_format_duration(duration),
        views=views,
        found_via=found_desc,
        why_removed=reason_text,
        why_not_again=why_not,
        removed_from_sources_line=line_num if removed_from_src else None
    )


def mark_transient_failure(video_id, error_msg):
    """
    Records a transient failure (e.g. bot verification, HTTP 429, network).
    Returns True if escalated to permanent rejection (after MAX_TRANSIENT_ATTEMPTS),
    False if put on cooldown.
    """
    if not video_id:
        return False
    data = load_memory()
    transient = data.setdefault("transient", {})
    record = transient.get(video_id, {
        "attempts": 0,
        "first_attempt": datetime.now(timezone.utc).isoformat(),
        "last_attempt": "",
        "cooldown_until": 0,
        "last_error": ""
    })

    record["attempts"] += 1
    record["last_attempt"] = datetime.now(timezone.utc).isoformat()
    record["last_error"] = str(error_msg)[:300]

    max_att = max_transient_attempts()
    cd_hours = transient_cooldown_hours()

    if record["attempts"] >= max_att:
        # Escalate to permanent rejection
        mark_rejected(
            video_id,
            "REPEATED_FAILURES",
            f"Exceeded max transient attempts ({record['attempts']}/{max_att}): {error_msg}"
        )
        return True
    else:
        record["cooldown_until"] = time.time() + (cd_hours * 3600)
        transient[video_id] = record
        save_memory(data)
        print(f"[SourceMemory] Video '{video_id}' placed on {cd_hours:.1f}h cooldown (attempt {record['attempts']}/{max_att}): {error_msg}")
        return False


def prescreen_candidates_batch(video_ids, yt_service=None):
    """
    Cheaply pre-screens candidates by metadata in batches of up to 50 via YouTube Data API
    (1 quota unit per 50 IDs).
    Returns dict: vid -> {usable: bool, reason_code: str, reason_text: str, title: str, duration: int, views: int}
    """
    global _quota_units_used
    if not video_ids:
        return {}

    results = {}
    remaining_ids = list(video_ids)

    # 1. YouTube Data API batching
    if yt_service is not None:
        for i in range(0, len(remaining_ids), 50):
            chunk = remaining_ids[i:i+50]
            try:
                from retry import retry_google_read
                res = retry_google_read(
                    lambda: yt_service.videos().list(
                        part="contentDetails,snippet,status,statistics",
                        id=",".join(chunk)
                    ).execute(),
                    task="YouTube source pre-screen"
                )
                _quota_units_used += 1

                found_ids = set()
                for item in res.get("items", []):
                    vid = item.get("id")
                    found_ids.add(vid)
                    snippet = item.get("snippet", {})
                    content = item.get("contentDetails", {})
                    status = item.get("status", {})
                    stats = item.get("statistics", {})

                    title = snippet.get("title", "(untitled)")
                    views = stats.get("viewCount", "0")
                    dur_str = content.get("duration")
                    dur_sec = parse_iso8601_duration(dur_str)

                    # Check privacy / deleted
                    privacy = status.get("privacyStatus", "public")
                    upload_status = status.get("uploadStatus", "processed")
                    if privacy in ("private", "") or upload_status in ("rejected", "failed"):
                        results[vid] = {
                            "usable": False,
                            "reason_code": "PRIVATE_OR_DELETED",
                            "reason_text": f"Video privacyStatus={privacy}, uploadStatus={upload_status}",
                            "title": title, "duration": dur_sec, "views": views
                        }
                        continue

                    # Check live stream
                    live_broadcast = snippet.get("liveBroadcastContent", "none")
                    if live_broadcast in ("live", "upcoming"):
                        results[vid] = {
                            "usable": False,
                            "reason_code": "LIVE_OR_UPCOMING",
                            "reason_text": f"Video is live or upcoming stream (liveBroadcastContent={live_broadcast})",
                            "title": title, "duration": dur_sec, "views": views
                        }
                        continue

                    # Check Short and minimum duration
                    min_dur = min_source_duration_sec()
                    if dur_sec is not None:
                        if dur_sec <= 60:
                            results[vid] = {
                                "usable": False,
                                "reason_code": "SHORT_OR_TOO_SHORT",
                                "reason_text": f"Video is a Short (duration {dur_sec}s <= 60s)",
                                "title": title, "duration": dur_sec, "views": views
                            }
                            continue
                        if dur_sec < min_dur:
                            results[vid] = {
                                "usable": False,
                                "reason_code": "SHORT_OR_TOO_SHORT",
                                "reason_text": f"Video duration {dur_sec}s < required {min_dur}s",
                                "title": title, "duration": dur_sec, "views": views
                            }
                            continue

                    results[vid] = {
                        "usable": True,
                        "reason_code": "USABLE",
                        "reason_text": "Passed long-form pre-screening",
                        "title": title, "duration": dur_sec, "views": views
                    }

                # Missing items were deleted or invalid ID
                for vid in chunk:
                    if vid not in found_ids and vid not in results:
                        results[vid] = {
                            "usable": False,
                            "reason_code": "PRIVATE_OR_DELETED",
                            "reason_text": "Video ID not found via YouTube Data API (deleted or private)",
                            "title": "unknown", "duration": None, "views": None
                        }
            except Exception as e:
                # Fail-open / fallback on API error
                print(f"[SourceMemory] Warning: API pre-screen chunk failed ({e}); falling back to metadata checks.")
                break

    # 2. Fallback to source_filter duration fetching for any remaining unknown IDs
    unknown_ids = [vid for vid in video_ids if vid not in results]
    if unknown_ids:
        try:
            import source_filter
            durations = source_filter.get_video_durations(unknown_ids)
            min_dur = min_source_duration_sec()
            for vid in unknown_ids:
                dur = durations.get(vid)
                if dur is not None:
                    if dur <= 60:
                        results[vid] = {
                            "usable": False,
                            "reason_code": "SHORT_OR_TOO_SHORT",
                            "reason_text": f"Video is a Short (duration {dur}s <= 60s)",
                            "title": "unknown", "duration": dur, "views": None
                        }
                    elif dur < min_dur:
                        results[vid] = {
                            "usable": False,
                            "reason_code": "SHORT_OR_TOO_SHORT",
                            "reason_text": f"Video duration {dur}s < required {min_dur}s",
                            "title": "unknown", "duration": dur, "views": None
                        }
                    else:
                        results[vid] = {
                            "usable": True,
                            "reason_code": "USABLE",
                            "reason_text": "Passed long-form duration check",
                            "title": "unknown", "duration": dur, "views": None
                        }
                else:
                    # Fail-open per R2/Part A: if duration cannot be fetched, allow candidate
                    results[vid] = {
                        "usable": True,
                        "reason_code": "UNKNOWN_FAIL_OPEN",
                        "reason_text": "Duration unknown; fail-open allowed",
                        "title": "unknown", "duration": None, "views": None
                    }
        except Exception:
            for vid in unknown_ids:
                results[vid] = {
                    "usable": True,
                    "reason_code": "UNKNOWN_FAIL_OPEN",
                    "reason_text": "Duration unknown; fail-open allowed",
                    "title": "unknown", "duration": None, "views": None
                }

    return results


def select_candidates(state, max_candidates=5, yt_service=None):
    """
    Selects up to max_candidates usable long-form videos:
    1. Direct video URLs evaluated BEFORE any channel discovery.
    2. Rejected Shorts/too-short videos do NOT fill the cap; selector continues until
       max_candidates USABLE videos are found or sources exhausted.
    3. Cheap batch pre-screening via YouTube API.
    4. Channel fallback (up to MAX_CHANNEL_PAGES / 150 IDs) only when direct URLs exhausted.
    5. Returns list of (url, note) tuples.
    """
    global _last_exhaustion_summary
    if not is_reject_memory_enabled():
        return None  # Let caller fall back to legacy next_source_candidates

    if not os.path.exists(SOURCES_FILE):
        _last_exhaustion_summary = "sources.txt does not exist"
        return []

    # Collect known processed IDs
    state.setdefault("done_videos", [])
    processed_ids = set(state["done_videos"])
    processed_ids.update(state.get("skipped_video_ids", []))
    processed_ids.update(state.get("failed_video_ids", []))
    processed_ids.update(
        key.split(":", 1)[0]
        for key in state.get("clips", [])
        if ":" in key
    )
    from source import video_id_from_url
    processed_ids.update(
        video_id for video_id in
        (video_id_from_url(url) for url in state.get("done", []))
        if video_id
    )

    # Check guard ledger
    try:
        import guard
        ledger = guard.load_ledger()
        for vid, entry in ledger.items():
            if entry.get("status") == "consumed" or len(entry.get("clips", [])) >= guard.max_clips_per_video():
                processed_ids.add(vid)
    except Exception:
        pass

    # Read sources.txt
    direct_entries = []
    channel_entries = []
    with open(SOURCES_FILE, "r", encoding="utf-8-sig") as f:
        for idx, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            note = ""
            if "|" in stripped:
                parts = stripped.split("|", 1)
                u = parts[0].strip()
                note = parts[1].strip()
            else:
                u = stripped

            if is_channel_url(u):
                channel_entries.append((u, note, idx))
            else:
                vid = extract_video_id(u)
                if vid:
                    direct_entries.append((u, note, idx, vid))

    # Rejection memory & cooldown
    mem = load_memory()
    rejected_ids = set(mem.get("rejected", {}).keys())

    # Candidate tracking
    total_checked = 0
    total_rejected = 0
    usable_candidates = []
    seen_ids = set(processed_ids)
    seen_ids.update(rejected_ids)

    # Order selection for direct entries
    pick_mode = source_pick_mode()
    unscreened_direct = [
        item for item in direct_entries
        if item[3] not in seen_ids and not is_in_cooldown(item[3])
    ]

    if pick_mode == "random":
        random.seed(int(time.time() * 1000) % 100000)
        random.shuffle(unscreened_direct)

    # 1. EVALUATE DIRECT VIDEO URLS FIRST
    # Batch pre-screen un-screened direct URLs
    batch_size = 50
    for i in range(0, len(unscreened_direct), batch_size):
        if len(usable_candidates) >= max_candidates:
            break
        chunk = unscreened_direct[i:i+batch_size]
        chunk_ids = [c[3] for c in chunk]
        total_checked += len(chunk_ids)

        screen_results = prescreen_candidates_batch(chunk_ids, yt_service=yt_service)

        for url, note, line_num, vid in chunk:
            res = screen_results.get(vid, {})
            if res.get("usable"):
                usable_candidates.append((url, note))
                seen_ids.add(vid)
                if len(usable_candidates) >= max_candidates:
                    break
            else:
                total_rejected += 1
                reason_code = res.get("reason_code", "SHORT_OR_TOO_SHORT")
                reason_text = res.get("reason_text", "Rejected during pre-screening")
                mark_rejected(
                    vid, reason_code, reason_text,
                    url=url, title=res.get("title"),
                    duration=res.get("duration"), views=res.get("views"),
                    source_origin=f"sources.txt line {line_num}"
                )
                seen_ids.add(vid)

    # 2. FALLBACK TO CHANNEL DISCOVERY ONLY IF NEEDED
    if len(usable_candidates) < max_candidates and channel_entries:
        if yt_service is None:
            try:
                from upload import get_service
                yt_service = get_service()
            except Exception as e:
                print(f"[SourceMemory] Channel discovery fallback: could not get YouTube service ({e})")

        if yt_service is not None:
            from source import resolve_channel_id
            from retry import retry_google_read

            max_pages = max_channel_pages()
            for ch_url, ch_note, ch_line in channel_entries:
                if len(usable_candidates) >= max_candidates:
                    break
                try:
                    ch_id = resolve_channel_id(yt_service, ch_url)
                except Exception as e:
                    print(f"[SourceMemory] Channel lookup failed for {ch_url}: {e}")
                    continue

                page_token = None
                for _ in range(max_pages):
                    if len(usable_candidates) >= max_candidates:
                        break
                    try:
                        res = retry_google_read(lambda: yt_service.search().list(
                            part="snippet",
                            channelId=ch_id,
                            type="video",
                            order="date",
                            maxResults=50,
                            pageToken=page_token,
                        ).execute(), task="YouTube source search")
                    except Exception as e:
                        print(f"[SourceMemory] Channel search page failed: {e}")
                        break

                    search_items = [
                        item for item in res.get("items", [])
                        if item.get("id", {}).get("videoId")
                        and item["id"]["videoId"] not in seen_ids
                        and not is_in_cooldown(item["id"]["videoId"])
                    ]

                    if search_items:
                        ch_chunk_ids = [it["id"]["videoId"] for it in search_items]
                        total_checked += len(ch_chunk_ids)
                        screen_results = prescreen_candidates_batch(ch_chunk_ids, yt_service=yt_service)

                        for it in search_items:
                            vid = it["id"]["videoId"]
                            v_url = f"https://www.youtube.com/watch?v={vid}"
                            res = screen_results.get(vid, {})
                            if res.get("usable"):
                                usable_candidates.append((v_url, ch_note))
                                seen_ids.add(vid)
                                title = res.get("title") or it.get("snippet", {}).get("title", "(untitled)")
                                views = res.get("views", "unknown")
                                print(f"[Source] Usable candidate video selected: {title} ({views} views)")
                                if len(usable_candidates) >= max_candidates:
                                    break
                            else:
                                total_rejected += 1
                                reason_code = res.get("reason_code", "SHORT_OR_TOO_SHORT")
                                reason_text = res.get("reason_text", "Rejected during channel pre-screening")
                                # Do NOT delete from sources.txt since discovered via channel
                                mark_rejected(
                                    vid, reason_code, reason_text,
                                    url=v_url, title=res.get("title"),
                                    duration=res.get("duration"), views=res.get("views"),
                                    source_origin="channel lookup"
                                )
                                seen_ids.add(vid)

                    page_token = res.get("nextPageToken")
                    if not page_token:
                        break

    _last_exhaustion_summary = f"{total_checked} links checked, {total_rejected} rejected, {len(usable_candidates)} usable"
    if _quota_units_used > 0:
        print(f"[SourceMemory] Pre-screening used {_quota_units_used} YouTube API quota units.")

    return usable_candidates


def restore(video_id, *, dry_run=False):
    """
    Restores a rejected video ID:
    1. Removes from data/rejected_ids.json rejected list.
    2. Marks corresponding block in remove.txt as RESTORED.
    3. Re-adds the direct URL to sources.txt if not already present.
    """
    if not video_id:
        return False

    mem = load_memory()
    entry = mem.get("rejected", {}).get(video_id)
    if not entry:
        print(f"[SourceMemory] Video ID '{video_id}' is not in rejected list.")
        return False

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    target_url = entry.get("url") or f"https://www.youtube.com/watch?v={video_id}"

    if dry_run:
        print(f"[SourceMemory] [DRY RUN] Would restore '{video_id}' ({target_url}) to sources.txt.")
        return True

    # 1. Remove from rejected_ids.json
    del mem["rejected"][video_id]
    save_memory(mem)

    # 2. Mark remove.txt block RESTORED
    if os.path.exists(REMOVE_FILE):
        try:
            with open(REMOVE_FILE, "r", encoding="utf-8") as f:
                content = f.read()
            # Replace REMOVED line matching this video_id
            pattern = rf"(REMOVED\s+[^\n]+\|\s+{re.escape(video_id)}\s+\|[^\n]+)"
            new_content = re.sub(
                pattern,
                rf"RESTORED {now_str} (was: \1)",
                content
            )
            with open(REMOVE_FILE, "w", encoding="utf-8") as f:
                f.write(new_content)
        except Exception as e:
            print(f"[SourceMemory] Warning updating {REMOVE_FILE}: {e}")

    # 3. Re-add to sources.txt
    try:
        with open(SOURCES_FILE, "r", encoding="utf-8-sig") as f:
            lines = f.readlines()
        already_present = any(extract_video_id(l) == video_id for l in lines)
        if not already_present:
            crlf = any("\r\n" in l for l in lines)
            newline = "\r\n" if crlf else "\n"
            note_suffix = " | Restored candidate"
            new_line = f"{target_url}{note_suffix}{newline}"
            with open(SOURCES_FILE, "a", encoding="utf-8") as f:
                f.write(new_line)
            print(f"[SourceMemory] Restored '{video_id}' to {SOURCES_FILE}.")
    except Exception as e:
        print(f"[SourceMemory] Warning re-adding to {SOURCES_FILE}: {e}")

    return True


def get_status():
    """Returns comprehensive counts and the first 10 pending usable URLs."""
    total_lines = 0
    comments = 0
    blanks = 0
    channel_count = 0
    direct_count = 0
    direct_urls = []

    if os.path.exists(SOURCES_FILE):
        with open(SOURCES_FILE, "r", encoding="utf-8-sig") as f:
            for line in f:
                total_lines += 1
                stripped = line.strip()
                if not stripped:
                    blanks += 1
                    continue
                if stripped.startswith("#"):
                    comments += 1
                    continue
                u = stripped.split("|", 1)[0].strip() if "|" in stripped else stripped
                if is_channel_url(u):
                    channel_count += 1
                else:
                    vid = extract_video_id(u)
                    if vid:
                        direct_count += 1
                        direct_urls.append((u, vid))

    mem = load_memory()
    rejected_count = len(mem.get("rejected", {}))
    rejected_set = set(mem.get("rejected", {}).keys())

    consumed_set = set()
    try:
        import guard
        ledger = guard.load_ledger()
        for vid, entry in ledger.items():
            if entry.get("status") == "consumed" or len(entry.get("clips", [])) >= guard.max_clips_per_video():
                consumed_set.add(vid)
    except Exception:
        pass

    usable_urls = []
    for u, vid in direct_urls:
        if vid not in rejected_set and vid not in consumed_set and not is_in_cooldown(vid):
            usable_urls.append(u)

    return {
        "sources_total_lines": total_lines,
        "direct_urls": direct_count,
        "channel_urls": channel_count,
        "comment_lines": comments,
        "blank_lines": blanks,
        "rejected_ids_count": rejected_count,
        "guard_consumed_count": len(consumed_set),
        "usable_direct_count": len(usable_urls),
        "first_10_usable": usable_urls[:10]
    }
