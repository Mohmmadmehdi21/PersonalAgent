import os, sys, json, time, shutil, urllib.request, smtplib, ssl, html

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from env_config import load_project_env

load_project_env()

from logger import log, log_warn, log_error, mask_secrets

def get_disk_free_gb(path="."):
    """Returns free disk space in GB."""
    try:
        total, used, free = shutil.disk_usage(path)
        return free / (1024 ** 3)
    except Exception:
        return 999.0

def get_active_channels():
    ch_raw = os.getenv("NOTIFY_CHANNEL", "none").strip().lower()
    channels = [c.strip() for c in ch_raw.split(",") if c.strip()]
    return channels

def send_telegram(text, parse_mode=None):
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        log_warn("[Notify] Telegram channel selected but TELEGRAM_CHAT_ID is missing.")
        return False
    return send_telegram_to(chat_id, text, parse_mode=parse_mode)

def send_telegram_to(chat_id, text, reply_markup=None, parse_mode=None):
    """Send through the existing Telegram integration to an explicit chat."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or not str(chat_id).strip():
        log_warn("[Notify] Telegram send requires TELEGRAM_BOT_TOKEN and a chat ID.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # Telegram max length is 4096; chunk at 4000
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    success = True

    for idx, chunk in enumerate(chunks):
        clean_chunk = mask_secrets(chunk)
        payload_dict = {"chat_id": str(chat_id), "text": clean_chunk}
        if parse_mode:
            payload_dict["parse_mode"] = parse_mode
        if reply_markup is not None and idx == len(chunks) - 1:
            payload_dict["reply_markup"] = reply_markup
        payload = json.dumps(payload_dict).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status != 200:
                    success = False
        except Exception as e:
            log_warn(f"[Notify] Telegram send failed: {mask_secrets(e)}")
            success = False

    if success:
        log("[Notify] Telegram notification successfully sent.")
    return success

def send_email(subject, body):
    host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
    port = int(os.getenv("SMTP_PORT", "465").strip())
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    to_addr = os.getenv("EMAIL_TO", "").strip()

    if not user or not password or not to_addr:
        log_warn("[Notify] Email channel selected but SMTP_USER, SMTP_PASSWORD, or EMAIL_TO is missing.")
        return False

    clean_body = mask_secrets(body)
    msg = MIMEMultipart()
    msg["From"] = user
    msg["To"] = to_addr
    msg["Subject"] = mask_secrets(subject)
    msg.attach(MIMEText(clean_body, "plain", "utf-8"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=context, timeout=15) as server:
            server.login(user, password)
            server.sendmail(user, [a.strip() for a in to_addr.split(",")], msg.as_string())
        log("[Notify] Email notification successfully sent.")
        return True
    except Exception as e:
        log_warn(f"[Notify] Email send failed: {e}")
        return False

def broadcast_message(subject, body):
    """Sends notification to all configured channels (telegram, email). Non-blocking."""
    channels = get_active_channels()
    if "none" in channels or not channels:
        return

    # Append disk space warning if low (< 5GB)
    free_gb = get_disk_free_gb(".")
    if free_gb < 5.0:
        warning = f"\n\n⚠️ DISK WARNING: Sirf {free_gb:.2f} GB space bacha hai! (5 GB se kam)"
        body += warning

    body = mask_secrets(body)

    for ch in channels:
        try:
            if ch == "telegram":
                _send_formatted_telegram(subject, body)
            elif ch == "email":
                send_email(subject, body)
        except Exception as e:
            log_warn(f"[Notify] Channel '{ch}' dispatch error: {e}")


def _send_formatted_telegram(subject, body):
    """Send notification text as escaped HTML, keeping each message well formed."""
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        log_warn("[Notify] Telegram channel selected but TELEGRAM_CHAT_ID is missing.")
        return False

    title = html.escape(mask_secrets(subject)[:200], quote=True)
    body_budget = max(500, 3800 - len(title))
    safe_body = mask_secrets(body)
    # Split plain text before escaping so markup and entities cannot be cut.
    chunks = []
    current = []
    current_size = 0
    for line in safe_body.splitlines() or [""]:
        fragments = [line[i:i+400] for i in range(0, len(line), 400)] or [""]
        for fragment in fragments:
            escaped = html.escape(fragment, quote=True)
            extra = len(escaped) + (1 if current else 0)
            if current and current_size + extra > body_budget:
                chunks.append("\n".join(current))
                current, current_size = [], 0
                extra = len(escaped)
            current.append(escaped)
            current_size += extra
    if current or not chunks:
        chunks.append("\n".join(current))

    ok = True
    for body_chunk in chunks:
        text = f"📢 <b>{title}</b>\n\n{body_chunk}"
        if not send_telegram_to(chat_id, text, parse_mode="HTML"):
            ok = False
    return ok

# High-level notification helpers
def notify_run_success(clips_info, total_seconds, is_dry_run=False, freed_space_str=None):
    mode_tag = "[DRY RUN - No Upload]" if is_dry_run else "[LIVE UPLOAD]"
    subject = f"YouTube Agent Run Success {mode_tag}"

    lines = [
        "✅ PersonalAgent run processing complete.",
        f"Mode: {'DRY RUN (Sirf local clips bane, upload nahi hua)' if is_dry_run else 'LIVE (YouTube API upload results)'}",
        f"Total Duration: {total_seconds:.1f}s",
        f"Clips Count: {len(clips_info)}\n",
        "Rendered / Uploaded Clips:"
    ]

    for idx, c in enumerate(clips_info):
        title = c.get("title", f"Clip {idx+1}")
        tpl = c.get("template", "INTRO_CARD")
        vid_id = c.get("youtube_video_id")
        visibility = c.get("youtube_privacy_status")
        if is_dry_run:
            result = "(Local Preview Only)"
        elif vid_id and visibility == "public":
            result = f"PUBLIC: https://youtu.be/{vid_id}"
        elif vid_id and visibility == "private":
            result = f"UPLOADED, PRIVATE (not publicly visible): https://youtu.be/{vid_id}"
        elif vid_id:
            result = f"Upload recorded; visibility not verified: https://youtu.be/{vid_id}"
        else:
            result = "No verified YouTube video ID"
        lines.append(f"{idx+1}. {title} [{tpl}] -> {result}")

    if freed_space_str:
        lines.append(f"\n🧹 Disk Safai: {freed_space_str} space khali hua.")

    body = "\n".join(lines)
    broadcast_message(subject, body)


def notify_upload_summary(task_id, clips_info, failures, total_seconds, attempted,
                          pending, needs_review, stopped_reason=None, failed_count=None):
    """Report one upload batch from its persisted queue outcomes."""
    uploaded = len(clips_info)
    failed_count = len(failures) if failed_count is None else int(failed_count)
    unresolved = pending + needs_review
    if stopped_reason:
        outcome = "paused/stopped"
    elif failed_count or unresolved:
        outcome = "completed with unresolved or failed clips"
    else:
        outcome = "completed"
    subject = f"YouTube upload run {task_id}: {outcome}"
    lines = [
        f"Task ID: {task_id}",
        f"Final task outcome: {outcome}",
        f"Upload requests attempted: {attempted}",
        f"Confirmed YouTube uploads: {uploaded}",
        f"Failed or blocked clips: {failed_count}",
        f"Pending clips: {pending}",
        f"Needs review: {needs_review}",
        f"Duration: {total_seconds:.1f}s",
    ]
    if stopped_reason:
        lines.append(f"Batch stop reason: {stopped_reason}")
    if clips_info:
        lines.append("Upload outcomes:")
        for item in clips_info:
            title = item.get("title", item.get("id", "clip"))
            video_id = item.get("youtube_video_id")
            visibility = item.get("youtube_privacy_status")
            if video_id and visibility == "public":
                result = f"PUBLIC: https://youtu.be/{video_id}"
            elif video_id and visibility == "private":
                result = f"uploaded private; not publicly visible: https://youtu.be/{video_id}"
            elif video_id:
                result = f"upload recorded; visibility unverified: https://youtu.be/{video_id}"
            else:
                result = "no verified YouTube video ID"
            lines.append(f"- {title}: {result}")
    if failures:
        lines.append("Failed clip details:")
        for item in failures:
            lines.append(f"- {item.get('title', 'clip')}: {item.get('error', 'failure')} "
                         f"({item.get('error_category', 'upload_failure')})")
    broadcast_message(subject, "\n".join(lines))

def notify_run_error(step_name, error_msg, attempts=1, video_id=None):
    subject = f"⚠️ YouTube Agent Alert: Step '{step_name}' Fail"
    body = (
        f"❌ PersonalAgent Run Mein Error Aaya!\n\n"
        f"Video ID: {video_id or 'N/A'}\n"
        f"Failed Step: {step_name}\n"
        f"Retry Attempts: {attempts}\n"
        f"Error Details: {error_msg}\n\n"
        f"ℹ️ Resume Note: Script dobara chalane par pending/failed step se resume hoga (completed steps skip rahenge)."
    )
    broadcast_message(subject, body)

def notify_quota_or_auth(error_type, details):
    subject = f"🚨 URGENT: YouTube {error_type}"
    body = (
        f"🚨 CRITICAL YOUTUBE ACTION REQUIRED\n\n"
        f"Issue: {error_type}\n"
        f"Details: {details}\n\n"
        f"ℹ️ Resolution:\n"
        f"- Quota Exceeded: YouTube daily API limit exhaust ho gayi hai. Yeh Pacific Midnight (approx 12:30 PM / 1:30 PM IST) par auto-reset hoti hai.\n"
        f"- Token Invalid: 'token.json' expire ya revoke ho gaya hai. 'python test_auth.py' chalakar re-login karein."
    )
    broadcast_message(subject, body)

def notify_queue_empty(summary=None):
    # FIXED-BUG F003: Support accurate exhaustion summary in Telegram notification while preserving backwards compatibility
    subject = "ℹ️ YouTube Agent: Queue Khali Hai"
    detail_line = f"\nChecked summary: {summary}\n" if summary else ""
    body = (
        "ℹ️ Queue Khali Hai\n\n"
        f"sources.txt mein koi naya unprocessed video URL nahi mila.{detail_line}\n"
        "Kripya sources.txt mein naye video URLs ya channel link add karein."
    )
    broadcast_message(subject, body)

def run_test():
    print("=" * 60)
    print("[Notify Test] Testing configured notification channels...")
    print("=" * 60)
    channels = get_active_channels()
    print(f"Configured Channels (NOTIFY_CHANNEL): {channels}")

    if "none" in channels or not channels:
        print("\n[INFO] NOTIFY_CHANNEL 'none' par set hai. Koi notification configure nahi hai.")
        print("Telegram enable karne ke liye .env mein ye set karein:")
        print("  NOTIFY_CHANNEL=telegram")
        print("  TELEGRAM_BOT_TOKEN=<your_bot_token>")
        print("  TELEGRAM_CHAT_ID=<your_chat_id>")
        print("\nEmail enable karne ke liye .env mein ye set karein:")
        print("  NOTIFY_CHANNEL=email")
        print("  SMTP_USER=<your_gmail>")
        print("  SMTP_PASSWORD=<app_password>")
        print("  EMAIL_TO=<recipient_email>\n")
        return

    test_sub = "🔔 PersonalAgent Test Notification"
    test_body = (
        "Yeh ek test notification hai from PersonalAgent!\n"
        "Telegram aur Email integration check safal raha.\n"
        f"Current Time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Free Disk Space: {get_disk_free_gb('.'):.2f} GB"
    )

    broadcast_message(test_sub, test_body)
    print("[Notify Test] Test broadcast completed. Check your configured inbox / Telegram chat.")

if __name__ == "__main__":
    if "--test" in sys.argv:
        run_test()
    else:
        run_test()
