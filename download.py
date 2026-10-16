import os, shutil, glob, time
import yt_dlp
from config import WORK

def find_binary(name):
    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            links_bin = os.path.join(local_app, "Microsoft", "WinGet", "Links", f"{name}.exe")
            if os.path.isfile(links_bin):
                return links_bin
            pattern = os.path.join(local_app, "Microsoft", "WinGet", "Packages", "**", f"{name}.exe")
            matches = glob.glob(pattern, recursive=True)
            if matches:
                return matches[0]
    return None

def download(url):
    os.makedirs(WORK, exist_ok=True)
    deno_bin = find_binary("deno")
    ffmpeg_bin = find_binary("ffmpeg")

    if not ffmpeg_bin:
        raise RuntimeError("FFmpeg nahi mila! Kripya FFmpeg install karein ya PATH check karein.")

    ffmpeg_dir = os.path.dirname(ffmpeg_bin)
    if ffmpeg_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")

    opts = {
        "format": "bv*[height<=1080]+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": f"{WORK}/%(id)s.%(ext)s",
        "quiet": False,
        "no_warnings": False,
    }
    if deno_bin:
        opts["js_runtimes"] = {"deno": {"path": deno_bin}}
    if ffmpeg_bin:
        opts["ffmpeg_location"] = ffmpeg_dir

    # 1. Video Caching Check: ID extract karo bina download kiye
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            meta = ydl.extract_info(url, download=False)
            video_id = meta["id"]
            cached_mp4 = os.path.join(WORK, f"{video_id}.mp4").replace("\\", "/")

            if os.path.isfile(cached_mp4) and os.path.getsize(cached_mp4) > 1024:
                print(f"[Download] Cached video found ({cached_mp4}). Skipping download.")
                return cached_mp4, {
                    "id": meta["id"],
                    "title": meta.get("title", ""),
                    "uploader": meta.get("uploader", ""),
                    "url": meta.get("webpage_url", url),
                    "license": meta.get("license"),
                    "duration": meta.get("duration", 0),
                }
    except Exception as e:
        print(f"[Download] Metadata check notice: {e}")

    # 2. Download loop (with 3 retries)
    last_err = None
    for attempt in range(1, 4):
        try:
            print(f"[Download] Attempt {attempt}/3: {url}")
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                path = ydl.prepare_filename(info)
            if not path.endswith(".mp4"):
                path = path.rsplit(".", 1)[0] + ".mp4"
            path = path.replace("\\", "/")
            print(f"[Download] Successfully downloaded: {info.get('title', '')} ({info.get('duration', 0)}s)")
            return path, {
                "id": info["id"],
                "title": info.get("title", ""),
                "uploader": info.get("uploader", ""),
                "url": info.get("webpage_url", url),
                "license": info.get("license"),
                "duration": info.get("duration", 0),
            }
        except Exception as e:
            last_err = e
            print(f"[Download] Attempt {attempt} failed: {e}")
            if attempt < 3:
                time.sleep(3 * attempt)
    raise RuntimeError(f"Video download 3 attempts ke baad fail ho gaya: {last_err}")
