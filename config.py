import os, glob, json
import re
from pathlib import Path
try:
    from dotenv import load_dotenv
except ImportError as exc:
    raise RuntimeError("python-dotenv is required; install project dependencies with 'python -m pip install -r requirements.txt'.") from exc
load_dotenv()


class ConfigurationError(ValueError):
    """A clear, value-safe startup configuration error."""


def _int_setting(name, default, minimum=None, maximum=None):
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"Configuration error: {name} must be an integer.") from exc
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        bounds = f"{minimum if minimum is not None else 'any'}..{maximum if maximum is not None else 'any'}"
        raise ConfigurationError(f"Configuration error: {name} must be in range {bounds}.")
    return value

# Windows: Ensure ffmpeg and deno directories are in os.environ["PATH"]
if os.name == "nt":
    local_app = os.environ.get("LOCALAPPDATA", "")
    if local_app:
        search_dirs = [
            os.path.join(local_app, "Microsoft", "WinGet", "Links"),
        ]
        for pkg in glob.glob(os.path.join(local_app, "Microsoft", "WinGet", "Packages", "*")):
            if "FFmpeg" in pkg:
                bin_sub = os.path.join(pkg, "ffmpeg-9.0.2-full_build", "bin")
                if os.path.isdir(bin_sub):
                    search_dirs.append(bin_sub)
                search_dirs.append(pkg)
            elif "Deno" in pkg:
                search_dirs.append(pkg)

        current_path = os.environ.get("PATH", "")
        for d in search_dirs:
            if os.path.isdir(d) and d not in current_path:
                os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]

# Pipeline & Quality settings
CLIPS_PER_DAY = min(4, max(1, _int_setting("CLIPS_PER_DAY", 2)))
HOOK_MIN_SCORE = _int_setting("HOOK_MIN_SCORE", 7, 1, 10)
ORIENTATION = os.getenv("ORIENTATION", "vertical")
try:
    SLOT_HOURS_IST = [int(h.strip()) for h in os.getenv("SLOT_HOURS_IST", "9,13,17,21").split(",")]
except ValueError as exc:
    raise ConfigurationError("Configuration error: SLOT_HOURS_IST must be comma-separated hours from 0 to 23.") from exc
if not SLOT_HOURS_IST or any(hour < 0 or hour > 23 for hour in SLOT_HOURS_IST):
    raise ConfigurationError("Configuration error: SLOT_HOURS_IST must contain hours from 0 to 23.")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
GROQ_MODEL = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", GROQ_MODEL)
GROQ_MAX_RETRIES = _int_setting("GROQ_MAX_RETRIES", 3, 0, 5)
DRY_RUN = os.getenv("DRY_RUN", "false").strip().lower() in ("true", "1", "t", "yes")
# Legacy compatibility setting; retry.py now always stops on account upload limits.
UPLOAD_LIMIT_GUARD_ENABLED = os.getenv("UPLOAD_LIMIT_GUARD_ENABLED", "false").strip().lower() in ("true", "1", "t", "yes")

# yt-dlp & YouTube Download Settings
YTDLP_MAX_RETRIES = _int_setting("YTDLP_MAX_RETRIES", 2, 0, 5)
YTDLP_RATE_LIMIT_COOLDOWN_SECONDS = _int_setting("YTDLP_RATE_LIMIT_COOLDOWN_SECONDS", 900, 10, 86400)
YTDLP_MAX_SOURCE_CANDIDATES = _int_setting("YTDLP_MAX_SOURCE_CANDIDATES", 5, 1, 20)
YTDLP_DOWNLOAD_TIMEOUT_SECONDS = _int_setting("YTDLP_DOWNLOAD_TIMEOUT_SECONDS", 120, 15, 600)
YTDLP_COOKIES_FILE = os.getenv("YTDLP_COOKIES_FILE", "").strip()
YTDLP_COOKIES_FROM_BROWSER = os.getenv("YTDLP_COOKIES_FROM_BROWSER", "").strip()

# Commentary / Value-Add settings
# Modes: text_only, text_tts, voice, face (future TODO)
COMMENTARY_MODE = os.getenv("COMMENTARY_MODE", "text_tts").strip().lower()
TTS_VOICE = os.getenv("TTS_VOICE", "en-US-GuyNeural").strip()
OWN_CONTENT_ENABLED = os.getenv("OWN_CONTENT_ENABLED", "false").strip().lower() in ("true", "1", "t", "yes")
OWN_RATIO = _int_setting("OWN_RATIO", 3, 1)

