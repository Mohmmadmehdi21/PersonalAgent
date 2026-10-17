import re
from urllib.parse import parse_qs, unquote, urlparse

import yt_dlp
from retry import retry_google_read


def is_channel_url(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host != "youtube.com" and not host.endswith(".youtube.com"):
        return False
    path = unquote(parsed.path).strip("/")
    first = path.split("/", 1)[0]
    return first.startswith("@") or first in ("channel", "user", "c")


def video_id_from_url(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path_parts = [part for part in unquote(parsed.path).split("/") if part]

    if host in ("youtu.be", "www.youtu.be") and path_parts:
        candidate = path_parts[0]
    elif parsed.path == "/watch":
        candidate = parse_qs(parsed.query).get("v", [""])[0]
    elif len(path_parts) >= 2 and path_parts[0] in ("shorts", "embed", "live"):
        candidate = path_parts[1]
    else:
        return None

    return candidate if re.fullmatch(r"[\w-]{11}", candidate) else None


def resolve_channel_id(youtube, url):
    parsed = urlparse(url)
    path = unquote(parsed.path).strip("/")
    parts = path.split("/")
    first = parts[0] if parts else ""

    if first == "channel" and len(parts) > 1:
        return parts[1]
    if first.startswith("@"):
        response = retry_google_read(
            lambda: youtube.channels().list(part="id", forHandle=first[1:]).execute(),
            task="YouTube channel lookup",
        )
    elif first == "user" and len(parts) > 1:
        response = retry_google_read(
            lambda: youtube.channels().list(part="id", forUsername=parts[1]).execute(),
            task="YouTube channel lookup",
        )
    else:
        # Resolve older custom /c/ channel links through yt-dlp metadata.
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True}) as ydl:
            info = ydl.extract_info(url, download=False)
        channel_id = (info or {}).get("channel_id") or (info or {}).get("id")
        if channel_id and re.fullmatch(r"UC[\w-]{22}", channel_id):
            return channel_id
        raise ValueError(f"Channel ID resolve nahi hua: {url}")

    items = response.get("items", [])
    if not items:
        raise ValueError(f"YouTube channel nahi mila: {url}")
    return items[0]["id"]


def find_popular_videos(youtube, channel_url, processed_video_ids, max_candidates=5):
    """Among batches of newest videos, pick up to max_candidates most viewed unprocessed ones."""
    channel_id = resolve_channel_id(youtube, channel_url)
    page_token = None
    candidates = []
    seen_ids = set(processed_video_ids)

    # At most 500 candidates per run. Pages are newest-first; views rank each page.
    for _ in range(10):
        if len(candidates) >= max_candidates:
            break
        response = retry_google_read(lambda: youtube.search().list(
            part="snippet",
            channelId=channel_id,
            type="video",
            order="date",
            maxResults=50,
            pageToken=page_token,
        ).execute(), task="YouTube source search")
        search_items = [
            item for item in response.get("items", [])
            if item.get("id", {}).get("videoId")
            and item["id"]["videoId"] not in seen_ids
        ]

        if search_items:
            ids = ",".join(item["id"]["videoId"] for item in search_items)
            videos = retry_google_read(lambda: youtube.videos().list(
                part="statistics,snippet", id=ids
            ).execute(), task="YouTube source statistics").get("items", [])
            if videos:
                videos.sort(
                    key=lambda video: (
                        int(video.get("statistics", {}).get("viewCount", 0)),
                        video.get("snippet", {}).get("publishedAt", ""),
                    ),
                    reverse=True,
                )
                for v in videos:
                    vid = v["id"]
                    if vid not in seen_ids:
                        seen_ids.add(vid)
                        title = v.get("snippet", {}).get("title", "(untitled)")
                        views = v.get("statistics", {}).get("viewCount", "unknown")
                        candidates.append(f"https://www.youtube.com/watch?v={vid}")
                        print(f"[Source] Candidate video selected: {title} ({views} views)")
                        if len(candidates) >= max_candidates:
                            break

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return candidates


def find_popular_video(youtube, channel_url, processed_video_ids):
    candidates = find_popular_videos(youtube, channel_url, processed_video_ids, max_candidates=1)
    return candidates[0] if candidates else None
