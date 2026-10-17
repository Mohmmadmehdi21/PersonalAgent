"""Read-only YouTube Data and Analytics API reporting helpers."""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from retry import classify_error, retry_google_read, youtube_error_reasons

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
REPORT_TIMEZONE = ZoneInfo("America/Los_Angeles")


class ReportingError(RuntimeError):
    pass


def _public_error(exc):
    original = exc
    chain = []
    current = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = getattr(current, "__cause__", None)
    original = next((item for item in reversed(chain)
                     if getattr(item, "error_details", None) or getattr(item, "content", None)
                     or getattr(item, "status_code", None) or getattr(item, "resp", None)), exc)
    classification = classify_error(original, service="youtube", operation="read")
    reasons = {reason.lower() for item in chain for reason in youtube_error_reasons(item)}
    text = " ".join(str(item).lower() for item in chain)
    if classification.category == "authentication":
        return ReportingError("YouTube read authorization is missing or expired. Run the report command again and complete the browser consent for token_growth.json.")
    if classification.category in {"youtube_quota", "rate_limited"} or reasons & {"quotaexceeded", "dailylimitexceeded", "userratelimitexceeded"}:
        return ReportingError("YouTube API quota/rate limit reached. Try again after the quota window resets.")
    if reasons & {"accessnotconfigured", "servicedisabled"} or "has not been used" in text:
        return ReportingError("Enable YouTube Data API v3 and YouTube Analytics API in the OAuth Google Cloud project, then retry.")
    if "insufficientpermissions" in reasons or "forbidden" in reasons:
        return ReportingError("This OAuth account lacks access to this channel/report. Check channel ownership and read-only consent.")
    if "commentsdisabled" in reasons:
        return ReportingError("Comments are disabled for this video (YouTube API reason: commentsDisabled).")
    if classification.category == "model_not_found":
        return ReportingError("The requested YouTube resource was not found.")
    return ReportingError("YouTube reporting request failed. Check API enablement, OAuth permissions, and local logs.")


def _read(call, task):
    try:
        return retry_google_read(call, task=task)
    except Exception as exc:
        raise _public_error(exc) from None


def _video_resource(youtube, video_id):
    if not VIDEO_ID_RE.fullmatch(video_id or ""):
        raise ReportingError("Video ID must be the 11-character YouTube ID (not a full URL).")
    response = _read(lambda: youtube.videos().list(part="snippet,statistics", id=video_id).execute(),
                     "YouTube video statistics")
    items = response.get("items", [])
    if not items:
        raise ReportingError("Video was not returned by YouTube; it may be private to another account, deleted, or the ID may be invalid.")
    return items[0]


def video_metric(youtube, video_id, metric):
    if metric not in {"viewCount", "likeCount", "commentCount"}:
        raise ReportingError("Unsupported video statistic.")
    item = _video_resource(youtube, video_id)
    stats = item.get("statistics", {})
    title = item.get("snippet", {}).get("title", "Untitled")
    if metric in stats:
        labels = {"viewCount": "Views", "likeCount": "Likes", "commentCount": "Comments"}
        return f"{title}\n{labels[metric]}: {int(stats[metric]):,}"
    if metric == "commentCount":
        # This distinguishes the documented commentsDisabled API error where the API exposes it.
        try:
            _read(lambda: youtube.commentThreads().list(part="id", videoId=video_id, maxResults=1).execute(),
                  "YouTube comment availability")
        except ReportingError as exc:
            if "commentsDisabled" in str(exc):
                raise
            return f"{title}\nComments: unavailable; YouTube omitted commentCount and did not provide a more specific reason."
    if metric == "likeCount":
        return f"{title}\nLikes: unavailable; YouTube omitted likeCount and does not expose the reason in this response."
    return f"{title}\nViews: unavailable; YouTube did not return viewCount."