# Notification & Reliability settings
NOTIFY_CHANNEL = os.getenv("NOTIFY_CHANNEL", "none").strip().lower()

# Disk Safai / Cleanup settings
CLEANUP = os.getenv("CLEANUP", "true").strip().lower() in ("true", "1", "t", "yes")
KEEP_TRANSCRIPT = os.getenv("KEEP_TRANSCRIPT", "true").strip().lower() in ("true", "1", "t", "yes")
KEEP_FINAL_CLIPS_DAYS = _int_setting("KEEP_FINAL_CLIPS_DAYS", 2, 0)
DELETE_SOURCE = os.getenv("DELETE_SOURCE", "true").strip().lower() in ("true", "1", "t", "yes")

# Directory paths
WORK = "work"
REVIEW_DIR = "review"
OWN_DIR = "own_videos"
COMMENTARY_AUDIO_DIR = "commentary_audio"

# Optional growth features (disabled by default).
TRACKING_ENABLED = os.getenv("TRACKING_ENABLED", "false").strip().lower() in ("true", "1", "t", "yes")
_min_samples_value = os.getenv("MIN_SAMPLES_PER_SLOT", "8")
MIN_SAMPLES_PER_SLOT = max(1, int(_min_samples_value)) if _min_samples_value.isdigit() else 8
_candidate_hours = [int(h.strip()) for h in os.getenv("CANDIDATE_HOURS_IST", "7,8,11,12,15,16,18,19,20,22").split(",") if h.strip().isdigit() and 0 <= int(h.strip()) <= 23]
CANDIDATE_HOURS_IST = _candidate_hours or [7, 8, 11, 12, 15, 16, 18, 19, 20, 22]
AUTO_SLOT_UPDATE = os.getenv("AUTO_SLOT_UPDATE", "false").strip().lower() in ("true", "1", "t", "yes")
LEARNING_ENABLED = os.getenv("LEARNING_ENABLED", "false").strip().lower() in ("true", "1", "t", "yes")
COMMENT_ENABLED = os.getenv("COMMENT_ENABLED", "false").strip().lower() in ("true", "1", "t", "yes")
_health_hours_value = os.getenv("HEALTH_MAX_HOURS", "26")
HEALTH_MAX_HOURS = max(1, int(_health_hours_value)) if _health_hours_value.isdigit() else 26
GROWTH_HEALTH_ENABLED = os.getenv("GROWTH_HEALTH_ENABLED", "false").strip().lower() in ("true", "1", "t", "yes")
SHEET_ID = os.getenv("SHEET_ID", "").strip()
if AUTO_SLOT_UPDATE:
    try:
        _growth_hours = __import__("json").loads(__import__("pathlib").Path("data/slots.json").read_text(encoding="utf-8")).get("hours")
        if isinstance(_growth_hours, list) and _growth_hours and len(set(_growth_hours)) == len(_growth_hours) and all(type(hour) is int and 0 <= hour <= 23 for hour in _growth_hours): SLOT_HOURS_IST = _growth_hours
    except Exception:
        pass


