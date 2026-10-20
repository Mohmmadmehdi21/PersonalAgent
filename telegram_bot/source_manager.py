"""Core Source Management Engine: Status, Refresh, Safe Cleanup, and Additions.

Handles:
1. Shared file locking for all source mutations (refresh, cleanup, add).
2. Authoritative used-state verification against state.json, used_videos.json, and queue.json.
3. Safe preview and confirmation lifecycle with atomic writes and timestamped backups.
4. Granular metrics recording for scheduler and Telegram monitoring.
"""
import copy
import csv
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from source import is_channel_url, video_id_from_url

SOURCES_FILE = Path("sources.txt")
STATE_FILE = Path("state.json")
USED_VIDEOS_FILE = Path("data/used_videos.json")
QUEUE_FILE = Path(os.getenv("REVIEW_DIR", "review")) / "queue.json"
SCHEDULER_FILE = Path("data/scheduler.json")
LOCK_FILE = Path("work/sources.lock")

_PREVIEWS = {}
_PENDING_INPUTS = {}


class SourceLock:
    """Inter-process and inter-thread lock for source operations."""

    def __init__(self, path=LOCK_FILE, timeout=15):
        self.path = Path(path)
        self.timeout = timeout
        self.held = False

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump({"pid": os.getpid(), "at": time.time()}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                self.held = True
                return True
            except FileExistsError:
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                    pid = data.get("pid")
                    from lock import is_pid_alive
                    if isinstance(pid, int) and pid > 0 and not is_pid_alive(pid):
                        self.path.unlink(missing_ok=True)
                        continue
                except (OSError, ValueError, TypeError):
                    try:
                        if time.time() - self.path.stat().st_mtime > 30:
                            self.path.unlink(missing_ok=True)
                            continue
                    except OSError:
                        pass
                time.sleep(0.1)
        return False

    def release(self):
        if self.held:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
            self.held = False

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("Another source operation is currently in progress.")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


def _read_sources_lines(sources_path=SOURCES_FILE):
    if not sources_path.exists():
        return []
    with open(sources_path, "r", encoding="utf-8", errors="replace") as handle:
        return [line.rstrip("\r\n") for line in handle]


def _read_json(path):
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def get_authoritative_state():
    """Load authoritative state ledgers to determine completed vs protected sources."""
    state = _read_json(STATE_FILE) or {}
    used_ledger = _read_json(USED_VIDEOS_FILE) or {}
    queue = _read_json(QUEUE_FILE) or []

    # 1. State done video IDs
    state_done = set(state.get("done_videos", []))
    for u in state.get("done", []):
        vid = video_id_from_url(u)
        if vid:
            state_done.add(vid)

    # 2. Used ledger consumed IDs
    consumed_ids = set()
    for vid, entry in used_ledger.items():
        if isinstance(entry, dict) and (entry.get("status") == "consumed" or len(entry.get("clips", [])) > 0):
            consumed_ids.add(str(vid))

    # 3. Active queue items - ANY video with active/pending/blocked clips is PROTECTED
    queue_unfinished = set()
    queue_uploaded = set()
    for item in queue:
        if not isinstance(item, dict):
            continue
        status = item.get("status", "")
        vid = item.get("source_id")
        if not vid:
            item_id = str(item.get("id", ""))
            if "_" in item_id:
                vid = item_id.split("_", 1)[0]
        if not vid:
            continue
        if status in {"pending", "processing", "needs_review", "failed", "blocked"}:
            queue_unfinished.add(str(vid))
        elif status == "uploaded":
            queue_uploaded.add(str(vid))

    # Conclusively used = in state_done or consumed_ids, AND NOT in queue_unfinished
    conclusively_used = (state_done | consumed_ids | queue_uploaded) - queue_unfinished

    return {
        "conclusively_used": conclusively_used,
        "protected_queue_ids": queue_unfinished,
        "state_done": state_done,
        "consumed_ids": consumed_ids,
    }


def get_sources_status(sources_path=SOURCES_FILE):
    """Compile comprehensive status of sources.txt, ledgers, and scheduler."""
    lines = _read_sources_lines(sources_path)
    auth_state = get_authoritative_state()
    conclusively_used = auth_state["conclusively_used"]

    total_lines = len(lines)
    comment_lines = 0
    blank_lines = 0
    channel_sources = []
    direct_video_ids = []
    direct_seen = set()
    duplicate_count = 0
    used_count = 0

    for line in lines:
        stripped = line.strip()
        if not stripped:
            blank_lines += 1
            continue
        if stripped.startswith("#"):
            comment_lines += 1
            continue
        url_part = stripped.split("|", 1)[0].strip()
        vid = video_id_from_url(url_part)
        if vid:
            if vid in direct_seen:
                duplicate_count += 1
            else:
                direct_seen.add(vid)
                direct_video_ids.append(vid)
            if vid in conclusively_used:
                used_count += 1
        elif is_channel_url(url_part):
            channel_sources.append(url_part)

    pending_count = len(direct_seen) - used_count

    # Scheduler info
    sched_state = _read_json(SCHEDULER_FILE) or {}
    jobs = sched_state.get("jobs", {})
    history = sched_state.get("source_refresh_history", [])

    last_attempt = None
    last_success = None
    last_result = None
    last_error = None

    # Check recent history
    if history:
        latest = history[-1]
        last_attempt = latest.get("ended_at") or latest.get("started_at")
        last_result = latest.get("status")
        last_error = latest.get("error")
        for h in reversed(history):
            if h.get("status") in {"success_new_sources", "success_no_new_sources"}:
                last_success = h.get("ended_at")
                break

    # Check daily jobs if history empty
    if not last_attempt:
        source_jobs = [j for k, j in jobs.items() if k.startswith("sources:")]
        if source_jobs:
            source_jobs.sort(key=lambda x: x.get("started_at", 0))
            latest_job = source_jobs[-1]
            last_attempt = latest_job.get("finished_at") or latest_job.get("started_at")
            last_result = latest_job.get("status")
            last_error = latest_job.get("error")

    # Scheduler configuration
    sched_config = sched_state.get("config", {})
    from telegram_bot.scheduler import _enabled, _clock
    scheduler_enabled = sched_config.get("scheduler_enabled", _enabled("SCHEDULER_ENABLED"))
    refresh_enabled = sched_config.get("source_refresh_enabled", _enabled("SCHEDULER_SOURCE_REFRESH_ENABLED"))
    timezone_str = os.getenv("SCHEDULER_TIMEZONE", "Asia/Kolkata").strip()
    refresh_time = os.getenv("SCHEDULER_SOURCE_REFRESH_TIME", "08:00").strip()

    return {
        "total_lines": total_lines,
        "active_entries": total_lines - comment_lines - blank_lines,
        "direct_video_count": len(direct_seen),
        "channel_count": len(channel_sources),
        "duplicate_count": duplicate_count,
        "used_count": used_count,
        "pending_count": max(0, pending_count),
        "last_attempt": last_attempt,
        "last_success": last_success,
        "last_result": last_result or "none recorded",
        "last_error": last_error,
        "scheduler_enabled": scheduler_enabled,
        "refresh_enabled": refresh_enabled,
        "timezone": timezone_str,
        "refresh_time": refresh_time,
    }


def get_source_summary(sources_path=SOURCES_FILE, limit=5):
    """Return overview breakdown and preview of next pending and used links."""
    lines = _read_sources_lines(sources_path)
    auth_state = get_authoritative_state()
    conclusively_used = auth_state["conclusively_used"]

    pending_list = []
    used_list = []
    channels = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        url_part = stripped.split("|", 1)[0].strip()
        note = stripped.split("|", 1)[1].strip() if "|" in stripped else ""
        vid = video_id_from_url(url_part)
        if vid:
            item = {"id": vid, "url": url_part, "note": note}
            if vid in conclusively_used:
                used_list.append(item)
            else:
                pending_list.append(item)
        elif is_channel_url(url_part):
            channels.append(url_part)

    return {
        "total_active": len(pending_list) + len(used_list) + len(channels),
        "pending_sample": pending_list[:limit],
        "used_sample": used_list[:limit],
        "channels": channels,
        "total_pending": len(pending_list),
        "total_used": len(used_list),
    }


def preview_cleanup(sources_path=SOURCES_FILE):
    """Perform read-only scan of sources.txt and return a cleanup proposal."""
    lines = _read_sources_lines(sources_path)
    auth_state = get_authoritative_state()
    conclusively_used = auth_state["conclusively_used"]

    eligible_removals = []
    duplicate_removals = []
    retained_lines = []
    seen_vids = set()

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            retained_lines.append(line)
            continue
        url_part = stripped.split("|", 1)[0].strip()
        vid = video_id_from_url(url_part)
        if vid:
            if vid in conclusively_used:
                eligible_removals.append({"index": idx, "line": line, "vid": vid, "reason": "Conclusively used in state ledger"})
                continue
            if vid in seen_vids:
                duplicate_removals.append({"index": idx, "line": line, "vid": vid, "reason": "Duplicate entry"})
                continue
            seen_vids.add(vid)
            retained_lines.append(line)
        else:
            # Channels, handles, or other lines are ALWAYS retained
            retained_lines.append(line)

    content_hash = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]
    preview_id = hashlib.sha256(f"{content_hash}:{time.time()}".encode("utf-8")).hexdigest()[:10]

    proposal = {
        "preview_id": preview_id,
        "content_hash": content_hash,
        "created_at": time.time(),
        "expires_at": time.time() + 300,
        "total_lines": len(lines),
        "eligible_used_count": len(eligible_removals),
        "duplicate_count": len(duplicate_removals),
        "retained_count": len(retained_lines),
        "eligible_removals": eligible_removals,
        "retained_lines": retained_lines,
    }

    _PREVIEWS[preview_id] = proposal
    return proposal


