"""Source Filter & Cache Validation System for PersonalAgent.

Provides:
1. Source candidate filtering:
   - Skips YouTube Shorts (URLs containing /shorts/)
   - Skips videos with duration <= 60 seconds
   - Skips videos with duration < MIN_SOURCE_DURATION_SEC (default 480s = 8 min)
   - Fail-open: allows candidate if duration cannot be fetched
   - Configurable via SOURCE_FILTER_ENABLED (default true)
2. Cached clips validation:
   - Validates work/<id>.clips.json (start >= 0, end <= duration, 25 <= length <= 60, start < end)
   - Renames invalid cache to .bad and triggers regeneration
"""

import os
import re
import json
import time
from urllib.parse import urlparse, parse_qs, unquote


def is_source_filter_enabled():
    """Returns True if source filtering is enabled in environment (default True)."""
    return os.getenv("SOURCE_FILTER_ENABLED", "true").strip().lower() in ("true", "1", "yes")


def min_source_duration_sec():
    """Minimum duration (in seconds) required for candidate source videos (default 480s = 8m)."""
    try:
        return float(os.getenv("MIN_SOURCE_DURATION_SEC", "480"))
    except (ValueError, TypeError):
        return 480.0


def extract_video_id(url):
    """Extracts standard 11-char YouTube video ID from URL string."""
    if not url or not isinstance(url, str):
        return None
    parsed = urlparse(url.strip())
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


def is_shorts_url(url):
    """Returns True if the URL points to a YouTube Short."""
    if not url:
        return False
    u = str(url).lower()
    return "/shorts/" in u or "shorts/" in u


