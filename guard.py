"""Duplicate Guard System for PersonalAgent.

Ensures:
1. A source video is never reused once consumed.
2. At most MAX_CLIPS_PER_VIDEO (default 4) clips are created per video over lifetime.
3. No overlapping or scene-repeating clips are created for the same video (enforces MIN_GAP_SEC).
4. Zero dependency on external libraries (pure Python standard library).
"""

import os
import sys
import re
import json
import hashlib
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, unquote


DATA_DIR = "data"
LEDGER_FILE = os.path.join(DATA_DIR, "used_videos.json")
STATE_FILE = "state.json"
SOURCES_FILE = "sources.txt"


def is_guard_enabled():
    """Returns True if duplicate guard is enabled in environment (default True)."""
    return os.getenv("GUARD_ENABLED", "true").strip().lower() in ("true", "1", "yes")


def max_clips_per_video():
    """Maximum clips allowed per source video over lifetime (default 4)."""
    try:
        return int(os.getenv("MAX_CLIPS_PER_VIDEO", "4"))
    except (ValueError, TypeError):
        return 4


def min_gap_sec():
    """Minimum seconds gap between two non-overlapping clips from the same video (default 10)."""
    try:
        return float(os.getenv("MIN_GAP_SEC", "10"))
    except (ValueError, TypeError):
        return 10.0


def is_count_dry_run():
    """Returns True if DRY_RUN executions should consume videos and record clips (default False)."""
    return os.getenv("GUARD_COUNT_DRY_RUN", "false").strip().lower() in ("true", "1", "yes")


