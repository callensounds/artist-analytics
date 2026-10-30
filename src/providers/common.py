"""Cache wrapper and link parsing shared by providers."""

from __future__ import annotations

import json
import re

from models import SourceResult, source_from_dict


# Bump when a cached error must not outlive a code fix.
CACHE_VERSION = "v3"


async def cached(cache, key: str, producer) -> SourceResult:
    if cache is not None:
        raw = await cache.get(key)
        if raw:
            try:
                stored = source_from_dict(json.loads(raw))
            except (json.JSONDecodeError, KeyError, TypeError):
                stored = None
            # Errors and unavailable cards used to sit in this cache for 45 minutes.
            if stored is not None and stored.status == "ok":
                return stored
    result = await producer()
    if cache is not None and result.cacheable and result.status == "ok":
        try:
            await cache.set(key, json.dumps(result.to_dict()), result.ttl)
        except Exception:
            pass
    return result


def script_body(html: str, marker: str) -> str | None:
    """The text inside the first <script> whose tag contains marker. Linear scan, no regex."""
    if not html or not marker:
        return None
    at = html.find(marker)
    if at < 0:
        return None
    start = html.find(">", at)
    if start < 0 or start - at > 400:
        return None
    end = html.find("</script>", start)
    if end < 0:
        return None
    return html[start + 1 : end]


def drop_blocks(text: str, start_tag: str, end_tag: str) -> str:
    """Remove start_tag...end_tag regions with str.find. Case-insensitive, non-overlapping."""
    if not text:
        return ""
    start_l = start_tag.lower()
    end_l = end_tag.lower()
    # Spotify's shelf is already lowercase. Avoid copying an 80KB string just to search it.
    if start_tag.islower() and start_tag.upper() not in text and start_tag.title() not in text:
        lowered = text
    else:
        lowered = text.lower()
    parts = []
    cursor = 0
    while cursor < len(text):
        start = lowered.find(start_l, cursor)
        if start < 0:
            parts.append(text[cursor:])
            break
        parts.append(text[cursor:start])
        end = lowered.find(end_l, start + len(start_l))
        if end < 0:
            break
        cursor = end + len(end_l)
    return "".join(parts)


def strip_tags(text: str) -> str:
    """Replace tags with newlines. The scan stops at the next '>' so it stays linear."""
    if not text:
        return ""
    parts = []
    cursor = 0
    while cursor < len(text):
        start = text.find("<", cursor)
        if start < 0:
            parts.append(text[cursor:])
            break
        parts.append(text[cursor:start])
        end = text.find(">", start + 1)
        if end < 0:
            break
        parts.append("\n")
        cursor = end + 1
    return "".join(parts)


def apple_artist_id(url: str) -> str | None:
    match = re.search(r"(?:music|itunes)\.apple\.com/.*/artist/(?:[^/]+/)?(\d+)", url or "")
    return match.group(1) if match else None


def spotify_artist_id(url: str) -> str | None:
    match = re.search(r"open\.spotify\.com/artist/([A-Za-z0-9]+)", url or "")
    return match.group(1) if match else None


def youtube_channel_id(url: str) -> str | None:
    if "music.youtube.com" in (url or ""):
        return None
    match = re.search(r"youtube\.com/channel/([A-Za-z0-9_-]+)", url or "")
    return match.group(1) if match else None


def youtube_handle(url: str) -> str | None:
    match = re.search(r"youtube\.com/@([A-Za-z0-9._-]+)", url or "")
    return match.group(1) if match else None


def instagram_handle(url: str) -> str | None:
    match = re.search(r"instagram\.com/([A-Za-z0-9._]+)/?", url or "")
    if not match:
        return None
    handle = match.group(1)
    if handle.lower() in {"p", "reel", "reels", "explore", "accounts", "embed", "stories"}:
        return None
    return handle


def tiktok_handle(url: str) -> str | None:
    match = re.search(r"tiktok\.com/@([A-Za-z0-9._]+)", url or "")
    return match.group(1) if match else None


def cache_key(platform: str, *parts: str) -> str:
    slug = ":".join(part.strip().casefold() for part in parts if part)
    return f"aa:{CACHE_VERSION}:{platform}:{slug or 'none'}"
