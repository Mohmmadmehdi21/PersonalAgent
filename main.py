import json, os, sys, time, re, traceback, errno, subprocess

if len(sys.argv) > 1 and sys.argv[1] == "telegram-bot":
    try:
        from config import validate_startup_config, ConfigurationError
        validate_startup_config("telegram-bot")
        from telegram_bot.poller import run_bot
        run_bot()
    except ConfigurationError as config_err:
        print(f"[Config] {config_err}")
        raise SystemExit(2)
    except KeyboardInterrupt:
        print("Telegram bot stopped.")
    except Exception as telegram_error:
        try:
            from logger import log_error, mask_secrets
            log_error(f"[Telegram] Bot stopped safely: {mask_secrets(telegram_error)}")
        except Exception:
            print("Telegram bot could not start; details were suppressed.")
        raise SystemExit(2)
    raise SystemExit(0)

if len(sys.argv) > 1 and sys.argv[1] in ("stats", "slots", "learn", "comments", "growth-health"):
    try: from growth.cli import dispatch; dispatch(sys.argv[1:])
    except Exception as growth_error:
        try:
            from logger import mask_secrets
            print(f"[Growth] Command failed: {mask_secrets(growth_error)}")
        except Exception:
            print("[Growth] Command failed; sensitive error details were suppressed.")
    raise SystemExit(0)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from config import (
    WORK, CLIPS_PER_DAY, DRY_RUN, REVIEW_DIR, HOOK_MIN_SCORE,
    OWN_CONTENT_ENABLED, OWN_RATIO, CLEANUP, validate_startup_config, ConfigurationError
)
try:
    from config import YTDLP_MAX_SOURCE_CANDIDATES, YTDLP_RATE_LIMIT_COOLDOWN_SECONDS
except ImportError:
    YTDLP_MAX_SOURCE_CANDIDATES = 5
    YTDLP_RATE_LIMIT_COOLDOWN_SECONDS = 900

if __name__ == "__main__":
    _startup_args = sys.argv[1:]
    _startup_command = "cleanup" if "cleanup" in _startup_args else (_startup_args[0] if _startup_args else "run")
    try:
        validate_startup_config(_startup_command)
    except ConfigurationError as _config_error:
        print(f"[Config] {_config_error}")
        raise SystemExit(2)

from download import download
from transcribe import transcribe
from find_clips import find_clips
from clip import make_clip, write_srt
from thumb import make_thumb
from upload import get_service, upload
from upload_auth import UploadApprovalRequiredError
from commentary import apply_commentary
try:
    import guard
except Exception as _guard_import_err:
    print(f"[Guard] warning (ignored): {_guard_import_err}")
try:
    import source_filter
except Exception as _source_filter_import_err:
    print(f"[SourceFilter] warning (ignored): {_source_filter_import_err}")
try:
    import source_memory
except Exception as _source_memory_import_err:
    print(f"[SourceMemory] warning (ignored): {_source_memory_import_err}")


from own_content import get_pending_own_video, archive_own_video
from source import find_popular_video, is_channel_url, video_id_from_url
try:
    from source import find_popular_videos
except ImportError:
    def find_popular_videos(yt, url, processed, max_candidates=5):
        cand = find_popular_video(yt, url, processed)
        return [cand] if cand else []

from lock import acquire_lock, release_lock
from logger import log, log_warn, log_error, log_event
from retry import (
    QuotaExceededError, AuthError, PermanentSourceError, FatalError,
    AccountUploadLimitError, UploadOutcomeUnknownError, UploadCommittedInterruptedError,
    ModelNotFoundError, ProviderQuotaError, BotVerificationError, RateLimitedError,
    is_download_cooldown_active, get_download_cooldown_remaining, set_download_cooldown,
    youtube_error_details, classify_error
)
from state_manager import (
    load_state, save_state_atomic, init_video_state, update_step_status,
    init_clip_state, update_clip_step, is_clip_already_uploaded,
    is_valid_json_file, is_valid_media_file, are_all_clips_complete
)
from notify import (
    notify_run_success, notify_run_error, notify_quota_or_auth,
    notify_queue_empty, notify_upload_summary
)
from cleanup import run_cleanup
from queue_manager import load_queue as _load_persistent_queue, save_queue as _save_persistent_queue, add_if_new
from render_cache import cache_matches, write_cache

QUEUE_FILE = os.path.join(REVIEW_DIR, "queue.json").replace("\\", "/")

def _monitor_progress(stage, task, *, message=None):
    log_event("pipeline_progress", stage=stage, task=task)
    if message and os.getenv("TELEGRAM_PROGRESS_ENABLED", "false").strip().lower() in ("true", "1", "t", "yes"):
        try:
            from notify import send_telegram_to
            chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
            if chat_id:
                send_telegram_to(chat_id, f"PersonalAgent: {message}")
        except Exception as progress_error:
            log_warn(f"[Monitoring] Progress notification failed safely: {progress_error}")


def _report_preparation_failure(exc, *, stage=None, task_id=None):
    """Send one evidence-based, redacted report at the pipeline boundary."""
    try:
        from telegram_bot.monitoring import read_registry
        progress = read_registry()
    except Exception:
        progress = {}
    stage = str(stage or progress.get("current_task") or progress.get("stage") or "unknown")[:80]
    cause = exc
    while getattr(cause, "__cause__", None) is not None:
        cause = cause.__cause__
    lowered = stage.lower()
    service = "groq" if any(word in lowered for word in ("analyze", "find_clips", "transcribe")) else "generic"
    classification = classify_error(cause, service=service)
    if isinstance(cause, ModuleNotFoundError):
        category = "missing_dependency"
    elif isinstance(cause, FileNotFoundError):
        category = "missing_file_or_executable"
    elif isinstance(cause, PermissionError):
        category = "permission_denied"
    elif isinstance(cause, OSError) and cause.errno == errno.ENOSPC:
        category = "insufficient_disk_space"
    elif isinstance(cause, json.JSONDecodeError):
        category = "invalid_response"
    elif isinstance(cause, subprocess.CalledProcessError) and any(word in lowered for word in ("render", "encode", "media", "clip")):
        category = "media_processing_failure"
    else:
        category = classification.category
    status = getattr(getattr(cause, "resp", None), "status", None) or getattr(cause, "status_code", None)
    if status is None:
        match = re.search(r"\bHTTP(?:\s*Error)?\s*[: ]?\s*(\d{3})\b", str(cause), re.I)
        status = match.group(1) if match else None
    actual = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?<query redacted>", str(cause))
    try:
        from logger import mask_secrets
        actual = mask_secrets(actual)[:350]
    except Exception:
        actual = "[error details suppressed]"
    next_step = {
        "authentication": "Check the configured provider credentials and permissions, then retry.",
        "rate_limited": "Wait for the reported cooldown/rate limit to clear before retrying.",
        "timeout": "Check network reachability and retry the task.",
        "network": "Check network reachability and retry the task.",
        "missing_dependency": "Install the missing package from the declared project requirements in the active environment.",
        "missing_file_or_executable": "Verify the referenced input file or required executable is installed and reachable.",
        "permission_denied": "Check filesystem permissions for the reported operation.",
        "insufficient_disk_space": "Free disk space on the processing volume, then retry.",
        "invalid_response": "Inspect the redacted API response and request parameters.",
        "media_processing_failure": "Inspect the media-tool stderr and validate the source media before retrying.",
        "permanent_source": "Verify the source video is public and available; replace it with a valid source if needed.",
        "bot_verification": "Review the source access challenge; if authentication is legitimately required, configure the supported downloader cookies.",
        "unexpected": "Inspect the redacted application traceback for the failing operation.",
        "permanent_http": "Review the API request and returned status before retrying.",
    }.get(category, "Inspect the redacted application traceback before retrying.")
    paused = category in {"rate_limited", "timeout", "network"}
    task_id = str(task_id or os.getenv("AGENT_CONTROL_TASK_ID", "").strip() or "unavailable")
    label = "Pipeline task" if "render" in lowered or "clip" in lowered else "Preparation"
    recovery = "processing paused; recoverable after cooldown" if paused else "recoverable after correction"
    report = (f"{label} failed; task status is failed ({recovery}).\n"
              f"Task: {task_id}\nStage: {stage}\nError: {type(cause).__name__} ({category})"
              f"{f', HTTP {status}' if status else ''}\n"
              f"Cause assessment: {'undetermined from available evidence' if category == 'unexpected' else category}.\n"
              f"Reason: {actual}\nNext step: {next_step}")
    notification_sent = False
    notification_error = None
    try:
        from notify import send_telegram_to, notify_run_error
        chat_id = os.getenv("AGENT_CONTROL_CHAT_ID", "").strip()
        if chat_id:
            notification_sent = bool(send_telegram_to(chat_id, report))
            if not notification_sent:
                notification_error = "Telegram did not confirm delivery."
                log_warn("[Prepare] Owner failure notification was not delivered; original error is preserved.")
        else:
            notify_run_error(f"prepare:{stage}", report)
    except Exception as notify_error:
        notification_error = notify_error
        log_warn(f"[Prepare] Owner failure notification failed: {notify_error}")
    if task_id != "unavailable":
        try:
            from telegram_bot.control import record_notification_result
            record_notification_result(task_id, "preparation_failure", notification_sent, notification_error)
        except Exception as state_error:
            log_warn(f"[Prepare] Could not record notification delivery state: {state_error}")

