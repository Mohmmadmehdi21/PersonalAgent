# Known Issues and Failure Points

Audit date: 2026-10-17. Items below are observations from the checked-out code/runtime, not changes made in this audit.

## High priority

1. **Resolved in Phase 3: workflow secret wiring.** `.github/workflows/daily.yml` now passes `GROQ_API_KEY` and writes OAuth files from masked environment variables without shell `echo` interpolation. The workflow runs `prepare` only with `DRY_RUN: "true"`; live YouTube publication is disabled in CI/scheduled Actions.
2. **Scheduled-run queue/cache persistence.** `.github/workflows/daily.yml` restores and saves `state.json`, `review/queue.json`, `work/`, and source state via `actions/cache@v4`. If a runner fails before cache save or if the cache expires, unpersisted artifacts must be regenerated from source.
3. **Live uploads are enabled in the local environment.** `.env` has `DRY_RUN=false`; `upload.py` inserts public videos. A run against a populated queue can publish without an additional confirmation step.
4. **The legacy upload-limit flag is now misleading.** `UPLOAD_LIMIT_GUARD_ENABLED` remains in config/examples for compatibility, but phase 1 makes `uploadLimitExceeded` an unconditional non-retryable batch stop. The local `.env` value no longer controls this safety behavior.
5. **Resolved in Phase 3: OAuth file ignore rules.** `.gitignore` now excludes `token_growth.json`, OAuth client files, service-account JSON, PEM, and key files. No credential file contents were read during this phase.

## Reliability and duplicate processing

6. **Upload outcome certainty is limited.** Timeouts and selected transport exceptions are classified as `UploadOutcomeUnknownError` to avoid duplicate inserts. If YouTube accepted a video but the response is lost or malformed, automatic recovery requires checking Studio; there is no reconciliation job that searches uploaded videos by queue item.
7. **A missing/unexpected upload response status can look like a failure after insertion.** `upload.py` requires returned `privacyStatus` to be `public` or `private`; any other value raises even though an ID was already returned. The queue can remain incomplete despite a created video.
8. **Queue and state are separate persistence systems.** Queue save and state save are not one transaction. A crash between them can leave queue/state out of sync; duplicate protection mitigates but does not fully reconcile it.
9. **Queue statuses migrate on the next queue read.** Phase 2 added backups and migration to the six canonical states. The point-in-time queue counts above are historical; `needs_review` items still require YouTube Studio reconciliation before retry.
10. **Source discovery ranks one recent page at a time.** `find_popular_video` asks for date-ordered pages of up to 50 results and returns the highest-view result from the first page containing unprocessed videos. It does not compare every candidate across the maximum 500-result scan, despite “popular” naming/comments. Processed/failed/skipped IDs are excluded, so those sources are not automatically retried.
11. **Cached artifacts are not versioned.** Existing transcript/clip JSON caches can be reused after model, prompt, clip threshold, crop or subtitle logic changes. `main.prepare_stage` accepts a non-empty JSON transcript cache before enforcing the word-timestamp schema; ASS subtitle generation has a compatibility estimate, but timing quality can differ.

## Configuration and deployment mismatches

12. **Live-mode defaults deserve deployment safeguards.** `config.py`, `.env.example`, and the local `.env` currently default or explicitly set `DRY_RUN=false` for local publishing, whereas the scheduled GitHub Actions workflow explicitly sets `DRY_RUN: "true"` to restrict cloud runs to draft preparation only. Local runs against a populated queue can publish only when owner approval is confirmed.
13. **`GROWTH_HEALTH_ENABLED` does not gate the growth health CLI.** The flag gates heartbeat writes in `main.py`; `python main.py growth-health` checks regardless. Growth feature flags are mostly opt-in, but `stats` itself dispatches without a tracking flag and can initiate OAuth/API reads.
14. **Growth slots/history require consistent sampling.** Slot changes require a minimum sample count for each current slot, but the experiment slot file only tracks the most recently chosen hour. Historical data is sparse initially, so suggestions may take a long time; no Analytics hourly data is used.
15. **Provider-side permissions are not validated offline or in CI.** Startup validates required local config, OAuth file shape, ADC presence, and Agent Platform project/model/region settings. It cannot verify Google Cloud API enablement/IAM, OAuth consent, YouTube visibility, Telegram delivery, Groq quotas/model availability, FFmpeg fonts, or media codecs without provider-side checks.

## Current execution/test constraints

- The default Windows `python.exe` shim and repository `.venv` launcher could not be launched. `py -3.13` is available, but `pytest` is not installed. The standard-library unittest suite now passes under Python 3.13.7 with fake API clients and no upload-guard override. No tests requiring live services were run.
- No live YouTube, Groq, Telegram, email or Sheets request was made.
- The local checkout has pre-existing modified and untracked files, including operational `state.json`, queue data and `token_growth.json`. Do not reset/clean the working tree as part of an implementation without an explicit data-preservation plan.

## YouTube Download Resilience & Verification (2026-10-18)

- **HTTP 429 & Rate Limit Cooldown**: When YouTube rate-limits source downloads (HTTP 429), it is classified as `rate_limited`. Repeated immediate attempts to download the same or other videos are halted, and a cooldown timestamp (`YTDLP_RATE_LIMIT_COOLDOWN_SECONDS`, default 15 minutes) is saved to `state.json`. Subsequent runs skip download attempts until the cooldown expires.
- **Bot Verification & PO Token Warnings**: Challenges like `Sign in to confirm you're not a bot` or `use --cookies-from-browser` are classified as non-retryable `bot_verification` / `BotVerificationError`. PO token / Visitor Data warnings in yt-dlp stderr are not treated as fatal unless the final extraction fails.
- **Multi-Candidate Fallback**: The pipeline evaluates up to `YTDLP_MAX_SOURCE_CANDIDATES` (default 5) eligible videos rather than aborting after the first failed candidate. If all candidates fail, the preparation stage finishes cleanly with a structured failure result and releases the run lock without crashing `main.py` or sending duplicate notifications.
- **Authenticated Cookie Support**: Users encountering bot challenges on their local Windows machine can export their legitimate session cookies to a file and configure `YTDLP_COOKIES_FILE=cookies.txt`, or specify `YTDLP_COOKIES_FROM_BROWSER=chrome`. Sensitive cookie files are excluded in `.gitignore` and redacted in logs.