def execute_cleanup(preview_id, sources_path=SOURCES_FILE, chat_id=None, task_id=None):
    """Atomically execute approved cleanup with backup creation."""
    tracker = None
    if chat_id or task_id:
        try:
            import uuid
            from telegram_bot.progress import start_task_progress
            task_id = task_id or uuid.uuid4().hex[:12]
            tracker = start_task_progress(task_id, "source_cleanup", chat_id=chat_id, task_name="Source Cleanup")
        except Exception:
            pass

    proposal = _PREVIEWS.get(preview_id)
    if not proposal:
        if tracker:
            tracker.finish(status="FAILED", error="Cleanup preview expired or not found")
        raise ValueError("Cleanup preview expired or not found. Please request a fresh preview.")

    if time.time() > proposal.get("expires_at", 0):
        _PREVIEWS.pop(preview_id, None)
        if tracker:
            tracker.finish(status="FAILED", error="Cleanup confirmation expired")
        raise ValueError("Cleanup confirmation expired. Please request a fresh preview.")

    if tracker:
        tracker.start_stage("scan_ledger", detail="Verifying cleanup proposal and locking sources")

    with SourceLock():
        if tracker:
            tracker.complete_stage("scan_ledger", detail="Cleanup proposal and lock verified")
            tracker.start_stage("backup", detail="Creating timestamped backup of sources.txt")

        # Verify content has not changed since preview
        current_lines = _read_sources_lines(sources_path)
        current_hash = hashlib.sha256("\n".join(current_lines).encode("utf-8")).hexdigest()[:16]
        if current_hash != proposal["content_hash"]:
            _PREVIEWS.pop(preview_id, None)
            if tracker:
                tracker.finish(status="FAILED", error="sources.txt changed since preview was generated")
            raise ValueError("sources.txt changed since the preview was generated. Operation cancelled to protect state.")

        # 1. Create timestamped backup
        backup_stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = sources_path.parent / f"sources.txt.bak_{backup_stamp}"
        shutil.copy2(sources_path, backup_path)

        if tracker:
            tracker.complete_stage("backup", detail=f"Backup created: {backup_path.name}")
            tracker.start_stage("filter_sources", detail="Filtering used and duplicate entries")

        # 2. Write retained lines to temp file
        retained = proposal["retained_lines"]
        newline = "\r\n" if "\r\n" in sources_path.read_text(encoding="utf-8", errors="replace") else "\n"
        output_text = newline.join(retained)
        if output_text and not output_text.endswith(newline):
            output_text += newline

        if tracker:
            tracker.complete_stage("filter_sources", detail=f"Retained {len(retained)} lines, removed {proposal['eligible_used_count']} used, {proposal['duplicate_count']} duplicates")
            tracker.start_stage("persist_sources", detail="Writing updated sources atomically")

        temp_fd, temp_file = tempfile.mkstemp(prefix="sources_clean_", suffix=".tmp", dir=str(sources_path.parent))
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
                handle.write(output_text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_file, sources_path)
        finally:
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass

        _PREVIEWS.pop(preview_id, None)

        if tracker:
            tracker.complete_stage("persist_sources", detail="Sources updated successfully")
            tracker.finish(status="COMPLETED")

        return {
            "removed_used": proposal["eligible_used_count"],
            "removed_duplicates": proposal["duplicate_count"],
            "remaining_lines": len(retained),
            "backup_path": str(backup_path),
        }