def load_queue():
    return _load_persistent_queue(QUEUE_FILE)

def save_queue(queue):
    return _save_persistent_queue(QUEUE_FILE, queue)

def next_source_candidates(state, max_candidates=None):
    """
    Channel link aur direct video links se up to max_candidates unprocessed videos chunta hai.
    Format support:
      - https://youtu.be/...
      - https://www.youtube.com/@handle
      - https://youtu.be/... | CC-BY license permission note
    """
    if max_candidates is None:
        max_candidates = YTDLP_MAX_SOURCE_CANDIDATES
    if not os.path.exists("sources.txt"):
        return []

    # Guarded hook: evaluate persistent rejection memory & direct URLs first
    try:
        import source_memory
        if source_memory.is_reject_memory_enabled():
            selected = source_memory.select_candidates(state, max_candidates=max_candidates)
            if selected is not None:
                return selected
    except Exception as _src_mem_err:
        print(f"[SourceMemory] warning (ignored): {_src_mem_err}")

    state.setdefault("done_videos", [])
    processed_ids = set(state["done_videos"])
    processed_ids.update(state.get("skipped_video_ids", []))
    processed_ids.update(state.get("failed_video_ids", []))
    processed_ids.update(
        key.split(":", 1)[0]
        for key in state.get("clips", [])
        if ":" in key
    )
    processed_ids.update(
        video_id for video_id in
        (video_id_from_url(url) for url in state.get("done", []))
        if video_id
    )

    entries = []
    # FIXED-BUG F002: Ensure sources.txt file handle is cleanly closed via context manager in Windows
    with open("sources.txt", encoding="utf-8") as _src_file:
        for line in _src_file:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            note = ""
            if "|" in line:
                parts = line.split("|", 1)
                url = parts[0].strip()
                note = parts[1].strip()
            else:
                url = line
            entries.append((url, note))

    candidates = []
    seen_urls = set()
    yt = None

    # Prefer channel sources; retrieve up to max_candidates
    for url, note in entries:
        if len(candidates) >= max_candidates:
            break
        if not is_channel_url(url):
            continue
        if yt is None:
            try:
                yt = get_service()
            except Exception as e:
                log_warn(f"[Source] Could not initialize YouTube client for channel discovery: {e}")
                break
        needed = max_candidates - len(candidates)
        channel_videos = find_popular_videos(yt, url, processed_ids, max_candidates=needed)
        for v_url in channel_videos:
            if v_url not in seen_urls:
                seen_urls.add(v_url)
                candidates.append((v_url, note))
                vid = video_id_from_url(v_url)
                if vid:
                    processed_ids.add(vid)

    # Direct video URLs
    for url, note in entries:
        if len(candidates) >= max_candidates:
            break
        if is_channel_url(url):
            continue
        video_id = video_id_from_url(url)
        if url not in state.get("done", []) and url not in seen_urls and (not video_id or video_id not in processed_ids):
            seen_urls.add(url)
            candidates.append((url, note))
            if video_id:
                processed_ids.add(video_id)

    return candidates


def next_source(state):
    """Backwards-compatible helper returning single next (url, note) tuple."""
    candidates = next_source_candidates(state, max_candidates=1)
    return candidates[0] if candidates else (None, "")

def duration_of(transcript, info_duration=0):
    if info_duration and info_duration > 0:
        return float(info_duration)
    return transcript[-1]["end"] + 1 if transcript else 0

def is_usable_clip(clip):
    """Reject malformed, too-short/long, low-hook, or commentary-free candidates."""
    if not isinstance(clip, dict):
        return False
    try:
        length = float(clip.get("end", 0)) - float(clip.get("start", 0))
        score = int(clip.get("hook_score", 0))
        script_words = len(str(clip.get("commentary_script", "")).split())
        return 25 <= length <= 60 and score >= HOOK_MIN_SCORE and script_words >= 15
    except (TypeError, ValueError):
        return False

def build_description(caption, hashtags, item):
    """
    Description mein source credit, link, permission note, aur transparency line add karta hai.
    """
    credit = f"\n\nSource: {item.get('source_title', '')} by {item.get('source_uploader', '')}\n{item.get('source_url', '')}"
    perm = item.get("permission_note")
    if perm:
        credit += f"\nPermission/License: {perm}"
    elif item.get("source_license"):
        credit += f"\nSource license metadata: {item.get('source_license')}"
    credit += "\n\nOriginal commentary, insight, and educational analysis added by the channel."

    tags_str = " ".join(f"#{h.lstrip('#')}" for h in hashtags)
    return f"{caption}\n\n{tags_str}{credit}"

def _draft_clip_id(source_id, start, end):
    """Stable, filesystem-safe identity for one source time range."""
    return f"{source_id}_{int(start)}_{int(end)}"


