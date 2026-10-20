# Secure setup and runtime configuration

## Runtime and dependencies

- Use Python **3.13.7** for the local Windows environment (`.python-version`), GitHub Actions workflow, and Docker image. The runtime now installs dependencies into an isolated environment; do not add another environment's `site-packages` to `PYTHONPATH`.
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

Live `run`/`render` validates that an OAuth token or OAuth client file exists when `DRY_RUN=false`. Channel URLs in `sources.txt` also need OAuth for YouTube Data API discovery; direct video URLs do not.

## Notification setup
 
 - BotFather se Telegram bot token lein aur bot ko chat mein start karein; channel enable karne par `TELEGRAM_CHAT_ID` bhi set karein.
 - Control commands ke liye `TELEGRAM_OWNER_IDS` mein apna numeric Telegram user ID comma-separated set karein, phir `python main.py telegram-bot` chalayein. Bot sirf private chat mein `/start`, `/help`, `/commands`, `/status`, `/health`, aur `/sources` accept karta hai. `TELEGRAM_OWNER_IDS` khaali ya invalid ho to polling start nahi hoti; unknown commands koi shell/code execute nahi karte. Ek local lock duplicate polling rokti hai; Telegram ka 409 conflict aane par bot band ho jata hai.
 - Monitoring ke liye `/status`, `/health`, `/queue`, `/logs`, `/task TASK_ID`, aur `/history` use karein. `/health` credentials/configuration ki maujoodgi aur saved task outcomes dikhata hai; ye provider API ko live probe nahi karta aur heartbeat akela end-to-end success ka proof nahi hai. Major stage/confirmed upload progress messages optional hain: `.env` mein `TELEGRAM_PROGRESS_ENABLED=true` karke enable karein; ye `TELEGRAM_CHAT_ID` par jaate hain.
 - Read-only reporting ke liye `/showanalytics 7d`, `/showanalytics 28d`, `/showviews VIDEO_ID`, `/showlikes VIDEO_ID`, `/showcomments VIDEO_ID`, aur `/videos` commands use karein. Analytics windows Pacific dates ke last complete 7/28 din use karte hain; YouTube latest metrics ko delay/omit kar sakta hai. `/videos` uploads playlist ko paginate karke 20 recent items tak list karta hai. Response cache nahi hai; har command live read request karti hai. OAuth grant `token_growth.json` mein rehta hai aur `/videos`/video statistics YouTube Data API v3, channel totals YouTube Analytics API v2 se aate hain.
 - Email ke liye SMTP host/port ke saath `SMTP_USER`, `SMTP_PASSWORD`, aur `EMAIL_TO` bharein.
 - Agar `NOTIFY_CHANNEL=none` hai to notification credentials ki zaroorat nahi.
 
+## Daily YouTube Source Refresh and Telegram Source Manager
+
+### Enabling Daily Source Refresh
+
+The in-process scheduler is integrated into the Telegram bot daemon (`python main.py telegram-bot`). To enable daily YouTube source refresh:
+
+1. In `.env`:
+   ```env
+   SCHEDULER_ENABLED=true
+   SCHEDULER_SOURCE_REFRESH_ENABLED=true
+   SCHEDULER_SOURCE_REFRESH_TIME=08:00
+   SCHEDULER_PREPARE_TIME=09:00
+   SCHEDULER_TIMEZONE=Asia/Kolkata
+   ```
+2. Alternatively, open Telegram bot `/start` -> `📚 Source Manager` -> `🕒 Daily Schedule` and click `🟢 Enable Daily Refresh`. This safely persists the toggle in `data/scheduler.json` without modifying `.env`.
+3. Workflow dependency: When both source refresh and clip preparation are enabled, `prepare` at 09:00 IST will wait for the 08:00 IST source refresh to complete successfully. If source refresh fails, dependent preparation is blocked until resolved.
+
+### Telegram Source Manager Buttons
+
+Send `/start` (or `/sources`) to access the interactive control panel:
+
+- **`📊 Source Status`**: Displays total line count, active direct video URLs, channel links, duplicate counts, conclusively consumed videos, pending eligible videos, scheduler active status, and last refresh timestamp/status.
+- **`🔎 View Summary`**: Shows tracked channels and samples of the next pending and recently used videos.
+- **`⚙️ Refresh Sources Now`**: Interactive manual refresh with confirmation (`▶️ Start Refresh` / `❌ Cancel`). Runs `make_sources` and `merge_sources` under an inter-process file lock (`work/sources.lock`), reporting scanned, appended, duplicate, and used skipped counts.
+- **`🧹 Clean Used Links`**: Safe, verified cleanup of `sources.txt`:
+  - **Authoritative Used Definition**: Only direct YouTube video URLs whose video IDs are conclusively verified as consumed in `state.json` or `data/used_videos.json` **AND** have no active, pending, processing, needs-review, or blocked clips in `review/queue.json` are eligible for removal.
+  - **Protected Entries**: Channels (e.g. `@MrBeast/videos`), pending unprocessed videos, and videos linked to active or blocked queue tasks (such as `4SNThp0YiU4`) are strictly preserved.
+  - **Preview & Confirmation**: Shows total entries, eligible removals, duplicate count, and retained lines. Requires explicit owner confirmation with a 5-minute preview token.
+  - **Atomic Backup**: Creates a timestamped backup (`sources.txt.bak_YYYYMMDD_HHMMSS`) before atomically replacing `sources.txt`.
+- **`➕ Add YouTube Source`**: Prompts the owner to send a YouTube link. Validates YouTube hostname and direct video / channel format, checks against `sources.txt` and used ledgers for deduplication, shows a confirmation preview, and atomically appends the new source upon confirmation.
+- **`🕒 Daily Schedule`**: Inspects scheduler configuration, daily refresh times, and allows toggling daily refresh on or off.
+
+### Backup and Recovery
+
+Before any cleanup modification, a backup is automatically created in the project root:
+```
+sources.txt.bak_YYYYMMDD_HHMMSS
+```
+To restore an earlier version if needed:
+```powershell
+Copy-Item sources.txt.bak_YYYYMMDD_HHMMSS sources.txt -Force
+```
+
+### Diagnosing Refresh Failures
+
+Check scheduler logs using `/logs` in Telegram or inspect `data/scheduler.json`:
+- `status`: `success_new_sources` (new links appended), `success_no_new_sources` (all candidates already present or consumed), `partial_results` (partial scrape failure), or `failed` (transient network or configuration error).
+- Detailed errors are logged in `data/scheduler.json` under `source_refresh_history` and masked in `last_error`.
+
 ## Telegram Bot Start / Stop / Exit Lifecycle Control

