import subprocess, shutil, os
from PIL import Image, ImageDraw, ImageFont

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

def make_thumb(clip, text, out, at=3):
    frame = out + ".frame.jpg"
    ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
    try:
        subprocess.run([ffmpeg_bin, "-y", "-ss", str(at), "-i", clip, "-frames:v", "1", frame],
                       check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode(errors="replace") if e.stderr else str(e)
        raise RuntimeError(f"FFmpeg make_thumb failed:\n{err}")
    img = Image.open(frame).convert("RGB")

    d = ImageDraw.Draw(img)
    size = img.width // 8
    font = _font(size)
    text = text.upper()
    while d.textlength(text, font=font) > img.width - 80 and size > 20:
        size -= 6
        font = _font(size)
    x = (img.width - d.textlength(text, font=font)) / 2
    d.text((x, img.height * 0.72), text, font=font, fill="#FFE600",
           stroke_width=max(6, size // 12), stroke_fill="black")
    img.save(out, quality=92)
    if os.path.exists(frame):
        try:
            os.remove(frame)
        except OSError:
            pass
