import os, subprocess, shutil
from config import ORIENTATION
from commentary import get_binary
from retry import retry_ffmpeg
from face_tracker import build_dynamic_crop
from subtitle_gen import write_ass

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
    try:
        write_ass(transcript, start, end, path.rsplit(".", 1)[0] + ".ass")
    except Exception as exc:
        print(f"[Subtitles] ASS generation failed ({exc}); using existing SRT captions.")

def make_clip(src, start, end, out, srt):
    dynamic_crop = None
    if ORIENTATION == "vertical":
        dynamic_crop = build_dynamic_crop(src, start, end)
        if dynamic_crop:
            base_vf = f"{dynamic_crop},scale=1080:1920,fps=30,setsar=1"
        else:
            base_vf = "crop=ih*9/16:ih,scale=1080:1920,fps=30,setsar=1"
        style = "FontSize=16,Outline=2,Alignment=2,MarginV=120,Bold=1"
    else:
        base_vf = "scale=1920:1080,fps=30,setsar=1"
        style = "FontSize=18,Outline=2,Alignment=2,MarginV=40,Bold=1"
    # srt path: Windows backslashes aur colons escape karo taaki ffmpeg filter fail na ho
    ass_path = srt.rsplit(".", 1)[0] + ".ass"
    subtitle_path = ass_path if os.path.isfile(ass_path) else srt
    clean_subtitle = subtitle_path.replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
    if subtitle_path.lower().endswith(".ass"):
        subtitle_filter = f",subtitles='{clean_subtitle}'"
    else:
        subtitle_filter = f",subtitles='{clean_subtitle}':force_style='{style}'"
    ffmpeg_bin = get_binary("ffmpeg")

    def _execute_ffmpeg(filter_graph):
        subprocess.run([ffmpeg_bin, "-y", "-ss", str(start), "-to", str(end), "-i", src,
                        "-vf", filter_graph, "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                        "-movflags", "+faststart", out],
                       check=True, capture_output=True)

    try:
        retry_ffmpeg(lambda: _execute_ffmpeg(base_vf + subtitle_filter))
    except Exception:
        if not dynamic_crop:
            raise
        print("[Face Tracker] Dynamic crop render failed; retrying with center crop.")
        center_vf = "crop=ih*9/16:ih,scale=1080:1920,fps=30,setsar=1" + subtitle_filter
        retry_ffmpeg(lambda: _execute_ffmpeg(center_vf))