def prepare_stage(source_fallbacks=0):
    """
    Stage 1: Download, transcribe, Groq commentary analysis, queue creation.
    Fully resumable based on cache files and state.json.
    """
    os.makedirs(WORK, exist_ok=True)
    os.makedirs(REVIEW_DIR, exist_ok=True)
    state = load_state()
    try:
        guard.bootstrap()
        guard.sync_state(state)
    except Exception as e:
        print(f"[Guard] warning (ignored): {e}")
    queue = load_queue()

    try:
        from telegram_bot.control import checkpoint
        if checkpoint() == "cancel":
            return
    except Exception as control_error:
        if os.getenv("AGENT_CONTROL_TASK_ID"):
            log_warn(f"[Telegram Control] Checkpoint failed safely: {control_error}")

    print("=" * 65)
    print("[STAGE 1: PREPARE] Analyzing content and drafting commentary")
    print("=" * 65)
    log("[Stage 1: Prepare] Starting content preparation and commentary drafting")
    _monitor_progress("prepare", "select source", message="Source preparation started.")

    # 1. Check for own video (OWN_RATIO check)
    total_clips_done = len(state.get("clips", []))
    own_video_file = get_pending_own_video()
    if OWN_CONTENT_ENABLED and own_video_file and (total_clips_done % OWN_RATIO == 0):
        print(f"[Own Content] Processing own video from own_videos/ ({own_video_file})...")
        base_name = os.path.splitext(os.path.basename(own_video_file))[0]
        txt_path = os.path.splitext(own_video_file)[0] + ".txt"
        title = base_name.replace("_", " ").title()
        desc = "Original video created by this channel."
        if os.path.exists(txt_path):
            with open(txt_path, "r", encoding="utf-8") as f:
                desc = f.read().strip()

        item = {
            "id": f"own_{base_name}",
            "source_id": f"own_{base_name}",
            "source_title": title,
            "source_uploader": "Channel Owner",
            "source_url": "Original Upload",
            "permission_note": "Self-produced original content",
            "source_license": "self-produced",
            "rights_status": "self_owned",
            "analysis_status": "not_required_original",
            "key": f"own_{base_name}",
            "start": 0.0,
            "end": 0.0,
            "hook_score": 10,
            "title": title[:50],
            "template": "ORIGINAL_OWN",
            "intro_text": "",
            "pause_at": 0.0,
            "pause_text": "",
            "takeaway_bullets": [],
            "commentary_script": "",
            "caption": desc[:200],
            "hashtags": ["original", "shorts", "creator"],
            "thumbnail_text": "ORIGINAL",
            "status": "pending",
            "is_own": True,
            "source_video_path": own_video_file
        }
        add_if_new(queue, item)
        save_queue(queue)
        print(f"[Own Content] Queued own video: '{title}'.")
        print("\n[Stage 1 Complete] Draft queued. Run 'python main.py' to continue automated rendering/upload.")
        return

    # 2. Check rate limit cooldown before starting source downloads
    if is_download_cooldown_active(state):
        rem = get_download_cooldown_remaining(state)
        print(f"[Prepare] Rate-limit cooldown active. YouTube download paused for another {rem:.0f}s.")
        log_warn(f"[Prepare] Download rate-limit cooldown active ({rem:.0f}s remaining). Skipping prepare stage.")
        return

    # 3. Select source video candidates
    candidates = next_source_candidates(state, max_candidates=YTDLP_MAX_SOURCE_CANDIDATES)
    try:
        candidates = source_filter.filter_candidates(candidates)
    except Exception as e:
        print(f"[SourceFilter] warning (ignored): {e}")
    if not candidates:
        try:
            import source_memory
            if source_memory.is_reject_memory_enabled():
                summary = source_memory.get_exhaustion_summary()
                if summary:
                    print(f"[INFO] sources.txt queue exhausted: {summary}.")
        except Exception:
            summary = None
        print("[INFO] sources.txt mein koi naya video URL nahi mila.")
        log("[Prepare] sources.txt queue is empty.")
        log_event("task_result", task="prepare:source_selection", status="skipped", reason="no unprocessed source")
        notify_queue_empty(summary=summary)
        return

    # Step A: Download candidate source with multi-candidate fallback
    src = None
    info = None
    url = None
    note = None

    last_download_error = None
    for candidate_url, candidate_note in candidates:
        try:
            from telegram_bot.control import checkpoint
            if checkpoint() == "cancel":
                return
        except Exception as control_error:
            if os.getenv("AGENT_CONTROL_TASK_ID"):
                log_warn(f"[Telegram Control] Checkpoint failed safely: {control_error}")

        cand_vid = video_id_from_url(candidate_url) or "source_video"
        print(f"\n[Prepare] Trying candidate video: {candidate_url}")
        if candidate_note:
            print(f"Permission Note: {candidate_note}")
        init_video_state(state, cand_vid, candidate_url, note=candidate_note)
        _monitor_progress("prepare", "download source", message=f"Downloading {cand_vid}...")
        t0 = time.time()

        try:
            src, info = download(candidate_url)
            url = candidate_url
            note = candidate_note
            video_id = info["id"]
            init_video_state(state, video_id, candidate_url, title=info.get("title", ""), note=candidate_note)
            update_step_status(state, video_id, "download", "done", file_path=src)
            log_event("task_result", task="prepare:download", status="success", video_id=video_id)
            print(f"[Timing] Download check took {time.time() - t0:.1f}s")
            log(f"[Prepare] Video ready ({video_id}): {src}")
            break
        except PermanentSourceError as pse:
            last_download_error = pse
            log_error(f"[Prepare] Permanent source error for {candidate_url}: {pse}")
            log_event("task_result", task="prepare:download", status="skipped",
                      error_category="permanent_source", error=str(pse)[:500])
            state.setdefault("skipped_video_ids", []).append(cand_vid)
            state.setdefault("done", []).append(candidate_url)
            update_step_status(state, cand_vid, "download", "failed", error=str(pse))
            save_state_atomic(state)
            try:
                import source_memory
                if source_memory.is_reject_memory_enabled():
                    source_memory.mark_rejected(cand_vid, "PRIVATE_OR_DELETED", f"PermanentSourceError: {str(pse)[:200]}", url=candidate_url)
            except Exception:
                pass
            print(f"[Skip] Video {candidate_url} unavailable/restricted. Trying next candidate...")
            continue
        except BotVerificationError as bve:
            last_download_error = bve
            log_error(f"[Prepare] Bot verification required for {candidate_url}: {bve}")
            log_event("task_result", task="prepare:download", status="failure",
                      error_category="bot_verification", error=str(bve)[:500])
            state.setdefault("failed_video_ids", []).append(cand_vid)
            update_step_status(state, cand_vid, "download", "failed", error=str(bve))
            save_state_atomic(state)
            try:
                import source_memory
                if source_memory.is_reject_memory_enabled():
                    source_memory.mark_transient_failure(cand_vid, f"Bot verification required: {str(bve)[:200]}")
            except Exception:
                pass
            print(f"[Auth] Bot verification required for {candidate_url}. Trying next candidate...")
            continue
        except RateLimitedError as rle:
            last_download_error = rle
            log_error(f"[Prepare] Rate limited (HTTP 429) downloading {candidate_url}: {rle}")
            log_event("task_result", task="prepare:download", status="failure",
                      error_category="rate_limited", error=str(rle)[:500])
            set_download_cooldown(state, YTDLP_RATE_LIMIT_COOLDOWN_SECONDS)
            state.setdefault("failed_video_ids", []).append(cand_vid)
            update_step_status(state, cand_vid, "download", "failed", error=str(rle))
            save_state_atomic(state)
            try:
                import source_memory
                if source_memory.is_reject_memory_enabled():
                    source_memory.mark_transient_failure(cand_vid, f"HTTP 429 Rate Limited: {str(rle)[:200]}")
            except Exception:
                pass
            print(f"[RateLimit] Rate limit active. Cooldown set for {YTDLP_RATE_LIMIT_COOLDOWN_SECONDS}s.")
            raise
        except Exception as e:
            last_download_error = e
            log_error(f"[Prepare] Download failed for {candidate_url}: {e}")
            log_event("task_result", task="prepare:download", status="failure",
                      error_category=type(e).__name__, error=str(e)[:500])
            state.setdefault("failed_video_ids", []).append(cand_vid)
            update_step_status(state, cand_vid, "download", "failed", error=str(e))
            save_state_atomic(state)
            try:
                import source_memory
                if source_memory.is_reject_memory_enabled():
                    source_memory.mark_transient_failure(cand_vid, f"Download error ({type(e).__name__}): {str(e)[:200]}")
            except Exception:
                pass
            print(f"[Notice] Candidate {candidate_url} failed: {e}. Trying next candidate...")
            continue

    if not src or not info:
        log_error(f"[Prepare] All {len(candidates)} candidate video(s) failed to download.")
        details = last_download_error
        if details is None:
            details = BotVerificationError("All source candidates required bot verification or were unavailable.")
        raise RuntimeError(f"All {len(candidates)} source candidates failed; last error: {details}") from details

    try:
        guard.claim(info["id"], url)
    except Exception as e:
        print(f"[Guard] warning (ignored): {e}")

    # Step B: Transcribe (with caching)
    try:
        from telegram_bot.control import checkpoint
        if checkpoint() == "cancel":
            return
    except Exception as control_error:
        if os.getenv("AGENT_CONTROL_TASK_ID"):
            log_warn(f"[Telegram Control] Checkpoint failed safely: {control_error}")
    _monitor_progress("prepare", "transcribe source")
    t0 = time.time()
    print("\n[Stage] Transcribing audio with Whisper...")
    try:
        transcript_cache = os.path.splitext(src)[0] + ".transcript.json"
        # Let transcribe() validate provenance/model metadata before reusing cache.
        transcript = transcribe(src)
        update_step_status(state, video_id, "transcribe", "done", file_path=transcript_cache)
        log_event("task_result", task="prepare:transcribe", status="success",
                  video_id=video_id, segment_count=len(transcript))
        print(f"[Timing] Transcription step took {time.time() - t0:.1f}s")
        log(f"[Prepare] Transcription complete: {len(transcript)} segments")
    except Exception as e:
        log_error(f"[Prepare] Transcription failed: {e}")
        log_event("task_result", task="prepare:transcribe", status="failure",
                  error_category=type(e).__name__, error=str(e)[:500])
        try:
            update_step_status(state, video_id, "transcribe", "failed", error=str(e))
        except Exception as state_error:
            log_error(f"[Prepare] Could not persist transcription failure status: {state_error}")
        raise

    # Step C: Groq Analysis & Commentary Drafting
    try:
        from telegram_bot.control import checkpoint
        if checkpoint() == "cancel":
            return
    except Exception as control_error:
        if os.getenv("AGENT_CONTROL_TASK_ID"):
            log_warn(f"[Telegram Control] Checkpoint failed safely: {control_error}")
    _monitor_progress("prepare", "analyze transcript")
    recent_items = list(reversed(queue))
    previous_comm = [item.get("commentary_script") for item in recent_items if item.get("commentary_script")][:12]
    previous_templates = [item.get("template") for item in recent_items if item.get("template")][:8]

    t0 = time.time()
    print(f"\n[Stage] Groq analysis & commentary generation (CLIPS_PER_DAY={CLIPS_PER_DAY})...")
    total_dur = duration_of(transcript, info.get("duration"))
    try:
        clips = find_clips(
            transcript, total_dur, CLIPS_PER_DAY,
            previous_commentaries=previous_comm,
            previous_templates=previous_templates,
            video_id=video_id,
            audio_path=src
        )
        update_step_status(state, video_id, "find_clips", "done", file_path=f"work/{video_id}.clips.json")
        log_event("task_result", task="prepare:find_clips", status="success",
                  video_id=video_id, candidate_count=len(clips))
        print(f"[Timing] {len(clips)} viral clips identified ({time.time() - t0:.1f}s)")
        log(f"[Prepare] Clips drafted: {len(clips)} clips")
    except FatalError as fe:
        log_error(f"[Prepare] Fatal Groq error: {fe}")
        update_step_status(state, video_id, "find_clips", "failed", error=str(fe))
        if isinstance(fe, ModelNotFoundError):
            failure_type = "Groq Model Configuration Error"
        elif isinstance(fe, AuthError):
            failure_type = "Groq Authentication Error"
        elif isinstance(fe, ProviderQuotaError):
            failure_type = "Groq Quota/Credits Exhausted"
        else:
            failure_type = "Groq API Error"
        log_event("task_result", task="prepare:find_clips", status="failure",
                  error_category=type(fe).__name__, error=str(fe)[:500])
        raise
    except Exception as e:
        log_error(f"[Prepare] find_clips failed: {e}")
        update_step_status(state, video_id, "find_clips", "failed", error=str(e))
        log_event("task_result", task="prepare:find_clips", status="failure",
                  error_category=type(e).__name__, error=str(e)[:500])
        raise

    try:
        from telegram_bot.control import checkpoint
        if checkpoint() == "cancel":
            return
    except Exception as control_error:
        if os.getenv("AGENT_CONTROL_TASK_ID"):
            log_warn(f"[Telegram Control] Checkpoint failed safely: {control_error}")

    try:
        clips = guard.filter_clips(info["id"], clips)
    except Exception as e:
        print(f"[Guard] warning (ignored): {e}")

    # Do not queue a bad cached/fallback clip. Skip this source and try the next
    # popular unprocessed video from the configured channel (bounded per run).
    from clip_quality import assess_candidate, rights_status
    source_rights = rights_status(permission_note=note, source_license=info.get("license", ""))
    candidates = []
    video_state = state.setdefault("videos", {}).setdefault(video_id, {})
    video_state.setdefault("clip_candidates", [])
    try:
        with open(os.path.join(WORK, f"{video_id}.clips.meta.json"), "r", encoding="utf-8") as metadata_stream:
            rejected_by_model = json.load(metadata_stream).get("rejected_candidates", [])
        for rejected_candidate in rejected_by_model:
            record = {"source_id": video_id, "source_url": info.get("url", url),
                      "start": rejected_candidate.get("start"), "end": rejected_candidate.get("end"),
                      "hook_score": rejected_candidate.get("hook_score"),
                      "analysis_status": "ai_success", "status": "rejected",
                      "reason": rejected_candidate.get("reason", "candidate rejected by validation")}
            if not any(r.get("start") == record["start"] and r.get("end") == record["end"]
                       and r.get("reason") == record["reason"] for r in video_state["clip_candidates"]):
                video_state["clip_candidates"].append(record)
                log_event("clip_candidate", **record)
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    for index, clip in enumerate(clips):
        assessment = assess_candidate(clip, total_dur, transcript,
                                      hook_min_score=HOOK_MIN_SCORE, rights_status=source_rights)
        clip["quality_status"] = assessment["status"]
        clip["quality_reason"] = assessment["reason"]
        if assessment["status"] == "rejected":
            record = {"source_id": video_id, "source_url": info.get("url", url),
                      "start": clip.get("start"), "end": clip.get("end"),
                      "hook_score": clip.get("hook_score"),
                      "analysis_status": clip.get("analysis_status", "legacy_cache_unverified"),
                      "status": "rejected", "reason": assessment["reason"]}
            video_state["clip_candidates"] = [r for r in video_state["clip_candidates"]
                                               if not (r.get("start") == record["start"] and r.get("end") == record["end"])]
            video_state["clip_candidates"].append(record)
            log_event("clip_candidate", **record)
        else:
            clip["rights_status"] = source_rights
            candidates.append(clip)
    clips = candidates
    queued_for_source = [
        item for item in queue
        if item.get("source_id") == video_id
        and item.get("status") in ("pending", "needs_review", None)
        and is_usable_clip(item)
    ]
    for item in queue:
        if (item.get("source_id") == video_id
                and item.get("status") in ("pending", None)
                and not is_usable_clip(item)):
            item["status"] = "failed"

    if not clips and not queued_for_source:
        state.setdefault("skipped_video_ids", [])
        if video_id not in state["skipped_video_ids"]:
            state["skipped_video_ids"].append(video_id)
        state.setdefault("videos", {}).setdefault(video_id, {})["status"] = "skipped_no_usable_clips"
        save_queue(queue)
        save_state_atomic(state)
        try:
            import source_memory
            if source_memory.is_reject_memory_enabled():
                source_memory.mark_rejected(video_id, "NO_USABLE_CLIPS", "Video had no usable 25-60 second commentary clips", url=info.get("url", url), title=info.get("title"))
        except Exception:
            pass
        log_warn(f"[Prepare] {video_id} had no usable 25-60 second clips; trying another channel video.")
        print(f"[Skip] {video_id} mein usable clip nahi mili. Channel se agla popular video try kar rahe hain.")
        if source_fallbacks < 4:
            return prepare_stage(source_fallbacks + 1)
        print("[Prepare] Is run mein 5 source videos check ho chuke; agle run mein phir continue hoga.")
        return

    # Add drafted clips into queue & state
    new_drafts = 0
    created_draft_ids = []
    for i, c in enumerate(clips):
        key = f"{info['id']}:{int(c['start'])}-{int(c['end'])}"
        if any(item.get("key") == key for item in queue):
            continue

        item = {
            # The list index is not stable across source refreshes; range-based
            # IDs keep separate highlights from sharing confirmation/file names.
            "id": _draft_clip_id(info["id"], c["start"], c["end"]),
            "source_id": info["id"],
            "source_title": info.get("title", ""),
            "source_uploader": info.get("uploader", ""),
            "source_url": info.get("url", url),
            "permission_note": note,
            "key": key,
            "start": c["start"],
            "end": c["end"],
            "hook_score": c.get("hook_score", 8),
            "hook_reason": c.get("reason", ""),
            "analysis_status": c.get("analysis_status", "legacy_cache_unverified"),
            "analysis_reason": c.get("analysis_reason", "Analysis provenance unavailable"),
            "quality_status": c.get("quality_status", "needs_review"),
            "quality_reason": c.get("quality_reason", "Quality assessment unavailable"),
            "rights_status": c.get("rights_status", "unverified"),
            "source_license": info.get("license", ""),
            "title": c.get("title", f"Highlight Part {i+1}"),
            "template": c.get("template", "INTRO_CARD"),
            "intro_text": c.get("intro_text", ""),
            "pause_at": c.get("pause_at", 15.0),
            "pause_text": c.get("pause_text", ""),
            "takeaway_bullets": c.get("takeaway_bullets", []),
            "commentary_script": c.get("commentary_script", ""),
            "caption": c.get("caption", ""),
            "hashtags": c.get("hashtags", []),
            "thumbnail_text": c.get("thumbnail_text", "WATCH THIS"),
            "status": "pending",
            "review_reason": None,
            "is_own": False,
            "source_video_path": src
        }
        queue.append(item)
        init_clip_state(state, video_id, key, item)
        new_drafts += 1
        created_draft_ids.append(item["id"])

    save_queue(queue)
    save_state_atomic(state)
    controlled_task = os.getenv("AGENT_CONTROL_TASK_ID", "").strip()
    if controlled_task and created_draft_ids:
        try:
            from telegram_bot.control import associate_clips
            associate_clips(controlled_task, created_draft_ids)
        except Exception as control_error:
            log_warn(f"[Telegram Control] Could not associate prepared clips: {control_error}")
    _monitor_progress("prepare", "drafts queued", message=f"Preparation complete: {new_drafts} clip draft(s) queued.")
    print("\n" + "=" * 65)
    # FIXED-BUG F001: Misleading success message when 0 drafts were saved
    if new_drafts > 0:
        print(f"[SUCCESS] {new_drafts} commentary clip drafts saved to 'review/queue.json'!")
    else:
        print(f"[INFO] 0 new commentary clip drafts saved (all clips already queued or duplicate).")
    print("-> Drafts queue mein hain; normal 'python main.py' run unhe automatically process karega.")
    print("=" * 65 + "\n")

