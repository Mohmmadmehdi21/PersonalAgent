import subprocess, shutil
from config import ORIENTATION

def _ts(t):
    h, m, s = int(t // 3600), int(t % 3600 // 60), t % 60
    return f"{h:02}:{m:02}:{s:06.3f}".replace(".", ",")

def write_srt(transcript, start, end, path):
    lines, i = [], 1
    for s in transcript:
        if s["end"] <= start or s["start"] >= end:
            continue
        a, b = max(s["start"], start) - start, min(s["end"], end) - start
        lines += [str(i), f"{_ts(a)} --> {_ts(b)}", s["text"], ""]
        i += 1
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

def make_clip(src, start, end, out, srt):
    if ORIENTATION == "vertical":
        vf = "crop=ih*9/16:ih,scale=1080:1920"
        style = "FontSize=16,Outline=2,Alignment=2,MarginV=120,Bold=1"
    else:
        vf = "scale=1920:1080"
        style = "FontSize=18,Outline=2,Alignment=2,MarginV=40,Bold=1"
    # srt path: Windows backslashes aur colons escape karo taaki ffmpeg filter fail na ho
    clean_srt = srt.replace("\\", "/").replace(":", r"\:")
    vf += f",subtitles='{clean_srt}':force_style='{style}'"
    ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
    try:
        subprocess.run([ffmpeg_bin, "-y", "-ss", str(start), "-to", str(end), "-i", src,
                        "-vf", vf, "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                        "-c:a", "aac", "-movflags", "+faststart", out],
                       check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode(errors="replace") if e.stderr else str(e)
        raise RuntimeError(f"FFmpeg make_clip failed:\n{err}")

