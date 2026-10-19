# PersonalAgent Architecture Baseline

Audit date: 2026-10-17  
Branch: `feature/growth`  
Scope: current working tree, including pre-existing uncommitted changes. This describes the checked-out code, not only the last commit.

## Project map

| Area | Files | Responsibility |
|---|---|---|
| Entrypoint and orchestration | `main.py` | Growth CLI dispatch, source selection, prepare/render stages, queue lifecycle, upload, cleanup, notifications, run lock |
| Source discovery and media | `source.py`, `download.py`, `transcribe.py`, `find_clips.py` | Resolve YouTube channel/direct links, choose source video, cache/download, Whisper transcript, Groq clip selection and local fallback |
| Clip production | `clip.py`, `face_tracker.py`, `subtitle_gen.py`, `commentary.py`, `thumb.py` | Vertical crop, subtitles, commentary cards/TTS, clip assembly and thumbnail generation |
| Upload/auth | `upload.py`, `retry.py`, `test_auth.py` | YouTube OAuth using `token.json`, public video insert, thumbnail set, retry/error classification |
| Persistence/reliability | `state_manager.py`, `lock.py`, `logger.py`, `cleanup.py`, `notify.py`, `review.py` | JSON state, run lock, logs, safe-cleanup selection, Telegram/email notifications, legacy manual review CLI |
| Optional growth tools | `growth/*.py` | Separate OAuth, upload/stat tracking, slot suggestions, prompt learnings, comments and lightweight CLI |
| Runtime data | `sources.txt`, `review/queue.json`, `state.json`, `work/`, `logs/`, `data/`, `own_videos/` | Sources, resumable queue/state, generated media/cache, logs, growth records, optional owner media |

## Execution flow

```text
python main.py
  ├─ growth command? -> growth.cli dispatch and exit
  └─ acquire work/run.lock
      ├─ prepare_stage
      │   ├─ optionally enqueue one own_videos item (OWN_CONTENT_ENABLED)
      │   ├─ read sources.txt; prefer unprocessed channel sources
      │   ├─ source.py resolves channel, scans up to 10 date-ordered pages
      │   │   └─ ranks unprocessed results by views within the first page with candidates
      │   ├─ download.py -> yt-dlp, validated source cache, bounded download retry
      │   ├─ transcribe.py -> faster-whisper (CUDA, then CPU) or transcript cache
      │   ├─ find_clips.py -> cached clip JSON, otherwise Groq; local audio/speech fallback
      │   ├─ reject unusable results; try up to four additional channel sources
      │   └─ append pending clip records to review/queue.json and state.json
      ├─ render_stage
      │   ├─ pending items persisted as processing while claimed
      │   ├─ render cache -> subtitles/ASS -> face crop (center fallback)
      │   ├─ commentary cards/TTS and thumbnail selection/overlay
      │   ├─ DRY_RUN=true: local output only
      │   └─ otherwise: durable upload checkpoint -> YouTube public insert -> optional growth tracking -> state/queue save
      ├─ optional cleanup and run notification
      ├─ optional heartbeat write
      └─ release work/run.lock
```

CLI commands include `prepare`, `render`, `cleanup [--dry]`, and the optional growth commands `stats`, `slots`, `learn`, `comments`, `growth-health`. The growth commands dispatch before the media pipeline imports, so they do not need Whisper/FFmpeg imports. The default run does prepare then render; `prepare` alone only queues work, while `render` consumes existing queue work.

The optional Telegram scheduler can also refresh the curated long-video source list once per day before scheduled preparation. When `SCHEDULER_SOURCE_REFRESH_ENABLED=true`, it runs `make_sources.py`, appends eligible unseen links through `merge_sources.py`, then launches that day's prepare task only after refresh succeeds. This refresh is opt-in and does not publish videos.

## Queue, state, and caches