def parse_iso8601_duration(duration_str):
    """
    Parses ISO 8601 duration string (e.g. PT15M33S, PT8M, PT45S, PT1H2M10S, P1DT2H)
    into total integer seconds. Returns None if unparseable.
    """
    if not duration_str or not isinstance(duration_str, str):
        return None
    pattern = r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$"
    match = re.match(pattern, duration_str.strip())
    if not match:
        return None
    days = int(match.group(1) or 0)
    hours = int(match.group(2) or 0)
    minutes = int(match.group(3) or 0)
    seconds = int(match.group(4) or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def get_video_durations(video_ids):
    """
    Fetches duration (in seconds) for a list of video IDs.
    Returns dict: {video_id: duration_in_seconds or None}
    Uses non-interactive yt-dlp metadata only; this helper must never start OAuth.
    Fail-open: returns None for IDs whose duration could not be determined.
    """
    durations = {vid: None for vid in video_ids if vid}
    if not durations:
        return durations

    try:
        import yt_dlp
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "socket_timeout": 15,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            for vid in durations:
                try:
                    v_url = f"https://www.youtube.com/watch?v={vid}"
                    info = ydl.extract_info(v_url, download=False)
                    dur = (info or {}).get("duration")
                    if dur is not None:
                        durations[vid] = float(dur)
                except Exception:
                    pass
    except Exception:
        pass

    return durations


def filter_candidates(candidates):
    """
    Filters candidate videos before prepare/download:
      - Skips URLs containing /shorts/
      - Skips videos with duration <= 60 seconds
      - Skips videos with duration < MIN_SOURCE_DURATION_SEC (default 480s = 8 min)
    Prints: [SourceFilter] skipped Short/too short: <id>
    Fail-open: If duration is unknown, allows the candidate.
    If SOURCE_FILTER_ENABLED is false, returns original candidates list.
    """
    if not is_source_filter_enabled() or not candidates:
        return candidates

    min_dur = min_source_duration_sec()
    filtered = []

    cand_info = []
    for item in candidates:
        if isinstance(item, tuple) and len(item) == 2:
            url, note = item
        else:
            url, note = str(item), ""
        vid = extract_video_id(url)
        cand_info.append({"url": url, "note": note, "vid": vid, "raw": item})

    pending_for_duration = []
    for c in cand_info:
        vid = c["vid"] or c["url"]
        if is_shorts_url(c["url"]):
            print(f"[SourceFilter] skipped Short/too short: {vid}")
            try:
                import source_memory
                if source_memory.is_reject_memory_enabled():
                    source_memory.mark_rejected(
                        c["vid"] or vid,
                        "SHORT_OR_TOO_SHORT",
                        f"Skipped Short URL ({c['url']})",
                        url=c["url"]
                    )
            except Exception:
                pass
            continue
        pending_for_duration.append(c)

    id_list = [c["vid"] for c in pending_for_duration if c["vid"]]
    durations = get_video_durations(id_list)

    for c in pending_for_duration:
        vid = c["vid"] or c["url"]
        dur = durations.get(c["vid"])

        if dur is not None:
            if dur <= 60.0:
                print(f"[SourceFilter] skipped Short/too short: {vid}")
                try:
                    import source_memory
                    if source_memory.is_reject_memory_enabled():
                        source_memory.mark_rejected(
                            c["vid"] or vid,
                            "SHORT_OR_TOO_SHORT",
                            f"Skipped Short/too short (duration {dur}s <= 60s)",
                            url=c["url"],
                            duration=dur
                        )
                except Exception:
                    pass
                continue
            if dur < min_dur:
                print(f"[SourceFilter] skipped Short/too short: {vid}")
                try:
                    import source_memory
                    if source_memory.is_reject_memory_enabled():
                        source_memory.mark_rejected(
                            c["vid"] or vid,
                            "SHORT_OR_TOO_SHORT",
                            f"Skipped Short/too short (duration {dur}s < required {min_dur}s)",
                            url=c["url"],
                            duration=dur
                        )
                except Exception:
                    pass
                continue

        filtered.append(c["raw"])

    return filtered


def validate_cache_file(clips_cache_path, video_duration=None):
    """
    Validates work/<id>.clips.json file:
      - start >= 0
      - end <= video duration (if video_duration provided, with 0.5s tolerance)
      - 25 <= (end - start) <= 60
      - start < end
    If any clip is invalid:
      Renames clips_cache_path to clips_cache_path + ".bad" (preserving file).
      Prints notice and returns False.
    Returns True if all clips are valid, or False if cache is invalid/corrupt.
    """
    if not is_source_filter_enabled():
        return True

    if not clips_cache_path or not os.path.isfile(clips_cache_path):
        return True

    try:
        with open(clips_cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        _rename_to_bad(clips_cache_path, reason="corrupt JSON")
        return False

    if not isinstance(data, list) or len(data) == 0:
        _rename_to_bad(clips_cache_path, reason="empty or non-list format")
        return False

    dur = float(video_duration) if video_duration and float(video_duration) > 0 else None

    for idx, c in enumerate(data):
        if not isinstance(c, dict):
            _rename_to_bad(clips_cache_path, reason=f"clip #{idx} is not an object")
            return False

        try:
            start = float(c.get("start"))
            end = float(c.get("end"))
        except (TypeError, ValueError):
            _rename_to_bad(clips_cache_path, reason=f"clip #{idx} has non-numeric timestamps")
            return False

        # Rule: start >= 0
        if start < 0:
            _rename_to_bad(clips_cache_path, reason=f"clip #{idx} has negative start ({start}s)")
            return False

        # Rule: start < end
        if end <= start:
            _rename_to_bad(clips_cache_path, reason=f"clip #{idx} end ({end}s) <= start ({start}s)")
            return False

        # Rule: clip length 25 - 60 sec
        clip_len = end - start
        if not (25.0 <= clip_len <= 60.0):
            _rename_to_bad(clips_cache_path, reason=f"clip #{idx} length ({clip_len:.1f}s) not between 25 and 60s")
            return False

        # Rule: end <= video duration (with 0.5s tolerance for minor container discrepancies)
        if dur is not None and end > (dur + 0.5):
            _rename_to_bad(clips_cache_path, reason=f"clip #{idx} end ({end}s) exceeds source duration ({dur:.1f}s)")
            return False

    return True


def _rename_to_bad(clips_cache_path, reason=""):
    """Renames an invalid clips cache file to .bad without deleting it."""
    try:
        bad_path = clips_cache_path + ".bad"
        if os.path.exists(bad_path):
            bad_path = f"{clips_cache_path}.bad_{int(time.time())}"
        os.replace(clips_cache_path, bad_path)
        print(f"[SourceFilter] Invalid cached clips detected in '{clips_cache_path}' ({reason}). Renamed to '{os.path.basename(bad_path)}' and regenerating clips.")
    except Exception as e:
        print(f"[SourceFilter] warning: Could not rename invalid cache file: {e}")
