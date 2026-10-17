# Implementation Plan

This plan follows the 2026-10-17 codebase audit. It is intentionally ordered to protect the existing upload flow and persisted user data.

## Phase 0 — Baseline (complete)

- Read the current source tree, workflow, config, README, tests, queue/state shape, logs inventory and local operational flags.
- Map the default pipeline and growth CLI boundaries.
- Add the architecture and known-issues documents plus offline URL characterization tests.
- No production code, configuration, secrets, queue, state, cache or generated media changed.

## Phase 1 — Reliability and error handling (complete)

1. Centralize API error classification and retry decisions. Permanent/model/auth/quota/account-limit errors stop immediately; transient network, timeout, rate-limit and server errors use finite budgets.
2. Disable Groq SDK-internal retries and apply explicit API timeouts to Groq, YouTube/Sheets HTTP clients, and yt-dlp.
3. Save queue/state at each clip boundary; preserve uncertain uploads against accidental duplicate inserts and handle Ctrl+C by preserving known status and releasing the run lock.
4. Add structured JSON task/retry result events and offline tests for classifications, delays, exhaustion and interruption.
5. Run the full unittest suite with fake API clients; no live service writes.

## Phase 2 — Fix deployment/config drift

1. Align the scheduled workflow with Groq environment variables and the intended API key secret name.
2. Validate the scheduled runtime dependency installation, Python version, FFmpeg availability, OAuth token provisioning and output permissions.
3. Choose safe persistence for `review/queue.json` and resume caches across ephemeral runner invocations; do not commit generated media or secrets as a shortcut.
4. Make the deployment's live-upload opt-in explicit and independently configurable; verify with mocked upload calls before any live run.
5. Add secret-file ignore rules and CI checks that prevent OAuth artifacts/API keys from entering commits or logs.

## Phase 3 — Reliability and data integrity

1. Add queue/state reconciliation that can identify already-uploaded videos without re-uploading uncertain outcomes.
2. Make each clip transition explicit and persist upload outcome before optional thumbnail/tracking side effects.
3. Make upload-limit handling stop immediately and preserve the exact pending item; keep bounded transient retries for genuinely transient failures.
4. Add idempotency tests for crashes around upload, state update, queue update and thumbnail errors.
5. Version cache metadata by source identity, transcript model, prompt/schema and render settings; invalidate only derived artifacts whose inputs changed.

## Phase 4 — Source and media quality

1. Decide whether “popular latest” means global ranking across a bounded candidate set or popularity within the newest page; document API quota implications.
2. Test fallback source selection with fake API pages, processed IDs, unavailable sources, empty transcripts and no usable clips.
3. Add media fixtures and mock/contract tests for Whisper cache schema, ASS/SRT fallback, crop fallback, commentary/TTS and thumbnail selection.
4. Validate licensing/permission metadata behavior and ensure source credits are not mistaken for reuse authorization.

## Phase 5 — Growth features and observability

1. Test each growth command against temporary data and fake APIs; keep scopes isolated from upload OAuth.
2. Verify that comment writes are opt-in, public-only, deduplicated and suppressed in dry-run.
3. Validate snapshots and slot learning over time, including missing/private/deleted videos and malformed data.
4. Add health reporting around lock state, pending queue age, repeated source failures, authentication and disk capacity.

## Phase 6 — Staged release

1. Run tests and import/compile checks on the chosen Python runtime.
2. Run a full local DRY_RUN with mocked external services, then a small real-media render with upload disabled.
3. Review exact diffs and persistent data effects; preserve the existing queue/state/caches.
4. Enable a live upload only after an explicit user go-ahead and a verified rollback/recovery procedure.

## Acceptance evidence to collect

- Test results and Python/dependency versions.
- Diff limited to approved implementation scope.
- Queue/state migration or compatibility evidence (ideally none required).
- Mock proof that no API insert/comment occurs in dry-run and that retries cannot duplicate uncertain uploads.
- Sanitized deployment configuration and successful scheduled workflow run without printing secrets.
