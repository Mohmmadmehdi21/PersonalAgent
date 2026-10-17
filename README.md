# YouTube Clip Agent - Value-Add, Commentary & Reliability System

For installation, secure credentials, Google OAuth/ADC separation, and runtime requirements, see [docs/SETUP.md](docs/SETUP.md).

Channel/video source -> Whisper transcript -> Groq clip & commentary drafts -> subtitles, commentary cards & Edge-TTS voiceover -> thumbnail -> automatic YouTube upload.

## Groq API Setup

Set `GROQ_API_KEY` in your local `.env` using a key from GroqCloud. Text generation
uses `GROQ_MODEL` (default `qwen/qwen3.8-27b`); thumbnail frame ranking uses
`GROQ_VISION_MODEL` (defaults to the same model). Install the updated dependencies
with `pip install -r requirements.txt`. If the key is missing, clip drafting falls
back to the built-in local selector and thumbnail ranking uses its 2-second frame.
The default Qwen 3.8 27B model is listed by Groq for production, JSON output and
vision input. `GROQ_MAX_RETRIES` controls retries after the first attempt (default 3,
maximum 5); `API_TIMEOUT_SECONDS` controls the request timeout. Groq's Python SDK
client is reused within the process, with SDK-internal retries disabled so the
configured bounded retry policy remains in control.

---

## 🎯 Commentary and Value-Add Editing
Har clip ke liye transcript se commentary aur analysis draft hota hai taaki YouTube ki "Reused Content" policy meet ho sake:
- **INTRO_CARD**: Clip se pehle 5-8 sec ka card ("Why this matters" + opinion) + TTS voiceover.
- **PAUSE_COMMENT**: Clip ke beech key moment pe freeze frame + commentary callout + TTS voiceover.
- **BREAKDOWN**: Clip ke baad 8-12 sec ka educational takeaway card (2-3 bullets).
- **INTRO_PLUS_BREAKDOWN**: Intro card + main clip + takeaway breakdown card.

Commentary modes (`.env` mein `COMMENTARY_MODE`):
- `text_tts` (default): Pillow text cards + free Edge-TTS voiceover (Hindi/English).
- `text_only`: Sirf visual cards, no audio.
- `voice`: Custom user audio from `commentary_audio/<clip_id>.mp3` (fallback to `text_tts`).
- `face`: Future placeholder (TODO).

---

## 🛡️ Reliability, Resume & Retry
1. **Single Instance Lock**: `work/run.lock` Task Scheduler ya double run ke overlap ko rokta hai. Sirf dead process ka lock recover hota hai; live process ka purana lock retain hota hai.
2. **State Machine (`state.json`)**: Har video aur clip ke steps (`download`, `transcribe`, `find_clips`, `cut`, `thumbnail`, `upload`) atomic save hote hain.
3. **Smart Resume**: Dobara chalane par valid cache files ko source of truth maana jaata hai:
   - `work/<id>.mp4` valid hai to download skip.
   - `work/<id>.transcript.json` valid hai to Whisper skip.
   - `work/<id>.clips.json` valid hai to Groq call skip.
   - Clip render ho chuki ho to re-render skip.
   - Upload ho chuki ho to duplicate upload skip.
4. **Single Clip Failure Isolation**: Agar 4 mein se 1 clip fail ho, to baaki 3 upload hote rahenge; run rukega nahi.
5. **Backoff Retries & Error Handling**:
   - Download: Transient network/timeout errors use bounded retries (`YTDLP_MAX_RETRIES`, default 2). Bot verification challenges and HTTP 429 rate limiting stop retrying immediately.
   - Multi-Candidate Fallback: Up to `YTDLP_MAX_SOURCE_CANDIDATES` (default 5) eligible candidates are evaluated before ending the preparation stage cleanly without crashing.
   - Rate Limit Cooldown: HTTP 429 applies a persistent cooldown (`YTDLP_RATE_LIMIT_COOLDOWN_SECONDS`, default 900s) saved to `state.json` to prevent hammering YouTube.
   - Optional Authentication: Support for legitimate user cookies via `YTDLP_COOKIES_FILE` (exported cookies.txt) or `YTDLP_COOKIES_FROM_BROWSER` (e.g. `chrome`, `firefox`). Cookies are never logged or committed.
   - Groq: one initial attempt plus `GROQ_MAX_RETRIES` (default 3) with exponential backoff, jitter, and 429 Retry-After.
   - FFmpeg / Thumbnail: 2 attempts stderr print ke sath.
   - YouTube Upload: 5 attempts only for transient server/rate errors. Account `uploadLimitExceeded`, permanent 4xx, quota, and auth failures stop without repeated retries. If a transport timeout leaves insert outcome unclear, the clip is marked uncertain for Studio verification.
   - Groq: model-not-found/auth/configuration errors stop immediately; transient network, timeout, 5xx, and rate-limit errors use the bounded configured retry policy. API SDK retries are disabled so the shared retry budget controls attempts.

---

## 📢 Notifications (`notify.py`)
Channels (`.env` mein `NOTIFY_CHANNEL`): `telegram`, `email`, `none`, ya `telegram,email`.
- **Run Safal**: Render/uploaded clips ki list, titles, links aur run time.
- **Run Error**: Step name, error details, aur resume status.
- **Quota / Auth Alert**: YouTube token invalid ya quota exhaust hone par urgent message.
- **Low Disk Warning**: Drive par 5 GB se kam space bache hone par notification mein auto-alert.
- **Test Command**: `python notify.py --test`

