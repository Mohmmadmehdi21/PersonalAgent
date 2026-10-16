import json, os
from config import WORK, CLIPS_PER_DAY, DRY_RUN
from download import download
from transcribe import transcribe
from find_clips import find_clips
from clip import make_clip, write_srt
from thumb import make_thumb
from upload import get_service, upload
from source import find_popular_video, is_channel_url

def load_state():
    return json.load(open("state.json")) if os.path.exists("state.json") else {"done": [], "clips": []}

def source_entries():
    for line in open("sources.txt", encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#"):
            yield line

def duration_of(transcript, info_duration=0):
    if info_duration and info_duration > 0:
        return float(info_duration)
    return transcript[-1]["end"] + 1 if transcript else 0

def main():
    import time
    start_total = time.time()
    os.makedirs(WORK, exist_ok=True)
    state = load_state()
    state.setdefault("done", [])
    state.setdefault("clips", [])
    state.setdefault("done_videos", [])

    # Older state files only recorded clip keys, so use those to avoid reprocessing
    # videos that were already handled before channel URLs were supported.
    processed_video_ids = set(state["done_videos"])
    processed_video_ids.update(key.split(":", 1)[0] for key in state["clips"] if ":" in key)

    yt = None
    selected = None
    for source_entry in source_entries():
        if is_channel_url(source_entry):
            if yt is None:
                yt = get_service()
            url = find_popular_video(yt, source_entry, processed_video_ids)
            if url:
                selected = (source_entry, url, True)
                break
            print(f"[INFO] Channel mein abhi unprocessed popular video nahi mila: {source_entry}")
        elif source_entry not in state["done"]:
            selected = (source_entry, source_entry, False)
            break

    if not selected:
        print("[INFO] sources.txt mein koi naya video URL nahi hai. URL add karo.")
        return
    source_entry, url, from_channel = selected

    print("=" * 60)
    print(f"Processing Source Video: {url}")
    print("=" * 60)

    # 1. Download
    t0 = time.time()
    src, info = download(url)
    print(f"[Timing] Download took {time.time() - t0:.1f}s")

    # 2. Transcribe
    t0 = time.time()
    print("\n[Stage] Transcribing audio with Whisper...")
    transcript = transcribe(src)
    print(f"[Timing] Transcription step took {time.time() - t0:.1f}s")



    # 3. Find viral clips with Gemini
    t0 = time.time()
    print("\n[Stage] Finding viral clips with Gemini...")
    total_dur = duration_of(transcript, info.get("duration"))
    clips = find_clips(transcript, total_dur, CLIPS_PER_DAY)
    print(f"{len(clips)} viral clips mile ({time.time() - t0:.1f}s)")

    if DRY_RUN:
        print("\n" + "=" * 60)
        print("[Main] DRY_RUN=True hai. Upload skip ho raha hai.")
        print("=" * 60)
    else:
        print("\n" + "=" * 60)
        print("[Main] YouTube par direct public upload shuru ho raha hai...")
        print("=" * 60)
        if yt is None:
            yt = get_service()

    # 4. Generate clips and thumbnails
    generated_files = []
    for i, c in enumerate(clips):
        key = f"{info['id']}:{int(c['start'])}-{int(c['end'])}"
        if key in state["clips"] and not DRY_RUN:
            print(f"[Skip] Clip already uploaded in previous run: {c['title']}")
            continue

        base = f"{WORK}/{info['id']}_{i}"
        srt, out, th = base + ".srt", base + ".mp4", base + ".jpg"
        print(f"\n--- [Clip {i+1}/{len(clips)}] '{c['title']}' ---")
        print(f"Time range: {c['start']}s -> {c['end']}s (Duration: {c['end'] - c['start']:.1f}s)")

        t_clip = time.time()
        write_srt(transcript, c["start"], c["end"], srt)
        make_clip(src, c["start"], c["end"], out, srt)
        make_thumb(out, c["thumbnail_text"], th)
        generated_files.extend([out, th])
        print(f"Clip + Thumb generated in {time.time() - t_clip:.1f}s")

        credit = f"\n\nSource: {info['title']} by {info['uploader']}\n{info['url']}"
        desc = c["caption"] + "\n\n" + " ".join(f"#{h}" for h in c["hashtags"]) + credit

        if DRY_RUN:
            print(f"[Main] DRY_RUN=True hai. Upload skip ho raha hai.")
            print(f"[DRY-RUN] Saved clip: {out}")
            print(f"[DRY-RUN] Saved thumb: {th}")
            print(f"[DRY-RUN] Caption: {c['caption']}")
            print(f"[DRY-RUN] Thumbnail Text: {c['thumbnail_text']}")
        else:
            print("[Main] YouTube par direct public upload ho raha hai...")
            vid = upload(yt, out, th, c["title"], desc, c["hashtags"])
            state["clips"].append(key)
            print(f"[Uploaded] Video ID: {vid} - {c['title']}")


    if not DRY_RUN:
        if from_channel:
            state["done_videos"].append(info["id"])
        else:
            state["done"].append(source_entry)
        json.dump(state, open("state.json", "w"), indent=2)
        print("\n[SUCCESS] Clip upload process complete; state.json update ho gaya.")
    else:
        print("\n" + "=" * 60)
        print("[SUCCESS] DRY RUN complete! Kripya 'work/' folder check kijiye clips aur thumbnails ke liye.")
        print(f"Total time taken: {time.time() - start_total:.1f}s")
        print(f"Generated clips: {[f for f in generated_files if f.endswith('.mp4')]}")
        print("=" * 60)



if __name__ == "__main__":
    main()
