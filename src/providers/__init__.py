"""Collect every public source for one artist. One failure does not blank the others."""

from __future__ import annotations

import asyncio

from analytics import build_insights, build_patterns
from cache import cache_from_env
from http_client import HttpClient
from models import errored
from providers.apple import fetch_apple
from providers.instagram import fetch_instagram
from providers.musicbrainz import MB_OPEN_TIMEOUT, open_musicbrainz, timed_out
from providers.spotify import fetch_spotify
from providers.tiktok import fetch_tiktok
from providers.youtube import fetch_youtube

# The whole dashboard answers within about 10s. Rendering gets the last half second.
DASHBOARD_DEADLINE = 9.5
# No single source gets more than this, even when the deadline would allow it.
PROVIDER_TIMEOUT = 8.0
RENDER_MARGIN = 0.4


def _public_links(links: dict | None) -> dict:
    return {key: value for key, value in (links or {}).items() if not str(key).startswith("_")}


def _has_saved_links(links: dict) -> bool:
    if links.get("spotify_id") or links.get("apple_id") or links.get("instagram") or links.get("tiktok"):
        return True
    if links.get("youtube_channel_ids") or links.get("youtube_handles"):
        return True
    return False


def _budget(deadline: float | None, cap: float = PROVIDER_TIMEOUT) -> float:
    if deadline is None:
        return cap
    left = deadline - asyncio.get_running_loop().time() - RENDER_MARGIN
    return max(0.05, min(cap, left))


async def _bounded(coro, platform: str, label: str, deadline: float | None = None):
    try:
        return await asyncio.wait_for(coro, _budget(deadline))
    except asyncio.TimeoutError:
        return errored(
            platform,
            label,
            "This source took too long and was skipped so the rest of the page could load.",
        )
    except MemoryError:
        return errored(
            platform,
            label,
            "This source used too much memory and was skipped so the rest of the page could load.",
        )
    except Exception:
        return errored(platform, label, "This source failed and was skipped so the rest of the page could load.")


async def _finish_catalog(rest, partial, deadline: float | None = None):
    """Catalog runs beside the other providers. A timeout keeps the artist match."""
    if rest is None:
        partial.links = _public_links(partial.links)
        return partial
    try:
        return await asyncio.wait_for(rest, _budget(deadline))
    except asyncio.TimeoutError:
        partial.links = _public_links(partial.links)
        partial.notices = list(partial.notices or []) + [
            "The MusicBrainz catalog took too long and was skipped so the rest of the page could load."
        ]
        return partial
    except Exception:
        partial.links = _public_links(partial.links)
        return partial


async def collect_artist(
    name: str,
    env=None,
    http=None,
    cache=None,
    known_links=None,
    deadline_s: float = DASHBOARD_DEADLINE,
) -> dict:
    deadline = asyncio.get_running_loop().time() + deadline_s
    client = http or HttpClient(deadline=deadline - RENDER_MARGIN)
    store = cache or cache_from_env(env)
    # Identity (search + URL relations) has to finish first. The catalog does not.
    try:
        opened = await asyncio.wait_for(open_musicbrainz(name, client, store), _budget(deadline, MB_OPEN_TIMEOUT))
    except asyncio.TimeoutError:
        opened = (await timed_out(name, store), None)
    except Exception:
        opened = (errored("musicbrainz", "MusicBrainz", "MusicBrainz failed before returning a result."), None)
    if not isinstance(opened, tuple) or len(opened) != 2 or opened[0] is None:
        opened = (errored("musicbrainz", "MusicBrainz", "MusicBrainz failed before returning a result."), None)
    partial, rest = opened
    links = dict(partial.links or {})
    # Ids already stored for this artist fill gaps. They are not guessed.
    for key, value in (known_links or {}).items():
        if value and not links.get(key):
            links[key] = value
    # A MusicBrainz error with no saved URLs blocks Spotify, Instagram, and TikTok.
    # Cached relations, or a Spotify id already on the links, still let those cards run.
    if partial.status != "ok" and not _has_saved_links(links):
        links["_incomplete"] = True

    musicbrainz, apple, spotify, youtube, instagram, tiktok = await asyncio.gather(
        _finish_catalog(rest, partial, deadline),
        _bounded(fetch_apple(name, links, client, store), "apple", "Apple Music", deadline),
        _bounded(fetch_spotify(name, links, env, client, store), "spotify", "Spotify", deadline),
        _bounded(fetch_youtube(name, links, env, client, store), "youtube", "YouTube", deadline),
        _bounded(fetch_instagram(name, links, client, store), "instagram", "Instagram", deadline),
        _bounded(fetch_tiktok(name, links, client, store), "tiktok", "TikTok", deadline),
        return_exceptions=True,
    )
    if isinstance(musicbrainz, BaseException) or musicbrainz is None:
        partial.links = _public_links(partial.links)
        musicbrainz = partial
    sources = [musicbrainz]
    for platform, label, result in (
        ("apple", "Apple Music", apple),
        ("spotify", "Spotify", spotify),
        ("youtube", "YouTube", youtube),
        ("instagram", "Instagram", instagram),
        ("tiktok", "TikTok", tiktok),
    ):
        if isinstance(result, BaseException) or result is None:
            result = errored(platform, label, "The source failed before returning a result.")
        sources.append(result)
    payload = [source.to_dict() for source in sources]
    return {
        "sources": payload,
        "patterns": build_patterns(payload),
        "insights": build_insights(payload),
    }