- `review/queue.json` is a JSON list of clip dictionaries. `queue_manager.py` validates and atomically replaces it, archives prior bytes before changed writes, safely migrates legacy statuses, and preserves corrupt/unknown data. Recovery steps are in [QUEUE_RECOVERY.md](QUEUE_RECOVERY.md).
- Canonical task states are `pending`, `processing`, `uploaded`, `failed`, `blocked`, and `needs_review`. Upload-phase crashes and uncertain outcomes require manual verification instead of resubmission.
- `state.json` stores top-level completed/source IDs and per-video/per-clip stage status. `state_manager.py` attempts an atomic replace but falls back to a direct write if that fails. Valid cache files and state entries are used to resume and prevent duplicate uploads.
- `work/` stores source MP4s, transcript JSON, clip-analysis JSON, rendered clips, subtitle files, commentary intermediates and thumbnails. Cache lookup is keyed mostly by video/clip IDs and filenames; cached analysis/transcript content can therefore outlive prompt/model/processing changes.
- `logs/` holds timestamped run logs. `work/run.lock` is created exclusively to prevent overlapping runs and recovers only locks with a dead owner.
- `data/` is the growth feature store: `videos.csv` and snapshots, optional `slots.json`, experiment slot data, learnings, comments, pin reminder, heartbeat. Growth CSV/JSON helper writes generally use temp files and replace.

## External services and authentication

- **Groq**: `GROQ_API_KEY`, `GROQ_MODEL`, and `GROQ_VISION_MODEL` are used for clip/commentary analysis, optional learning summaries, comments and thumbnail frame ranking. Missing clip-generation key selects a local fallback; thumbnail selection falls back to a fixed-time frame.
- **YouTube upload**: `upload.py` uses `client_secret.json` and `token.json` with `youtube.upload` plus `youtube.readonly`. Inserts explicitly request `privacyStatus=public`; a returned private status is logged and accepted. Thumbnail assignment is attempted after video insertion and its failure is non-fatal.
- **Growth OAuth**: `growth/auth.py` uses a separate `token_growth.json` for read-only statistics, comment-writing and optional Sheets scopes. Scope escalation is checked and can open a browser consent flow.
- **Google Agent Platform ADC**: Not currently used by the Groq-based pipeline. Optional project/location/model/ADC settings are validated independently; they do not reuse YouTube OAuth or Telegram credentials.
- **Notifications**: `notify.py` supports Telegram, email, both, or none based on environment configuration.
- **Configuration/security**: `.env` and credential files are ignored by Git; `config.validate_startup_config()` checks only settings required by the selected command. `logger.mask_secrets()` redacts environment-backed secrets, common credential formats, absolute credential paths, and the user-home path before logs or notification payloads are emitted.
- **Media tooling**: yt-dlp, faster-whisper, FFmpeg/ffprobe, OpenCV face detector and Pillow; Edge-TTS provides generated voice when enabled by commentary mode.

## Current checked-out settings and observed runtime data

- `.env` currently has `DRY_RUN=false`, `UPLOAD_LIMIT_GUARD_ENABLED=false`, `COMMENTARY_MODE=text_tts`, and `CLEANUP=true`. Growth feature flags are absent from `.env`, so their code defaults apply (off). Secret values are intentionally not recorded here.
- `upload.py` requests a public upload. `uploadLimitExceeded` now stops the batch immediately regardless of the legacy `UPLOAD_LIMIT_GUARD_ENABLED` setting; only classified transient errors receive bounded retries.
- At audit time, `review/queue.json` contained 7 entries: 3 completed, 3 approved/pending-for-render status, and 1 skipped as unusable. One approved entry had `upload_error=uploadLimitExceeded`. These counts are a point-in-time snapshot, not a contract.
- `state.json` and generated media/log artifacts are populated from prior runs. No live YouTube/Groq calls were made for this audit.

## Baseline tests

Existing tests are `tests/test_growth.py` and `tests/test_upload_retry_queue.py`. They use fake YouTube APIs, temporary files, and stubs to cover growth CSV/snapshot behavior, slots/learnings/comments CLI behavior, upload retry classification, queue persistence, upload batch stopping and lock release. `tests/test_baseline.py` adds stable URL parsing and channel-resolution characterization without calling YouTube.

## Known boundaries

The checked-out `.env`, media, state and queue are operational data, not source fixtures. Code/config, model and workflow have local edits in this audit checkout. See [KNOWN_ISSUES.md](KNOWN_ISSUES.md) and [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) before implementation work.
