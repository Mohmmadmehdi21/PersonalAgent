import os, shutil, glob
from config import OWN_DIR

def get_pending_own_video():
    """Returns the first uncompleted own video in own_videos/ folder, or None."""
    if not os.path.exists(OWN_DIR):
        os.makedirs(OWN_DIR, exist_ok=True)
    done_dir = os.path.join(OWN_DIR, "done")
    if not os.path.exists(done_dir):
        os.makedirs(done_dir, exist_ok=True)

    candidates = glob.glob(os.path.join(OWN_DIR, "*.mp4"))
    for f in candidates:
        if os.path.isfile(f) and not f.startswith(done_dir):
            return f
    return None

def archive_own_video(video_path):
    """Moves an uploaded own video to own_videos/done/."""
    done_dir = os.path.join(OWN_DIR, "done")
    os.makedirs(done_dir, exist_ok=True)
    dest = os.path.join(done_dir, os.path.basename(video_path))
    shutil.move(video_path, dest)
    # Move matching .txt if exists
    txt_path = os.path.splitext(video_path)[0] + ".txt"
    if os.path.exists(txt_path):
        shutil.move(txt_path, os.path.join(done_dir, os.path.basename(txt_path)))
    print(f"[Own Video] Moved {video_path} to {dest}")
