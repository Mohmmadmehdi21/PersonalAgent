import json, os, sys, time

if len(sys.argv) > 1 and sys.argv[1] == "telegram-bot":
    try:
        from telegram_bot.poller import run_bot
        run_bot()
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
from commentary import apply_commentary
try:
    import guard
except Exception as _guard_import_err:
    print(f"[Guard] warning (ignored): {_guard_import_err}")

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
    is_download_cooldown_active, get_download_cooldown_remaining, set_download_cooldown
)
from state_manager import (
    load_state, save_state_atomic, init_video_state, update_step_status,
    init_clip_state, update_clip_step, is_clip_already_uploaded,
    is_valid_json_file, is_valid_media_file, are_all_clips_complete
)
from notify import (
    notify_run_success, notify_run_error, notify_quota_or_auth,
    notify_queue_empty
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
    for line in open("sources.txt", encoding="utf-8"):
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
    if not candidates:
        print("[INFO] sources.txt mein koi naya video URL nahi mila.")
        log("[Prepare] sources.txt queue is empty.")
        log_event("task_result", task="prepare:source_selection", status="skipped", reason="no unprocessed source")
        notify_queue_empty()
        return

    # Step A: Download candidate source with multi-candidate fallback
    src = None
    info = None
    url = None
    note = None

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
            log_error(f"[Prepare] Permanent source error for {candidate_url}: {pse}")
            log_event("task_result", task="prepare:download", status="skipped",
                      error_category="permanent_source", error=str(pse)[:500])
            state.setdefault("skipped_video_ids", []).append(cand_vid)
            state.setdefault("done", []).append(candidate_url)
            update_step_status(state, cand_vid, "download", "failed", error=str(pse))
            save_state_atomic(state)
            print(f"[Skip] Video {candidate_url} unavailable/restricted. Trying next candidate...")
            continue
        except BotVerificationError as bve:
            log_error(f"[Prepare] Bot verification required for {candidate_url}: {bve}")
            log_event("task_result", task="prepare:download", status="failure",
                      error_category="bot_verification", error=str(bve)[:500])
            state.setdefault("failed_video_ids", []).append(cand_vid)
            update_step_status(state, cand_vid, "download", "failed", error=str(bve))
            save_state_atomic(state)
            print(f"[Auth] Bot verification required for {candidate_url}. Trying next candidate...")
            continue
        except RateLimitedError as rle:
            log_error(f"[Prepare] Rate limited (HTTP 429) downloading {candidate_url}: {rle}")
            log_event("task_result", task="prepare:download", status="failure",
                      error_category="rate_limited", error=str(rle)[:500])
            set_download_cooldown(state, YTDLP_RATE_LIMIT_COOLDOWN_SECONDS)
            state.setdefault("failed_video_ids", []).append(cand_vid)
            update_step_status(state, cand_vid, "download", "failed", error=str(rle))
            save_state_atomic(state)
            notify_run_error(
                "download_rate_limit",
                f"YouTube HTTP 429 Rate Limit. Pausing downloads for {YTDLP_RATE_LIMIT_COOLDOWN_SECONDS}s. "
                "Configure legitimate cookies (YTDLP_COOKIES_FILE or YTDLP_COOKIES_FROM_BROWSER) or wait for cooldown.",
                video_id=cand_vid,
            )
            print(f"[RateLimit] Rate limit active. Cooldown set for {YTDLP_RATE_LIMIT_COOLDOWN_SECONDS}s.")
            return
        except Exception as e:
            log_error(f"[Prepare] Download failed for {candidate_url}: {e}")
            log_event("task_result", task="prepare:download", status="failure",
                      error_category=type(e).__name__, error=str(e)[:500])
            state.setdefault("failed_video_ids", []).append(cand_vid)
            update_step_status(state, cand_vid, "download", "failed", error=str(e))
            save_state_atomic(state)
            print(f"[Notice] Candidate {candidate_url} failed: {e}. Trying next candidate...")
            continue

    if not src or not info:
        log_error(f"[Prepare] All {len(candidates)} candidate video(s) failed to download.")
        notify_run_error(
            "download_candidates_exhausted",
            f"All {len(candidates)} source candidate(s) failed to download. "
            "If YouTube requires bot verification, configure YTDLP_COOKIES_FILE or YTDLP_COOKIES_FROM_BROWSER.",
        )
        return

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
        update_step_status(state, video_id, "transcribe", "failed", error=str(e))
        notify_run_error("transcribe", str(e), video_id=video_id)
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
        notify_quota_or_auth(failure_type, str(fe))
        raise
    except Exception as e:
        log_error(f"[Prepare] find_clips failed: {e}")
        update_step_status(state, video_id, "find_clips", "failed", error=str(e))
        log_event("task_result", task="prepare:find_clips", status="failure",
                  error_category=type(e).__name__, error=str(e)[:500])
        notify_run_error("find_clips", str(e), video_id=video_id)
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
            "id": f"{info['id']}_{i}",
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
    print(f"[SUCCESS] {new_drafts} commentary clip drafts saved to 'review/queue.json'!")
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
        if item.get("status") in ("pending", "needs_review", None)
    ]
    if not clips_to_process:
        print("\n[Render] Koi pending clips process karne ke liye nahi hain.\n")
        return

    control_task_id = os.getenv("AGENT_CONTROL_TASK_ID", "").strip()
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
                if control_task_id:
                    from telegram_bot.control import checkpoint
                    permission = checkpoint(clip=item, ask_upload=True)
                    if permission == "cancel":
                        item["status"] = "pending"
                        item.pop("processing_step", None)
                        item.pop("processing_started_at", None)
                        save_queue(queue)
                        break
                    if permission != "continue":
                        item["status"] = "pending"
                        item["last_result"] = "upload_confirmation_not_granted"
                        item.pop("processing_step", None)
                        item.pop("processing_started_at", None)
                        save_queue(queue)
                        continue
                print("[Main] YouTube par public upload ho raha hai...")
                item["processing_step"] = "uploading"
                item["upload_started_at"] = time.time()
                save_queue(queue)
                vid = upload(yt, final_clip, thumb_file, item["title"], desc, item["hashtags"])
                try:
                    if os.getenv("TRACKING_ENABLED", "false").strip().lower() in ("true", "1", "t", "yes"):
                        from growth.tracker import record_upload
                        record_upload(vid, item.get("source_id"), item.get("title"), item.get("publish_at"), item.get("hook_score"), item.get("template"), float(item.get("end", 0)) - float(item.get("start", 0)))
                except Exception as growth_error:
                    print(f"[Growth] Upload tracking skipped: {growth_error}")
                state.setdefault("clips", []).append(key)
                item["status"] = "uploaded"
                item["youtube_video_id"] = vid
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
                          youtube_video_id=vid)

                if item.get("is_own"):
                    archive_own_video(item["source_video_path"])
                try:
                    guard.record_clip(item.get("source_id"), item.get("start"), item.get("end"), uploaded=True, dry_run=DRY_RUN)
                except Exception as e:
                    print(f"[Guard] warning (ignored): {e}")

        except UploadCommittedInterruptedError as committed_error:
            item["status"] = "uploaded"
            item["youtube_video_id"] = committed_error.video_id
            item.pop("processing_step", None)
            item.pop("processing_started_at", None)
            item.pop("upload_started_at", None)
            state.setdefault("clips", []).append(key)
            update_clip_step(state, video_id, key, "upload", "done", youtube_id=committed_error.video_id)
            batch_stopped_reason = "interrupted after confirmed upload"
            succeeded_clips.append(item)
            log_event("task_interrupted_after_commit", task=f"clip:{item.get('id', key)}",
                      video_id=committed_error.video_id)
            print(f"[INTERRUPTED] Upload {committed_error.video_id} succeeded; stopping before the next clip.")
            break

        except AccountUploadLimitError as limit_error:
            reason = "uploadLimitExceeded"
            batch_stopped_reason = reason
            item["status"] = "blocked"
            item["upload_error"] = reason
            update_clip_step(state, video_id, key, "upload", "blocked", error=reason)
            save_queue(queue)
            failed_clips.append({"title": item.get("title", item.get("id", key)), "error": reason})
            limit_msg = "[Quota Limit] Aaj ki upload limit poori ho chuki hai. Clips local 'work/' folder mein saved hain."
            print(f"\n{limit_msg}\n")
            log_warn(limit_msg)
            message = (
                f"YouTube account upload limit reached for clip '{item.get('title', key)}' "
                f"({item.get('id', key)}): {reason}. Current clip is blocked; "
                "remaining clips were left queued. Resolve the YouTube account restriction, then rerun."
            )
            log_error(f"[Upload] {message} Details: {limit_error}")
            log_event("task_result", task=f"clip:{item.get('id', key)}", status="failure",
                      error_category="account_upload_limit", error=str(limit_error)[:500])
            notify_quota_or_auth("YouTube Account Upload Limit Reached", message)
            print(f"\n[UPLOAD PAUSED] {message}\n")
            break

        except UploadOutcomeUnknownError as unknown_error:
            batch_stopped_reason = "upload outcome uncertain; manual verification required"
            item["status"] = "needs_review"
            item["upload_error"] = str(unknown_error)
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
            failed_clips.append({"title": item["title"], "error": str(clip_err)})
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
    if succeeded_clips:
        notify_run_success(succeeded_clips, total_time, is_dry_run=DRY_RUN, freed_space_str=freed_str)
    elif failed_clips:
        notify_run_error("render_stage", failed_clips[0]["error"], video_id=clips_to_process[0].get("source_id"))
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
                clip_states = [x.get("status") for x in load_queue() if str(x.get("id", "")) in associated]
                if task.get("cancel_requested"):
                    status = "cancelled"
                elif "blocked" in clip_states:
                    status = "blocked"
                elif "needs_review" in clip_states:
                    status = "needs_review"
                elif "failed" in clip_states:
                    status = "failed"
                else:
                    status = "completed"
                finish(control_task_id, status)
            except Exception as control_error:
                log_warn(f"[Telegram Control] Could not finalize task: {control_error}")
    except Exception as e:
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
        log_error(f"[Main Crash] Unhandled exception: {e}")
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
