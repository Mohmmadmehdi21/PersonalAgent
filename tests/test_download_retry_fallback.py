"""Unit tests for YouTube download retry, bot verification, rate limiting, and candidate fallback."""
import os
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import MagicMock, call, patch

os.environ.setdefault("PERSONALAGENT_MONITOR_FILE", "tests/.monitoring-test.json")

from config import YTDLP_MAX_RETRIES, YTDLP_MAX_SOURCE_CANDIDATES, YTDLP_RATE_LIMIT_COOLDOWN_SECONDS
from retry import (
    AccountUploadLimitError,
    AuthError,
    BotVerificationError,
    ErrorClassification,
    FatalError,
    PermanentSourceError,
    RateLimitedError,
    RetryExhaustedError,
    classify_error,
    get_download_cooldown_remaining,
    is_bot_verification_error,
    is_download_cooldown_active,
    is_rate_limited_error,
    parse_retry_after,
    retry_download,
    retry_upload,
    set_download_cooldown,
)


class FakeAPIError(Exception):
    def __init__(self, status, message="API failure", reason=None, headers=None):
        super().__init__(message)
        self.status_code = status
        self.response = types.SimpleNamespace(headers=headers or {})
        self.error_details = [{"reason": reason}] if reason else []


class DownloadRetryAndFallbackTests(unittest.TestCase):

    def test_http_429_classified_as_rate_limited(self):
        # 1. HTTP 429 is classified correctly
        err1 = FakeAPIError(429, "HTTP Error 429: Too Many Requests")
        res1 = classify_error(err1, service="download", operation="download")
        self.assertEqual(res1.category, "rate_limited")
        self.assertFalse(res1.retryable)  # Non-retryable for download so caller can trigger cooldown
        self.assertTrue(res1.stop_batch)

        err2 = Exception("ERROR: [youtube] video_id: 429: Too Many Requests")
        res2 = classify_error(err2, service="download", operation="download")
        self.assertEqual(res2.category, "rate_limited")
        self.assertTrue(is_rate_limited_error(err2))

    def test_retry_after_respected_when_valid(self):
        # 2. Retry-After is respected when valid
        headers = {"Retry-After": "120"}
        err = FakeAPIError(429, "Rate limited", headers=headers)
        res = classify_error(err, service="download", operation="download")
        self.assertEqual(res.retry_after, 120.0)

        # In string form
        self.assertEqual(parse_retry_after("retry-after: 90"), 90.0)
        self.assertEqual(parse_retry_after("retry in 45s"), 45.0)

    def test_rate_limit_stops_repeated_requests_and_sets_cooldown(self):
        # 3. Persistent rate limiting stops repeated requests & sets cooldown
        state = {}
        now = 1000.0
        set_download_cooldown(state, 900, now=now)
        self.assertTrue(is_download_cooldown_active(state, now=now + 100))
        self.assertEqual(get_download_cooldown_remaining(state, now=now + 100), 800.0)
        self.assertFalse(is_download_cooldown_active(state, now=now + 901))

    def test_bot_verification_errors_classified_not_unexpected(self):
        # 4. Bot verification errors are not classified as ordinary transient errors
        bot_msgs = [
            "ERROR: [youtube] hCuqh2NU2Yg: Sign in to confirm you’re not a bot. Use --cookies-from-browser or --cookies for the authentication.",
            "Sign in to confirm you're not a bot",
            "Please solve this captcha or sign in to confirm you are not a bot",
            "Use --cookies for the authentication",
            "bot-detection mechanism triggered",
        ]
        for msg in bot_msgs:
            err = Exception(msg)
            res = classify_error(err, service="download", operation="download")
            self.assertEqual(res.category, "bot_verification", f"Failed for: {msg}")
            self.assertFalse(res.retryable)
            self.assertTrue(is_bot_verification_error(err))

            # retry_download must raise BotVerificationError, NOT NonRetryableAPIError
            with patch("retry.time.sleep"):
                with self.assertRaises(BotVerificationError):
                    retry_download(lambda: (_ for _ in ()).throw(err))

    def test_po_token_warning_non_fatal_when_extraction_succeeds(self):
        # 5. PO Token warnings are not automatically treated as fatal when the extraction succeeds
        # Simulate a function that logs PO token warning to stderr but returns valid output
        def extract_with_warning():
            sys.stderr.write("[youtube] Unable to fetch GVS PO Token: Missing required Visitor Data\n")
            return "/path/to/video.mp4", {"id": "test1234567", "title": "Test Video", "duration": 60}

        result = retry_download(extract_with_warning)
        self.assertEqual(result[0], "/path/to/video.mp4")
        self.assertEqual(result[1]["id"], "test1234567")

    def test_failed_extraction_returns_useful_error_category(self):
        # 6. An actual failed extraction returns a useful error category
        with patch("retry.time.sleep"):
            with self.assertRaises(RateLimitedError):
                retry_download(lambda: (_ for _ in ()).throw(FakeAPIError(429, "Too Many Requests")))

    def test_deleted_or_unavailable_video_skipped_safely(self):
        # 7. A deleted or unavailable video can be skipped safely
        perm_msgs = [
            "Video unavailable",
            "Private video",
            "This video has been removed by the uploader",
            "Sign in to confirm your age",
            "Premieres in 10 hours",
        ]
        for msg in perm_msgs:
            err = Exception(msg)
            res = classify_error(err, service="download", operation="download")
            self.assertEqual(res.category, "permanent_source")
            self.assertFalse(res.retryable)
            with patch("retry.time.sleep"):
                with self.assertRaises(PermanentSourceError):
                    retry_download(lambda: (_ for _ in ()).throw(err))

    def test_temporary_network_error_uses_bounded_retries(self):
        # 8. A temporary network error uses bounded retries
        calls = []

        def flaky_download():
            calls.append(len(calls) + 1)
            if len(calls) < 3:
                raise ConnectionError("Temporary connection reset by peer")
            return "work/good.mp4", {"id": "good_vid", "title": "Good"}

        with patch("retry.time.sleep") as mock_sleep:
            res = retry_download(flaky_download)
            self.assertEqual(len(calls), 3)
            self.assertEqual(mock_sleep.call_count, 2)
            self.assertEqual(res[0], "work/good.mp4")

    def test_failed_source_falls_back_to_another_candidate(self):
        # 9. A failed source can fall back to another candidate
        import main

        mock_state = {"done": [], "videos": {}, "clips": []}
        candidates = [
            ("https://www.youtube.com/watch?v=bot11111111", ""),
            ("https://www.youtube.com/watch?v=good2222222", ""),
        ]

        def fake_download(url):
            if "bot11111111" in url:
                raise BotVerificationError("Sign in to confirm you’re not a bot")
            return "work/good2222222.mp4", {"id": "good2222222", "title": "Good Video", "duration": 45}

        usable_clips = [{
            "id": "clip1",
            "key": "good2222222:clip1",
            "start": 0.0,
            "end": 30.0,
            "hook_score": 9,
            "title": "Good Clip",
            "template": "INTRO_CARD",
            "commentary_script": "This is an important educational analysis of this moment with enough words to be valid.",
            "intro_text": "Intro",
            "caption": "Caption",
            "hashtags": ["test"],
            "thumbnail_text": "HOOK",
        }]

        with patch("main.load_state", return_value=mock_state), \
             patch("main.save_state_atomic") as mock_save, \
             patch("state_manager.save_state_atomic"), \
             patch("main.load_queue", return_value=[]), \
             patch("main.save_queue"), \
             patch("main.next_source_candidates", return_value=candidates), \
             patch("main.source_filter.filter_candidates", side_effect=lambda items: items), \
             patch("main.download", side_effect=fake_download), \
             patch("main.transcribe", return_value=[{"start": 0, "end": 30, "text": "Hello"}]) as mock_transcribe, \
             patch("main.find_clips", return_value=usable_clips), \
             patch("main._monitor_progress"):
            main.prepare_stage()
            # Verify candidate 2 was downloaded and transcribed
            mock_transcribe.assert_called_once_with("work/good2222222.mp4")
            self.assertIn("bot11111111", mock_state.get("failed_video_ids", []))

    def test_rate_limiting_stops_additional_source_requests(self):
        # 10. Rate limiting stops additional source requests when appropriate
        import main

        mock_state = {"done": [], "videos": {}, "clips": []}
        candidates = [
            ("https://www.youtube.com/watch?v=rate1111111", ""),
            ("https://www.youtube.com/watch?v=never222222", ""),
        ]

        download_calls = []

        def fake_download(url):
            download_calls.append(url)
            raise RateLimitedError("HTTP 429 Too Many Requests")

        with patch("main.load_state", return_value=mock_state), \
             patch("main.save_state_atomic"), \
             patch("state_manager.save_state_atomic"), \
             patch("main.load_queue", return_value=[]), \
             patch("main.next_source_candidates", return_value=candidates), \
             patch("main.source_filter.filter_candidates", side_effect=lambda items: items), \
             patch("main.download", side_effect=fake_download), \
             patch("main.notify_run_error") as mock_notify, \
             patch("main._monitor_progress"):
            with self.assertRaises(RateLimitedError):
                main.prepare_stage()
            # Candidate 2 must NOT have been called because candidate 1 was rate limited
            self.assertEqual(len(download_calls), 1)
            self.assertEqual(download_calls[0], candidates[0][0])
            self.assertTrue(is_download_cooldown_active(mock_state))
            mock_notify.assert_not_called()

    def test_exhausting_candidate_limit_ends_stage_cleanly(self):
        # 11. Exhaustion propagates the final evidence to the pipeline boundary for one report.
        import main

        mock_state = {"done": [], "videos": {}, "clips": []}
        candidates = [
            ("https://www.youtube.com/watch?v=fail1111111", ""),
            ("https://www.youtube.com/watch?v=fail2222222", ""),
        ]

        def fake_download(url):
            raise BotVerificationError("Sign in to confirm you’re not a bot")

        with patch("main.load_state", return_value=mock_state), \
             patch("main.save_state_atomic"), \
             patch("state_manager.save_state_atomic"), \
             patch("main.load_queue", return_value=[]), \
             patch("main.next_source_candidates", return_value=candidates), \
             patch("main.source_filter.filter_candidates", side_effect=lambda items: items), \
             patch("main.download", side_effect=fake_download), \
             patch("main.notify_run_error") as mock_notify, \
             patch("main._monitor_progress"):
            with self.assertRaisesRegex(RuntimeError, "All 2 source candidates failed") as raised:
                main.prepare_stage()
            self.assertIsInstance(raised.exception.__cause__, BotVerificationError)
            mock_notify.assert_not_called()

    def test_transcription_never_starts_without_valid_downloaded_file(self):
        # 12. Transcription never starts without a valid downloaded file
        import main

        mock_state = {"done": [], "videos": {}, "clips": []}
        candidates = [("https://www.youtube.com/watch?v=fail1111111", "")]

        with patch("main.load_state", return_value=mock_state), \
             patch("main.save_state_atomic"), \
             patch("state_manager.save_state_atomic"), \
             patch("main.load_queue", return_value=[]), \
             patch("main.next_source_candidates", return_value=candidates), \
             patch("main.source_filter.filter_candidates", side_effect=lambda items: items), \
             patch("main.download", side_effect=PermanentSourceError("Video unavailable")), \
             patch("main.transcribe") as mock_transcribe, \
             patch("main._monitor_progress"):
            with self.assertRaises(RuntimeError):
                main.prepare_stage()
            mock_transcribe.assert_not_called()

    def test_queue_contents_unchanged_when_download_fails(self):
        # 13. Queue contents are unchanged when a download fails
        import main

        initial_queue = [{"id": "existing_clip_1", "status": "pending"}]
        mock_state = {"done": [], "videos": {}, "clips": []}
        candidates = [("https://www.youtube.com/watch?v=fail1111111", "")]

        with patch("main.load_state", return_value=mock_state), \
             patch("main.save_state_atomic"), \
             patch("state_manager.save_state_atomic"), \
             patch("main.load_queue", return_value=initial_queue), \
             patch("main.save_queue") as mock_save_queue, \
             patch("main.next_source_candidates", return_value=candidates), \
             patch("main.source_filter.filter_candidates", side_effect=lambda items: items), \
             patch("main.download", side_effect=BotVerificationError("bot verification")), \
             patch("main.notify_run_error"), \
             patch("main._monitor_progress"):
            with self.assertRaises(RuntimeError):
                main.prepare_stage()
            mock_save_queue.assert_not_called()
            self.assertEqual(len(initial_queue), 1)

    def test_run_lock_released_on_every_exit_path(self):
        # 14. Run locks and temporary resources are cleaned up
        import lock

        with tempfile.TemporaryDirectory() as td:
            lock_path = os.path.join(td, "run.lock")
            with patch("lock.LOCK_FILE", lock_path):
                lock.acquire_lock()
                self.assertTrue(lock._lock_acquired)
                self.assertTrue(os.path.exists(lock_path))
                # Releasing should work cleanly
                lock.release_lock()
                self.assertFalse(lock._lock_acquired)
                self.assertFalse(os.path.exists(lock_path))

    def test_telegram_notification_sent_without_exposing_credentials(self):
        # 15. Telegram notifications are sent correctly without exposing credentials
        import notify

        with patch.dict(os.environ, {"NOTIFY_CHANNEL": "telegram", "TELEGRAM_BOT_TOKEN": "test-token",
                                     "TELEGRAM_CHAT_ID": "test-chat"}), \
             patch("notify.send_telegram_to") as mock_send:
            secret_msg = "Error with ya29.SECRET_TOKEN and cookies.txt in /home/user/cookies.txt"
            notify.notify_run_error("download_rate_limit", secret_msg, video_id="test_vid")
            mock_send.assert_called_once()
            sent_text = mock_send.call_args[0][1]
            self.assertNotIn("ya29.SECRET_TOKEN", sent_text)
            self.assertIn("********", sent_text)

    def test_no_retry_or_source_selection_loop_runs_indefinitely(self):
        # 16. No retry or source-selection loop runs indefinitely
        import source

        mock_yt = MagicMock()
        mock_yt.search().list().execute.return_value = {"items": [], "nextPageToken": None}
        videos = source.find_popular_videos(mock_yt, "https://www.youtube.com/@test", set(), max_candidates=5)
        self.assertEqual(videos, [])

    def test_existing_youtube_upload_limit_exceeded_remains_unchanged(self):
        # 17. Existing YouTube upload error handling, including uploadLimitExceeded, remains unchanged and safe
        calls = []

        def upload_fail():
            calls.append(1)
            raise FakeAPIError(400, "upload limit reached", reason="uploadLimitExceeded")

        with patch("retry.time.sleep"):
            with self.assertRaises(AccountUploadLimitError):
                retry_upload(upload_fail)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