def cancel_cleanup(preview_id):
    """Cancel cleanup preview safely."""
    _PREVIEWS.pop(preview_id, None)


def validate_new_source(raw_url, sources_path=SOURCES_FILE):
    """Validate a candidate YouTube URL and check for deduplication."""
    cleaned = (raw_url or "").strip()
    if not cleaned:
        raise ValueError("URL cannot be empty.")

    parsed = urlparse(cleaned)
    host = (parsed.hostname or "").lower()
    if host not in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}:
        raise ValueError("Only valid YouTube URLs (youtube.com or youtu.be) are supported.")

    vid = video_id_from_url(cleaned)
    is_channel = is_channel_url(cleaned)

    if not vid and not is_channel:
        raise ValueError("Invalid YouTube URL. Must be a direct video URL (watch?v= or youtu.be/) or channel/handle link.")

    lines = _read_sources_lines(sources_path)
    existing_vids = set()
    existing_channels = set()

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        url_part = stripped.split("|", 1)[0].strip()
        v = video_id_from_url(url_part)
        if v:
            existing_vids.add(v)
        elif is_channel_url(url_part):
            existing_channels.add(url_part.lower())

    auth_state = get_authoritative_state()

    if vid:
        normalized_url = f"https://www.youtube.com/watch?v={vid}"
        exists = vid in existing_vids
        is_used = vid in auth_state["conclusively_used"]
        is_blocked = vid in auth_state["protected_queue_ids"]
        source_type = "video"
    else:
        normalized_url = cleaned
        exists = cleaned.lower() in existing_channels
        is_used = False
        is_blocked = False
        source_type = "channel"

    candidate_id = hashlib.sha256(f"{normalized_url}:{time.time()}".encode("utf-8")).hexdigest()[:10]

    report = {
        "candidate_id": candidate_id,
        "raw_url": cleaned,
        "normalized_url": normalized_url,
        "source_type": source_type,
        "video_id": vid,
        "already_exists": exists,
        "already_used": is_used,
        "is_blocked_active": is_blocked,
        "expires_at": time.time() + 300,
    }

    _PREVIEWS[candidate_id] = report
    return report


