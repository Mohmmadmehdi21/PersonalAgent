"""Offline tests for Telegram YouTube reports; all provider objects are fakes."""
import os
import unittest
from datetime import date

os.environ.setdefault("PERSONALAGENT_MONITOR_FILE", "tests/.monitoring-test.json")

from telegram_bot.commands import COMMAND_NAMES, handle_update
from telegram_bot.youtube_reports import (
    ReportingError, analytics_summary, list_recent_videos, video_metric,
)


def owner_update(text):
    return {"message": {"from": {"id": 77}, "chat": {"id": 77, "type": "private"}, "text": text}}


class FakeRequest:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error
    def execute(self):
        if self.error:
            raise self.error
        return self.result


class FakeResource:
    def __init__(self, response):
        self.response = response
        self.calls = []
    def list(self, **kwargs):
        self.calls.append(kwargs)
        value = self.response(kwargs) if callable(self.response) else self.response
        return value if isinstance(value, FakeRequest) else FakeRequest(value)


class FakeYouTube:
    def __init__(self, *, videos=None, channels=None, playlist=None, comments=None):
        self.video_resource = FakeResource(videos or {"items": []})
        self.channel_resource = FakeResource(channels or {"items": []})
        self.playlist_resource = FakeResource(playlist or {"items": []})
        self.comment_resource = FakeResource(comments or {"items": []})
    def videos(self): return self.video_resource
    def channels(self): return self.channel_resource
    def playlistItems(self): return self.playlist_resource
    def commentThreads(self): return self.comment_resource


class FakeAnalytics:
    def __init__(self, response):
        self.resource = FakeResource(response)
    def reports(self): return type("Reports", (), {"query": self.resource.list})()


class FakeReasonError(Exception):
    def __init__(self, reason, status=403):
        super().__init__("provider message")
        self.status_code = status
        self.error_details = [{"reason": reason}]


