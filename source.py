import re
from urllib.parse import unquote, urlparse

import yt_dlp


def is_channel_url(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host != "youtube.com" and not host.endswith(".youtube.com"):
        return False
    path = unquote(parsed.path).strip("/")
    first = path.split("/", 1)[0]
    return first.startswith("@") or first in ("channel", "user", "c")


def resolve_channel_id(youtube, url):
    parsed = urlparse(url)
    path = unquote(parsed.path).strip("/")
    parts = path.split("/")
    first = parts[0] if parts else ""

    if first == "channel" and len(parts) > 1:
        return parts[1]

    if first.startswith("@"):
        response = youtube.channels().list(
            part="id", forHandle=first[1:]
        ).execute()
    elif first == "user" and len(parts) > 1:
        response = youtube.channels().list(
            part="id", forUsername=parts[1]
        ).execute()
    else:
        # Legacy /c/ channel URLs are not directly resolvable through channels.list.
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


def find_popular_video(youtube, channel_url, processed_video_ids):
    """Return the highest-viewed unprocessed video among the channel's search results."""
    channel_id = resolve_channel_id(youtube, channel_url)
    page_token = None
    # Search at most 10 pages per run so an old channel does not consume the whole quota.
    for _ in range(10):
        request = youtube.search().list(
            part="snippet",
            channelId=channel_id,
            type="video",
            order="viewCount",
            maxResults=50,
            pageToken=page_token,
        )
        response = request.execute()

        for item in response.get("items", []):
            video_id = item.get("id", {}).get("videoId")
            if video_id and video_id not in processed_video_ids:
                title = item.get("snippet", {}).get("title", "(untitled)")
                print(f"[Source] Popular video selected: {title} ({video_id})")
                return f"https://www.youtube.com/watch?v={video_id}"

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return None