def append_new_source(candidate_id, sources_path=SOURCES_FILE):
    """Atomically append validated source to sources.txt."""
    candidate = _PREVIEWS.get(candidate_id)
    if not candidate:
        raise ValueError("Source addition request expired or not found.")

    if time.time() > candidate.get("expires_at", 0):
        _PREVIEWS.pop(candidate_id, None)
        raise ValueError("Source addition confirmation expired. Please enter the URL again.")

    if candidate.get("already_exists"):
        _PREVIEWS.pop(candidate_id, None)
        raise ValueError(f"Source already exists in {sources_path.name}.")

    with SourceLock():
        existing_lines = _read_sources_lines(sources_path)
        url_to_add = candidate["normalized_url"]

        # Final check against concurrent addition
        for line in existing_lines:
            url_part = line.strip().split("|", 1)[0].strip()
            if candidate.get("video_id") and video_id_from_url(url_part) == candidate["video_id"]:
                _PREVIEWS.pop(candidate_id, None)
                raise ValueError("This video source was already added by another operation.")
            if url_part.lower() == url_to_add.lower():
                _PREVIEWS.pop(candidate_id, None)
                raise ValueError("This source was already added by another operation.")

        newline = "\r\n" if "\r\n" in sources_path.read_text(encoding="utf-8", errors="replace") else "\n"
        line_to_append = f"{url_to_add} | Added via Telegram on {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"

        temp_fd, temp_file = tempfile.mkstemp(prefix="sources_add_", suffix=".tmp", dir=str(sources_path.parent))
        try:
            with open(sources_path, "rb") as original:
                content = original.read()
            with os.fdopen(temp_fd, "wb") as handle:
                handle.write(content)
                if content and not content.endswith(b"\n") and not content.endswith(b"\r\n"):
                    handle.write(newline.encode("utf-8"))
                handle.write((line_to_append + newline).encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_file, sources_path)
        finally:
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass

        _PREVIEWS.pop(candidate_id, None)

        return {
            "added_url": url_to_add,
            "source_type": candidate["source_type"],
            "total_sources": len(existing_lines) + 1,
        }


