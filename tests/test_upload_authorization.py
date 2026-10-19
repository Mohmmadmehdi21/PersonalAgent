import tempfile
import unittest
from pathlib import Path

from upload import upload
from upload_auth import UploadApprovalRequiredError, issue_upload_approval
from retry import UploadOutcomeUnknownError


class _Request:
    def __init__(self, response):
        self.response = response

    def execute(self):
        return self.response


class _Videos:
    def __init__(self, response=None):
        self.insert_calls = 0
        self.response = response or {"id": "yt_test_id1", "status": {"privacyStatus": "public"}}

    def insert(self, **_kwargs):
        self.insert_calls += 1
        return _Request(self.response)


class _YouTube:
    def __init__(self, response=None):
        self.video_api = _Videos(response)

    def videos(self):
        return self.video_api


class UploadAuthorizationTests(unittest.TestCase):
    def test_unapproved_or_mismatched_upload_never_reaches_insert(self):
        youtube = _YouTube()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            video = Path(temp) / "clip.mp4"
            video.write_bytes(b"test media")
            with self.assertRaises(UploadApprovalRequiredError):
                upload(youtube, str(video), "missing.jpg", "title", "description", [], clip_id="clip-1")
            wrong_clip_approval = issue_upload_approval("clip-2", "123456789")
            with self.assertRaises(UploadApprovalRequiredError):
                upload(youtube, str(video), "missing.jpg", "title", "description", [],
                       approval=wrong_clip_approval, clip_id="clip-1")
        self.assertEqual(youtube.video_api.insert_calls, 0)

    def test_owner_approved_upload_succeeds_once(self):
        youtube = _YouTube()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            video = Path(temp) / "clip.mp4"
            video.write_bytes(b"test media")
            approval = issue_upload_approval("clip-1", "123456789")
            result = upload(youtube, str(video), "missing.jpg", "title", "description", [],
                            approval=approval, clip_id="clip-1")
            self.assertEqual(result, "yt_test_id1")
            self.assertEqual(result.privacy_status, "public")
            with self.assertRaises(UploadApprovalRequiredError):
                upload(youtube, str(video), "missing.jpg", "title", "description", [],
                       approval=approval, clip_id="clip-1")
        self.assertEqual(youtube.video_api.insert_calls, 1)

    def test_private_result_is_reported_as_private_not_public(self):
        youtube = _YouTube({"id": "yt_test_id1", "status": {"privacyStatus": "private"}})
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            video = Path(temp) / "clip.mp4"
            video.write_bytes(b"test media")
            approval = issue_upload_approval("clip-1", "123456789")
            result = upload(youtube, str(video), "missing.jpg", "title", "description", [],
                            approval=approval, clip_id="clip-1")
        self.assertEqual(result, "yt_test_id1")
        self.assertEqual(result.privacy_status, "private")

    def test_malformed_youtube_id_is_not_returned_as_confirmed_upload(self):
        youtube = _YouTube({"id": "", "status": {"privacyStatus": "public"}})
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            video = Path(temp) / "clip.mp4"
            video.write_bytes(b"test media")
            approval = issue_upload_approval("clip-1", "123456789")
            with self.assertRaisesRegex(RuntimeError, "valid video ID"):
                upload(youtube, str(video), "missing.jpg", "title", "description", [],
                       approval=approval, clip_id="clip-1")
        self.assertEqual(youtube.video_api.insert_calls, 1)

    def test_missing_visibility_preserves_video_id_as_uncertain_outcome(self):
        youtube = _YouTube({"id": "yt_test_id1", "status": {}})
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            video = Path(temp) / "clip.mp4"
            video.write_bytes(b"test media")
            approval = issue_upload_approval("clip-1", "123456789")
            with self.assertRaises(UploadOutcomeUnknownError) as raised:
                upload(youtube, str(video), "missing.jpg", "title", "description", [],
                       approval=approval, clip_id="clip-1")
        self.assertEqual(raised.exception.video_id, "yt_test_id1")
        self.assertEqual(raised.exception.privacy_status, "unknown")


if __name__ == "__main__":
    unittest.main()