def video_id_from_url(url):
    """
    Extracts a canonical 11-character YouTube video ID from any standard URL form:
      - youtu.be/<id> (ignoring ?si=, ?t=, ?list=)
      - youtube.com/watch?v=<id> (ignoring other query parameters)
      - youtube.com/shorts/<id>
      - youtube.com/embed/<id>
      - youtube.com/live/<id>
      - m.youtube.com, music.youtube.com, etc.
    If no YouTube ID is found, returns SHA-1 hex digest of normalized URL string.
    """
    if not url or not isinstance(url, str):
        return None

    cleaned_url = url.strip()
    parsed = urlparse(cleaned_url)
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

    if candidate and re.fullmatch(r"[\w-]{11}", candidate):
        return candidate

    # Non-YouTube or unparsed direct URL: return sha1 of normalized URL
    norm = f"{parsed.scheme.lower()}://{host}{parsed.path.rstrip('/')}"
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def load_ledger():
    """
    Loads used_videos ledger from data/used_videos.json.
    If the file is corrupted, renames to used_videos.json.corrupt.bak and returns empty dict.
    """
    if not os.path.exists(LEDGER_FILE):
        return {}

    try:
        with open(LEDGER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
            raise ValueError("Ledger root must be a JSON object")
    except Exception as err:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        corrupt_backup = f"{LEDGER_FILE}.corrupt_{stamp}.bak"
        print(f"[Guard] WARNING: Ledger file '{LEDGER_FILE}' is corrupt ({err})! Renaming to '{corrupt_backup}'.")
        try:
            os.replace(LEDGER_FILE, corrupt_backup)
        except Exception as ren_err:
            print(f"[Guard] Could not rename corrupt ledger: {ren_err}")
        return {}


def save_ledger(ledger):
    """
    Atomically writes ledger to data/used_videos.json via temp file + os.replace.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    temp_fd, temp_path = tempfile.mkstemp(prefix="used_videos_", suffix=".tmp", dir=DATA_DIR)
    try:
        encoded = (json.dumps(ledger, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        with os.fdopen(temp_fd, "wb") as f:
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, LEDGER_FILE)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def bootstrap():
    """
    Initializes data/used_videos.json ledger from existing state.json if ledger does not exist yet.
    Only READS state.json, never writes to it.
    """
    if os.path.exists(LEDGER_FILE):
        return

    if not os.path.exists(STATE_FILE):
        save_ledger({})
        return

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception as e:
        print(f"[Guard] bootstrap could not read state.json ({e}). Starting fresh ledger.")
        save_ledger({})
        return

    ledger = {}
    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. Done URLs from state
    for u in state.get("done", []):
        vid = video_id_from_url(u)
        if vid and vid not in ledger:
            ledger[vid] = {
                "url": u,
                "status": "consumed",
                "claimed_at": now_iso,
                "clips": []
            }

    # 2. Done video IDs from state
    for vid in state.get("done_videos", []):
        if vid and vid not in ledger:
            ledger[vid] = {
                "url": f"https://www.youtube.com/watch?v={vid}",
                "status": "consumed",
                "claimed_at": now_iso,
                "clips": []
            }

    # 3. Existing clip keys (format "<video_id>:<start>-<end>")
    for key in state.get("clips", []):
        if ":" in key and "-" in key:
            try:
                parts = key.split(":", 1)
                vid = parts[0]
                rng = parts[1].split("-", 1)
                s = float(rng[0])
                e = float(rng[1])
                if vid not in ledger:
                    ledger[vid] = {
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "status": "consumed",
                        "claimed_at": now_iso,
                        "clips": []
                    }
                ledger[vid]["clips"].append({
                    "start": s,
                    "end": e,
                    "uploaded": True,
                    "at": now_iso
                })
            except Exception:
                pass

    # 4. Check video entries in state["videos"]
    for vid, vdata in state.get("videos", {}).items():
        if not isinstance(vdata, dict):
            continue
        v_url = vdata.get("source_url") or f"https://www.youtube.com/watch?v={vid}"
        v_status = vdata.get("status")
        if v_status in ("done", "completed"):
            if vid not in ledger:
                ledger[vid] = {
                    "url": v_url,
                    "status": "consumed",
                    "claimed_at": now_iso,
                    "clips": []
                }
        for ckey, cinfo in vdata.get("clips", {}).items():
            if not isinstance(cinfo, dict):
                continue
            if cinfo.get("status") in ("done", "uploaded") or ":" in ckey:
                try:
                    rng = ckey.split("-", 1) if "-" in ckey else (0, 0)
                    s = float(rng[0]) if len(rng) == 2 else 0.0
                    e = float(rng[1]) if len(rng) == 2 else 0.0
                    if vid not in ledger:
                        ledger[vid] = {
                            "url": v_url,
                            "status": "consumed",
                            "claimed_at": now_iso,
                            "clips": []
                        }
                    if not any(c.get("start") == s and c.get("end") == e for c in ledger[vid]["clips"]):
                        ledger[vid]["clips"].append({
                            "start": s,
                            "end": e,
                            "uploaded": True,
                            "at": now_iso
                        })
                except Exception:
                    pass

    save_ledger(ledger)
    print(f"[Guard] Bootstrapped ledger from state.json with {len(ledger)} tracked videos.")


def sync_state(state):
    """
    Adds all consumed videos in ledger that match sources.txt entries into the in-memory
    state["done"] and state["done_videos"] lists.
    Does NOT write to disk (main.py handles state persistence).
    """
    if not is_guard_enabled() or not isinstance(state, dict):
        return

    ledger = load_ledger()
    if not ledger:
        return

    consumed_vids = {
        vid for vid, entry in ledger.items()
        if entry.get("status") == "consumed" or len(entry.get("clips", [])) >= max_clips_per_video()
    }
    if not consumed_vids:
        return

    state.setdefault("done", [])
    state.setdefault("done_videos", [])

    # Also check sources.txt to match any verbatim URL variants
    if os.path.exists(SOURCES_FILE):
        try:
            with open(SOURCES_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    url = line.split("|", 1)[0].strip() if "|" in line else line
                    vid = video_id_from_url(url)
                    if vid and vid in consumed_vids:
                        if url not in state["done"]:
                            state["done"].append(url)
                        if vid not in state["done_videos"]:
                            state["done_videos"].append(vid)
        except Exception as e:
            print(f"[Guard] warning in sync_state reading sources.txt: {e}")

    for vid in consumed_vids:
        if vid not in state["done_videos"]:
            state["done_videos"].append(vid)
        watch_url = f"https://www.youtube.com/watch?v={vid}"
        if watch_url not in state["done"]:
            state["done"].append(watch_url)


def claim(video_id, url):
    """
    Claims a video in the ledger.
    If the video is already marked as 'consumed' or has reached MAX_CLIPS_PER_VIDEO,
    prints a message and returns False.
    Otherwise creates/updates entry and returns True.
    """
    if not is_guard_enabled():
        return True

    if not video_id:
        return True

    ledger = load_ledger()
    entry = ledger.get(video_id)
    limit = max_clips_per_video()

    if entry:
        if entry.get("status") == "consumed":
            print(f"[Guard] Video '{video_id}' is already consumed. Skipping.")
            return False
        if len(entry.get("clips", [])) >= limit:
            entry["status"] = "consumed"
            save_ledger(ledger)
            print(f"[Guard] Video '{video_id}' has reached maximum allowed clips ({limit}). Skipping.")
            return False
    else:
        now_iso = datetime.now(timezone.utc).isoformat()
        ledger[video_id] = {
            "url": url or f"https://www.youtube.com/watch?v={video_id}",
            "status": "claimed",
            "claimed_at": now_iso,
            "clips": []
        }
        save_ledger(ledger)

    return True


def filter_clips(video_id, clips):
    """
    Filters candidates from find_clips:
    1. Removes candidates that overlap or are closer than MIN_GAP_SEC to existing clips in ledger.
    2. Intra-list deduplication: between overlapping candidate clips, keeps the one with higher hook_score.
    3. Trims the list to (MAX_CLIPS_PER_VIDEO - existing_clips_count).
    4. Preserves clip structure and relative order.
    Fail-open: on any error, returns the original clips list unaltered.
    """
    if not is_guard_enabled() or not clips or not isinstance(clips, list):
        return clips

    try:
        ledger = load_ledger()
        entry = ledger.get(video_id, {})
        existing = entry.get("clips", [])
        limit = max_clips_per_video()
        remaining_slots = max(0, limit - len(existing))

        if remaining_slots <= 0:
            print(f"[Guard] Video '{video_id}' already has {len(existing)}/{limit} clips recorded. No slots left.")
            return []

        gap = min_gap_sec()

        # Step 1: Filter out clips that clash with existing ledger clips
        surviving = []
        for c in clips:
            try:
                c_start = float(c.get("start", 0))
                c_end = float(c.get("end", 0))
            except (ValueError, TypeError):
                continue

            clash = False
            for ex in existing:
                try:
                    ex_start = float(ex.get("start", 0))
                    ex_end = float(ex.get("end", 0))
                except (ValueError, TypeError):
                    continue
                # Overlap or closer than gap
                if not (c_end + gap <= ex_start or c_start >= ex_end + gap):
                    clash = True
                    break
            if not clash:
                surviving.append(c)

        # Step 2: Intra-list clash resolution (higher hook_score wins; tie keeps earlier)
        kept = []
        for cand in surviving:
            try:
                cand_start = float(cand.get("start", 0))
                cand_end = float(cand.get("end", 0))
                cand_score = float(cand.get("hook_score", 0) or 0)
            except (ValueError, TypeError):
                continue

            clashing_idx = None
            for idx, k in enumerate(kept):
                try:
                    k_start = float(k.get("start", 0))
                    k_end = float(k.get("end", 0))
                except (ValueError, TypeError):
                    continue
                if not (cand_end + gap <= k_start or cand_start >= k_end + gap):
                    clashing_idx = idx
                    break

            if clashing_idx is None:
                kept.append(cand)
            else:
                k_score = float(kept[clashing_idx].get("hook_score", 0) or 0)
                if cand_score > k_score:
                    # Replace with higher scoring candidate
                    kept[clashing_idx] = cand

        # Step 3: Trim to remaining slots
        if len(kept) > remaining_slots:
            kept = kept[:remaining_slots]

        # Preserve original relative order
        clip_order = {id(c): i for i, c in enumerate(clips)}
        kept.sort(key=lambda c: clip_order.get(id(c), 0))

        if len(kept) < len(clips):
            print(f"[Guard] filter_clips: Clamped clips from {len(clips)} to {len(kept)} (gap={gap}s, max_limit={limit}).")

        return kept

    except Exception as e:
        print(f"[Guard] warning in filter_clips (fail-open): {e}")
        return clips


def record_clip(video_id, start, end, uploaded=True, dry_run=False):
    """
    Records a finalized clip in the ledger.
    Once the first clip is recorded, marks the video status as 'consumed'.
    If dry_run is True and GUARD_COUNT_DRY_RUN is false, prints a notice without saving to ledger.
    """
    if not is_guard_enabled() or not video_id:
        return

    try:
        s = float(start)
        e = float(end)
    except (ValueError, TypeError):
        return

    if dry_run and not is_count_dry_run():
        print(f"[Guard] DRY_RUN active (GUARD_COUNT_DRY_RUN=false): Skipping ledger recording for {video_id}:{s}-{e}.")
        return

    try:
        ledger = load_ledger()
        now_iso = datetime.now(timezone.utc).isoformat()
        entry = ledger.setdefault(video_id, {
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "status": "consumed",
            "claimed_at": now_iso,
            "clips": []
        })

        # Mark as consumed immediately upon recording a clip
        entry["status"] = "consumed"
        entry.setdefault("clips", []).append({
            "start": s,
            "end": e,
            "uploaded": bool(uploaded),
            "at": now_iso
        })

        save_ledger(ledger)
        print(f"[Guard] Clip [{s}s - {e}s] recorded for '{video_id}'. Video marked as 'consumed'.")
    except Exception as e:
        print(f"[Guard] warning in record_clip: {e}")


def status_cli():
    """Prints the status of all tracked videos in the ledger."""
    ledger = load_ledger()
    print("=" * 65)
    print("DUPLICATE GUARD LEDGER STATUS (data/used_videos.json)")
    print("=" * 65)
    if not ledger:
        print("No videos tracked in ledger yet.")
        print("=" * 65)
        return

    limit = max_clips_per_video()
    print(f"{'Video ID':<15} {'Status':<10} {'Clips':<12} {'Claimed At':<22} {'URL'}")
    print("-" * 65)
    for vid, entry in ledger.items():
        status = entry.get("status", "unknown")
        clips_cnt = len(entry.get("clips", []))
        claimed = (entry.get("claimed_at") or "")[:19].replace("T", " ")
        url = entry.get("url", "")
        print(f"{vid:<15} {status:<10} {f'{clips_cnt}/{limit}':<12} {claimed:<22} {url}")
    print("=" * 65)


def release_cli(video_id):
    """Releases a consumed video from the ledger after user confirmation."""
    if not video_id:
        print("Error: video_id required. Usage: python guard.py release <video_id>")
        return

    ledger = load_ledger()
    if video_id not in ledger:
        print(f"[Guard] Video '{video_id}' is not in the ledger.")
        return

    entry = ledger[video_id]
    print(f"\nVideo '{video_id}' found in ledger:")
    print(f"  Status: {entry.get('status')}")
    print(f"  Clips count: {len(entry.get('clips', []))}")
    print(f"  URL: {entry.get('url')}")

    try:
        confirm = input(f"\nKya aap video '{video_id}' ko release karna chahte hain? Isse ye dobara use ho sakega. (y/N): ")
    except (EOFError, KeyboardInterrupt):
        print("\nRelease cancelled.")
        return

    if confirm.strip().lower() in ("y", "yes"):
        del ledger[video_id]
        save_ledger(ledger)
        print(f"[Guard] SUCCESS: Video '{video_id}' has been released and can now be processed again.")
    else:
        print("[Guard] Release cancelled.")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "status":
        status_cli()
    elif args[0] == "release":
        if len(args) < 2:
            print("Usage: python guard.py release <video_id>")
        else:
            release_cli(args[1])
    else:
        print(f"Unknown command: '{args[0]}'. Available commands: status, release <video_id>")