### Telegram Setup:
1. Telegram mein `@BotFather` kholein -> `/newbot` -> naam aur username dein -> bot token copy karein.
2. Apne bot ko Telegram mein start karein aur koi message bhejein.
3. Browser mein `https://api.telegram.org/bot<TOKEN>/getUpdates` kholein, `"chat":{"id": ...}` aapka `TELEGRAM_CHAT_ID` hai.
4. Dono values `.env` mein dalein (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).

---

## 🧹 Disk Safai / Cleanup
Disk safai sirf tab execute hoti hai jab us video ke saare clips upload (ya DRY_RUN mein render) ho chuke hon.
- `CLEANUP=true`: Policy on/off (default true).
- `KEEP_TRANSCRIPT=true`: Transcripts aur clips JSON hamesha preserve rahte hain.
- `DELETE_SOURCE=true`: Badi 1 GB source video upload verify hone ke baad delete ho jaati hai.
- `KEEP_FINAL_CLIPS_DAYS=2`: Uploaded clips/thumbnails N din baad clean hote hain.
- **Preview / Dry Cleanup**: `python main.py cleanup --dry` (bina delete kiye candidate list aur space to free dikhata hai).
- **Execute Cleanup**: `python main.py cleanup`

---

## 🛠️ CLI Commands Cheatsheet

```bash
# 1. Full Automated Run (Prepare -> Auto-render/upload -> Cleanup -> Notify)
python main.py

# 2. Stage 1: Sirf Prepare (Download, Whisper, Groq drafting)
python main.py prepare

# 3. Stage 2: Pending Clips Auto-render & Upload
python main.py render

# 5. Disk Cleanup Preview
python main.py cleanup --dry

# 6. Disk Cleanup Execution
python main.py cleanup

# 7. Notification Integration Test
python notify.py --test
```

---

## Growth Tracking & Learning (Optional)

Growth features are opt-in. `TRACKING_ENABLED`, `AUTO_SLOT_UPDATE`, `LEARNING_ENABLED`,
`COMMENT_ENABLED`, and `GROWTH_HEALTH_ENABLED` default to `false`; existing upload and
render behavior stays unchanged while they are off. Data is stored under `data/`.

- `python main.py stats` refreshes due 1-, 3-, and 7-day public-video snapshots in `data/videos.csv`.
- `python main.py slots` shows per-hour 7-day medians. It only suggests a change by default;
  `AUTO_SLOT_UPDATE=true` can write one change to `data/slots.json` after the sample threshold.
- `python main.py learn` refreshes a transcript-grounded guide no more than once per week,
  after at least 15 seven-day samples. `LEARNING_ENABLED=true` lets clip prompts read it.
- `python main.py comments` can draft comments in dry-run mode. Posting requires
  `COMMENT_ENABLED=true` and a separate growth OAuth consent. Pinning remains manual.
- `python main.py growth-health` checks `data/heartbeat.json`; set `GROWTH_HEALTH_ENABLED=true`
  to write heartbeat updates after pipeline runs.

Growth OAuth uses `token_growth.json`, separately from the upload token. The first stats,
Sheets, or comment operation that needs an unapproved scope opens a browser consent flow.
Optional Sheet sync is off unless `SHEET_ID` is set. No scopes are added to the existing
upload OAuth flow.

## Telegram Monitoring & YouTube Reports

Start the owner-only Telegram control bot with `python main.py telegram-bot`. Set
`TELEGRAM_OWNER_IDS` to comma-separated numeric user IDs. Monitoring commands include
`/status`, `/health`, `/queue`, `/logs`, `/task TASK_ID`, and `/history`.

Read-only reporting commands are `/showanalytics 7d`, `/showanalytics 28d`,
`/showviews VIDEO_ID`, `/showlikes VIDEO_ID`, `/showcomments VIDEO_ID`, and `/videos`.
Analytics uses channel-owner YouTube Analytics reports; individual video counts and recent
uploads use YouTube Data API. Both APIs must be enabled in the Google Cloud project for
`token_growth.json`. Existing `youtube.readonly` consent is reused, and missing consent can
open the browser authorization flow. Analytics date ranges cover the last complete 7 or 28
Pacific calendar days; the newest dates may be absent while YouTube finalizes them. `/videos`
uses pagination and returns at most 20 recent uploads. Reports are live and are not cached.
# Optional Telegram scheduler

The Telegram bot can run a once-per-day preparation job and an optional analytics report. Configure `SCHEDULER_ENABLED=true`, `SCHEDULER_TIMEZONE` (IANA name), `SCHEDULER_PREPARE_TIME` (`HH:MM`), and `DAILY_DRAFT_LIMIT` in `.env`, then start the existing single bot process with `python main.py telegram-bot`. Scheduled preparation only creates queue drafts; it never publishes videos. Public publishing remains a separate `/upload` action and still requires confirmation for each clip.

Set `SCHEDULER_ANALYTICS_ENABLED=true` to send a daily `7d` or `28d` report at `SCHEDULER_ANALYTICS_TIME`. `/pause` prevents new scheduled jobs and pauses pipeline checkpoints; `/resume` allows them again. Set `SCHEDULER_ENABLED=false` to disable the scheduler. Each daily job is durably claimed in `data/scheduler.json` before it starts, so a restart does not repeat that day's job. `SCHEDULER_MAX_ATTEMPTS` bounds recovery to at most three total attempts; only classified transient network, timeout, rate-limit, and server errors are retried. Authentication, quota, model, and configuration failures are not retried automatically.