def run_source_refresh(dest=SOURCES_FILE, chat_id=None, task_id=None):
    """Execute scheduled or manual source refresh with granular metric collection."""
    tracker = None
    if chat_id or task_id:
        try:
            import uuid
            from telegram_bot.progress import start_task_progress
            task_id = task_id or uuid.uuid4().hex[:12]
            tracker = start_task_progress(task_id, "source_refresh", chat_id=chat_id, task_name="Source Refresh")
        except Exception:
            pass

    if tracker:
        tracker.start_stage("init_lock", detail="Acquiring source lock")

    with SourceLock(timeout=10):
        if tracker:
            tracker.complete_stage("init_lock", detail="Source lock acquired")
            tracker.start_stage("channel_scan", detail="Scanning channel for new long videos")

        start_ts = time.time()
        run_id = hashlib.sha256(f"refresh:{start_ts}".encode("utf-8")).hexdigest()[:8]
        tz_name = os.getenv("SCHEDULER_TIMEZONE", "Asia/Kolkata").strip()

        # Step 1: Run make_sources
        make_result = {"scanned": 0, "error": None}
        try:
            from make_sources import main as make_main
            make_main()
        except Exception as exc:
            make_result["error"] = str(exc)

        if tracker:
            tracker.complete_stage("channel_scan", detail="Channel video discovery finished")
            tracker.start_stage("filter_merge", detail="Filtering duplicates and merging candidates")

        # Step 2: Run merge_sources
        merge_result = {
            "candidates": 0,
            "appended": 0,
            "skipped_duplicates": 0,
            "skipped_used": 0,
            "skipped_rejected": 0,
            "error": None,
        }
        dataset_file = Path("mrbeast_15min_plus.txt")
        if dataset_file.exists():
            try:
                from merge_sources import load_existing_sources_ids, load_state_known_ids, load_used_videos_ids, load_work_consumed_ids, extract_video_id
                existing_ids = load_existing_sources_ids(str(dest))
                state_ids = load_state_known_ids("state.json")
                used_ids = load_used_videos_ids("data/used_videos.json")
                work_ids = load_work_consumed_ids("work")

                with open(dataset_file, "r", encoding="utf-8") as f:
                    candidates = [line.strip() for line in f if line.strip()]
                merge_result["candidates"] = len(candidates)

                to_append = []
                for url in candidates:
                    vid = extract_video_id(url)
                    if not vid:
                        continue
                    if vid in existing_ids:
                        merge_result["skipped_duplicates"] += 1
                        continue
                    if vid in state_ids or vid in used_ids or vid in work_ids:
                        merge_result["skipped_used"] += 1
                        continue
                    to_append.append(vid)

                if to_append:
                    newline = "\r\n" if "\r\n" in dest.read_text(encoding="utf-8", errors="replace") else "\n"
                    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    lines_to_add = [f"# --- added {stamp} by source refresh ---"]
                    for vid in to_append:
                        lines_to_add.append(f"https://www.youtube.com/watch?v={vid} | MrBeast official, 15+ min")
                    lines_to_add.append("")

                    temp_fd, temp_file = tempfile.mkstemp(prefix="sources_refresh_", suffix=".tmp", dir=str(dest.parent))
                    try:
                        with open(dest, "rb") as orig:
                            orig_bytes = orig.read()
                        with os.fdopen(temp_fd, "wb") as handle:
                            handle.write(orig_bytes)
                            if orig_bytes and not orig_bytes.endswith(b"\n") and not orig_bytes.endswith(b"\r\n"):
                                handle.write(newline.encode("utf-8"))
                            handle.write(newline.join(lines_to_add).encode("utf-8"))
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.replace(temp_file, dest)
                        merge_result["appended"] = len(to_append)
                    finally:
                        if os.path.exists(temp_file):
                            try:
                                os.remove(temp_file)
                            except OSError:
                                pass
            except Exception as exc:
                merge_result["error"] = str(exc)
        else:
            merge_result["error"] = "Dataset file mrbeast_15min_plus.txt was not generated."

        if tracker:
            tracker.complete_stage("filter_merge", detail=f"{merge_result['appended']} new sources merged")
            tracker.start_stage("persist_record", detail="Recording execution history in scheduler")

        end_ts = time.time()

        # Classify final status
        if make_result["error"] and merge_result["appended"] == 0:
            status = "failed"
            error = f"Scraper error: {make_result['error']}"
        elif make_result["error"] and merge_result["appended"] > 0:
            status = "partial_results"
            error = f"Scraper partial: {make_result['error']}"
        elif merge_result["error"]:
            status = "failed"
            error = f"Merge error: {merge_result['error']}"
        elif merge_result["appended"] > 0:
            status = "success_new_sources"
            error = None
        else:
            status = "success_no_new_sources"
            error = None

        record = {
            "run_id": run_id,
            "started_at": start_ts,
            "ended_at": end_ts,
            "duration_seconds": round(end_ts - start_ts, 2),
            "timezone": tz_name,
            "sources_scanned": merge_result["candidates"],
            "new_links_found": merge_result["candidates"] - merge_result["skipped_duplicates"] - merge_result["skipped_used"],
            "links_appended": merge_result["appended"],
            "duplicates_skipped": merge_result["skipped_duplicates"],
            "used_skipped": merge_result["skipped_used"],
            "status": status,
            "error": error,
        }

        # Persist record to scheduler state
        _record_refresh_history(record)

        if tracker:
            tracker.complete_stage("persist_record")
            tracker.finish(status="COMPLETED" if status.startswith("success") else "FAILED", error=error)

        return record


