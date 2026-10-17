import os, sys, json, time, shutil, urllib.request, smtplib, ssl

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

load_dotenv()

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

def send_telegram(text):
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        log_warn("[Notify] Telegram channel selected but TELEGRAM_CHAT_ID is missing.")
        return False
    return send_telegram_to(chat_id, text)

def send_telegram_to(chat_id, text):
    """Send through the existing Telegram integration to an explicit chat."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or not str(chat_id).strip():
        log_warn("[Notify] Telegram send requires TELEGRAM_BOT_TOKEN and a chat ID.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # Telegram max length is 4096; chunk at 4000
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    success = True

    for chunk in chunks:
        clean_chunk = mask_secrets(chunk)
        payload = json.dumps({"chat_id": str(chat_id), "text": clean_chunk}).encode("utf-8")
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
                send_telegram(f"📢 <b>{subject}</b>\n\n{body}")
            elif ch == "email":
                send_email(subject, body)
        except Exception as e:
            log_warn(f"[Notify] Channel '{ch}' dispatch error: {e}")

# High-level notification helpers
def notify_run_success(clips_info, total_seconds, is_dry_run=False, freed_space_str=None):
    mode_tag = "[DRY RUN - No Upload]" if is_dry_run else "[LIVE UPLOAD]"
    subject = f"YouTube Agent Run Success {mode_tag}"

    lines = [
        f"✅ <b>PersonalAgent Run uploading are done!</b>",
        f"Mode: {'DRY RUN (Sirf local clips bane, upload nahi hua)' if is_dry_run else 'LIVE (YouTube par public upload complete)'}",
        f"Total Duration: {total_seconds:.1f}s",
        f"Clips Count: {len(clips_info)}\n",
        "<b>Rendered / Uploaded Clips:</b>"
    ]

    for idx, c in enumerate(clips_info):
        title = c.get("title", f"Clip {idx+1}")
        tpl = c.get("template", "INTRO_CARD")
        vid_id = c.get("youtube_video_id")
        link = f"https://youtu.be/{vid_id}" if vid_id else "(Local Preview Only)"
        lines.append(f"{idx+1}. {title} [{tpl}] -> {link}")

    if freed_space_str:
        lines.append(f"\n🧹 Disk Safai: {freed_space_str} space khali hua.")

    body = "\n".join(lines)
    broadcast_message(subject, body)

def notify_run_error(step_name, error_msg, attempts=1, video_id=None):
    subject = f"⚠️ YouTube Agent Alert: Step '{step_name}' Fail"
    body = (
        f"❌ <b>PersonalAgent Run Mein Error Aaya!</b>\n\n"
        f"Video ID: {video_id or 'N/A'}\n"
        f"Failed Step: {step_name}\n"
        f"Retry Attempts: {attempts}\n"
        f"Error Details: {error_msg}\n\n"
        f"ℹ️ <b>Resume Note:</b> Script dobara chalane par pending/failed step se resume hoga (completed steps skip rahenge)."
    )
    broadcast_message(subject, body)

def notify_quota_or_auth(error_type, details):
    subject = f"🚨 URGENT: YouTube {error_type}"
    body = (
        f"🚨 <b>CRITICAL YOUTUBE ACTION REQUIRED</b>\n\n"
        f"Issue: {error_type}\n"
        f"Details: {details}\n\n"
        f"ℹ️ <b>Resolution:</b>\n"
        f"- Quota Exceeded: YouTube daily API limit exhaust ho gayi hai. Yeh Pacific Midnight (approx 12:30 PM / 1:30 PM IST) par auto-reset hoti hai.\n"
        f"- Token Invalid: 'token.json' expire ya revoke ho gaya hai. 'python test_auth.py' chalakar re-login karein."
    )
    broadcast_message(subject, body)

def notify_queue_empty():
    subject = "ℹ️ YouTube Agent: Queue Khali Hai"
    body = (
        "ℹ️ <b>Queue Khali Hai</b>\n\n"
        "sources.txt mein koi naya unprocessed video URL nahi mila.\n"
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
