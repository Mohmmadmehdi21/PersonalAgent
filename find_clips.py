import json, os, re, time
from google import genai
from config import GEMINI_MODEL

def local_fallback_clips(transcript, duration, n=4):
    """Rule-based local fallback jab Gemini API down ya quota exhaust ho."""
    print("\n[Warning] Gemini API fail hui, local fallback clips generate kiye gaye hain.")
    clips = []
    if duration <= 30:
        segment_len = max(15.0, duration - 2)
        clips.append({
            "start": 0.0,
            "end": segment_len,
            "title": "Highlight Moment",
            "caption": "Check out this highlight moment!",
            "hashtags": ["shorts", "viral", "fyp", "trending", "youtube"],
            "thumbnail_text": "WATCH NOW"
        })
        return clips

    interval = duration / max(1, n)
    clip_len = min(45.0, max(25.0, interval * 0.75))

    for i in range(n):
        ideal_start = (i * interval) + (interval * 0.15)
        start = round(min(duration - clip_len - 1, max(2.0, ideal_start)), 1)
        end = round(min(duration - 1.0, start + clip_len), 1)

        # Transcript se matching dialogue nikaalo
        matching_texts = [
            s["text"].strip() for s in transcript
            if s.get("start", 0) >= start and s.get("end", 0) <= end and s.get("text")
        ]

        if matching_texts:
            title = matching_texts[0][:50]
            caption = " ".join(matching_texts)[:180]
        else:
            title = f"Best Moment Part {i+1}"
            caption = f"Must-watch viral moment - Part {i+1}!"

        thumb_words = ["WATCH THIS", "MUST SEE", "INSANE MOMENT", "WAIT FOR IT"]
        thumb_text = thumb_words[i % len(thumb_words)]

        clips.append({
            "start": start,
            "end": end,
            "title": title,
            "caption": caption,
            "hashtags": ["shorts", "viral", "trending", "video", "fyp"],
            "thumbnail_text": thumb_text
        })

    return clips

def optimize_transcript(transcript, max_chars=18000):
    """Payload ko slice/sample karta hai taaki Token Limit aur Rate Limit hit na ho."""
    compact = [[s["start"], s["end"], s["text"].strip()] for s in transcript if s.get("text", "").strip()]
    full_json = json.dumps(compact)
    if len(full_json) <= max_chars:
        return full_json

    # Agar transcript bada hai, to uniformly sample karo
    step = max(1, int(len(compact) / (max_chars // 50)))
    sampled = compact[::step]
    sampled_json = json.dumps(sampled)
    if len(sampled_json) > max_chars:
        return sampled_json[:max_chars].rsplit("]", 1)[0] + "]]"
    return sampled_json

def find_clips(transcript, duration, n=4):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("[Warning] GEMINI_API_KEY set nahi hai. Local fallback use kiya ja raha hai.")
        return local_fallback_clips(transcript, duration, n)

    # 1. Valid Model Only (strictly GEMINI_MODEL, default gemini-1.5-flash)
    model_name = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
    print(f"[Gemini] Requesting viral clips using model: '{model_name}'...")

    # 2. Optimized Transcript (Token limit prevention)
    transcript_snippet = optimize_transcript(transcript, max_chars=18000)

    prompt = f"""Neeche video transcript ka snippet hai: [start_sec, end_sec, text].
Video ki total duration {int(duration)} sec hai. Isme se {n} sabse viral-worthy, NON-OVERLAPPING moments chuno.
Rules:
- Har clip 25-60 sec ka ho, strong hook se shuru, complete thought pe khatam.
- Har clip ke liye catchy title (max 60 chars), 1-2 line caption, 5 hashtags (bina #), aur thumbnail_text (max 3 words, ALL CAPS).
- SIRF valid JSON array do bina kisi markdown ya extra text ke:
[{{"start": 12.0, "end": 55.0, "title": "...", "caption": "...", "hashtags": ["shorts", "viral"], "thumbnail_text": "WATCH THIS"}}]

Transcript snippet:
{transcript_snippet}"""

    # 3. Robust Retry Logic with Exponential Backoff (15s, 30s, 45s)
    retries = 3
    backoff = [15, 30, 45]
    r = None

    try:
        client = genai.Client(api_key=api_key)
    except Exception as e:
        print(f"[Gemini] Client initialization error: {e}")
        return local_fallback_clips(transcript, duration, n)

    for attempt in range(retries):
        try:
            r = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config={"response_mime_type": "application/json"}
            )
            if r and r.text:
                break
        except Exception as e:
            err_str = str(e)
            print(f"[Gemini] Attempt {attempt+1}/{retries} failed: {err_str[:120]}")

            # 402 Prepayment / Resource Exhausted pe wait karne ka fayda nahi
            if "402" in err_str or ("RESOURCE_EXHAUSTED" in err_str and "prepayment" in err_str.lower()):
                print("[Gemini] API credits depleted (402). Switching to local fallback...")
                return local_fallback_clips(transcript, duration, n)

            # 404 Not Found pe wait karne ke bajaye fallback karein
            if "404" in err_str:
                print(f"[Gemini] Model '{model_name}' API par nahi mila (404). Switching to local fallback...")
                return local_fallback_clips(transcript, duration, n)

            if attempt < retries - 1:
                wait_sec = backoff[attempt]
                print(f"[Gemini] Waiting {wait_sec}s before retry...")
                time.sleep(wait_sec)


    # 4. Local Rule-Based Fallback
    if not r or not r.text:
        return local_fallback_clips(transcript, duration, n)

    # Parse JSON
    try:
        raw_text = r.text.strip()
        clean_text = re.sub(r"^```(?:json)?|```$", "", raw_text, flags=re.M).strip()
        match = re.search(r"\[\s*\{.*\}\s*\]", clean_text, re.DOTALL)
        if match:
            clean_text = match.group(0)
        clips = json.loads(clean_text)
        good = []
        for c in clips:
            c["start"] = float(c["start"])
            c["end"] = min(float(c["end"]), duration)
            if 15 <= c["end"] - c["start"] <= 90:
                good.append(c)
        if good:
            print(f"[Gemini] {len(good)} suitable clips identify hue.")
            return good[:n]
    except Exception as parse_err:
        print(f"[Gemini] JSON parsing error: {parse_err}")

    return local_fallback_clips(transcript, duration, n)
