import os, json, sys
from config import REVIEW_DIR, WORK
from find_clips import local_fallback_clips

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

QUEUE_FILE = os.path.join(REVIEW_DIR, "queue.json").replace("\\", "/")

def load_queue():
    if not os.path.exists(QUEUE_FILE):
        return []
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[Review] Error reading queue.json: {e}")
        return []

def save_queue(queue):
    from queue_manager import save_queue as save_persistently
    save_persistently(QUEUE_FILE, queue)

def review_cli():
    queue = load_queue()
    if not queue:
        print("\n[Review] Koi clips queue mein nahi hain.")
        print("Pehle 'python main.py prepare' run karein taaki clips draft ban sakein.\n")
        return

    pending_items = [item for item in queue if item.get("status") in ("pending", "needs_review", None)]
    if not pending_items:
        print("\n[Review] Saare clips pehle se reviewed hain!")
        approved_count = sum(1 for item in queue if item.get("status") == "approved")
        print(f"Total Approved: {approved_count} / {len(queue)}")
        if approved_count > 0:
            print("Render start karne ke liye chalayein: python main.py render\n")
        return

    print("=" * 65)
    print("[HUMAN REVIEW STEP] YouTube Clip Agent")
    print("=" * 65)
    print(f"Total Clips in Queue: {len(queue)} | Pending Review: {len(pending_items)}\n")

    for idx, item in enumerate(queue):
        if item.get("status") not in ("pending", "needs_review", None):
            continue

        print("-" * 65)
        print(f"Clip [{idx + 1}/{len(queue)}] - Key: {item.get('key')}")
        print(f"Title:       {item.get('title')}")
        print(f"Hook Score:  {item.get('hook_score', 8)}/10")
        print(f"Time Range:  {item.get('start')}s -> {item.get('end')}s ({item.get('end', 0) - item.get('start', 0):.1f}s)")
        print(f"Template:    {item.get('template')}")
        if item.get("status") == "needs_review":
            print(f"REVIEW REQUIRED: {item.get('review_reason') or item.get('quality_reason') or 'quality/rights check'}")
            print(f"Rights:      {item.get('rights_status', 'unverified')} | License metadata: {item.get('source_license') or 'none'}")
            print(f"Source:      {item.get('source_url', 'unknown')}")

        if item.get("template") in ("INTRO_CARD", "INTRO_PLUS_BREAKDOWN"):
            print(f"Intro Card:  {item.get('intro_text')}")
        if item.get("template") == "PAUSE_COMMENT":
            print(f"Pause At:    {item.get('pause_at')}s")
            print(f"Pause Card:  {item.get('pause_text')}")
        if item.get("template") in ("BREAKDOWN", "INTRO_PLUS_BREAKDOWN"):
            bullets = item.get("takeaway_bullets", [])
            print(f"Takeaways:   {' | '.join(bullets)}")

        print(f"TTS Script:  {item.get('commentary_script')}")
        print(f"Caption:     {item.get('caption')}")
        print(f"Thumbnail:   {item.get('thumbnail_text')}")
        print("-" * 65)

        while True:
            prompt_text = "[Enter]=Approve | [e]=Edit | [r]=Regenerate | [s]=Skip | [q]=Save & Quit: "
            if item.get("status") == "needs_review":
                prompt_text = "[a]=Approve only after checking source rights and quality | [s]=Keep on hold | [q]=Save & Quit: "
            try:
                choice = input(prompt_text).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nExiting review...")
                save_queue(queue)
                return

            if choice == "" or choice == "a":
                if (item.get("status") == "needs_review" or
                        (item.get("quality_status") == "needs_review" and not item.get("quality_reviewed"))):
                    if choice != "a":
                        print("Use [a] only after checking the source license/permission and clip quality.")
                        continue
                    confirm = input("Confirm you checked source rights, platform policy and clip quality? [y/N]: ").strip().lower()
                    if confirm != "y":
                        print("Clip remains in needs_review.")
                        continue
                    item["status"] = "pending"
                    item["quality_reviewed"] = True
                    item["rights_reviewed"] = True
                else:
                    item["status"] = "pending"
                print(f"[APPROVED] Clip {idx + 1} APPROVED.")
                break

            elif choice == "e":
                print("\n--- EDIT COMMENTARY ---")
                new_title = input(f"New Title [{item.get('title')}]: ").strip()
                if new_title:
                    item["title"] = new_title

                new_script = input(f"New Commentary Script [{item.get('commentary_script')}]: ").strip()
                if new_script:
                    item["commentary_script"] = new_script

                if item.get("template") in ("INTRO_CARD", "INTRO_PLUS_BREAKDOWN"):
                    new_intro = input(f"New Intro Text [{item.get('intro_text')}]: ").strip()
                    if new_intro:
                        item["intro_text"] = new_intro

                if item.get("template") == "PAUSE_COMMENT":
                    new_pause = input(f"New Pause Text [{item.get('pause_text')}]: ").strip()
                    if new_pause:
                        item["pause_text"] = new_pause

                new_thumb = input(f"New Thumbnail Text [{item.get('thumbnail_text')}]: ").strip().upper()
                if new_thumb:
                    item["thumbnail_text"] = new_thumb

                if item.get("status") == "needs_review" or (item.get("quality_status") == "needs_review" and not item.get("quality_reviewed")):
                    confirm = input("Confirm you checked source rights, platform policy and clip quality? [y/N]: ").strip().lower()
                    if confirm != "y":
                        print("Clip remains in needs_review.")
                        continue
                    item["quality_reviewed"] = True
                    item["rights_reviewed"] = True
                item["status"] = "pending"
                print(f"[APPROVED] Changes saved & Clip {idx + 1} APPROVED.")
                break

            elif choice == "r":
                print("\n[Review] Re-drafting commentary...")
                # Re-draft commentary with fresh creative twist
                item["commentary_script"] = f"Here is another angle: pay attention to the strategy and timing shown right here."
                item["intro_text"] = f"Key observation: This sequence reveals why execution matters more than planning."
                print(f"New Draft: {item['commentary_script']}")

            elif choice == "s":
                item["status"] = "skipped"
                print(f"[SKIPPED] Clip {idx + 1} SKIPPED.")
                break

            elif choice == "q":
                save_queue(queue)
                print("\n[Review] Progress saved. Exiting.")
                return
            else:
                print("Invalid input. Kripya [Enter], [e], [r], [s], ya [q] chunein.")

        save_queue(queue)

    save_queue(queue)
    approved = sum(1 for item in queue if item.get("status") == "pending" and item.get("quality_reviewed"))
    skipped = sum(1 for item in queue if item.get("status") == "skipped")
    print("\n" + "=" * 65)
    print(f"Review Session Complete! Approved: {approved} | Skipped: {skipped}")
    if approved > 0:
        print("Clips render karne ke liye chalayein: python main.py render")
    print("=" * 65 + "\n")

if __name__ == "__main__":
    review_cli()