### Lifecycle States and Architecture

PersonalAgent implements an explicit 4-state lifecycle model:
- `STOPPED`: Application services and commands are dormant. Only the Telegram control transport is listening for lifecycle commands.
- `STARTING`: Application services (config check, task reconciliation, scheduler) are initializing.
- `RUNNING`: Full bot functionality is active: monitoring, control menus, queue commands, source manager, and scheduler.
- `STOPPING`: Graceful shutdown in progress: scheduler thread is joined, pipeline tasks paused, state saved.

### Two-Layer Architecture

1. **Control Transport (Supervisor / Poller)**: Remains online even when the bot is `STOPPED`. This allows the authorized owner to send `/start` or click `▶️ Start Bot` at any time without needing SSH or server terminal access.
2. **Application Services**: Includes command router, AI natural language classifier, source refresh, video preparation, queue processor, and daily scheduler. These are strictly stopped and isolated when state is `STOPPED`.

### Startup Gate Behavior

When the bot is `STOPPED`:
- Sending `/start` displays the startup welcome menu explaining that application services are currently stopped, along with `▶️ Start Bot` and `🚪 Exit` controls.
- Any other incoming command or message (`/status`, `/upload`, `/help`, `Prepare clips`, unknown commands, etc.) is intercepted with:
  ```
  Please first start the bot.
  ```
  along with `▶️ Start Bot` and `🚪 Exit` controls.
- No protected command execution, background tasks, or AI intent queries occur.
- Repeated messages receive exactly one response each; duplicate execution is prevented.

### Interactive Lifecycle Buttons

- **`▶️ Start Bot` (`life:start`)**:
  - Verifies owner authorization.
  - Reconciles active task status and unpauses pipeline checkpoints.
  - Starts the scheduler thread if configured and enabled.
  - Sets state to `RUNNING` and presents the main control menu.
  - If startup encounters an error, state is cleanly reset to `STOPPED` and an error is logged.
- **`⏹️ Stop Bot` (`life:stop_prompt`)**:
  - Prompts owner with `⏹️ Confirm Stop` and `❌ Cancel`.
  - On confirmation, stops scheduler gracefully, pauses background pipeline execution, and transitions to `STOPPED`.
  - Preserves all queue items, `sources.txt`, upload history, and pending approval tokens.
  - Control transport remains active with `▶️ Start Bot` available.
- **`🚪 Exit` (`life:exit_prompt`)**:
  - Prompts owner with `🚪 Yes, Exit` and `❌ Cancel`.
  - On confirmation, stops application services, marks state `STOPPED`, and signals the polling loop to release the lock (`PollLock`) and terminate the Python process cleanly.

### Supervisor and Auto-Restart Configuration

When deployed under Docker (`restart: unless-stopped`) or systemd (`Restart=always`):
- Upon boot, `reconcile_lifecycle_on_boot()` reconciles the persisted state. A previous `RUNNING` or `STARTING` state from an unexpected crash is automatically reset to `STOPPED` to prevent unsafe restart loops.
- To allow the bot to auto-start immediately upon container/service launch, set in `.env`:
  ```env
  BOT_AUTO_START=true
  ```
  (Defaults to `false`, requiring the owner to click `▶️ Start Bot`).


