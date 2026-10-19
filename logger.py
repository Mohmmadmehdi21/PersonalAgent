import os, sys, time, glob, re, logging, json
from pathlib import Path
from datetime import datetime

LOGS_DIR = "logs"
os.makedirs(LOGS_DIR, exist_ok=True)

# Patterns for sensitive keys to mask
SENSITIVE_PATTERNS = [
    re.compile(r"(AIzaSy[A-Za-z0-9_-]{33})"),                        # Standard Google API Key
    re.compile(r"(AQ\.[A-Za-z0-9_-]{40,60})"),                      # AI Studio Key variant
    re.compile(r"(\d{8,11}:[A-Za-z0-9_-]{35})"),                   # Telegram Bot Token
    re.compile(r"(ya29\.[A-Za-z0-9_.-]+)"),                         # Google OAuth Token
    re.compile(r"(1//[A-Za-z0-9_-]{20,})"),                         # Google OAuth refresh token
    re.compile(r"\b(eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b"), # JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"(gsk_[A-Za-z0-9_-]{20,})"),                       # Groq API Key
    re.compile(r"(client_secret[=:][\s'\"]*)([A-Za-z0-9_-]{20,})"), # Client secret
    re.compile(r"(password[=:][\s'\"]*)([^\s'\"]{6,})", re.I),     # Passwords
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*"),              # Authorization headers
    re.compile(r"(?i)((?:api[_-]?key|token|access[_-]?token|refresh[_-]?token|bot[_-]?token|client[_-]?secret|password|private[_-]?key|chat[_-]?id)[\"']?\s*[:=]\s*[\"']?)[^\s,;\"']+"),
    re.compile(r"(?i)(https?://api\.telegram\.org/bot)[^/\s]+"),
    re.compile(r"(?i)(?:[A-Z]:[\\/]|/)[^\s\"'<>]*(?:token(?:_growth)?\.json|client_secret\.json|service[-_]account[^\s\"'<>]*\.json|cookies?[^\s\"'<>]*(?:\.txt)?)"),
    re.compile(r"(?i)(?:[A-Z]:[\\/]|\\\\[^\\/\s]+[\\/][^\\/\s]+[\\/])[^\s\"'<>]*"),
    # Require a path separator after the leading slash; this keeps Telegram
    # commands such as /task, /help, and /status intact.
    re.compile(r"(?<![A-Za-z0-9:/])/(?:[^/\s\"'<>]+/)+[^/\s\"'<>]+"),
]

def mask_secrets(text):
    if not isinstance(text, str):
        text = str(text)
    masked = text
    try:
        home = str(Path.home()).rstrip("\\/")
        if home:
            masked = re.sub(re.escape(home).replace("\\\\", r"[\\\\/]"), "<USER_HOME>", masked, flags=re.I)
    except (OSError, RuntimeError):
        pass
    for p in SENSITIVE_PATTERNS:
        if p.groups > 1:
            masked = p.sub(r"\1********", masked)
        else:
            masked = p.sub("********", masked)
    # Redact actual configured values, including nonstandard provider token formats.
    for name, value in os.environ.items():
        if re.search(r"(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PRIVATE[_-]?KEY|CREDENTIALS|CHAT[_-]?ID|COOKIE)", name, re.I):
            value = value.strip()
            if len(value) >= 3 and not value.lower().startswith(("your_", "replace_", "<")):
                masked = re.sub(re.escape(value), "********", masked, flags=re.I)
    return masked

class MaskingFormatter(logging.Formatter):
    def format(self, record):
        orig_msg = super().format(record)
        return mask_secrets(orig_msg)

def clean_old_logs(days=30):
    """Deletes log files older than `days` days."""
    try:
        now = time.time()
        cutoff = now - (days * 86400)
        for log_file in glob.glob(os.path.join(LOGS_DIR, "run_*.log")):
            if os.path.isfile(log_file):
                if os.path.getmtime(log_file) < cutoff:
                    try:
                        os.remove(log_file)
                    except OSError:
                        pass
    except Exception:
        pass

_current_log_file = None

def setup_logger():
    global _current_log_file
    logger = logging.getLogger("PersonalAgent")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    _current_log_file = os.path.join(LOGS_DIR, f"run_{timestamp}.log")

    formatter = MaskingFormatter(
        fmt="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # File handler
    fh = logging.FileHandler(_current_log_file, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    # Clean old logs on startup
    clean_old_logs(30)
    return logger

def get_current_log_file():
    return _current_log_file

app_logger = setup_logger()

def log(msg):
    app_logger.info(msg)

def log_warn(msg):
    app_logger.warning(msg)

def log_error(msg):
    app_logger.error(msg)

def log_event(event, **fields):
    """Write machine-readable task/retry events through the secret-masking formatter."""
    payload = {"event": str(event), **fields}
    app_logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
    try:
        from telegram_bot.monitoring import record_event
        record_event(event, fields)
    except Exception:
        # Monitoring storage is optional and must never interrupt pipeline work.
        pass
