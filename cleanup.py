import os, sys, time, glob, re

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from config import WORK
from state_manager import load_state, are_all_clips_complete
from logger import log, log_warn, log_error

def is_safe_work_path(file_path):
    """Verifies that the target path is strictly inside the work/ folder."""
    try:
        real_work = os.path.realpath(WORK)
        real_target = os.path.realpath(file_path)
        return real_target.startswith(real_work) and real_target != real_work
    except Exception:
        return False

def format_size(bytes_num):
    if bytes_num >= 1024 ** 3:
        return f"{bytes_num / (1024 ** 3):.2f} GB"
    elif bytes_num >= 1024 ** 2:
        return f"{bytes_num / (1024 ** 2):.2f} MB"
    elif bytes_num >= 1024:
        return f"{bytes_num / 1024:.1f} KB"
    return f"{bytes_num} B"

def get_cleanup_candidates(state=None, is_dry_run=False):
    """
    Identifies files eligible for cleanup according to safety policy.
    Never includes transcripts, clips json, state, or files from active/failed runs.
    """
    state = state or load_state()
    delete_source = os.getenv("DELETE_SOURCE", "true").strip().lower() in ("true", "1", "yes")
    keep_transcript = os.getenv("KEEP_TRANSCRIPT", "true").strip().lower() in ("true", "1", "yes")
    keep_days = int(os.getenv("KEEP_FINAL_CLIPS_DAYS", "2"))

    now = time.time()
    clip_cutoff = now - (keep_days * 86400)
    orphan_cutoff = now - (7 * 86400)

    candidates = []

    # 1. 7-day-old partial/temp files (*.part, *.ytdl, *.tmp, *.f*)
    if os.path.exists(WORK):
        for entry in os.listdir(WORK):
            fpath = os.path.join(WORK, entry)
            if not os.path.isfile(fpath) or not is_safe_work_path(fpath):
                continue
            ext = os.path.splitext(entry)[1].lower()
            if ext in (".part", ".ytdl", ".tmp") or re.search(r"\.f\d+$", entry):
                if os.path.getmtime(fpath) < orphan_cutoff:
                    candidates.append((fpath, os.path.getsize(fpath), "Stale partial/temp download (>7 days)"))

    # 2. Completed video files
    # Find videos where all clips are completed
    videos = state.get("videos", {})
    completed_video_ids = set()

    # Check top-level done_videos as well
    for vid in state.get("done_videos", []):
        completed_video_ids.add(vid)

    for vid, vdata in videos.items():
        if are_all_clips_complete(state, vid, is_dry_run=is_dry_run) or vdata.get("status") == "done":
            completed_video_ids.add(vid)

    for vid in completed_video_ids:
        # A. Large source video
        if delete_source:
            src_mp4 = os.path.join(WORK, f"{vid}.mp4")
            if os.path.isfile(src_mp4) and is_safe_work_path(src_mp4):
                candidates.append((src_mp4, os.path.getsize(src_mp4), f"Uploaded source video ({vid}.mp4)"))

        # B. Intermediate rendering files for this video
        intermediate_patterns = [
            f"{vid}_*_base.mp4",
            f"{vid}_*_comm_*.png",
            f"{vid}_*_comm_*.mp4",
            f"{vid}_*_tts.mp3",
            f"{vid}_*.srt",
            f"{vid}_*.concat.txt",
            f"{vid}_*.frame.jpg"
        ]
        for pat in intermediate_patterns:
            for fpath in glob.glob(os.path.join(WORK, pat)):
                if os.path.isfile(fpath) and is_safe_work_path(fpath):
                    # Transcripts must be kept
                    if keep_transcript and (".transcript.json" in fpath or ".clips.json" in fpath):
                        continue
                    candidates.append((fpath, os.path.getsize(fpath), "Intermediate render asset"))

        # C. Old final clips / thumbnails (> KEEP_FINAL_CLIPS_DAYS old)
        final_patterns = [f"{vid}_*.mp4", f"{vid}_*.jpg"]
        for pat in final_patterns:
            for fpath in glob.glob(os.path.join(WORK, pat)):
                if not os.path.isfile(fpath) or not is_safe_work_path(fpath):
                    continue
                # Do not delete base or comm files here (they are handled in B)
                if "_base" in fpath or "_comm" in fpath or fpath.endswith(f"{vid}.mp4"):
                    continue
                if os.path.getmtime(fpath) < clip_cutoff:
                    candidates.append((fpath, os.path.getsize(fpath), f"Expired uploaded clip/thumb (>{keep_days} days)"))

    # Remove duplicates preserving order
    seen = set()
    unique_candidates = []
    for c in candidates:
        if c[0] not in seen:
            seen.add(c[0])
            unique_candidates.append(c)

    return unique_candidates

def run_cleanup(dry_run=True, force=False):
    """
    Executes or previews disk cleanup.
    If dry_run=True and not force: prints candidate files without deleting.
    Returns (freed_bytes, freed_size_str).
    """
    state = load_state()
    candidates = get_cleanup_candidates(state, is_dry_run=dry_run)
    total_bytes = sum(c[1] for c in candidates)
    total_str = format_size(total_bytes)

    print("=" * 65)
    print(f"[DISK CLEANUP ENGINE] ({'PREVIEW / DRY-RUN' if dry_run and not force else 'EXECUTING DELETION'})")
    print("=" * 65)

    if not candidates:
        print("[Cleanup] Koi eligible files delete karne ke liye nahi mili.")
        print("[Cleanup] Saari transcripts, clips JSON, aur active working files safe hain.\n")
        return 0, "0 B"

    print(f"Total Candidate Files: {len(candidates)} | Potential Space to Free: {total_str}\n")
    print(f"{'File':<55} | {'Size':<10} | {'Reason'}")
    print("-" * 90)

    for fpath, sz, reason in candidates:
        rel_path = os.path.relpath(fpath, ".")
        print(f"{rel_path:<55} | {format_size(sz):<10} | {reason}")
    print("-" * 90)

    if dry_run and not force:
        print(f"\n[Cleanup Dry-Run] Koi file delete nahi hui. Total space jo free HOGA: {total_str}")
        print("Asli deletion ke liye run karein: python main.py cleanup\n")
        return total_bytes, total_str

    # Actual Deletion
    freed_bytes = 0
    deleted_count = 0
    for fpath, sz, _ in candidates:
        if not is_safe_work_path(fpath):
            log_warn(f"[Cleanup Safety] Skipping path outside work/: {fpath}")
            continue
        try:
            os.remove(fpath)
            freed_bytes += sz
            deleted_count += 1
        except Exception as e:
            log_error(f"[Cleanup] Failed to delete {fpath}: {e}")

    freed_str = format_size(freed_bytes)
    log(f"[Cleanup] Disk cleanup completed: {deleted_count} files removed, {freed_str} space freed.")
    print(f"\n[SUCCESS] Disk Cleanup Complete! {deleted_count} files delete kiye gaye.")
    print(f"[SUCCESS] {freed_str} disk space khali hua!\n")
    return freed_bytes, freed_str
