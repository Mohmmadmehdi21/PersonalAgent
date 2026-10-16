import os, glob
from dotenv import load_dotenv
load_dotenv()

# Windows: Ensure ffmpeg and deno directories are in os.environ["PATH"]
if os.name == "nt":
    local_app = os.environ.get("LOCALAPPDATA", "")
    if local_app:
        search_dirs = [
            os.path.join(local_app, "Microsoft", "WinGet", "Links"),
        ]
        for pkg in glob.glob(os.path.join(local_app, "Microsoft", "WinGet", "Packages", "*")):
            if "FFmpeg" in pkg:
                bin_sub = os.path.join(pkg, "ffmpeg-9.0.2-full_build", "bin")
                if os.path.isdir(bin_sub):
                    search_dirs.append(bin_sub)
                search_dirs.append(pkg)
            elif "Deno" in pkg:
                search_dirs.append(pkg)

        current_path = os.environ.get("PATH", "")
        for d in search_dirs:
            if os.path.isdir(d) and d not in current_path:
                os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]

CLIPS_PER_DAY = int(os.getenv("CLIPS_PER_DAY", 4))
ORIENTATION = os.getenv("ORIENTATION", "vertical")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
# Boolean parsing for DRY_RUN
DRY_RUN = os.getenv("DRY_RUN", "false").strip().lower() in ("true", "1", "t", "yes")
WORK = "work"
