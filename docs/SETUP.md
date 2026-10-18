# Secure setup and runtime configuration

## Runtime and dependencies

- Use Python **3.11 or newer**. The scheduled workflow currently selects Python 3.11; Phase 3 checks were run with Python 3.13. End-to-end media/API behavior was not exercised here.
- Install Python packages with `python -m pip install -r requirements.txt`.
- Install FFmpeg and make `ffmpeg` available on `PATH`. On Windows, yt-dlp may also require Deno for some YouTube player challenges; install it and make `deno` available on `PATH` if downloads report that requirement.
- `requirements.txt` currently lists direct dependencies without version pins. This intentionally avoids untested upgrades/downgrades in the existing pipeline. For a deployment, create and review a platform-specific lock file after validating the full media pipeline on that platform.
- Direct dependencies: `yt-dlp`, `brotlicffi` (Brotli support for yt-dlp), `faster-whisper`, `groq`, `google-api-python-client`, `google-auth-oauthlib`, `google-auth-httplib2`, `pillow`, `python-dotenv`, `tzdata`, `edge-tts`, `mediapipe`, and `opencv-python`. CUDA is optional; Whisper falls back to CPU. FFmpeg is an external executable, not a Python package.

## Configuration file

1. Copy `.env.example` to `.env` and fill only the credentials for services you enable. Keep `.env` local; it is ignored by Git.
2. Environment variables supplied by the operating system or deployment secret store take precedence over values in `.env`.
3. Do not paste secrets into source files, issue reports, Telegram messages, screenshots, or logs. Never commit OAuth token/client files or service-account keys.
4. Startup checks report missing variable names and safe next steps; secret values are not included in validation errors.

## Independent identities

These credentials are separate and must not be substituted for one another:

| Service | Credential | Purpose |
| --- | --- | --- |
| Groq | `GROQ_API_KEY` | Current transcript analysis, commentary, thumbnail frame ranking, optional learnings/comments, and Telegram intent classification. Set `GROQ_MODEL` and `GROQ_VISION_MODEL` to models enabled for the account; current default is `qwen/qwen3.8-27b`. `GROQ_MAX_RETRIES` is 3 retries after the first attempt (0–5); `API_TIMEOUT_SECONDS` sets the request timeout. If the key is absent, clip drafting uses its explicit local fallback; optional AI features that need a key report that requirement. |
| YouTube Data API | `client_secret.json` and `token.json` | Existing upload/source-search OAuth. Create an OAuth client in the intended Google Cloud project, enable YouTube Data API v3, configure the consent screen, and authorize the requested upload/read scopes. Keep this token dedicated to the existing upload flow. |
| Growth YouTube/Sheets | `client_secret.json` and `token_growth.json` | Separate OAuth token for optional statistics, comments, Telegram reports, and Sheets. Read-only YouTube reporting uses the existing `youtube.readonly` scope; if it is missing/expired, the growth auth helper opens browser consent. Enable both YouTube Data API v3 and YouTube Analytics API in the same Google Cloud project. It does not replace or expand `token.json`. Sheets requires the spreadsheet scope and `SHEET_ID`. |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_OWNER_IDS` | Existing notifications use `TELEGRAM_CHAT_ID`; optional control bot uses the same bot token and only allows the listed numeric owner user IDs in private chats. Start it with `python main.py telegram-bot`. |
| Email | `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO` | Optional email notifications. Use a provider app password where required, not an account password. |
| Google Agent Platform ADC | `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `VERTEX_AI_MODEL`, and either Application Default Credentials or `GOOGLE_APPLICATION_CREDENTIALS` | Optional separate Google Cloud identity. The current agent uses Groq and does not call Agent Platform. If any Agent Platform setting is supplied, startup requires the project, region, model, and locally discoverable ADC. This does not read `token.json`, `token_growth.json`, or Telegram credentials. |

