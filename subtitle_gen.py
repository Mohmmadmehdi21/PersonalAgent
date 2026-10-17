"""Generate word-highlighted ASS captions from Whisper transcripts."""
import re

from config import ORIENTATION


def _ass_time(seconds):
    total_cs = max(0, int(round(float(seconds) * 100)))
    hours, remainder = divmod(total_cs, 360000)
    minutes, remainder = divmod(remainder, 6000)
    seconds_cs, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02}:{seconds_cs:02}.{centiseconds:02}"


def _safe_word(text):
    text = str(text).replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    return re.sub(r"[\r\n]+", " ", text).strip()


def _segment_words(segment):
    words = segment.get("words") or []
    output = []
    for word in words:
        if not isinstance(word, dict):
            continue
        text = _safe_word(word.get("word", word.get("text", "")))
        try:
            start, end = float(word["start"]), float(word["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if text and end > start:
            output.append((start, end, text))
    if output:
        return output

    # Compatibility path for older cached transcripts without word timestamps.
    text_words = str(segment.get("text", "")).split()
    try:
        seg_start, seg_end = float(segment["start"]), float(segment["end"])
    except (KeyError, TypeError, ValueError):
        return []
    if not text_words or seg_end <= seg_start:
        return []
    duration_per_word = (seg_end - seg_start) / len(text_words)
    return [
        (seg_start + i * duration_per_word,
         seg_start + (i + 1) * duration_per_word,
         _safe_word(word))
        for i, word in enumerate(text_words)
    ]


def write_ass(transcript, clip_start, clip_end, path):
    """Write bottom-center ASS captions with 3-4 words and karaoke highlighting."""
    if ORIENTATION == "vertical":
        width, height, font_size, margin_v = 1080, 1920, 64, 180
    else:
        width, height, font_size, margin_v = 1920, 1080, 54, 65

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,Arial Black,{font_size},&H0000FFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,3,2,40,40,{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    words = []
    for segment in transcript:
        if not isinstance(segment, dict):
            continue
        for word_start, word_end, word_text in _segment_words(segment):
            if word_end <= clip_start or word_start >= clip_end:
                continue
            start = max(word_start, clip_start) - clip_start
            end = min(word_end, clip_end) - clip_start
            if end > start:
                words.append((start, end, word_text))

    for offset in range(0, len(words), 4):
        group = words[offset:offset + 4]
        if not group:
            continue
        event_start = group[0][0]
        event_end = max(group[-1][1], event_start + 0.12)
        karaoke = []
        for start, end, text in group:
            duration_cs = max(1, int(round((end - start) * 100)))
            karaoke.append(f"{{\\kf{duration_cs}}}{text}")
        lines.append(
            f"Dialogue: 0,{_ass_time(event_start)},{_ass_time(event_end)},Default,,0,0,0,,"
            + " ".join(karaoke)
        )

    with open(path, "w", encoding="utf-8-sig") as ass_file:
        ass_file.write("\n".join(lines) + "\n")
    return path
