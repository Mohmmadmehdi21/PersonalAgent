"""Generate a conservative prompt guide from completed seven-day snapshots."""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .tracker import DATA_DIR, read_videos

LEARNINGS_FILE = DATA_DIR / "learnings.txt"
REFRESH_FILE = DATA_DIR / "learnings_refreshed_at.txt"


def prompt_block():
    if os.getenv("LEARNING_ENABLED", "false").strip().lower() not in ("true", "1", "t", "yes"):
        return ""
    path = DATA_DIR / "learnings.txt"
    if not path.is_file():
        return ""
    return "\n\nLEARNINGS (sirf guide, pakka niyam nahi, transcript mein jo asli hai usse bahar mat jao):\n" + path.read_text(encoding="utf-8")


def refresh_learnings(rows=None, generator=None, now=None):
    rows = rows if rows is not None else read_videos()
    now = now or datetime.now(timezone.utc)
    eligible = [row for row in rows if row.get("views_7d", "").isdigit()]
    if len(eligible) < 15:
        print(f"Learning sample too small ({len(eligible)}/15); learnings not refreshed.")
        return False
    if REFRESH_FILE.exists():
        try:
            last = datetime.fromisoformat(REFRESH_FILE.read_text(encoding="utf-8").strip())
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if now - last < timedelta(days=7):
                print("Learnings refreshed less than 7 days ago; skipping.")
                return False
        except (OSError, ValueError):
            pass
    ranked = sorted(eligible, key=lambda row: int(row["views_7d"]), reverse=True)
    top = ranked[:5]
    bottom = ranked[-5:]
    examples = "\n".join(
        f"{label}: title={row.get('title','')!r}, hook_score={row.get('hook_score','')}, "
        f"template={row.get('template','')}, clip_length_seconds={row.get('clip_length_seconds','')}, "
        f"views_7d={row.get('views_7d','')}"
        for label, group in (("TOP", top), ("BOTTOM", bottom)) for row in group
    )
    prompt = ("Summarize recurring content patterns in these top and bottom short-video examples. "
              "Keep it brief, transcript-grounded, non-prescriptive, and avoid claiming causation.\n" + examples)
    if generator is None:
        from google import genai
        from config import GEMINI_MODEL
        import os
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            print("GEMINI_API_KEY missing; learnings not refreshed.")
            return False
        response = genai.Client(api_key=api_key).models.generate_content(model=GEMINI_MODEL, contents=prompt)
        text = response.text or ""
    else:
        text = generator(prompt)
    output = ("Patterns that appeared more often among stronger examples / weaker examples:\n"
              "These observations are hints only; they do not prove causation. A sample of 15–20 videos "
              "is small and may be unreliable. Re-check as more data arrives.\n\n"
              "Examples (TOP 5 and BOTTOM 5; descriptive only):\n" + examples + "\n\n"
              "Gemini summary:\n" + text.strip() + "\n")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LEARNINGS_FILE.write_text(output, encoding="utf-8")
    REFRESH_FILE.write_text(now.astimezone(timezone.utc).isoformat(), encoding="utf-8")
    print(f"Learnings saved to {LEARNINGS_FILE}.")
    return True