The project/API and model names can be checked locally for presence and basic file shape. Actual Google Cloud API enablement, IAM permissions, OAuth consent state, and provider-side model availability require provider-side validation and are not tested by offline startup checks. Groq uses its official Python SDK (`groq`) with the Chat Completions API; configured model access is ultimately verified by the first successful request. Groq documents Qwen 3.8 27B as a production model supporting JSON and vision input. See [Groq Python library](https://console.groq.com/docs/libraries), [supported models](https://console.groq.com/docs/models), [JSON modes](https://console.groq.com/docs/structured-outputs), and [vision inputs](https://console.groq.com/docs/vision).

## YouTube OAuth setup

1. In Google Cloud Console, select the project that will own the OAuth client and enable **YouTube Data API v3**.
2. Configure OAuth consent for the account that owns the channel; create a Desktop application OAuth client and save its JSON as `client_secret.json` in the project root.
3. Run `python test_auth.py` or start the requested pipeline command. Browser consent creates/refreshes `token.json`. Do not delete or copy this token into `token_growth.json`.
4. Growth commands prompt separately when the additional read/comment/Sheets scope is needed and store that grant in `token_growth.json`.

Live `run`/`render` validates that an OAuth token or OAuth client file exists when `DRY_RUN=false`. Channel-source discovery may also require YouTube OAuth during preparation, even when rendering is dry-run.

## Notification setup

- BotFather se Telegram bot token lein aur bot ko chat mein start karein; channel enable karne par `TELEGRAM_CHAT_ID` bhi set karein.
- Control commands ke liye `TELEGRAM_OWNER_IDS` mein apna numeric Telegram user ID comma-separated set karein, phir `python main.py telegram-bot` chalayein. Bot sirf private chat mein `/start`, `/help`, `/commands`, `/status`, aur `/health` accept karta hai. `TELEGRAM_OWNER_IDS` khaali ya invalid ho to polling start nahi hoti; unknown commands koi shell/code execute nahi karte. Ek local lock duplicate polling rokti hai; Telegram ka 409 conflict aane par bot band ho jata hai.
- Monitoring ke liye `/status`, `/health`, `/queue`, `/logs`, `/task TASK_ID`, aur `/history` use karein. `/health` credentials/configuration ki maujoodgi aur saved task outcomes dikhata hai; ye provider API ko live probe nahi karta aur heartbeat akela end-to-end success ka proof nahi hai. Major stage/confirmed upload progress messages optional hain: `.env` mein `TELEGRAM_PROGRESS_ENABLED=true` karke enable karein; ye `TELEGRAM_CHAT_ID` par jaate hain.
- Read-only reporting ke liye `/showanalytics 7d`, `/showanalytics 28d`, `/showviews VIDEO_ID`, `/showlikes VIDEO_ID`, `/showcomments VIDEO_ID`, aur `/videos` commands use karein. Analytics windows Pacific dates ke last complete 7/28 din use karte hain; YouTube latest metrics ko delay/omit kar sakta hai. `/videos` uploads playlist ko paginate karke 20 recent items tak list karta hai. Response cache nahi hai; har command live read request karti hai. OAuth grant `token_growth.json` mein rehta hai aur `/videos`/video statistics YouTube Data API v3, channel totals YouTube Analytics API v2 se aate hain.
- Email ke liye SMTP host/port ke saath `SMTP_USER`, `SMTP_PASSWORD`, aur `EMAIL_TO` bharein.
- Agar `NOTIFY_CHANNEL=none` hai to notification credentials ki zaroorat nahi.

## Security checks and tests

- `.gitignore` excludes `.env`, YouTube/growth token JSON, OAuth client JSON, service-account credential JSON, PEM, and key files. Before committing, review `git status` and confirm no credential file is staged.
- Logger redaction masks configured secret values, common API/OAuth formats, bearer headers, credential-file paths, and the current user's home path. Telegram/email payloads pass through the same redactor.
- Run `python -m unittest discover -s tests -q` for offline tests. These tests use fakes and must not perform uploads, comments, OAuth browser login, or provider API writes.
