import os, json, hashlib, math, tempfile, time
from contextlib import contextmanager
from faster_whisper import WhisperModel
from config import WHISPER_MODEL
from state_manager import is_valid_json_file


@contextmanager
def _pyav_open_compat(av_module=None):
    """Bridge faster-whisper 1.2.1's removed PyAV 19 keyword at this call boundary.

    PyAV 19 removed metadata_errors; metadata is now decoded with UTF-8/surrogateescape.
    Older PyAV versions retain the explicit ignore behavior expected by faster-whisper.
    """
    if av_module is None:
        import av as av_module
    major = int(str(getattr(av_module, "__version__", "0")).split(".", 1)[0])
    if major < 19:
        yield
        return
    original_open = av_module.open

    def compatible_open(*args, **kwargs):
        kwargs.pop("metadata_errors", None)
        return original_open(*args, **kwargs)

    av_module.open = compatible_open
    try:
        yield
    finally:
        av_module.open = original_open


def _atomic_json(path, payload):
    fd, temp_path = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def _source_fingerprint(path):
    stat = os.stat(path)
    payload = f"{os.path.abspath(path)}|{stat.st_size}|{stat.st_mtime_ns}|{WHISPER_MODEL}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _valid_word_timed_cache(cached):
    if not isinstance(cached, list) or not cached:
        return False
    try:
        return all(
            isinstance(segment, dict) and isinstance(segment.get("words"), list)
            and math.isfinite(float(segment.get("start", -1)))
            and math.isfinite(float(segment.get("end", -1)))
            and float(segment.get("end", -1)) > float(segment.get("start", -1))
            for segment in cached
        )
    except (TypeError, ValueError, OverflowError):
        return False

def transcribe(path):
    cache_path = os.path.splitext(path)[0] + ".transcript.json"
    alt_cache = os.path.splitext(path)[0] + "_transcript.json"
    metadata_path = cache_path + ".meta.json"
    started = time.monotonic()
    signature = _source_fingerprint(path)

    # 1. Check if valid cached transcript exists
    for c in [cache_path, alt_cache]:
        if is_valid_json_file(c):
            with open(c, "r", encoding="utf-8") as f:
                cached = json.load(f)
            cache_meta_path = c + ".meta.json"
            try:
                with open(cache_meta_path, "r", encoding="utf-8") as meta_stream:
                    cache_meta = json.load(meta_stream)
            except (OSError, ValueError):
                cache_meta = {}
            valid_timing = _valid_word_timed_cache(cached)
            if valid_timing and cache_meta.get("signature") == signature and cache_meta.get("model") == WHISPER_MODEL:
                print(f"[Transcribe] Reusing source/model-verified word-timed cache ({c}).")
                from logger import log_event
                log_event("transcription", status="cache_reused", model=WHISPER_MODEL,
                          source_fingerprint=signature[:12], segment_count=len(cached), processing_seconds=0)
                return cached
            print(f"[Transcribe] Cache is legacy, stale, or incompatible ({c}); regenerating safely.")

    print(f"[Transcribe] Starting faster-whisper transcription (model={WHISPER_MODEL})...")
    try:
        model = WhisperModel(WHISPER_MODEL, device="cuda", compute_type="float16")
        print("[Transcribe] Running on GPU (CUDA)")
    except Exception as e:
        print(f"[Transcribe] GPU unavailable ({type(e).__name__}). Using CPU (int8).")
        model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")

    # faster-whisper 1.2.1 still passes metadata_errors to av.open. Remove that
    # obsolete kwarg only on PyAV 19+, where it was removed and is no longer needed.
    import av
    with _pyav_open_compat(av):
        segments, info = model.transcribe(path, vad_filter=True, word_timestamps=True)
    total_duration = info.duration if info and info.duration else 0

    results = []
    for s in segments:
        text = s.text.strip()
        if text and math.isfinite(float(s.start)) and math.isfinite(float(s.end)) and s.end > s.start:
            word_data = []
            for word in (getattr(s, "words", None) or []):
                word_text = str(getattr(word, "word", "")).strip()
                word_start = getattr(word, "start", None)
                word_end = getattr(word, "end", None)
                if word_text and word_start is not None and word_end is not None and word_end > word_start:
                    word_data.append({
                        "start": round(float(word_start), 3),
                        "end": round(float(word_end), 3),
                        "word": word_text
                    })
            results.append({
                "start": round(s.start, 2),
                "end": round(s.end, 2),
                "text": text,
                "words": word_data
            })
        if total_duration > 0:
            pct = min(100.0, (s.end / total_duration) * 100)
            print(f"\r[Transcribe] Progress: {pct:5.1f}% ({int(s.end)}s / {int(total_duration)}s)", end="", flush=True)

    elapsed = round(time.monotonic() - started, 3)
    print(f"\n[Transcribe] Completed: {len(results)} segments extracted in {elapsed:.1f}s.")

    # 2. Save transcript to cache file
    _atomic_json(cache_path, results)
    _atomic_json(metadata_path, {"schema": 1, "signature": signature, "model": WHISPER_MODEL,
                                 "segments": len(results), "processing_seconds": elapsed,
                                 "word_timestamps": True})
    print(f"[Transcribe] Saved transcript cache to: {cache_path}")
    from logger import log_event
    log_event("transcription", status="success", model=WHISPER_MODEL,
              source_fingerprint=signature[:12], segment_count=len(results),
              processing_seconds=elapsed, word_timestamps=True)

    return results
