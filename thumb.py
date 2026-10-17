import base64, json, os, subprocess
from PIL import Image, ImageDraw, ImageFont
from commentary import get_binary
from retry import retry_ffmpeg, retry_groq, create_groq_client
from config import GROQ_MODEL, GROQ_VISION_MODEL

FONTS = ["arialbd.ttf", "Arial Bold.ttf", "C:/Windows/Fonts/arialbd.ttf",
         "C:/Windows/Fonts/arial.ttf", "DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]

def _font(size):
    for f in FONTS:
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default()

def _video_duration(clip):
    ffprobe_bin = get_binary("ffprobe")
    result = subprocess.run(
        [ffprobe_bin, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", clip],
        capture_output=True, text=True, check=True
    )
    return max(0.0, float(result.stdout.strip()))


def _extract_frame(clip, at, frame):
    ffmpeg_bin = get_binary("ffmpeg")
    subprocess.run([ffmpeg_bin, "-y", "-ss", str(max(0.0, at)), "-i", clip,
                    "-frames:v", "1", "-q:v", "3", frame],
                   check=True, capture_output=True)


def _ask_vision(frames):
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return None
    try:
        prompt = (
            "Analyze these 3 ordered frames from a short video. Which frame has the best human facial "
            "expression, emotional reaction, or visually engaging action for a YouTube thumbnail? "
            "Return only JSON: {\"best_index\": 0, \"reason\": \"...\"}. "
            "Use a zero-based index matching the frame order."
        )
        content = [{"type": "text", "text": prompt}]
        for frame in frames:
            with Image.open(frame) as image:
                image = image.convert("RGB")
                image.thumbnail((640, 640))
                import io
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=78, optimize=True)
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}
            })

        model_name = os.getenv("GROQ_VISION_MODEL", os.getenv("GROQ_MODEL", GROQ_VISION_MODEL or GROQ_MODEL))
        client = create_groq_client(api_key)
        response = retry_groq(lambda: client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": content}],
            response_format={"type": "json_object"}
        ))
        payload = json.loads(response.choices[0].message.content or "{}")
        index = int(payload.get("best_index", -1))
        if 0 <= index < len(frames):
            print(f"[Thumbnail] Groq selected frame {index}: {payload.get('reason', '')}")
            return index
    except Exception as exc:
        print(f"[Thumbnail] Groq frame ranking unavailable ({exc}); using 2-second fallback.")
    return None


def _overlay_text(frame, text, out):
    img = Image.open(frame).convert("RGB")
    d = ImageDraw.Draw(img)
    size = max(24, img.width // 8)
    font = _font(size)
    text = text.upper()
    while d.textlength(text, font=font) > img.width - 80 and size > 20:
        size -= 6
        font = _font(size)
    x = (img.width - d.textlength(text, font=font)) / 2
    y = img.height * 0.72
    d.text((x, y), text, font=font, fill="#FFE600",
           stroke_width=max(6, size // 12), stroke_fill="black")
    img.save(out, quality=92)


def make_thumb(clip, text, out, at=2.0):
    candidate_frames = [out + f".candidate{i}.jpg" for i in range(3)]
    fallback_frame = out + ".fallback.jpg"
    selected_frame = None

    try:
        try:
            duration = _video_duration(clip)
            timestamps = [duration * fraction for fraction in (0.15, 0.45, 0.75)]
            for timestamp, frame in zip(timestamps, candidate_frames):
                _extract_frame(clip, timestamp, frame)
            best_index = _ask_vision(candidate_frames)
            if best_index is not None:
                selected_frame = candidate_frames[best_index]
        except Exception as exc:
            print(f"[Thumbnail] Candidate frame extraction failed ({exc}); using 2-second fallback.")

        if selected_frame is None:
            fallback_time = min(float(at), duration) if "duration" in locals() and duration > 0 else float(at)
            def _fallback_extract():
                _extract_frame(clip, fallback_time, fallback_frame)
            retry_ffmpeg(_fallback_extract)
            selected_frame = fallback_frame

        _overlay_text(selected_frame, text, out)
    finally:
        for frame in candidate_frames + [fallback_frame]:
            if os.path.exists(frame):
                try:
                    os.remove(frame)
                except OSError:
                    pass
