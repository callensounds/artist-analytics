"""YouTube upload timestamps from the Data API. publishedAt is exact to the second."""

from __future__ import annotations

from urllib.parse import quote, urlencode

from analytics import compact_count, names_match, scrub
from http_client import TransportError
from models import Metric, SourceResult, errored, make_item, missing_credentials, sort_items, unavailable
from providers.common import cache_key, cached
from runtime import env_get

API = "https://www.googleapis.com/youtube/v3"
NAME_MATCH = "Matched by name via the YouTube Data API, not from a MusicBrainz URL."


def youtube_url(path: str, params: dict) -> str:
    """Query string uses the key as a plain Python str. quote, not quote_plus."""
    return f"{API}/{path}?{urlencode(params, quote_via=quote)}"


def _message(payload, status: int, secret: str) -> str:
    text = f"YouTube returned HTTP {status}."
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            text = str(error["message"])
    return scrub(text, [secret])


def redact_key(url: str, secret: str | None) -> str:
    """Replace the key query value with its prefix and length."""
    if not secret:
        return url
    marker = f"{secret[:4]}…({len(secret)})"
    redacted = url.replace(secret, marker)
    from urllib.parse import quote

    encoded = quote(secret, safe="")
    if encoded and encoded != secret:
        redacted = redacted.replace(encoded, marker)
    return redacted


async def _get(http, path: str, params: dict, secret: str):
    # The key goes in a header. A query-string key was still rejected in
    # production after the value was read, so the header is what we send.
    query = {key: value for key, value in params.items() if key != "key"}
    url = youtube_url(path, query)
    try:
        result, payload = await http.get_json(
            url,
            timeout=8,
            headers={"X-Goog-Api-Key": secret},
        )
    except TransportError as exc:
        raise TransportError(str(exc)) from exc
    if not result.ok or not isinstance(payload, dict):
        message = _message(payload, result.status, secret)
        if "API key not valid" in message:
            message += " A logged-in check of the key string is at /admin/diag."
        raise TransportError(message)
    return payload


def parse_videos(payload: dict) -> list:
    items = []
    for row in payload.get("items") or []:
        snippet = row.get("snippet") or {}
        content = row.get("contentDetails") or {}
        title = snippet.get("title") or ""
        if title in {"Private video", "Deleted video"}:
            continue
        video_id = content.get("videoId") or (snippet.get("resourceId") or {}).get("videoId")
        raw = content.get("videoPublishedAt") or snippet.get("publishedAt") or ""
        item = make_item(
            title=title,
            kind="video",
            raw=raw,
            url=f"https://www.youtube.com/watch?v={video_id}" if video_id else None,
        )
        if item is not None:
            items.append(item)
    return sort_items(items)


def choose_channel(channels: list[dict]) -> dict | None:
    best = None
    best_score = -1
    for channel in channels:
        title = ((channel.get("snippet") or {}).get("title") or "").casefold()
        subscribers = int(((channel.get("statistics") or {}).get("subscriberCount") or 0))
        penalty = 0
        if title.endswith(" - topic") or title.endswith("vevo"):
            penalty = 10**12
        score = subscribers - penalty
        if score > best_score:
            best = channel
            best_score = score
    return best


async def fetch_youtube(name: str, links: dict, env, http, cache) -> SourceResult:
    api_key = env_get(env, "YOUTUBE_API_KEY")
    if not api_key:
        return missing_credentials(
            "youtube",
            "YouTube",
            "Set the Worker secret YOUTUBE_API_KEY. Public upload times use the Data API; no user OAuth.",
        )
    channel_ids = list((links or {}).get("youtube_channel_ids") or [])
    handles = list((links or {}).get("youtube_handles") or [])

    async def produce() -> SourceResult:
        matched_by_name = False
        try:
            channel = None
            if channel_ids:
                payload = await _get(
                    http,
                    "channels",
                    {
                        "part": "snippet,statistics,contentDetails",
                        "id": ",".join(channel_ids[:10]),
                        "key": api_key,
                        "maxResults": "10",
                    },
                    api_key,
                )
                channel = choose_channel(payload.get("items") or [])
            elif handles:
                payload = await _get(
                    http,
                    "channels",
                    {
                        "part": "snippet,statistics,contentDetails",
                        "forHandle": handles[0],
                        "key": api_key,
                    },
                    api_key,
                )
                channel = (payload.get("items") or [None])[0]
            else:
                search = await _get(
                    http,
                    "search",
                    {
                        "part": "snippet",
                        "type": "channel",
                        "q": name,
                        "maxResults": "3",
                        "key": api_key,
                    },
                    api_key,
                )
                found = None
                for row in search.get("items") or []:
                    title = (row.get("snippet") or {}).get("title") or ""
                    if names_match(title, name):
                        found = (row.get("id") or {}).get("channelId")
                        break
                if not found:
                    return unavailable(
                        "youtube",
                        "YouTube",
                        "No YouTube channel title matched that artist closely enough to chart.",
                    )
                matched_by_name = True
                payload = await _get(
                    http,
                    "channels",
                    {
                        "part": "snippet,statistics,contentDetails",
                        "id": found,
                        "key": api_key,
                    },
                    api_key,
                )
                channel = (payload.get("items") or [None])[0]
            if not channel:
                return unavailable("youtube", "YouTube", "YouTube did not return a channel.")
            uploads = (
                ((channel.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
            )
            if not uploads:
                return unavailable("youtube", "YouTube", "The channel has no public uploads playlist.")
            videos = await _get(
                http,
                "playlistItems",
                {
                    "part": "snippet,contentDetails",
                    "playlistId": uploads,
                    "maxResults": "50",
                    "key": api_key,
                },
                api_key,
            )
        except TransportError as exc:
            return errored("youtube", "YouTube", str(exc))
        items = parse_videos(videos)
        stats = channel.get("statistics") or {}
        hidden = stats.get("hiddenSubscriberCount")
        subscribers = None if hidden else compact_count(stats.get("subscriberCount"))
        title = (channel.get("snippet") or {}).get("title")
        channel_id = channel.get("id")
        notices = [
            "Times are videoPublishedAt when YouTube sends it, otherwise snippet.publishedAt. "
            "Both are UTC to the second. The chart uses the 50 most recent public uploads."
        ]
        if matched_by_name:
            notices.append(NAME_MATCH)
        return SourceResult(
            platform="youtube",
            label="YouTube",
            status="ok",
            profile_name=title,
            profile_url=f"https://www.youtube.com/channel/{channel_id}" if channel_id else None,
            metrics=[
                Metric(
                    label="Subscribers",
                    available=bool(subscribers),
                    value=subscribers,
                    reason=None if subscribers else "The channel hides its subscriber count.",
                ),
                Metric(label="Recent uploads", available=True, value=str(len(items))),
            ],
            items=items,
            notices=notices,
        )

    return await cached(cache, cache_key("youtube", name, ",".join(channel_ids), ",".join(handles)), produce)
