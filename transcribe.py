import os, json
from faster_whisper import WhisperModel
from config import WHISPER_MODEL

def transcribe(path):
    cache_path = os.path.splitext(path)[0] + ".transcript.json"
    alt_cache = os.path.splitext(path)[0] + "_transcript.json"

    # 1. Check if cached transcript exists
    for c in [cache_path, alt_cache]:
        if os.path.exists(c):
            print(f"[Transcribe] Found cached transcript ({c}). Skipping Whisper.")
            with open(c, "r", encoding="utf-8") as f:
                return json.load(f)

    print(f"[Transcribe] Starting faster-whisper transcription (model={WHISPER_MODEL})...")
    try:
        model = WhisperModel(WHISPER_MODEL, device="cuda", compute_type="float16")
        print("[Transcribe] Running on GPU (CUDA)")
    except Exception as e:
        print(f"[Transcribe] GPU unavailable ({e}). Using CPU (int8).")
        model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")

    segments, info = model.transcribe(path, vad_filter=True)
    total_duration = info.duration if info and info.duration else 0

    results = []
    for s in segments:
        text = s.text.strip()
        if text:
            results.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": text})
        if total_duration > 0:
            pct = min(100.0, (s.end / total_duration) * 100)
            print(f"\r[Transcribe] Progress: {pct:5.1f}% ({int(s.end)}s / {int(total_duration)}s)", end="", flush=True)

    print(f"\n[Transcribe] Completed: {len(results)} segments extracted.")

    # 2. Save transcript to cache file
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"[Transcribe] Saved transcript cache to: {cache_path}")

    return results
