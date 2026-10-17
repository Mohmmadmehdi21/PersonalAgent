import os, sys, shutil, subprocess, asyncio, glob
from PIL import Image, ImageDraw, ImageFont
from config import (
    ORIENTATION, COMMENTARY_MODE, TTS_VOICE, WORK, COMMENTARY_AUDIO_DIR
)
from retry import retry_ffmpeg

# Resolution setup
if ORIENTATION == "vertical":
    CANVAS_WIDTH = 1080
    CANVAS_HEIGHT = 1920
else:
    CANVAS_WIDTH = 1920
    CANVAS_HEIGHT = 1080

FONTS = [
    "arialbd.ttf",
    "Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "DejaVuSans-Bold.ttf",
]

def _get_font(size):
    for f in FONTS:
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default()

def get_binary(name):
    b = shutil.which(name)
    if b:
        return b
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

def get_media_duration(file_path):
    ffprobe_bin = get_binary("ffprobe")
    cmd = [
        ffprobe_bin, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(res.stdout.strip())
    except Exception:
        return 0.0

async def _async_generate_tts(text, voice, out_path):
    import edge_tts
    comm = edge_tts.Communicate(text, voice)
    await comm.save(out_path)

def generate_tts(text, voice, out_path):
    """Edge-TTS se audio file generate karta hai aur duration return karta hai."""
    try:
        asyncio.run(_async_generate_tts(text, voice, out_path))
        dur = get_media_duration(out_path)
        return out_path, dur
    except Exception as e:
        print(f"[Commentary TTS] edge-tts error ({e}). Audio skip kiya ja raha hai.")
        return None, 0.0

def resolve_commentary_audio(clip_key, script_text, mode=None, voice=None):
    """
    Audio mode ke hisaab se commentary audio file aur uski duration return karta hai.
    Modes: text_only, text_tts, voice, face (future)
    """
    mode = mode or COMMENTARY_MODE
    voice = voice or TTS_VOICE
    os.makedirs(WORK, exist_ok=True)
    os.makedirs(COMMENTARY_AUDIO_DIR, exist_ok=True)

    # Sanitize clip_key for safe filesystem filenames on Windows
    safe_key = str(clip_key).replace(":", "_").replace("/", "_").replace("\\", "_").replace("?", "_")

    # 1. text_only: No audio
    if mode == "text_only":
        return None, 0.0

    # 2. voice: Check custom user audio file
    if mode == "voice":
        custom_file = os.path.join(COMMENTARY_AUDIO_DIR, f"{safe_key}.mp3")
        if os.path.exists(custom_file):
            dur = get_media_duration(custom_file)
            print(f"[Commentary Voice] Custom audio mila: {custom_file} ({dur:.1f}s)")
            return custom_file, dur
        print(f"[Commentary Voice] Custom audio nahi mila ({custom_file}). text_tts par fallback ho raha hai.")

    # 3. text_tts: Generate free TTS
    if not script_text or not script_text.strip():
        return None, 0.0

    tts_file = os.path.join(WORK, f"{safe_key}_tts.mp3").replace("\\", "/")
    if os.path.exists(tts_file) and os.path.getsize(tts_file) > 1000:
        dur = get_media_duration(tts_file)
        return tts_file, dur

    print(f"[Commentary TTS] Generating voiceover ({voice})...")
    audio_path, dur = generate_tts(script_text, voice, tts_file)
    return audio_path, dur

def _wrap_text(text, font, max_width, draw):
    words = text.split()
    lines = []
    current_line = []
    for w in words:
        test_line = " ".join(current_line + [w])
        if draw.textlength(test_line, font=font) <= max_width:
            current_line.append(w)
        else:
            if current_line:
                lines.append(" ".join(current_line))
            current_line = [w]
    if current_line:
        lines.append(" ".join(current_line))
    return lines

def render_intro_card(intro_text, title="", out_path="intro_card.png"):
    """Creates a high-impact intro card image."""
    img = Image.new("RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), color="#0F141C")
    draw = ImageDraw.Draw(img)

    # Background accent card
    card_margin = 60
    card_rect = [card_margin, 200, CANVAS_WIDTH - card_margin, CANVAS_HEIGHT - 200]
    draw.rounded_rectangle(card_rect, radius=30, fill="#161F2E", outline="#F59E0B", width=6)

    # Badge: WHY THIS MATTERS
    badge_w = 460
    badge_h = 70
    badge_x = (CANVAS_WIDTH - badge_w) // 2
    badge_y = 260
    draw.rounded_rectangle([badge_x, badge_y, badge_x + badge_w, badge_y + badge_h], radius=20, fill="#F59E0B")
    badge_font = _get_font(34)
    draw.text((badge_x + 25, badge_y + 15), "⚡ WHY THIS MATTERS", font=badge_font, fill="#0F141C")

    # Title
    curr_y = badge_y + badge_h + 60
    if title:
        title_font = _get_font(52)
        title_lines = _wrap_text(title, title_font, CANVAS_WIDTH - (card_margin * 2 + 100), draw)[:2]
        for line in title_lines:
            line_w = draw.textlength(line, font=title_font)
            draw.text(((CANVAS_WIDTH - line_w) // 2, curr_y), line, font=title_font, fill="#FFFFFF")
            curr_y += 65
        curr_y += 30

    # Divider
    draw.line([(card_margin + 100, curr_y), (CANVAS_WIDTH - (card_margin + 100), curr_y)], fill="#2E3C51", width=3)
    curr_y += 50

    # Intro Commentary text
    body_font = _get_font(44)
    body_lines = _wrap_text(intro_text, body_font, CANVAS_WIDTH - (card_margin * 2 + 120), draw)[:7]
    for line in body_lines:
        line_w = draw.textlength(line, font=body_font)
        draw.text(((CANVAS_WIDTH - line_w) // 2, curr_y), line, font=body_font, fill="#E2E8F0")
        curr_y += 68

    # Footer note
    footer_font = _get_font(28)
    footer_text = "ORIGINAL BREAKDOWN & CONTEXT"
    fw = draw.textlength(footer_text, font=footer_font)
    draw.text(((CANVAS_WIDTH - fw) // 2, card_rect[3] - 80), footer_text, font=footer_font, fill="#64748B")

    img.save(out_path, quality=95)
    return out_path

def render_pause_card(base_frame_path, pause_text, out_path="pause_card.png"):
    """Takes a frozen video frame and composites a semi-transparent commentary box."""
    if not os.path.exists(base_frame_path):
        img = Image.new("RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), color="#111827")
    else:
        img = Image.open(base_frame_path).convert("RGBA").resize((CANVAS_WIDTH, CANVAS_HEIGHT))

    # Dimming overlay
    dim = Image.new("RGBA", (CANVAS_WIDTH, CANVAS_HEIGHT), (0, 0, 0, 150))
    img = Image.alpha_composite(img, dim)

    # Commentary Card Box
    draw = ImageDraw.Draw(img)
    box_w = CANVAS_WIDTH - 120
    box_h = 580
    box_x = 60
    box_y = (CANVAS_HEIGHT - box_h) // 2

    # Draw card box with gold accent
    draw.rounded_rectangle([box_x, box_y, box_x + box_w, box_y + box_h], radius=25, fill=(15, 23, 42, 235), outline="#38BDF8", width=5)

    # Pause badge
    badge_font = _get_font(32)
    badge_w = 420
    draw.rounded_rectangle([box_x + 40, box_y + 35, box_x + 40 + badge_w, box_y + 95], radius=15, fill="#38BDF8")
    draw.text((box_x + 65, box_y + 48), "⏸️ KEY MOMENT PAUSE", font=badge_font, fill="#0F172A")

    # Pause comment text
    text_font = _get_font(42)
    lines = _wrap_text(pause_text, text_font, box_w - 80, draw)[:5]
    ty = box_y + 130
    for line in lines:
        draw.text((box_x + 45, ty), line, font=text_font, fill="#F8FAFC")
        ty += 62

    res_img = img.convert("RGB")
    res_img.save(out_path, quality=95)
    return out_path

def render_takeaway_card(bullets, title="", out_path="takeaway_card.png"):
    """Creates an educational takeaway breakdown card."""
    img = Image.new("RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), color="#0B131F")
    draw = ImageDraw.Draw(img)

    card_margin = 60
    card_rect = [card_margin, 180, CANVAS_WIDTH - card_margin, CANVAS_HEIGHT - 180]
    draw.rounded_rectangle(card_rect, radius=30, fill="#131D2E", outline="#10B981", width=6)

    # Badge: STRATEGY & LESSONS
    badge_w = 480
    badge_h = 70
    badge_x = (CANVAS_WIDTH - badge_w) // 2
    badge_y = 240
    draw.rounded_rectangle([badge_x, badge_y, badge_x + badge_w, badge_y + badge_h], radius=20, fill="#10B981")
    badge_font = _get_font(34)
    draw.text((badge_x + 35, badge_y + 15), "🎓 LESSON & TAKEAWAYS", font=badge_font, fill="#022C22")

    curr_y = badge_y + badge_h + 60
    if title:
        title_font = _get_font(48)
        title_lines = _wrap_text(title, title_font, CANVAS_WIDTH - (card_margin * 2 + 100), draw)[:2]
        for line in title_lines:
            line_w = draw.textlength(line, font=title_font)
            draw.text(((CANVAS_WIDTH - line_w) // 2, curr_y), line, font=title_font, fill="#FFFFFF")
            curr_y += 60
        curr_y += 20

    draw.line([(card_margin + 80, curr_y), (CANVAS_WIDTH - (card_margin + 80), curr_y)], fill="#1E293B", width=3)
    curr_y += 50

    # Bullets
    bullet_font = _get_font(40)
    for b in bullets[:3]:
        b_clean = b.lstrip("•-* 123456789.")
        b_lines = _wrap_text(f"• {b_clean}", bullet_font, CANVAS_WIDTH - (card_margin * 2 + 120), draw)
        for idx, bl in enumerate(b_lines):
            # First line has bullet, subsequent indented
            fill_color = "#34D399" if idx == 0 else "#E2E8F0"
            draw.text((card_margin + 60, curr_y), bl, font=bullet_font, fill=fill_color)
            curr_y += 62
        curr_y += 25

    # Footer
    footer_font = _get_font(28)
    footer_text = "EDUCATIONAL ANALYSIS BY THIS CHANNEL"
    fw = draw.textlength(footer_text, font=footer_font)
    draw.text(((CANVAS_WIDTH - fw) // 2, card_rect[3] - 70), footer_text, font=footer_font, fill="#64748B")

    img.save(out_path, quality=95)
    return out_path

def build_card_video(image_path, audio_path, duration, out_video_path):
    """
    Renders a normalized video segment from a still image + optional audio.
    Strict normalization: 30 fps, SAR 1, scale to canvas, aac 44100Hz stereo.
    """
    ffmpeg_bin = get_binary("ffmpeg")
    dur_str = f"{duration:.2f}"

    if audio_path and os.path.exists(audio_path) and os.path.getsize(audio_path) > 500:
        cmd = [
            ffmpeg_bin, "-y",
            "-loop", "1", "-i", image_path,
            "-i", audio_path,
            "-t", dur_str,
            "-vf", f"scale={CANVAS_WIDTH}:{CANVAS_HEIGHT},fps=30,setsar=1",
            "-af", "apad",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            out_video_path
        ]
    else:
        # Generate with silent audio track so concat never fails due to missing audio streams
        cmd = [
            ffmpeg_bin, "-y",
            "-loop", "1", "-i", image_path,
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t", dur_str,
            "-vf", f"scale={CANVAS_WIDTH}:{CANVAS_HEIGHT},fps=30,setsar=1",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            out_video_path
        ]

    retry_ffmpeg(lambda: subprocess.run(cmd, check=True, capture_output=True))
    return out_video_path

def extract_frame_at(video_path, at_seconds, out_frame_path):
    """Extracts a single frame from video at at_seconds."""
    ffmpeg_bin = get_binary("ffmpeg")
    cmd = [
        ffmpeg_bin, "-y",
        "-ss", str(max(0.1, at_seconds)),
        "-i", video_path,
        "-vframes", "1",
        out_frame_path
    ]
    retry_ffmpeg(lambda: subprocess.run(cmd, check=True, capture_output=True))
    return out_frame_path

def split_video_segment(video_path, start_sec, end_sec, out_path):
    """Cuts a segment from video_path with normalized 30fps/44100Hz audio."""
    ffmpeg_bin = get_binary("ffmpeg")
    dur = end_sec - start_sec
    cmd = [
        ffmpeg_bin, "-y",
        "-ss", str(start_sec),
        "-to", str(end_sec),
        "-i", video_path,
        "-vf", f"scale={CANVAS_WIDTH}:{CANVAS_HEIGHT},fps=30,setsar=1",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        out_path
    ]
    retry_ffmpeg(lambda: subprocess.run(cmd, check=True, capture_output=True))
    return out_path

def concat_segments(segment_paths, out_final_path):
    """Concatenates multiple normalized video segments into out_final_path."""
    ffmpeg_bin = get_binary("ffmpeg")
    list_file = out_final_path + ".concat.txt"

    with open(list_file, "w", encoding="utf-8") as f:
        for p in segment_paths:
            clean_p = os.path.abspath(p).replace("\\", "/")
            f.write(f"file '{clean_p}'\n")

    cmd = [
        ffmpeg_bin, "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", list_file,
        "-c", "copy",
        out_final_path
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError:
        # If stream copy fails, re-encode concat filter
        filter_complex = "".join([f"[{i}:v][{i}:a]" for i in range(len(segment_paths))])
        filter_complex += f"concat=n={len(segment_paths)}:v=1:a=1[v][a]"
        inputs = []
        for p in segment_paths:
            inputs.extend(["-i", p])
        cmd_reencode = [
            ffmpeg_bin, "-y"
        ] + inputs + [
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
            out_final_path
        ]
        retry_ffmpeg(lambda: subprocess.run(cmd_reencode, check=True, capture_output=True))

    # Cleanup list file
    if os.path.exists(list_file):
        try:
            os.remove(list_file)
        except OSError:
            pass

    return out_final_path

def apply_commentary(base_clip_mp4, clip_info, out_enhanced_mp4):
    """
    Main entry point: combines base clip with original commentary template segments.
    Templates:
      - INTRO_CARD: intro card + base clip
      - PAUSE_COMMENT: base clip part 1 + freeze pause card + base clip part 2
      - BREAKDOWN: base clip + lesson breakdown card
      - INTRO_PLUS_BREAKDOWN: intro + base clip + lesson breakdown
    """
    template = clip_info.get("template", "INTRO_CARD")
    clip_key = str(clip_info.get("id") or clip_info.get("key", "clip")).replace(":", "_").replace("/", "_").replace("\\", "_").replace("?", "_")
    title = clip_info.get("title", "")
    script = clip_info.get("commentary_script", "")

    # Resolve commentary audio (TTS or voice)
    audio_path, audio_dur = resolve_commentary_audio(clip_key, script)

    base_dur = get_media_duration(base_clip_mp4)
    prefix = os.path.join(WORK, f"{clip_key}_comm").replace("\\", "/")

    print(f"[Commentary Engine] Applying template '{template}' on {base_clip_mp4} (Base dur: {base_dur:.1f}s)...")

    # A) INTRO_CARD
    if template == "INTRO_CARD":
        intro_text = clip_info.get("intro_text") or script or "Here is why this moment is crucial to watch."
        card_img = prefix + "_intro.png"
        render_intro_card(intro_text, title=title, out_path=card_img)

        # Card duration: match audio + 0.8s padding, or default 6.0s
        dur = max(5.0, min(10.0, audio_dur + 0.8)) if audio_dur > 0 else 6.0
        intro_seg = prefix + "_intro_seg.mp4"
        build_card_video(card_img, audio_path, dur, intro_seg)

        concat_segments([intro_seg, base_clip_mp4], out_enhanced_mp4)
        print(f"[Commentary Engine] INTRO_CARD applied: {out_enhanced_mp4}")
        return out_enhanced_mp4

    # B) PAUSE_COMMENT
    elif template == "PAUSE_COMMENT":
        pause_at_rel = float(clip_info.get("pause_at", 15.0))
        # Ensure pause_at is within clip
        if pause_at_rel < 3.0 or pause_at_rel > (base_dur - 3.0):
            pause_at_rel = max(3.0, base_dur * 0.45)

        pause_text = clip_info.get("pause_text") or script or "Notice what happened right here."
        raw_frame = prefix + "_frame.png"
        pause_card_img = prefix + "_pause_card.png"

        # 1. Extract frame at pause point
        extract_frame_at(base_clip_mp4, pause_at_rel, raw_frame)
        render_pause_card(raw_frame, pause_text, out_path=pause_card_img)

        # 2. Build pause video segment
        pause_dur = max(3.5, min(6.5, audio_dur + 0.6)) if audio_dur > 0 else 4.0
        pause_seg = prefix + "_pause_seg.mp4"
        build_card_video(pause_card_img, audio_path, pause_dur, pause_seg)

        # 3. Split base video
        part1 = prefix + "_part1.mp4"
        part2 = prefix + "_part2.mp4"
        split_video_segment(base_clip_mp4, 0.0, pause_at_rel, part1)
        split_video_segment(base_clip_mp4, pause_at_rel, base_dur, part2)

        # 4. Concat part1 + pause_seg + part2
        concat_segments([part1, pause_seg, part2], out_enhanced_mp4)
        print(f"[Commentary Engine] PAUSE_COMMENT applied at {pause_at_rel:.1f}s: {out_enhanced_mp4}")
        return out_enhanced_mp4

    # C) BREAKDOWN
    elif template == "BREAKDOWN":
        bullets = clip_info.get("takeaway_bullets") or [
            "Smart preparation beats brute force every time.",
            "Key strategic decision made the difference.",
            "Notice how anticipation creates the biggest payoff."
        ]
        card_img = prefix + "_takeaway.png"
        render_takeaway_card(bullets, title=title, out_path=card_img)

        dur = max(7.0, min(12.0, audio_dur + 1.0)) if audio_dur > 0 else 8.5
        takeaway_seg = prefix + "_takeaway_seg.mp4"
        build_card_video(card_img, audio_path, dur, takeaway_seg)

        concat_segments([base_clip_mp4, takeaway_seg], out_enhanced_mp4)
        print(f"[Commentary Engine] BREAKDOWN applied: {out_enhanced_mp4}")
        return out_enhanced_mp4

    # D) INTRO_PLUS_BREAKDOWN
    elif template == "INTRO_PLUS_BREAKDOWN":
        # Intro
        intro_text = clip_info.get("intro_text") or "Watch closely how this unfolds."
        intro_img = prefix + "_intro.png"
        render_intro_card(intro_text, title=title, out_path=intro_img)
        intro_dur = max(5.0, min(9.0, (audio_dur * 0.5) + 0.8)) if audio_dur > 0 else 5.5
        intro_seg = prefix + "_intro_seg.mp4"
        build_card_video(intro_img, audio_path, intro_dur, intro_seg)

        # Takeaways
        bullets = clip_info.get("takeaway_bullets") or [
            "High stakes demand instant adaptability.",
            "Observation and patience decide the outcome."
        ]
        takeaway_img = prefix + "_takeaway.png"
        render_takeaway_card(bullets, title=title, out_path=takeaway_img)
        takeaway_dur = max(6.0, min(10.0, 7.0))
        takeaway_seg = prefix + "_takeaway_seg.mp4"
        build_card_video(takeaway_img, None, takeaway_dur, takeaway_seg)

        concat_segments([intro_seg, base_clip_mp4, takeaway_seg], out_enhanced_mp4)
        print(f"[Commentary Engine] INTRO_PLUS_BREAKDOWN applied: {out_enhanced_mp4}")
        return out_enhanced_mp4

    else:
        # Fallback: return base clip directly
        return base_clip_mp4