class YouTubeReportTests(unittest.TestCase):
    def setUp(self):
        from telegram_bot import lifecycle
        lifecycle.set_state(lifecycle.STATE_RUNNING)

    def test_video_metrics_use_data_api_values(self):
        yt = FakeYouTube(videos={"items": [{"id": "abcdefghijk", "snippet": {"title": "Real title"},
                                            "statistics": {"viewCount": "1200", "likeCount": "31", "commentCount": "7"}}]})
        self.assertEqual(video_metric(yt, "abcdefghijk", "viewCount"), "Real title\nViews: 1,200")
        self.assertEqual(video_metric(yt, "abcdefghijk", "likeCount"), "Real title\nLikes: 31")
        self.assertEqual(video_metric(yt, "abcdefghijk", "commentCount"), "Real title\nComments: 7")
        self.assertEqual(yt.video_resource.calls[0]["part"], "snippet,statistics")

    def test_missing_metrics_are_not_fabricated_and_comment_disabled_is_distinguished(self):
        yt = FakeYouTube(videos={"items": [{"id": "abcdefghijk", "snippet": {"title": "Hidden"}, "statistics": {"viewCount": "0"}}]},
                         comments=FakeRequest(error=FakeReasonError("commentsDisabled")))
        self.assertIn("does not expose the reason", video_metric(yt, "abcdefghijk", "likeCount"))
        with self.assertRaisesRegex(ReportingError, "commentsDisabled"):
            video_metric(yt, "abcdefghijk", "commentCount")

    def test_video_id_is_validated_before_api_access(self):
        yt = FakeYouTube()
        with self.assertRaisesRegex(ReportingError, "11-character"):
            video_metric(yt, "https://youtu.be/abcdefghijk", "viewCount")
        self.assertEqual(yt.video_resource.calls, [])

    def test_analytics_range_and_actual_report_metrics(self):
        report = {"columnHeaders": [{"name": name} for name in
                   ("views", "likes", "comments", "estimatedMinutesWatched", "averageViewDuration")],
                  "rows": [[900, 25, 4, 120.5, 38.2]]}
        analytics = FakeAnalytics(report)
        output = analytics_summary(analytics, "7d", today=date(2026, 10, 18))
        call = analytics.resource.calls[0]
        self.assertEqual(call["ids"], "channel==MINE")
        self.assertEqual(call["startDate"], "2026-10-11")
        self.assertEqual(call["endDate"], "2026-10-17")
        self.assertEqual(call["metrics"], "views,likes,comments,estimatedMinutesWatched,averageViewDuration")
        self.assertIn("Views: 900", output)
        self.assertIn("Likes: 25", output)
        self.assertIn("Comments: 4", output)
        self.assertIn("120.5 min", output)
        with self.assertRaisesRegex(ReportingError, "28d"):
            analytics_summary(analytics, "30d", today=date(2026, 10, 18))

    def test_analytics_no_rows_says_no_data_instead_of_zero(self):
        output = analytics_summary(FakeAnalytics({"columnHeaders": [], "rows": []}), "28d",
                                   today=date(2026, 10, 18))
        self.assertIn("no data returned", output)
        self.assertNotIn("Views: 0", output)

    def test_missing_channel_metric_is_explicitly_unavailable(self):
        output = analytics_summary(FakeAnalytics({"columnHeaders": [{"name": "views"}], "rows": [[3]]}),
                                   "7d", today=date(2026, 10, 18))
        self.assertIn("Likes: unavailable", output)
        self.assertIn("Comments: unavailable", output)

    def test_quota_and_api_disabled_errors_are_actionable(self):
        with self.assertRaisesRegex(ReportingError, "quota/rate limit"):
            video_metric(FakeYouTube(videos=FakeRequest(error=FakeReasonError("quotaExceeded", 403))),
                         "abcdefghijk", "viewCount")
        with self.assertRaisesRegex(ReportingError, "Enable YouTube Data API v3"):
            video_metric(FakeYouTube(videos=FakeRequest(error=FakeReasonError("accessNotConfigured", 403))),
                         "abcdefghijk", "viewCount")

    def test_recent_videos_paginate_uploads_playlist_and_batch_stats(self):
        video_ids = [f"id{i:09d}" for i in range(12)]
        playlist = FakeResource(lambda query: {
            "items": [{"contentDetails": {"videoId": video_id}} for video_id in
                      video_ids[0:10] if query.get("pageToken") is None]
                     or [{"contentDetails": {"videoId": video_id}} for video_id in video_ids[10:]],
            "nextPageToken": "page-2" if query.get("pageToken") is None else None,
        })
        yt = FakeYouTube(channels={"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "uploads-id"}}}]})
        yt.playlist_resource = playlist
        yt.video_resource = FakeResource(lambda query: {"items": [
            {"id": video_id, "snippet": {"title": f"Video {index}"}, "statistics": {"viewCount": str(index)}}
            for index, video_id in enumerate(query["id"].split(","), 1)
        ]})
        output = list_recent_videos(yt, limit=12, page_size=10)
        self.assertEqual(playlist.calls[1]["pageToken"], "page-2")
        self.assertIn("up to 12", output)
        self.assertIn("Video 12 — 12 views", output)
        self.assertEqual(len(yt.video_resource.calls), 1)

    def test_commands_are_allowlisted_and_owner_guarded(self):
        self.assertTrue({"showanalytics", "showviews", "showlikes", "showcomments", "videos"}.issubset(COMMAND_NAMES))
        called = []
        factory = lambda: (called.append("client") or FakeYouTube(), FakeAnalytics({}))
        response = handle_update(owner_update("/showanalytics 14d"), {77}, report_client_factory=factory)[1]
        self.assertIn("Usage: /showanalytics 7d or /showanalytics 28d", response)
        self.assertEqual(called, [])
        denied = handle_update({"message": {"from": {"id": 88}, "chat": {"id": 88, "type": "private"},
                                "text": "/showviews abcdefghijk"}}, {77}, report_client_factory=factory)
        self.assertIsNone(denied)
        self.assertEqual(called, [])

    def test_report_command_calls_injected_api_and_returns_provider_values(self):
        yt = FakeYouTube(videos={"items": [{"id": "abcdefghijk", "snippet": {"title": "Live"},
                                            "statistics": {"viewCount": "42"}}]})
        analytics = FakeAnalytics({"columnHeaders": [{"name": "views"}, {"name": "likes"}, {"name": "comments"}],
                                   "rows": [[200, 9, 1]]})
        factory = lambda: (yt, analytics)
        response = handle_update(owner_update("/showviews abcdefghijk"), {77}, report_client_factory=factory)
        self.assertEqual(response[1], "Live\nViews: 42")
        summary = handle_update(owner_update("/showanalytics 28d"), {77}, report_client_factory=factory)
        self.assertIn("Views: 200", summary[1])
        video_id = "abcdefghijk"
        yt_for_list = FakeYouTube(
            channels={"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "uploads"}}}]},
            playlist={"items": [{"contentDetails": {"videoId": video_id}}]},
            videos={"items": [{"id": video_id, "snippet": {"title": "Latest"}, "statistics": {"viewCount": "3"}}]},
        )
        videos_result = handle_update(owner_update("/videos"), {77},
                                      report_client_factory=lambda: (yt_for_list, None))
        self.assertIn("Latest — 3 views", videos_result[1])

    def test_oauth_errors_return_reauthorization_steps(self):
        yt = FakeYouTube(videos=FakeRequest(error=FakeReasonError("authError", 401)))
        with self.assertRaisesRegex(ReportingError, "browser consent"):
            video_metric(yt, "abcdefghijk", "viewCount")


if __name__ == "__main__":
    unittest.main()
