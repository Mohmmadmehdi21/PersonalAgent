"""Provenance-aware render cache helpers."""
import hashlib
import json
import os
import tempfile


def render_signature(item):
    source = str(item.get("source_video_path", ""))
    try:
        stat = os.stat(source)
        source_fingerprint = [stat.st_size, stat.st_mtime_ns]
    except OSError:
        source_fingerprint = None
    transcript = os.path.splitext(source)[0] + ".transcript.json"
    try:
        stat = os.stat(transcript)
        transcript_fingerprint = [stat.st_size, stat.st_mtime_ns]
    except OSError:
        transcript_fingerprint = None
    payload = {
        "cache_schema": 1,
        "source": source, "source_fingerprint": source_fingerprint,
        "transcript_fingerprint": transcript_fingerprint,
        "start": item.get("start"), "end": item.get("end"),
        "is_own": bool(item.get("is_own")), "title": item.get("title"),
        "template": item.get("template"), "intro_text": item.get("intro_text"),
        "pause_at": item.get("pause_at"), "pause_text": item.get("pause_text"),
        "takeaway_bullets": item.get("takeaway_bullets"),
        "commentary_script": item.get("commentary_script"),
        "thumbnail_text": item.get("thumbnail_text"),
        "orientation": os.getenv("ORIENTATION", "vertical"),
        "commentary_mode": os.getenv("COMMENTARY_MODE", "text_tts"),
        "tts_voice": os.getenv("TTS_VOICE", "en-US-GuyNeural"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def cache_matches(path, item):
    try:
        with open(path, "r", encoding="utf-8") as stream:
            metadata = json.load(stream)
        return metadata.get("signature") == render_signature(item)
    except (OSError, ValueError, AttributeError):
        return False


def write_cache(path, item):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"signature": render_signature(item)}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
