import json
import os
import unittest
from unittest.mock import patch

import notify


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class TelegramNotificationFormattingTests(unittest.TestCase):
    def test_broadcast_uses_html_mode_and_escapes_dynamic_content(self):
        payloads = []

        def capture(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            return _Response()

        with patch.dict(os.environ, {
            "NOTIFY_CHANNEL": "telegram",
            "TELEGRAM_BOT_TOKEN": "9876543210:syntheticTelegramTokenValue_1234567890123456789",
            "TELEGRAM_CHAT_ID": "12345",
        }), patch.object(notify.urllib.request, "urlopen", side_effect=capture), \
             patch.object(notify, "get_disk_free_gb", return_value=20):
            notify.broadcast_message("Run <clip> & check", 'Title: A < B & "quoted"')

        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertIn("<b>Run &lt;clip&gt; &amp; check</b>", payload["text"])
        self.assertIn("Title: A &lt; B &amp; &quot;quoted&quot;", payload["text"])
        self.assertNotIn('Title: A < B', payload["text"])

    def test_upload_summary_distinguishes_public_private_and_unverified_visibility(self):
        with patch.object(notify, "broadcast_message") as broadcast:
            notify.notify_run_success([
                {"title": "public", "youtube_video_id": "abcdefghijk", "youtube_privacy_status": "public"},
                {"title": "private", "youtube_video_id": "lmnopqrstuv", "youtube_privacy_status": "private"},
                {"title": "unknown", "youtube_video_id": "12345678901"},
            ], 1.0)

        body = broadcast.call_args.args[1]
        self.assertIn("PUBLIC: https://youtu.be/abcdefghijk", body)
        self.assertIn("UPLOADED, PRIVATE (not publicly visible)", body)
        self.assertIn("visibility not verified", body)
        self.assertNotIn("public upload complete", body.lower())

    def test_final_upload_summary_reports_task_counts_and_partial_failure(self):
        with patch.object(notify, "broadcast_message") as broadcast:
            notify.notify_upload_summary(
                "task-17",
                [{"id": "clip-1", "title": "A <title>", "youtube_video_id": "abcdefghijk",
                  "youtube_privacy_status": "public"}],
                [{"title": "clip-2", "error": "quota reached", "error_category": "account_upload_limit"}],
                4.5, attempted=2, pending=1, needs_review=0, stopped_reason="uploadLimitExceeded",
                failed_count=1,
            )
        subject, body = broadcast.call_args.args
        self.assertIn("task-17", subject)
        self.assertIn("Upload requests attempted: 2", body)
        self.assertIn("Confirmed YouTube uploads: 1", body)
        self.assertIn("Failed or blocked clips: 1", body)
        self.assertIn("Pending clips: 1", body)
        self.assertIn("uploadLimitExceeded", body)
        self.assertIn("account_upload_limit", body)


if __name__ == "__main__":
    unittest.main()
