import os, shutil, glob, time
from pathlib import Path
import yt_dlp
from config import (
    WORK,
    YTDLP_COOKIES_FILE,
    YTDLP_COOKIES_FROM_BROWSER,
    YTDLP_DOWNLOAD_TIMEOUT_SECONDS,
)
from logger import log_warn
from retry import retry_download, PermanentSourceError, BotVerificationError, RateLimitedError
from source import video_id_from_url
from state_manager import is_valid_media_file

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
        "socket_timeout": YTDLP_DOWNLOAD_TIMEOUT_SECONDS,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"]
            }
        },
    }
    if deno_bin:
        opts["js_runtimes"] = {"deno": {"path": deno_bin}}
    if ffmpeg_bin:
        opts["ffmpeg_location"] = ffmpeg_dir

    # 1. Check for cookies.txt in root directory or configured YTDLP_COOKIES_FILE
    cookie_path = None
    if YTDLP_COOKIES_FILE and os.path.isfile(YTDLP_COOKIES_FILE):
        cookie_path = YTDLP_COOKIES_FILE
    elif os.path.isfile("cookies.txt"):
        cookie_path = "cookies.txt"

    if cookie_path:
        opts["cookiefile"] = cookie_path
        print(f"[Download] Using cookies file: {os.path.basename(cookie_path)}")
    elif YTDLP_COOKIES_FROM_BROWSER:
        browser_spec = tuple(part.strip() for part in YTDLP_COOKIES_FROM_BROWSER.split(":") if part.strip())
        if browser_spec:
            opts["cookiesfrombrowser"] = browser_spec
            print(f"[Download] Using browser cookies: {browser_spec[0]}")
    else:
        browser_env = os.getenv("YTDLP_COOKIES_FROM_BROWSER", "").strip()
        if browser_env:
            opts["cookiesfrombrowser"] = tuple(part.strip() for part in browser_env.split(":") if part.strip())
            print(f"[Download] Using browser cookies: {opts['cookiesfrombrowser'][0]}")

    # 1. Video Caching Check: Check fast cache by ID if available
    fast_vid = video_id_from_url(url)
    if fast_vid:
        cached_mp4 = os.path.join(WORK, f"{fast_vid}.mp4").replace("\\", "/")
        if is_valid_media_file(cached_mp4):
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    meta = ydl.extract_info(url, download=False)
                    print(f"[Download] Valid cached video found ({cached_mp4}). Skipping download.")
                    return cached_mp4, {
                        "id": meta.get("id", fast_vid),
                        "title": meta.get("title", f"Video {fast_vid}"),
                        "uploader": meta.get("uploader", "YouTube"),
                        "url": meta.get("webpage_url", url),
                        "license": meta.get("license"),
                        "duration": meta.get("duration", 0),
                    }
            except Exception:
                print(f"[Download] Valid cached video found ({cached_mp4}). Skipping download.")
                return cached_mp4, {
                    "id": fast_vid,
                    "title": f"Video {fast_vid}",
                    "uploader": "YouTube",
                    "url": url,
                    "license": None,
                    "duration": 0,
                }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            meta = ydl.extract_info(url, download=False)
            video_id = meta["id"]
            cached_mp4 = os.path.join(WORK, f"{video_id}.mp4").replace("\\", "/")

            if is_valid_media_file(cached_mp4):
                print(f"[Download] Valid cached video found ({cached_mp4}). Skipping download.")
                return cached_mp4, {
                    "id": meta["id"],
                    "title": meta.get("title", ""),
                    "uploader": meta.get("uploader", ""),
                    "url": meta.get("webpage_url", url),
                    "license": meta.get("license"),
                    "duration": meta.get("duration", 0),
                }
            elif os.path.isfile(cached_mp4):
                print(f"[Download] Cached file ({cached_mp4}) corrupt ya incomplete mila. Re-download kiya jayega.")
    except Exception as e:
        print(f"[Download] Metadata check notice: {e}")

    # 2. Download execution via retry_download (3 attempts, 30s/60s/120s backoff)
    def _execute_download():
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)
        if not path.endswith(".mp4"):
            path = path.rsplit(".", 1)[0] + ".mp4"
        path = path.replace("\\", "/")

        if not is_valid_media_file(path):
            raise RuntimeError(f"Downloaded file {path} is corrupt or unreadable.")

        print(f"[Download] Successfully downloaded: {info.get('title', '')} ({info.get('duration', 0)}s)")
        return path, {
            "id": info["id"],
            "title": info.get("title", ""),
            "uploader": info.get("uploader", ""),
            "url": info.get("webpage_url", url),
            "license": info.get("license"),
            "duration": info.get("duration", 0),
        }

    return retry_download(_execute_download)
