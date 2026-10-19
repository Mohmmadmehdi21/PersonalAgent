"""Merge MrBeast 15+ min dataset into sources.txt (Append-only, Fail-safe).

Rules:
1. Standard library only (no runtime dependencies).
2. Read sources.txt exactly as is; preserve every existing line, comment, and CRLF line ending.
3. Only append new lines at the end.
4. Skip candidates whose ID is already in sources.txt, state.json, data/used_videos.json, or work/.
5. Preserve view-count descending order.
6. Support --dry (prints plan without writing) and --limit N (appends top N).
7. Atomic write via temp file + os.replace, with post-write integrity verification.
"""

import os
import sys
import re
import json
import csv
import argparse
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, unquote


SOURCES_FILE = "sources.txt"
DATASET_TXT = "mrbeast_15min_plus.txt"
DATASET_CSV = "mrbeast_15min_plus_details.csv"
STATE_FILE = "state.json"
USED_VIDEOS_FILE = os.path.join("data", "used_videos.json")
WORK_DIR = "work"


def extract_video_id(url):
    """Extracts standard 11-char YouTube video ID from any standard URL form."""
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
    elif len(path_parts) == 1 and host.endswith("youtube.com") and path_parts[0] not in ("channel", "c", "user", "@mrbeast"):
        candidate = path_parts[0]

    return candidate if candidate and re.fullmatch(r"[\w-]{11}", candidate) else None


