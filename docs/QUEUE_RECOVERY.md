# Queue persistence and recovery

`review/queue.json` remains a JSON list of clip objects. Existing metadata is kept, and legacy status values are migrated in place only after the exact original bytes have been saved under `review/backups/`. Queue writes use a same-directory temporary file, `fsync`, and atomic replace. Each changed replacement archives the previous queue; backups are retained until an operator removes them deliberately.

## Task states

| State | Automatic action |
| --- | --- |
| `pending` | Eligible for rendering and upload. |
| `processing` | Durable checkpoint while rendering or uploading. |
| `uploaded` | YouTube returned a confirmed video ID, stored as `youtube_video_id`. |
| `failed` | Processing failed; not retried automatically. |
| `blocked` | Account limit, quota, or authentication requires intervention. |
| `needs_review` | Upload may have committed, state is inconsistent, or a duplicate/unknown status needs reconciliation. |

Old `approved` and `rendered_dry_run` items become `pending`; confirmed `completed` items with an ID become `uploaded`; uncertain uploads become `needs_review`. A legacy completion without an ID is held for review. Unknown status values are preserved in `legacy_status` and held for review. No queue entries are silently removed.

## Restart behavior and duplicate protection

Before a clip is processed, the queue records `processing_step=rendering`. A restart in that phase returns the task to `pending`, so validated media caches can be reused. Immediately before calling YouTube, the queue durably records `processing_step=uploading`. If a restart or timeout occurs after that point without a confirmed ID, the task becomes `needs_review`; it is never automatically submitted again. A confirmed upload response is saved as `uploaded` with its ID. Account limits, quota, and authentication errors become `blocked` and stop the current batch.

The run lock is created exclusively, preventing two local processes from claiming the queue at once. A lock owned by a live process is kept even if old; an unverifiable lock is preserved and fails closed. A lock owned by a dead PID can be recovered.

## Operator recovery

1. Stop the agent and make a copy of `review/queue.json` and the selected backup before editing anything.
2. For every `needs_review` item, check YouTube Studio using the title/time and inspect the channel's recent uploads.
3. If the upload exists, record its exact video ID and set `status` to `uploaded`. If YouTube confirms it does not exist, set `status` to `pending` and clear `processing_step` / `upload_started_at` so it can safely retry.
4. Keep unresolved items as `needs_review`. Do not change them to `pending` based only on a timeout message.
5. To restore a backup, stop the process first and copy the chosen `review/backups/queue-*.json` over `review/queue.json`, keeping the current queue copy alongside it. The next run validates/migrates the restored data and creates another backup before any migration.

No live YouTube upload was performed as part of queue tests. This JSON-file design targets the current single-instance local agent; multiple machines require shared transactional storage such as SQLite on a single host or PostgreSQL.