def render_stage():
    """
    Stage 2: Renders pending items and uploads to YouTube.
    Features:
      - Duplicate upload protection
      - Cache validation for rendered clips/thumbs (skip re-render)
      - Single clip failure isolation (one clip failing doesn't stop others)
      - Quota / Auth immediate stop with high-priority alert
      - Automatic disk cleanup and notification
    """
    os.makedirs(WORK, exist_ok=True)
    state = load_state()
    queue = load_queue()
    _monitor_progress("render", "load pending queue")

    clips_to_process = [
        item for item in queue
        if item.get("status") in ("pending", None)
    ]
    if not clips_to_process:
        print("\n[Render] Koi pending clips process karne ke liye nahi hain.\n")
        return

    control_task_id = os.getenv("AGENT_CONTROL_TASK_ID", "").strip()
    if not DRY_RUN and not control_task_id:
        message = "Live publishing requires an owner-controlled Telegram task and per-clip confirmation."
        log_error(f"[Render] {message}")
        log_event("task_result", task="render_stage", status="failure",
                  error_category="approval_required", error=message)
        return
    if control_task_id:
        try:
            from telegram_bot.control import associate_clips
            associate_clips(control_task_id, [item.get("id", "") for item in clips_to_process])
        except Exception as control_error:
            log_warn(f"[Telegram Control] Could not associate clip IDs: {control_error}")

    print("=" * 65)
    print(f"[STAGE 2: RENDER] Processing {len(clips_to_process)} queued clips automatically")
    print("=" * 65)
    log(f"[Stage 2: Render] Processing {len(clips_to_process)} queued clips automatically")

    yt = None
    if DRY_RUN:
        print("\n[Main] DRY_RUN=True hai. Upload skip ho raha hai. Sirf enhanced clips aur thumbnails banenge.\n")
    else:
        print("\n[Main] YouTube par direct public upload shuru ho raha hai...\n")
        try:
            yt = get_service()
        except AuthError as ae:
            log_error(f"[Render] Auth error: {ae}")
            notify_quota_or_auth("YouTube Authentication Failed", str(ae))
            return
        except Exception as e:
            log_error(f"[Render] YouTube service initialization error: {e}")
            notify_quota_or_auth("YouTube Service Error", str(e))
            return

    rendered_files = []
    succeeded_clips = []
    upload_attempted_count = 0
    failed_clips = []
    batch_stopped_reason = None
    t_stage_start = time.time()

    for i, item in enumerate(clips_to_process):
        if control_task_id:
            try:
                from telegram_bot.control import checkpoint
                if checkpoint(clip=item) == "cancel":
                    item["status"] = "pending"
                    item.pop("processing_step", None)
                    item.pop("processing_started_at", None)
                    save_queue(queue)
                    break
            except Exception as control_error:
                log_warn(f"[Telegram Control] Checkpoint failed safely: {control_error}")
        item["status"] = "processing"
        item["processing_step"] = "rendering"
        item["processing_started_at"] = time.time()
        save_queue(queue)
        key = item["key"]
        video_id = item.get("source_id", "source")
        _monitor_progress("render", f"clip {item.get('id', key)}")
        print("-" * 65)
        print(f"Processing Clip [{i+1}/{len(clips_to_process)}]: '{item['title']}'")
        print(f"Template: {item.get('template')} | Hook Score: {item.get('hook_score')}/10")
        print("-" * 65)

        # 1. Duplicate Protection: Check if already uploaded
        if not DRY_RUN and is_clip_already_uploaded(state, key):
            print(f"[Duplicate Protection] Clip {key} pehle se YouTube par uploaded hai. Skipping duplicate upload.")
            item["status"] = "needs_review"
            item["recovery_note"] = "State says this clip was uploaded, but queue has no confirmed YouTube ID; verify manually."
            save_queue(queue)
            continue

        base_prefix = os.path.join(WORK, item["id"]).replace("\\", "/")
        base_clip = base_prefix + "_base.mp4"
        final_clip = base_prefix + ".mp4"
        srt_file = base_prefix + ".srt"
        thumb_file = base_prefix + ".jpg"
        render_meta = base_prefix + ".render.json"
        t_render = time.time()

        try:
            # 2. Simple Render Check: Skip re-render if valid final clip and thumbnail exist
            cache_hit = (os.path.isfile(final_clip) and os.path.getsize(final_clip) > 0
                         and os.path.isfile(thumb_file) and os.path.getsize(thumb_file) > 0)
            if cache_hit:
                print(f"[Render Cache] Valid rendered clip & thumbnail found ({final_clip}). Skipping re-render.")
                rendered_files.extend([final_clip, thumb_file])
            else:
                if item.get("is_own"):
                    final_clip = item["source_video_path"]
                else:
                    # Base clip with burned subtitles
                    src_video = item["source_video_path"]
                    transcript_cache = os.path.splitext(src_video)[0] + ".transcript.json"
                    if not os.path.exists(transcript_cache):
                        transcript_cache = os.path.splitext(src_video)[0] + "_transcript.json"

                    with open(transcript_cache, "r", encoding="utf-8") as f:
                        full_transcript = json.load(f)

                    print(f"[Render] Burning subtitles for base clip ({item['start']}s -> {item['end']}s)...")
                    write_srt(full_transcript, item["start"], item["end"], srt_file)
                    make_clip(src_video, item["start"], item["end"], base_clip, srt_file)

                    # Apply Value-Add Commentary Engine
                    print(f"[Render] Applying commentary template ({item.get('template')})...")
                    apply_commentary(base_clip, item, final_clip)

                # Generate Thumbnail
                print(f"[Render] Creating thumbnail with text: '{item['thumbnail_text']}'...")
                thumb_source = base_clip if not item.get("is_own") and os.path.isfile(base_clip) else final_clip
                make_thumb(thumb_source, item["thumbnail_text"], thumb_file)
                write_cache(render_meta, item)
                rendered_files.extend([final_clip, thumb_file])
                print(f"[Render] Completed in {time.time() - t_render:.1f}s -> {final_clip}")

            log_event("clip_render", task=f"clip:{item.get('id', key)}", status="success",
                      cache_reused=cache_hit, processing_seconds=round(time.time() - t_render, 3),
                      source_id=video_id, analysis_status=item.get("analysis_status"),
                      quality_status=item.get("quality_status"), rights_status=item.get("rights_status"))

            update_clip_step(state, video_id, key, "cut", "done", file_path=final_clip)
            update_clip_step(state, video_id, key, "thumbnail", "done", file_path=thumb_file)

            # 3. Description
            desc = build_description(item["caption"], item["hashtags"], item)

            # 4. Upload or Dry-Run
            if DRY_RUN:
                print(f"[Main] DRY_RUN=True: Upload skip ho raha hai.")
                print(f"[DRY-RUN] Enhanced Clip: {final_clip}")
                print(f"[DRY-RUN] Custom Thumb:  {thumb_file}")
                item["status"] = "pending"
                item["last_result"] = "rendered_dry_run"
                item.pop("processing_step", None)
                item.pop("processing_started_at", None)
                succeeded_clips.append(item)
                log_event("task_result", task=f"clip:{item.get('id', key)}", status="success", mode="dry_run")
                try:
                    guard.record_clip(item.get("source_id"), item.get("start"), item.get("end"), uploaded=False, dry_run=DRY_RUN)
                except Exception as e:
                    print(f"[Guard] warning (ignored): {e}")
            else:
                approval = None
                if control_task_id:
                    from telegram_bot.control import checkpoint
                    permission = checkpoint(clip=item, ask_upload=True)
                    if permission == "cancel":
                        item["status"] = "pending"
                        item.pop("processing_step", None)
                        item.pop("processing_started_at", None)
                        save_queue(queue)
                        break
                    from upload_auth import UploadApproval
                    if permission == "denied":
                        item["status"] = "pending"
                        item["last_result"] = "upload_confirmation_denied"
                        item.pop("processing_step", None)
                        item.pop("processing_started_at", None)
                        save_queue(queue)
                        continue
                    if not isinstance(permission, UploadApproval):
                        batch_stopped_reason = "upload confirmation state unavailable; batch paused safely"
                        item["status"] = "failed"
                        item["last_result"] = "upload_confirmation_unavailable"
                        item["last_error"] = "The confirmation state could not be matched to this task and clip; no upload request was sent."
                        item.pop("processing_step", None)
                        item.pop("processing_started_at", None)
                        save_queue(queue)
                        failed_clips.append({"title": item.get("title", item.get("id", key)),
                                             "error": "approval_required: confirmation could not be safely validated; no upload request was sent. Check /task and /queue before retrying.",
                                             "error_category": "approval_required"})
                        log_event("task_result", task=f"clip:{item.get('id', key)}", status="failure",
                                  error_category="approval_required", error="Confirmation did not match the active task and clip.")
                        break
                    approval = permission
                print("[Main] YouTube par public upload ho raha hai...")
                item["processing_step"] = "uploading"
                item["upload_started_at"] = time.time()
                save_queue(queue)
                upload_attempted_count += 1
                vid = upload(yt, final_clip, thumb_file, item["title"], desc, item["hashtags"],
                             approval=approval, clip_id=item.get("id"))
                try:
                    if os.getenv("TRACKING_ENABLED", "false").strip().lower() in ("true", "1", "t", "yes"):
                        from growth.tracker import record_upload
                        record_upload(vid, item.get("source_id"), item.get("title"), item.get("publish_at"), item.get("hook_score"), item.get("template"), float(item.get("end", 0)) - float(item.get("start", 0)))
                except Exception as growth_error:
                    print(f"[Growth] Upload tracking skipped: {growth_error}")
                state.setdefault("clips", []).append(key)
                item["status"] = "uploaded"
                item["youtube_video_id"] = vid
                item["youtube_privacy_status"] = getattr(vid, "privacy_status", "unknown")
                item.pop("processing_step", None)
                item.pop("processing_started_at", None)
                item.pop("upload_started_at", None)
                update_clip_step(state, video_id, key, "upload", "done", youtube_id=vid)
                print(f"[Uploaded] YouTube Video ID: {vid} - {item['title']}")
                _monitor_progress("render", f"clip {item.get('id', key)} uploaded",
                                  message=f"Clip upload confirmed: {item.get('title', item.get('id', key))}.")
                log(f"[Uploaded] Clip uploaded successfully: {vid} ({item['title']})")
                succeeded_clips.append(item)
                log_event("task_result", task=f"clip:{item.get('id', key)}", status="success",
                          youtube_video_id=str(vid), privacy_status=item["youtube_privacy_status"])

                if item.get("is_own"):
                    archive_own_video(item["source_video_path"])
                try:
                    guard.record_clip(item.get("source_id"), item.get("start"), item.get("end"), uploaded=True, dry_run=DRY_RUN)
                except Exception as e:
                    print(f"[Guard] warning (ignored): {e}")

        except UploadApprovalRequiredError as approval_error:
            # The shared upload boundary rejected this before YouTube's insert call.
            # Keep the clip retryable and stop the batch rather than treating it as
            # an uncertain publication or moving on to another clip.
            batch_stopped_reason = "required owner upload approval was not accepted"
            item["status"] = "failed"
            item["last_error"] = str(approval_error)
            item["last_result"] = "upload_approval_required"
            item.pop("processing_step", None)
            item.pop("processing_started_at", None)
            item.pop("upload_started_at", None)
            update_clip_step(state, video_id, key, "upload", "failed", error=str(approval_error))
            failed_clips.append({"title": item.get("title", item.get("id", key)),
                                 "error": ("approval_required: owner approval was not accepted; no YouTube upload request was sent. "
                                           "Fix the approval handoff, then retry this failed clip through the owner command."),
                                 "error_category": "approval_required"})
            log_event("task_result", task=f"clip:{item.get('id', key)}", status="failure",
                      error_category="approval_required", error=str(approval_error)[:300])
            print(f"\n[UPLOAD STOPPED] Owner approval was not accepted for '{item.get('title', key)}'; no upload request was sent.\n")
            break

        except UploadCommittedInterruptedError as committed_error:
            item["status"] = "uploaded"
            item["youtube_video_id"] = committed_error.video_id
            item["youtube_privacy_status"] = committed_error.privacy_status or "unknown"
            item.pop("processing_step", None)
            item.pop("processing_started_at", None)
            item.pop("upload_started_at", None)
            state.setdefault("clips", []).append(key)
            update_clip_step(state, video_id, key, "upload", "done", youtube_id=committed_error.video_id)
            batch_stopped_reason = "interrupted after confirmed upload"
            succeeded_clips.append(item)
            log_event("task_interrupted_after_commit", task=f"clip:{item.get('id', key)}",
                      video_id=committed_error.video_id, privacy_status="unknown")
            print(f"[INTERRUPTED] Upload {committed_error.video_id} succeeded; stopping before the next clip.")
            break

        except AccountUploadLimitError as limit_error:
            reason = "uploadLimitExceeded"
            original_api_error = limit_error
            while getattr(original_api_error, "__cause__", None) is not None:
                original_api_error = original_api_error.__cause__
            api_details = youtube_error_details(original_api_error)
            batch_stopped_reason = reason
            item["status"] = "blocked"
            item["upload_error"] = reason
            item["upload_error_details"] = api_details
            update_clip_step(state, video_id, key, "upload", "blocked", error=reason)
            save_queue(queue)
            failed_clips.append({"title": item.get("title", item.get("id", key)), "error": reason})
            limit_msg = "[Quota Limit] Aaj ki upload limit poori ho chuki hai. Clips local 'work/' folder mein saved hain."
            print(f"\n{limit_msg}\n")
            log_warn(limit_msg)
            reason_detail = ", ".join(
                f"{detail.get('domain', 'API')}/{detail.get('reason', 'unknown')}"
                for detail in api_details.get("details", [])
            ) or api_details.get("api_status") or reason
            message = (
                f"YouTube account upload limit reached for clip '{item.get('title', key)}' "
                f"({item.get('id', key)}): {reason_detail}; HTTP {api_details.get('http_status') or 'unknown'}. "
                "Current clip is preserved as blocked and later clips remain queued. Verify the YouTube account/channel status in Studio. "
                "After resolving it, use the existing owner-authorized retry/approval flow; uploads will not resume automatically."
            )
            log_error(f"[Upload] {message} API details: {api_details}")
            log_event("task_result", task=f"clip:{item.get('id', key)}", status="failure",
                      error_category="account_upload_limit", error=f"{reason_detail}; HTTP {api_details.get('http_status') or 'unknown'}")
            try:
                from notify import send_telegram_to
                owner_chat = os.getenv("AGENT_CONTROL_CHAT_ID", "").strip()
                if owner_chat:
                    if not send_telegram_to(owner_chat, message):
                        log_warn("[Upload] Account-limit notification was not delivered; queue state is preserved.")
                else:
                    notify_quota_or_auth("YouTube Account Upload Limit Reached", message)
            except Exception as notify_error:
                log_warn(f"[Upload] Account-limit notification failed safely: {notify_error}")
            print(f"\n[UPLOAD PAUSED] {message}\n")
            break

        except UploadOutcomeUnknownError as unknown_error:
            batch_stopped_reason = "upload outcome uncertain; manual verification required"
            item["status"] = "needs_review"
            item["upload_error"] = str(unknown_error)
            if getattr(unknown_error, "video_id", None):
                item["youtube_video_id"] = unknown_error.video_id
                item["youtube_privacy_status"] = getattr(unknown_error, "privacy_status", "unknown")
            save_queue(queue)
            update_clip_step(state, video_id, key, "upload", "uncertain", error=str(unknown_error))
            failed_clips.append({"title": item.get("title", item.get("id", key)), "error": "upload outcome uncertain; manual verification required"})
            message = (
                f"Upload outcome for clip '{item.get('title', key)}' ({item.get('id', key)}) is unknown. "
                "It was removed from automatic retry pending manual YouTube Studio verification; batch stopped."
            )
            log_error(f"[Upload] {message} Details: {unknown_error}")
            log_event("task_result", task=f"clip:{item.get('id', key)}", status="uncertain",
                      error_category="upload_outcome_unknown", error=str(unknown_error)[:500])
            notify_run_error("upload_outcome_uncertain", message, video_id=video_id)
            print(f"\n[UPLOAD PAUSED] {message}\n")
            break

        except QuotaExceededError as qe:
            item["status"] = "blocked"
            item["last_error"] = str(qe)
            log_error(f"[Upload] Quota exceeded: {qe}")
            log_event("task_result", task=f"clip:{item.get('id', key)}", status="failure",
                      error_category="youtube_quota", error=str(qe)[:500])
            update_clip_step(state, video_id, key, "upload", "failed", error=str(qe))
            failed_clips.append({"title": item["title"], "error": "YouTube Upload Quota Exceeded (Reset at Pacific midnight)"})
            notify_quota_or_auth("YouTube API Quota Exceeded", str(qe))
            print(f"\n[CRITICAL] YouTube upload quota exhaust ho gaya! Further uploads stopped for today.\n")
            break

        except AuthError as ae:
            item["status"] = "blocked"
            item["last_error"] = str(ae)
            log_error(f"[Upload] Auth error: {ae}")
            log_event("task_result", task=f"clip:{item.get('id', key)}", status="failure",
                      error_category="authentication", error=str(ae)[:500])
            update_clip_step(state, video_id, key, "upload", "failed", error=str(ae))
            failed_clips.append({"title": item["title"], "error": f"Auth error: {ae}"})
            notify_quota_or_auth("YouTube Token / Auth Expired", str(ae))
            print(f"\n[CRITICAL] YouTube authentication token fail ho gaya! Run 'python test_auth.py' to re-login.\n")
            break

        except KeyboardInterrupt:
            if item.get("youtube_video_id"):
                batch_stopped_reason = "interrupted after confirmed upload"
                log_event("task_interrupted_after_commit", task=f"clip:{item.get('id', key)}",
                          video_id=item["youtube_video_id"])
                print(f"\n[INTERRUPTED] Upload {item['youtube_video_id']} is confirmed and saved. Stopping the batch.\n")
            else:
                if item.get("processing_step") == "uploading":
                    item["status"] = "needs_review"
                    item["last_error"] = "Interrupted during upload; verify YouTube Studio before retrying."
                else:
                    item["status"] = "pending"
                    item.pop("processing_step", None)
                    item.pop("processing_started_at", None)
                    item["last_error"] = "Interrupted before upload started; safe to resume from validated caches."
                batch_stopped_reason = "interrupted by user"
                failed_clips.append({"title": item.get("title", key), "error": "interrupted by user; item preserved as pending"})
                log_event("task_interrupted", task=f"clip:{item.get('id', key)}", status="pending")
                print("\n[INTERRUPTED] Current clip remains pending; prior queue/state updates are saved.\n")
            break

        except Exception as clip_err:
            # Single clip failure isolation: Log, mark failed in state, continue with remaining clips!
            if item.get("processing_step") == "uploading":
                item["status"] = "needs_review"
                item["recovery_note"] = "Upload raised an unclassified error after request start; verify YouTube Studio before retrying."
            else:
                item["status"] = "failed"
            item["last_error"] = str(clip_err)
            item["attempt_count"] = int(item.get("attempt_count", 0)) + 1
            log_error(f"[Clip Failure] Error processing clip '{item['title']}': {clip_err}")
            update_clip_step(state, video_id, key, "cut", "failed", error=str(clip_err))
            failed_clips.append({"title": item["title"], "error": str(clip_err),
                                 "_exception": clip_err, "_stage": item.get("processing_step") or "clip processing"})
            state.setdefault("failed_video_ids", [])
            if video_id not in state["failed_video_ids"]:
                state["failed_video_ids"].append(video_id)
            log_event("task_result", task=f"clip:{item.get('id', key)}", status="failure",
                      error_category=type(clip_err).__name__, error=str(clip_err)[:500])
            log_event("clip_render", task=f"clip:{item.get('id', key)}", status="failure",
                      processing_seconds=round(time.time() - t_render, 3),
                      error_category=type(clip_err).__name__, source_id=video_id)
            print(f"\n[Warning] Clip '{item['title']}' fail hua ({clip_err}). Baaki clips continue ho rahe hain.\n")

        finally:
            # Commit each clip boundary so an interruption cannot discard earlier completed items.
            save_queue(queue)
            save_state_atomic(state)

    save_queue(queue)
    save_state_atomic(state)

    freed_str = None
    if not DRY_RUN:
        # Mark source as done if all queued clips completed
        all_done = True
        for item in clips_to_process:
            s_url = item.get("source_url")
            source_id = item.get("source_id")
            if item.get("status") != "uploaded":
                all_done = False
                continue
            if s_url and s_url not in state.get("done", []):
                state.setdefault("done", []).append(s_url)
            if source_id and not str(source_id).startswith("own_"):
                state.setdefault("done_videos", [])
                if source_id not in state["done_videos"]:
                    state["done_videos"].append(source_id)

        save_state_atomic(state)

        # Trigger cleanup if policy enabled
        if CLEANUP and all_done:
            _, freed_str = run_cleanup(dry_run=False)

        print("\n" + "=" * 65)
        if batch_stopped_reason:
            print(f"[UPLOAD BATCH STOPPED] {batch_stopped_reason} | Succeeded: {len(succeeded_clips)} | Failed: {len(failed_clips)}")
        else:
            print(f"[SUCCESS] Clip upload sequence complete! Succeeded: {len(succeeded_clips)} | Failed: {len(failed_clips)}")
        if failed_clips:
            print("\nFailed Clips Summary:")
            for fc in failed_clips:
                print(f"  - {fc['title']}: {fc['error']}")
        print("=" * 65 + "\n")

    else:
        # DRY_RUN complete
        if CLEANUP:
            run_cleanup(dry_run=True)

        print("\n" + "=" * 65)
        print(f"[SUCCESS] DRY RUN complete! Enhanced clips 'work/' folder mein ready hain:")
        for rf in rendered_files:
            if rf.endswith(".mp4"):
                print(f"  [CLIP] {rf}")
        if failed_clips:
            print("\nFailed Clips:")
            for fc in failed_clips:
                print(f"  - {fc['title']}: {fc['error']}")
        print("=" * 65 + "\n")

    # Send Notification
    total_time = time.time() - t_stage_start
    if not DRY_RUN and clips_to_process:
        queue_failed = sum(item.get("status") in {"failed", "blocked"} for item in clips_to_process)
        queue_pending = sum(item.get("status") == "pending" for item in clips_to_process)
        queue_review = sum(item.get("status") == "needs_review" for item in clips_to_process)
        notify_upload_summary(control_task_id, succeeded_clips, failed_clips, total_time,
                              upload_attempted_count, queue_pending, queue_review,
                              stopped_reason=batch_stopped_reason, failed_count=queue_failed)
    elif succeeded_clips:
        notify_run_success(succeeded_clips, total_time, is_dry_run=DRY_RUN, freed_space_str=freed_str)
    diagnostic_failure = next((item for item in failed_clips if item.get("_exception") is not None), None)
    if diagnostic_failure:
        try:
            _monitor_progress("render", f"clip {diagnostic_failure.get('title', 'unknown')} {diagnostic_failure.get('_stage')}")
            _report_preparation_failure(diagnostic_failure["_exception"])
        except Exception as report_error:
            log_warn(f"[Render] Failure report could not be built or sent: {report_error}")
    elif DRY_RUN and failed_clips and not succeeded_clips:
        first_failure = failed_clips[0]
        notify_run_error(first_failure.get("error_category", "render_stage"), first_failure["error"],
                         video_id=clips_to_process[0].get("source_id"))
    log_event("task_result", task="render_stage",
              status="failure" if failed_clips else "success",
              succeeded=len(succeeded_clips), failed=len(failed_clips),
              stopped_reason=batch_stopped_reason)