def load_existing_sources_ids(sources_path):
    """Loads video IDs already present in sources.txt."""
    existing_ids = set()
    if not os.path.exists(sources_path):
        return existing_ids
    with open(sources_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            url = line.split("|", 1)[0].strip() if "|" in line else line
            vid = extract_video_id(url)
            if vid:
                existing_ids.add(vid)
    return existing_ids


def load_state_known_ids(state_path):
    """Loads video IDs already tracked/consumed in state.json."""
    state_ids = set()
    if not os.path.exists(state_path):
        return state_ids
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
        for u in state.get("done", []):
            vid = extract_video_id(u)
            if vid:
                state_ids.add(vid)
        for vid in state.get("done_videos", []):
            if vid:
                state_ids.add(vid)
        for key in state.get("clips", []):
            if ":" in key:
                vid = key.split(":", 1)[0]
                if vid:
                    state_ids.add(vid)
        for vid, vdata in state.get("videos", {}).items():
            if vid:
                state_ids.add(vid)
    except Exception as e:
        print(f"[Merge] Warning reading state.json ({e}).")
    return state_ids


def load_used_videos_ids(used_videos_path):
    """Loads consumed video IDs from data/used_videos.json ledger."""
    used_ids = set()
    if not os.path.exists(used_videos_path):
        return used_ids
    try:
        with open(used_videos_path, "r", encoding="utf-8") as f:
            ledger = json.load(f)
        for vid, entry in ledger.items():
            if entry.get("status") == "consumed" or len(entry.get("clips", [])) > 0:
                used_ids.add(vid)
    except Exception as e:
        print(f"[Merge] Warning reading used_videos.json ({e}).")
    return used_ids


def load_work_consumed_ids(work_dir):
    """Loads video IDs that have already been processed in work/ directory."""
    work_ids = set()
    if not os.path.isdir(work_dir):
        return work_ids
    try:
        for fname in os.listdir(work_dir):
            if fname.endswith(".mp4") or fname.endswith(".clips.json"):
                base = fname.split(".")[0]
                if base and not base.startswith("tmp"):
                    work_ids.add(base)
    except Exception:
        pass
    return work_ids


def main():
    parser = argparse.ArgumentParser(description="Merge MrBeast 15+ min dataset into sources.txt safely.")
    parser.add_argument("--dry", action="store_true", help="Dry run: print plan without modifying sources.txt")
    parser.add_argument("--limit", type=int, default=None, help="Append only top N videos (default: all)")
    parser.add_argument("--file", default=DATASET_TXT, help="Path to mrbeast_15min_plus.txt")
    parser.add_argument("--details", default=DATASET_CSV, help="Path to mrbeast_15min_plus_details.csv")
    parser.add_argument("--dest", default=SOURCES_FILE, help="Path to sources.txt")
    args = parser.parse_args()

    if not os.path.exists(args.file):
        print(f"[Error] Dataset file '{args.file}' not found! Run 'python make_sources.py' first.")
        sys.exit(1)

    if not os.path.exists(args.dest):
        print(f"[Error] Destination file '{args.dest}' not found!")
        sys.exit(1)

    # 1. Read existing sources.txt exactly as raw bytes to detect line endings and encoding
    with open(args.dest, "rb") as f:
        original_bytes = f.read()

    newline = "\r\n" if b"\r\n" in original_bytes else "\n"
    original_text = original_bytes.decode("utf-8")
    original_lines = original_text.splitlines(keepends=True)

    # 2. Collect existing IDs to skip
    existing_sources_ids = load_existing_sources_ids(args.dest)
    state_ids = load_state_known_ids(STATE_FILE)
    used_ids = load_used_videos_ids(USED_VIDEOS_FILE)
    work_ids = load_work_consumed_ids(WORK_DIR)

    # Load duration details from CSV for secondary validation
    durations = {}
    titles = {}
    views = {}
    if os.path.exists(args.details):
        try:
            with open(args.details, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    vid = extract_video_id(r.get("url", ""))
                    if vid:
                        durations[vid] = float(r.get("duration_min", 0)) * 60
                        titles[vid] = r.get("title", "")
                        views[vid] = int(r.get("views", 0) or 0)
        except Exception as e:
            print(f"[Merge] Warning reading CSV details ({e}).")

    # 3. Read dataset URLs in order
    with open(args.file, "r", encoding="utf-8") as f:
        candidate_urls = [line.strip() for line in f if line.strip()]

    to_append = []
    skipped = []

    for url in candidate_urls:
        vid = extract_video_id(url)
        if not vid:
            skipped.append((url, "Invalid YouTube video ID"))
            continue

        if "/shorts/" in url.lower():
            skipped.append((vid, "Shorts URL detected"))
            continue

        if vid in existing_sources_ids:
            skipped.append((vid, "Already present in sources.txt"))
            continue

        if vid in state_ids:
            skipped.append((vid, "Already present in state.json (done/clips)"))
            continue

        if vid in used_ids:
            skipped.append((vid, "Already present in data/used_videos.json (consumed)"))
            continue

        if vid in work_ids:
            skipped.append((vid, "Already exists in work/ folder"))
            continue

        dur = durations.get(vid)
        if dur is not None and dur < 900:  # < 15 min
            skipped.append((vid, f"Duration ({dur/60:.1f}m) < 15 minutes"))
            continue

        # Format line with note
        note = "MrBeast official, 15+ min"
        formatted_line = f"https://www.youtube.com/watch?v={vid} | {note}"
        to_append.append({
            "id": vid,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "line": formatted_line,
            "title": titles.get(vid, ""),
            "views": views.get(vid, 0),
            "duration_min": round(dur / 60, 1) if dur else None,
        })

    # Apply optional limit
    total_eligible = len(to_append)
    if args.limit is not None and args.limit > 0:
        to_append = to_append[:args.limit]

    print("=" * 70)
    print("MERGE SOURCES PLAN SUMMARY")
    print("=" * 70)
    print(f"Total dataset candidates: {len(candidate_urls)}")
    print(f"Skipped candidates:       {len(skipped)}")
    print(f"Eligible for append:      {total_eligible}")
    if args.limit is not None:
        print(f"Limited to top:           {len(to_append)} (--limit {args.limit})")
    print(f"Existing sources.txt lines: {len(original_lines)}")

    if skipped:
        print("\nSkipped Candidates Sample (First 10):")
        for item, reason in skipped[:10]:
            print(f"  - {item}: {reason}")
        if len(skipped) > 10:
            print(f"  ... and {len(skipped) - 10} more skipped items.")

    print(f"\nFirst 10 Videos to be Appended:")
    for i, item in enumerate(to_append[:10]):
        print(f"  {i+1}. {item['id']} | {item.get('duration_min', '?')} min | {item.get('views', 0):,} views | {item.get('title', '')}")

    if args.dry:
        print("\n" + "=" * 70)
        print("[DRY RUN COMPLETE] No changes were written to sources.txt.")
        print("To apply changes, run without --dry.")
        print("=" * 70)
        return

    if not to_append:
        print("\n[No Changes] No new eligible video links; sources.txt was left untouched.")
        return

    # Real Append Execution
    print("\n[Executing Append to sources.txt]...")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    header_comment = f"# --- added {stamp} by merge_sources.py: MrBeast official channel, 15+ min, sorted by views ---"

    # Ensure existing file ends with newline
    needs_newline = len(original_text) > 0 and not original_text.endswith("\n") and not original_text.endswith("\r\n")

    new_block_lines = []
    if needs_newline:
        new_block_lines.append("")
    new_block_lines.append(header_comment)
    for item in to_append:
        new_block_lines.append(item["line"])
    new_block_lines.append("")  # Trailing blank line

    new_block_text = newline.join(new_block_lines)

    # Atomic write
    tmp_path = args.dest + ".tmp"
    with open(tmp_path, "wb") as f:
        f.write(original_bytes)
        f.write(new_block_text.encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp_path, args.dest)

    # Post-write verification
    with open(args.dest, "rb") as f:
        written_bytes = f.read()

    # Verify original bytes are intact at the beginning
    if not written_bytes.startswith(original_bytes):
        print("[CRITICAL ERROR] Integrity check failed! Original content was modified!")
        sys.exit(1)

    with open(args.dest, "r", encoding="utf-8") as f:
        updated_lines = f.readlines()

    print(f"[SUCCESS] Appended {len(to_append)} videos to '{args.dest}'.")
    print(f"Original line count: {len(original_lines)} -> New line count: {len(updated_lines)}")
    print("=" * 70)


if __name__ == "__main__":
    main()
