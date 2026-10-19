"""
MrBeast ke official channel ke saare 15+ minute wale videos ki link list banata hai.

Chalane ka tareeka (K:\\PersonalAgent mein, venv active ho):
    python -m pip install -U yt-dlp
    python make_sources.py

Output:
    mrbeast_15min_plus.txt          -> sirf URLs, ek line mein ek (views ke hisaab se sorted)
    mrbeast_15min_plus_details.csv  -> title, duration, views (sirf dekhne ke liye)

Isko agent ki sources.txt mein copy karne se pehle ek baar dekh lena.
"""
import csv
import yt_dlp

# Official channel(s). Dusre official channels (Gaming, Beast Reacts, Beast Philanthropy)
# chahiye to unki /videos URL yahan add kar do.
CHANNELS = [
    "https://www.youtube.com/@MrBeast/videos",
]
MIN_SECONDS = 15 * 60
OUT_TXT = "mrbeast_15min_plus.txt"
OUT_CSV = "mrbeast_15min_plus_details.csv"


def walk(entries):
    """Nested playlists/tabs ko flat video entries mein badalta hai."""
    for e in entries or []:
        if not e:
            continue
        if e.get("entries") is not None:
            yield from walk(e["entries"])
        else:
            yield e


def main():
    flat_opts = {"extract_flat": True, "quiet": True, "skip_download": True,
                 "ignoreerrors": True}
    seen, videos = set(), []

    with yt_dlp.YoutubeDL(flat_opts) as ydl:
        for ch in CHANNELS:
            print("Fetching:", ch)
            info = ydl.extract_info(ch, download=False)
            for e in walk((info or {}).get("entries")):
                vid = e.get("id")
                if not vid or vid in seen:
                    continue
                if e.get("live_status") in ("is_live", "is_upcoming"):
                    continue
                seen.add(vid)
                videos.append({
                    "id": vid,
                    "title": e.get("title") or "",
                    "duration": e.get("duration"),
                    "views": e.get("view_count") or 0,
                })

    print(f"Total videos mile: {len(videos)}")

    # Jin videos ki duration flat list mein nahi aayi, unki duration alag se nikalo.
    missing = [v for v in videos if v["duration"] is None]
    if missing:
        print(f"{len(missing)} videos ki duration alag se check ho rahi hai (thoda time lagega)...")
        full_opts = {"quiet": True, "skip_download": True, "ignoreerrors": True}
        with yt_dlp.YoutubeDL(full_opts) as ydl:
            for v in missing:
                try:
                    info = ydl.extract_info(
                        f"https://www.youtube.com/watch?v={v['id']}", download=False)
                    if info:
                        v["duration"] = info.get("duration")
                        v["views"] = info.get("view_count") or v["views"]
                except Exception as ex:
                    print("skip", v["id"], ex)

    long_videos = [v for v in videos if (v["duration"] or 0) >= MIN_SECONDS]
    long_videos.sort(key=lambda v: v["views"], reverse=True)

    with open(OUT_TXT, "w", encoding="utf-8") as f:
        for v in long_videos:
            f.write(f"https://www.youtube.com/watch?v={v['id']}\n")

    with open(OUT_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["url", "title", "duration_min", "views"])
        for v in long_videos:
            w.writerow([f"https://www.youtube.com/watch?v={v['id']}", v["title"],
                        round(v["duration"] / 60, 1), v["views"]])

    print(f"Done: {len(long_videos)} videos (15+ min) -> {OUT_TXT}")


if __name__ == "__main__":
    main()