def validate_startup_config(command="run"):
    """Validate only credentials needed by the selected command; never print secret values."""
    command = (command or "run").strip().lower()
    if command == "cleanup":
        return []

    errors = []
    warnings = []
    channels = [part.strip().lower() for part in NOTIFY_CHANNEL.split(",") if part.strip()]
    unknown_channels = sorted(set(channels) - {"none", "telegram", "email"})
    if unknown_channels:
        errors.append("NOTIFY_CHANNEL supports only none, telegram, and email.")
    if "none" in channels and len(channels) > 1:
        errors.append("NOTIFY_CHANNEL=none cannot be combined with another notification channel.")

    def configured(name):
        value = os.getenv(name, "").strip()
        return bool(value) and not value.lower().startswith(("your_", "replace_", "<"))

    if "telegram" in channels:
        if not configured("TELEGRAM_BOT_TOKEN"):
            errors.append("NOTIFY_CHANNEL includes telegram but TELEGRAM_BOT_TOKEN is missing.")
        if not configured("TELEGRAM_CHAT_ID"):
            errors.append("NOTIFY_CHANNEL includes telegram but TELEGRAM_CHAT_ID is missing.")
    if "email" in channels:
        missing = [name for name in ("SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO") if not configured(name)]
        if missing:
            errors.append("NOTIFY_CHANNEL includes email but required settings are missing: " + ", ".join(missing))
        try:
            smtp_port = int(os.getenv("SMTP_PORT", "465").strip())
            if not 1 <= smtp_port <= 65535:
                raise ValueError
        except ValueError:
            errors.append("SMTP_PORT must be an integer from 1 to 65535 when email notifications are enabled.")

    placeholder_key = not configured("GROQ_API_KEY")
    if placeholder_key and (LEARNING_ENABLED or COMMENT_ENABLED):
        errors.append("GROQ_API_KEY is required when LEARNING_ENABLED or COMMENT_ENABLED is true.")
    if not GROQ_MODEL.strip():
        errors.append("GROQ_MODEL must not be empty.")
    if not GROQ_VISION_MODEL.strip():
        errors.append("GROQ_VISION_MODEL must not be empty.")
    if placeholder_key and command in ("run", "prepare"):
        warnings.append("GROQ_API_KEY is missing or still a placeholder; clip selection will use its local fallback.")

    cookies_file = os.getenv("YTDLP_COOKIES_FILE", "").strip()
    if cookies_file and not Path(cookies_file).expanduser().is_file():
        warnings.append(f"YTDLP_COOKIES_FILE is set but file was not found: {cookies_file}")

    # Channel discovery needs YouTube OAuth even during DRY_RUN; live render always does.
    source_text = ""
    try:
        source_text = Path("sources.txt").read_text(encoding="utf-8").lower()
    except OSError:
        pass
    channel_source = any(marker in source_text for marker in ("/@", "/channel/", "/c/", "/user/"))
    youtube_needed = (command == "render" and not DRY_RUN) or (command in ("run", "prepare") and channel_source)
    if command in ("run", "render") and not DRY_RUN:
        youtube_needed = True
    if youtube_needed:
        token_file = Path("token.json")
        client_file = Path("client_secret.json")
        if not token_file.is_file() and not client_file.is_file():
            errors.append("YouTube OAuth is required, but neither token.json nor client_secret.json is present. Add the OAuth client file or authenticate first.")
        if client_file.is_file():
            try:
                client_data = json.loads(client_file.read_text(encoding="utf-8"))
                client = client_data.get("installed") or client_data.get("web")
                if not isinstance(client, dict) or not client.get("client_id") or not client.get("client_secret"):
                    errors.append("client_secret.json is not a valid Google OAuth client file (expected installed/web client credentials).")
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                errors.append("client_secret.json cannot be read as valid JSON; replace it with a fresh OAuth client download.")

    # Agent Platform ADC is a separate optional Google Cloud identity. The current
    # clip pipeline uses Groq; these checks apply only when its settings are supplied.
    cloud_project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    cloud_region = os.getenv("GOOGLE_CLOUD_LOCATION", "").strip()
    vertex_model = os.getenv("VERTEX_AI_MODEL", "").strip()
    adc_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if any((cloud_project, cloud_region, vertex_model, adc_path)):
        if not cloud_project:
            errors.append("GOOGLE_CLOUD_PROJECT is required when configuring Agent Platform ADC.")
        elif not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", cloud_project):
            errors.append("GOOGLE_CLOUD_PROJECT must be a valid Google Cloud project ID.")
        if not cloud_region:
            errors.append("GOOGLE_CLOUD_LOCATION is required when configuring Agent Platform ADC.")
        elif cloud_region != "global" and not re.fullmatch(r"[a-z]+-[a-z0-9-]+[0-9]", cloud_region):
            errors.append("GOOGLE_CLOUD_LOCATION must be a Google Cloud region name (or global).")
        if not vertex_model:
            errors.append("VERTEX_AI_MODEL is required when configuring Agent Platform ADC.")
        if adc_path and not Path(adc_path).expanduser().is_file():
            errors.append("GOOGLE_APPLICATION_CREDENTIALS points to a file that does not exist.")
        try:
            default_adc_file = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
        except (OSError, RuntimeError):
            default_adc_file = Path("__missing_home__") / "application_default_credentials.json"
        if not adc_path and not default_adc_file.is_file():
            errors.append("Agent Platform ADC credentials were not found. Run 'gcloud auth application-default login' or set GOOGLE_APPLICATION_CREDENTIALS.")

    if errors:
        raise ConfigurationError("Startup configuration is incomplete:\n- " + "\n- ".join(errors))
    for warning in warnings:
        print(f"[Config] Warning: {warning}")
    return warnings
