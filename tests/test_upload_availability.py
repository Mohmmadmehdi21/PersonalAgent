"""Unit tests for YouTube Upload Availability Recovery Alert."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import upload_availability
from upload_availability import (
    is_upload_limit_exceeded_error,
    load_upload_limit_state,
    save_upload_limit_state,
    record_upload_limit_exceeded,
    record_upload_success,
    deliver_recovery_alert,
    check_and_deliver_pending_recovery_alert,
    reconcile_upload_limit_on_boot,
    get_upload_limit_summary,
    MAX_DELIVERY_ATTEMPTS,
)
from retry import AccountUploadLimitError, QuotaExceededError, AuthError, NonRetryableAPIError


class FakeHttpError(Exception):
    def __init__(self, status_code, content):
        super().__init__(str(content))
        self.resp = MagicMock(status=status_code)
        self.status_code = status_code
        self.content = json.dumps(content).encode("utf-8") if isinstance(content, dict) else str(content).encode("utf-8")


class TestUploadAvailability(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.state_file = Path(self.tmp_dir.name) / "upload_limit_state.json"
        self.env_patcher = patch.dict(os.environ, {
            "YOUTUBE_UPLOAD_LIMIT_FILE": str(self.state_file),
            "AGENT_CONTROL_CHAT_ID": "123456",
            "TELEGRAM_CHAT_ID": "123456",
        })
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()
        self.tmp_dir.cleanup()

    # 1. Restriction is active: no recovery alert
    def test_01_restriction_active_no_recovery_alert(self):
        sent_messages = []
        mock_sender = lambda chat_id, text: sent_messages.append((chat_id, text)) or True

        err = AccountUploadLimitError("YouTube channel daily upload limit reached")
        recorded, reason = record_upload_limit_exceeded(err, clip_id="clip_1", clip_title="Clip 1", file_path=self.state_file)
        self.assertTrue(recorded)

        state = load_upload_limit_state(self.state_file)
        self.assertTrue(state["restriction_active"])
        self.assertFalse(state["recovery_detected"])
        self.assertFalse(state["alert_pending"])
        self.assertFalse(state["alert_delivered"])
        self.assertEqual(state["reason"], "uploadLimitExceeded")
        self.assertEqual(state["clip_id"], "clip_1")
        self.assertEqual(len(sent_messages), 0)

        # Attempting to deliver recovery alert when none is pending returns False
        delivered, status = deliver_recovery_alert(sender=mock_sender, file_path=self.state_file)
        self.assertFalse(delivered)
        self.assertEqual(status, "no_pending_alert")
        self.assertEqual(len(sent_messages), 0)

    # 2. Unrelated HTTP 403: no false recovery alert
    def test_02_unrelated_http_403_no_false_alert(self):
        sent_messages = []
        mock_sender = lambda chat_id, text: sent_messages.append((chat_id, text)) or True

        # Generic 403 Forbidden (e.g., accessNotConfigured)
        unrelated_403 = FakeHttpError(403, {
            "error": {
                "errors": [{"domain": "usageLimits", "reason": "accessNotConfigured", "message": "Access not configured"}],
                "code": 403,
                "message": "Access Not Configured"
            }
        })

        self.assertFalse(is_upload_limit_exceeded_error(unrelated_403))
        recorded, reason = record_upload_limit_exceeded(unrelated_403, file_path=self.state_file)
        self.assertFalse(recorded)
        self.assertEqual(reason, "not_upload_limit_error")

        # Quota exceeded 403
        quota_err = QuotaExceededError("YouTube API quota exceeded")
        self.assertFalse(is_upload_limit_exceeded_error(quota_err))

        state = load_upload_limit_state(self.state_file)
        self.assertFalse(state["restriction_active"])
        self.assertFalse(state["alert_pending"])
        self.assertEqual(len(sent_messages), 0)

    # 3. Analytics API failure: no false recovery alert
    def test_03_analytics_api_failure_no_false_alert(self):
        sent_messages = []
        mock_sender = lambda chat_id, text: sent_messages.append((chat_id, text)) or True

        analytics_err = RuntimeError("Analytics report request timed out")
        self.assertFalse(is_upload_limit_exceeded_error(analytics_err))

        state = load_upload_limit_state(self.state_file)
        self.assertFalse(state["alert_pending"])
        self.assertEqual(len(sent_messages), 0)

    # 4. Recovery confirmed by a legitimate successful upload: one alert
    def test_04_recovery_confirmed_by_successful_upload_one_alert(self):
        sent_messages = []
        mock_sender = lambda chat_id, text: sent_messages.append((chat_id, text)) or True

        # Step A: Restriction occurs
        err = AccountUploadLimitError("YouTube limit exceeded")
        record_upload_limit_exceeded(err, clip_id="clip_1", clip_title="Clip 1", file_path=self.state_file)

        # Step B: Legitimate upload succeeds
        success, reason = record_upload_success(
            clip_id="clip_2",
            youtube_video_id="abc12345678",
            title="Clip 2 Success",
            sender=mock_sender,
            file_path=self.state_file,
        )
        self.assertTrue(success)

        # Step C: Exactly one alert was sent
        self.assertEqual(len(sent_messages), 1)
        chat_id, text = sent_messages[0]
        self.assertEqual(chat_id, "123456")
        self.assertIn("YouTube Upload Availability Restored", text)
        self.assertIn("/upload", text)

        state = load_upload_limit_state(self.state_file)
        self.assertFalse(state["restriction_active"])
        self.assertTrue(state["recovery_detected"])
        self.assertTrue(state["alert_delivered"])
        self.assertFalse(state["alert_pending"])
        self.assertEqual(state["recovered_video_id"], "abc12345678")

        # Step D: Second delivery attempt is prevented (no duplicate alert)
        delivered, status = deliver_recovery_alert(sender=mock_sender, file_path=self.state_file)
        self.assertFalse(delivered)
        self.assertEqual(status, "no_pending_alert")
        self.assertEqual(len(sent_messages), 1)

    # 5. Telegram delivery failure: bounded retry without losing the alert
    def test_05_telegram_delivery_failure_bounded_retry(self):
        # Restriction occurs
        record_upload_limit_exceeded(
            AccountUploadLimitError("upload limit"),
            clip_id="clip_fail",
            file_path=self.state_file,
        )

        # Legitimate upload succeeds, but Telegram is temporarily failing
        failing_sender = lambda chat_id, text: False
        success, reason = record_upload_success(
            clip_id="clip_fail",
            youtube_video_id="vid_fail_1",
            sender=failing_sender,
            file_path=self.state_file,
        )
        self.assertTrue(success)

        # Alert is pending because delivery failed
        state = load_upload_limit_state(self.state_file)
        self.assertFalse(state["restriction_active"])
        self.assertTrue(state["recovery_detected"])
        self.assertTrue(state["alert_pending"])
        self.assertFalse(state["alert_delivered"])
        self.assertEqual(state["delivery_attempts"], 1)

        # Now Telegram recovers
        sent_messages = []
        working_sender = lambda chat_id, text: sent_messages.append((chat_id, text)) or True

        # Background retry (e.g., scheduler tick with cooldown=0 for test)
        retried, status = check_and_deliver_pending_recovery_alert(
            sender=working_sender,
            file_path=self.state_file,
            cooldown=0.0,
        )
        self.assertTrue(retried)
        self.assertEqual(status, "delivered")
        self.assertEqual(len(sent_messages), 1)

        state = load_upload_limit_state(self.state_file)
        self.assertTrue(state["alert_delivered"])
        self.assertFalse(state["alert_pending"])

        # Bounded retry verification: if sender persistently fails, it caps at MAX_DELIVERY_ATTEMPTS
        state["alert_delivered"] = False
        state["alert_pending"] = True
        state["delivery_attempts"] = MAX_DELIVERY_ATTEMPTS
        save_upload_limit_state(state, self.state_file)

        retried, status = check_and_deliver_pending_recovery_alert(
            sender=working_sender,
            file_path=self.state_file,
            cooldown=0.0,
        )
        self.assertFalse(retried)
        self.assertEqual(status, "max_attempts_exhausted")

    # 6. Agent restarts after recovery: no duplicate alert
    def test_06_agent_restarts_after_recovery_no_duplicate_alert(self):
        sent_messages = []
        mock_sender = lambda chat_id, text: sent_messages.append((chat_id, text)) or True

        # Simulate completed recovery and delivered alert
        state = load_upload_limit_state(self.state_file)
        state["restriction_active"] = False
        state["recovery_detected"] = True
        state["alert_pending"] = False
        state["alert_delivered"] = True
        state["alert_delivered_at"] = 1729400000.0
        save_upload_limit_state(state, self.state_file)

        # Process boots up and reconciles
        reconciled = reconcile_upload_limit_on_boot(sender=mock_sender, file_path=self.state_file)
        self.assertFalse(reconciled["restriction_active"])
        self.assertTrue(reconciled["alert_delivered"])
        self.assertEqual(len(sent_messages), 0)  # No duplicate alert sent

    # 7. Restriction occurs again after recovery: a new recovery event can generate a new alert
    def test_07_restriction_recurrence_and_second_recovery(self):
        sent_messages = []
        mock_sender = lambda chat_id, text: sent_messages.append((chat_id, text)) or True

        # Event 1: First restriction and recovery
        record_upload_limit_exceeded(AccountUploadLimitError("limit 1"), file_path=self.state_file)
        record_upload_success(clip_id="c1", youtube_video_id="v1", sender=mock_sender, file_path=self.state_file)
        self.assertEqual(len(sent_messages), 1)

        # Event 2: Restriction recurs!
        err2 = AccountUploadLimitError("limit 2 occurred on next batch")
        record_upload_limit_exceeded(err2, clip_id="c2", clip_title="Clip 2", file_path=self.state_file)

        state = load_upload_limit_state(self.state_file)
        self.assertTrue(state["restriction_active"])
        self.assertFalse(state["recovery_detected"])
        self.assertFalse(state["alert_delivered"])
        self.assertEqual(len(sent_messages), 1)  # Still 1 message, no false alert

        # Event 3: Second recovery occurs
        record_upload_success(clip_id="c3", youtube_video_id="v3", sender=mock_sender, file_path=self.state_file)
        self.assertEqual(len(sent_messages), 2)  # Exactly 2 alerts across the 2 distinct recovery events

    # 8. Agent offline during the restriction: saved state is reconciled on restart without claiming unverified recovery
    def test_08_agent_offline_during_restriction_reconciles_without_false_claim(self):
        sent_messages = []
        mock_sender = lambda chat_id, text: sent_messages.append((chat_id, text)) or True

        # Restriction was active before process went offline
        record_upload_limit_exceeded(AccountUploadLimitError("offline limit"), file_path=self.state_file)

        # Agent boots up later
        reconciled = reconcile_upload_limit_on_boot(sender=mock_sender, file_path=self.state_file)

        # It must NOT claim recovery simply because time elapsed
        self.assertTrue(reconciled["restriction_active"])
        self.assertFalse(reconciled["recovery_detected"])
        self.assertFalse(reconciled["alert_delivered"])
        self.assertEqual(len(sent_messages), 0)

        # Summary indicates restricted
        summary = get_upload_limit_summary(self.state_file)
        self.assertIn("RESTRICTED", summary)

    # 9. Existing upload confirmation and queue protections remain functional
    def test_09_existing_queue_protections_intact(self):
        # Verification that recording an upload limit restriction does not corrupt or wipe queue files
        dummy_queue = [{"id": "item1", "status": "blocked", "upload_error": "uploadLimitExceeded"}]
        queue_path = Path(self.tmp_dir.name) / "queue.json"
        queue_path.write_text(json.dumps(dummy_queue), encoding="utf-8")

        record_upload_limit_exceeded(AccountUploadLimitError("limit test"), clip_id="item1", file_path=self.state_file)

        loaded_queue = json.loads(queue_path.read_text(encoding="utf-8"))
        self.assertEqual(len(loaded_queue), 1)
        self.assertEqual(loaded_queue[0]["id"], "item1")
        self.assertEqual(loaded_queue[0]["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