def analytics_summary(analytics, period, *, today=None):
    if period not in {"7d", "28d"}:
        raise ReportingError("Use /showanalytics 7d or /showanalytics 28d.")
    days = int(period[:-1])
    today = today or datetime.now(REPORT_TIMEZONE).date()
    end_date = today - timedelta(days=1)
    start_date = end_date - timedelta(days=days - 1)
    params = {
        "ids": "channel==MINE",
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "metrics": "views,likes,comments,estimatedMinutesWatched,averageViewDuration",
    }
    response = _read(lambda: analytics.reports().query(**params).execute(), "YouTube Analytics report")
    headers = [header.get("name") for header in response.get("columnHeaders", [])]
    rows = response.get("rows", [])
    if not rows:
        return (f"Channel analytics ({period}, {start_date} to {end_date}): no data returned. "
                "The latest dates can lag while YouTube finalizes metric availability.")
    row = rows[0]
    values = dict(zip(headers, row))
    lines = [f"Channel analytics — {period} ({start_date} to {end_date}, Pacific dates)"]
    labels = {
        "views": ("Views", "int", ""), "likes": ("Likes", "int", ""),
        "comments": ("Comments", "int", ""),
        "estimatedMinutesWatched": ("Watch time", "float", " min"),
        "averageViewDuration": ("Avg. view duration", "float", " sec"),
    }
    for name, (label, number_type, suffix) in labels.items():
        if name in values and values[name] is not None:
            value = float(values[name])
            rendered = format(int(value), ",d") if number_type == "int" else format(value, ".1f")
            lines.append(f"{label}: {rendered}{suffix}")
        else:
            lines.append(f"{label}: unavailable; YouTube omitted this metric from the report without a more specific reason.")
    lines.append("Note: YouTube may omit the most recent dates until all metrics are available.")
    return "\n".join(lines)


def list_recent_videos(youtube, *, limit=20, page_size=10):
    """Use uploads-playlist pagination, then batch video details/statistics."""
    try:
        channels = _read(lambda: youtube.channels().list(part="contentDetails", mine=True, maxResults=1).execute(),
                         "YouTube channel lookup")
        channel_items = channels.get("items", [])
        if not channel_items:
            raise ReportingError("The authorized account has no accessible YouTube channel.")
        playlist_id = channel_items[0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        if not playlist_id:
            raise ReportingError("YouTube did not return an uploads playlist for this channel.")
        ids = []
        token = None
        while len(ids) < limit:
            params = {"part": "contentDetails,snippet", "playlistId": playlist_id,
                      "maxResults": min(page_size, limit - len(ids))}
            if token:
                params["pageToken"] = token
            page = _read(lambda params=params: youtube.playlistItems().list(**params).execute(),
                         "YouTube uploads playlist")
            for item in page.get("items", []):
                video_id = item.get("contentDetails", {}).get("videoId")
                if video_id:
                    ids.append(video_id)
            token = page.get("nextPageToken")
            if not token or not page.get("items"):
                break
        if not ids:
            return "No channel videos were returned."
        details = _read(lambda: youtube.videos().list(part="snippet,statistics", id=",".join(ids[:50])).execute(),
                        "YouTube recent video statistics")
        by_id = {item.get("id"): item for item in details.get("items", [])}
        lines = [f"Recent channel videos (up to {limit}):"]
        for index, video_id in enumerate(ids, 1):
            item = by_id.get(video_id)
            if not item:
                lines.append(f"{index}. {video_id}: details unavailable")
                continue
            title = str(item.get("snippet", {}).get("title", "Untitled")).replace("\n", " ")[:80]
            stats = item.get("statistics", {})
            views = f"{int(stats['viewCount']):,}" if "viewCount" in stats else "unavailable"
            lines.append(f"{index}. {title} — {views} views (ID: {video_id})")
        return "\n".join(lines)
    except ReportingError:
        raise
    except Exception as exc:
        raise _public_error(exc) from None
