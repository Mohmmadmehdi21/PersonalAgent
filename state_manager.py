import os, json, time, subprocess, shutil, glob
from logger import log, log_warn, log_error

STATE_FILE = "state.json"

def get_binary(name):
    """Find an executable without importing the commentary/TTS module."""
    binary = shutil.which(name)
    if binary:
        return binary
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
    return name

def is_valid_json_file(file_path):
    """Returns True if file exists, is non-empty, and parses into valid JSON."""
    if not file_path or not os.path.isfile(file_path):
        return False
    if os.path.getsize(file_path) < 5:
        return False
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data is not None and (isinstance(data, (list, dict)) and len(data) > 0)
    except Exception:
        return False

def is_valid_media_file(file_path):
    """Returns True if media file exists, is non-empty, and ffprobe reads duration > 0."""
    if not file_path or not os.path.isfile(file_path):
        return False
    if os.path.getsize(file_path) < 1024:
        return False

    ffprobe = get_binary("ffprobe")
    cmd = [
        ffprobe, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        dur = float(res.stdout.strip())
        return dur > 0.0
    except Exception:
        return False

def load_state():
    """Loads state.json. Returns default structure if missing or corrupt."""
    if not os.path.exists(STATE_FILE):
        return {"done": [], "clips": [], "done_videos": [], "videos": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("done", [])
            data.setdefault("clips", [])
            data.setdefault("done_videos", [])
            data.setdefault("videos", {})
            return data
    except Exception as e:
        log_warn(f"[State] state.json reading failed ({e}). Returning fresh state structure.")
        return {"done": [], "clips": [], "done_videos": [], "videos": {}}

def save_state_atomic(state):
    """Atomically writes state to state.json via temporary file replace to prevent corruption."""
    tmp_file = STATE_FILE + ".tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp_file, STATE_FILE)
    except Exception as e:
        log_error(f"[State] Atomic state save error: {e}")
        # Fallback to direct write
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

def init_video_state(state, video_id, url, title="", note=""):
    """Initializes or retrieves state tracking dict for a video source."""
    state.setdefault("videos", {})
    if video_id not in state["videos"]:
        state["videos"][video_id] = {
            "source_id": video_id,
            "source_url": url,
            "source_title": title,
            "permission_note": note,
            "status": "pending",
            "attempts": 0,
            "error": None,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "steps": {
                "download": {"status": "pending", "file": None, "error": None, "attempts": 0, "updated_at": None},
                "transcribe": {"status": "pending", "file": None, "error": None, "attempts": 0, "updated_at": None},
                "find_clips": {"status": "pending", "file": None, "error": None, "attempts": 0, "updated_at": None}
            },
            "clips": {}
        }
    return state["videos"][video_id]

def update_step_status(state, video_id, step_name, status, file_path=None, error=None, attempts=1):
    """Updates status of a video pipeline step (download, transcribe, find_clips)."""
    vid_data = state.setdefault("videos", {}).setdefault(video_id, {
        "source_id": video_id,
        "steps": {},
        "clips": {}
    })
    step = vid_data.setdefault("steps", {}).setdefault(step_name, {})
    step["status"] = status
    if file_path:
        step["file"] = file_path
    if error is not None:
        step["error"] = str(error)
    step["attempts"] = step.get("attempts", 0) + attempts
    step["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    vid_data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if status == "failed":
        vid_data["status"] = "failed"
        vid_data["error"] = f"Step '{step_name}' failed: {error}"
    save_state_atomic(state)

def init_clip_state(state, video_id, clip_key, clip_data):
    """Registers a clip under the video's state."""
    vid_data = state.setdefault("videos", {}).setdefault(video_id, {"clips": {}})
    clips = vid_data.setdefault("clips", {})
    if clip_key not in clips:
        clips[clip_key] = {
            "key": clip_key,
            "id": clip_data.get("id"),
            "title": clip_data.get("title"),
            "template": clip_data.get("template"),
            "start": clip_data.get("start"),
            "end": clip_data.get("end"),
            "hook_score": clip_data.get("hook_score"),
            "status": "pending",
            "cut": {"status": "pending", "file": None, "error": None, "updated_at": None},
            "thumbnail": {"status": "pending", "file": None, "error": None, "updated_at": None},
            "upload": {"status": "pending", "youtube_video_id": None, "error": None, "updated_at": None}
        }
    return clips[clip_key]

def update_clip_step(state, video_id, clip_key, step_name, status, file_path=None, error=None, youtube_id=None):
    """Updates status for a specific clip's step (cut, thumbnail, upload)."""
    vid_data = state.setdefault("videos", {}).setdefault(video_id, {"clips": {}})
    clip = vid_data.setdefault("clips", {}).setdefault(clip_key, {
        "key": clip_key,
        "status": "pending"
    })
    step = clip.setdefault(step_name, {})
    step["status"] = status
    if file_path:
        step["file"] = file_path
    if error is not None:
        step["error"] = str(error)
    if youtube_id:
        step["youtube_video_id"] = youtube_id
        clip["youtube_video_id"] = youtube_id
    step["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    # Update overall clip status
    if status == "failed":
        clip["status"] = "failed"
        clip["error"] = f"Clip step '{step_name}' failed: {error}"
    elif step_name == "upload" and status == "done":
        clip["status"] = "uploaded"
    elif step_name == "cut" and status == "done" and clip.get("status") == "pending":
        clip["status"] = "rendered"

    save_state_atomic(state)

def is_clip_already_uploaded(state, clip_key):
    """Duplicate protection: Checks if a clip key has already been uploaded."""
    if clip_key in state.get("clips", []):
        return True
    for vid, vdata in state.get("videos", {}).items():
        clip = vdata.get("clips", {}).get(clip_key)
        if clip:
            if clip.get("status") == "uploaded" or clip.get("upload", {}).get("status") == "done":
                return True
            if clip.get("youtube_video_id"):
                return True
    return False

def are_all_clips_complete(state, video_id, is_dry_run=False):
    """Checks if all drafted clips for a video have been rendered / uploaded."""
    vid = state.get("videos", {}).get(video_id)
    if not vid:
        return False
    clips = vid.get("clips", {})
    if not clips:
        return False
    for k, c in clips.items():
        if is_dry_run:
            if c.get("cut", {}).get("status") != "done":
                return False
        else:
            if c.get("upload", {}).get("status") != "done" and not c.get("youtube_video_id"):
                return False
    return True