def _record_refresh_history(record):
    """Record execution to data/scheduler.json."""
    SCHEDULER_FILE.parent.mkdir(parents=True, exist_ok=True)
    sched_state = _read_json(SCHEDULER_FILE) or {"jobs": {}}
    history = sched_state.setdefault("source_refresh_history", [])
    history.append(record)
    if len(history) > 50:
        sched_state["source_refresh_history"] = history[-50:]

    # Also update daily job entry if present
    date_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    job_key = f"sources:{date_key}"
    job = sched_state.setdefault("jobs", {}).setdefault(job_key, {})
    job.update(
        status="completed" if record["status"].startswith("success") else "failed",
        finished_at=record["ended_at"],
        result=record,
        error=record["error"],
    )

    temp_fd, temp_file = tempfile.mkstemp(prefix="sched_hist_", suffix=".tmp", dir=str(SCHEDULER_FILE.parent))
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
            json.dump(sched_state, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_file, SCHEDULER_FILE)
    finally:
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except OSError:
                pass


def set_scheduler_config(key, value):
    """Safely update persistent scheduler configuration in data/scheduler.json."""
    SCHEDULER_FILE.parent.mkdir(parents=True, exist_ok=True)
    sched_state = _read_json(SCHEDULER_FILE) or {"jobs": {}}
    config = sched_state.setdefault("config", {})
    config[key] = value

    temp_fd, temp_file = tempfile.mkstemp(prefix="sched_cfg_", suffix=".tmp", dir=str(SCHEDULER_FILE.parent))
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
            json.dump(sched_state, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_file, SCHEDULER_FILE)
    finally:
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except OSError:
                pass


def get_scheduler_config():
    """Read effective scheduler configuration with persistent overrides."""
    sched_state = _read_json(SCHEDULER_FILE) or {}
    config = sched_state.get("config", {})
    from telegram_bot.scheduler import _enabled
    return {
        "scheduler_enabled": config.get("scheduler_enabled", _enabled("SCHEDULER_ENABLED")),
        "source_refresh_enabled": config.get("source_refresh_enabled", _enabled("SCHEDULER_SOURCE_REFRESH_ENABLED")),
        "timezone": os.getenv("SCHEDULER_TIMEZONE", "Asia/Kolkata").strip(),
        "source_refresh_time": os.getenv("SCHEDULER_SOURCE_REFRESH_TIME", "08:00").strip(),
        "prepare_time": os.getenv("SCHEDULER_PREPARE_TIME", "09:00").strip(),
    }


def set_pending_input(user_id, action):
    """Register pending conversational input for a specific user ID."""
    _PENDING_INPUTS[str(user_id)] = {"action": action, "at": time.time()}


def get_pending_input(user_id):
    """Retrieve pending conversational input if not expired."""
    entry = _PENDING_INPUTS.get(str(user_id))
    if not entry:
        return None
    if time.time() - entry.get("at", 0) > 300:
        _PENDING_INPUTS.pop(str(user_id), None)
        return None
    return entry.get("action")


def clear_pending_input(user_id):
    """Clear pending conversational input for a user ID."""
    _PENDING_INPUTS.pop(str(user_id), None)

