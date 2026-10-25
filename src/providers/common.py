"""Cache wrapper and link parsing shared by providers."""

from __future__ import annotations

import json
import re

from models import SourceResult, source_from_dict


async def cached(cache, key: str, producer) -> SourceResult:
    if cache is not None:
        raw = await cache.get(key)
        if raw:
            try:
                return source_from_dict(json.loads(raw))
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
    result = await producer()
    if cache is not None and result.cacheable:
        try:
            await cache.set(key, json.dumps(result.to_dict()), result.ttl)
        except Exception:
            pass
    return result


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
    return f"aa:v1:{platform}:{slug or 'none'}"