def main():
    args = sys.argv[1:]
    control_task_id = os.getenv("AGENT_CONTROL_TASK_ID", "").strip()
    acquire_lock()
    try:
        from telegram_bot.monitoring import begin_run
        begin_run()
    except Exception as monitor_error:
        log_warn(f"[Monitoring] Could not initialize task registry: {monitor_error}")
    try:
        if "cleanup" in args:
            dry = "--dry" in args or DRY_RUN
            run_cleanup(dry_run=dry, force=("--yes" in args or not dry))
        elif "prepare" in args:
            prepare_stage()
        elif "render" in args:
            render_stage()
        else:
            # Default run: prepare first
            prepare_stage()
            queue = load_queue()
            render_stage()
        try:
            if os.getenv("GROWTH_HEALTH_ENABLED", "false").strip().lower() in ("true", "1", "t", "yes"): __import__("growth.tracker", fromlist=["record_success"]).record_success()
        except Exception as growth_error:
            print(f"[Growth] Heartbeat update skipped: {growth_error}")
        try:
            from telegram_bot.monitoring import finish_run
            finish_run()
        except Exception as monitor_error:
            log_warn(f"[Monitoring] Could not finalize task registry: {monitor_error}")
        if control_task_id:
            try:
                from telegram_bot.control import finish, task_info
                task = task_info(control_task_id) or {}
                associated = set(task.get("clip_ids", []))
                queue_snapshot = load_queue()
                associated_items = [x for x in queue_snapshot
                                    if isinstance(x, dict) and str(x.get("id", "")) in associated]
                clip_states = [x.get("status") for x in associated_items]
                result = None
                if task.get("kind") == "prepare":
                    verified = [item for item in associated_items
                                if item.get("status") in {"pending", "needs_review"}
                                and str(item.get("id", "")).strip()
                                and str(item.get("title", "")).strip()]
                    result = {"verified_draft_count": len(verified),
                              "clip_ids": [str(item["id"]) for item in verified]}
                elif task.get("kind") == "upload":
                    result = {
                        "selected": len(associated_items),
                        "uploaded": sum(x.get("status") == "uploaded" and bool(x.get("youtube_video_id"))
                                         for x in associated_items),
                        "failed_or_blocked": sum(x.get("status") in {"failed", "blocked"}
                                                  for x in associated_items),
                        "pending": sum(x.get("status") == "pending" for x in associated_items),
                        "needs_review": sum(x.get("status") == "needs_review" for x in associated_items),
                        "publications": sum(x.get("youtube_privacy_status") == "public"
                                            for x in associated_items),
                    }
                if task.get("cancel_requested"):
                    status = "cancelled"
                elif task.get("kind") == "prepare":
                    # Review state belongs to each queue item; preparation itself can finish
                    # successfully while its drafts remain pending human review.
                    status = "failed" if any(state in {"failed", "blocked"} for state in clip_states) else "completed"
                elif "blocked" in clip_states:
                    status = "blocked"
                elif "needs_review" in clip_states:
                    status = "needs_review"
                elif "failed" in clip_states:
                    status = "failed"
                else:
                    status = "completed"
                finish(control_task_id, status, result=result)
            except Exception as control_error:
                log_warn(f"[Telegram Control] Could not finalize task: {control_error}")
    except Exception as e:
        failure_stage = None
        try:
            from telegram_bot.monitoring import read_registry
            registry = read_registry()
            failure_stage = registry.get("current_task") or registry.get("stage")
        except Exception as stage_error:
            log_warn(f"[Monitoring] Could not preserve failing stage before cleanup: {stage_error}")
        if control_task_id:
            try:
                from telegram_bot.control import finish
                finish(control_task_id, "failed", error=e)
            except Exception:
                pass
        try:
            from telegram_bot.monitoring import finish_run
            finish_run("failed", error=e)
        except Exception:
            pass
        log_error("[Main Crash] Unhandled exception with traceback:\n" + traceback.format_exc())
        if "cleanup" not in args and "render" not in args:
            try:
                _report_preparation_failure(e, stage=failure_stage, task_id=control_task_id)
            except Exception as report_error:
                log_warn(f"[Prepare] Failure report could not be built or sent; original exception preserved: {report_error}")
        else:
            notify_run_error("main_pipeline", str(e))
        raise
    except KeyboardInterrupt:
        try:
            from telegram_bot.monitoring import finish_run
            finish_run("interrupted")
        except Exception:
            pass
        log_event("run_interrupted", task="main", status="interrupted")
        print("\n[Interrupted] Current task state was preserved where possible; run lock will be released.\n")
        raise SystemExit(130)
    finally:
        release_lock()

if __name__ == "__main__":
    main()
