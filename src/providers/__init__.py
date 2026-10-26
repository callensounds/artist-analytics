"""Collect every public source for one artist. One failure does not blank the others."""

from __future__ import annotations

import asyncio

from analytics import build_insights, build_patterns
from cache import cache_from_env
from http_client import RequestsClient
from models import errored
from providers.apple import fetch_apple
from providers.instagram import fetch_instagram
from providers.musicbrainz import MB_OPEN_TIMEOUT, open_musicbrainz
from providers.spotify import fetch_spotify
from providers.tiktok import fetch_tiktok
from providers.youtube import fetch_youtube

# One slow source must not hold the rest of the dashboard.
PROVIDER_TIMEOUT = 15


def _public_links(links: dict | None) -> dict:
    return {key: value for key, value in (links or {}).items() if not str(key).startswith("_")}


def _has_saved_links(links: dict) -> bool:
    if links.get("spotify_id") or links.get("apple_id") or links.get("instagram") or links.get("tiktok"):
        return True
    if links.get("youtube_channel_ids") or links.get("youtube_handles"):
        return True
    return False


async def _bounded(coro, platform: str, label: str):
    try:
        return await asyncio.wait_for(coro, PROVIDER_TIMEOUT)
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


async def _finish_catalog(rest, partial):
    """Catalog runs beside the other providers. A timeout keeps the artist match."""
    if rest is None:
        partial.links = _public_links(partial.links)
        return partial
    try:
        return await asyncio.wait_for(rest, PROVIDER_TIMEOUT)
    except asyncio.TimeoutError:
        partial.links = _public_links(partial.links)
        partial.notices = list(partial.notices or []) + [
            "The MusicBrainz catalog took too long and was skipped so the rest of the page could load."
        ]
        return partial
    except Exception:
        partial.links = _public_links(partial.links)
        return partial


async def collect_artist(name: str, env=None, http=None, cache=None) -> dict:
    client = http or RequestsClient()
    store = cache or cache_from_env(env)
    # Identity (search + URL relations) has to finish first. The catalog does not.
    try:
        opened = await asyncio.wait_for(open_musicbrainz(name, client, store), MB_OPEN_TIMEOUT)
    except asyncio.TimeoutError:
        opened = (
            errored(
                "musicbrainz",
                "MusicBrainz",
                "This source took too long and was skipped so the rest of the page could load.",
            ),
            None,
        )
    except Exception:
        opened = (errored("musicbrainz", "MusicBrainz", "MusicBrainz failed before returning a result."), None)
    if not isinstance(opened, tuple) or len(opened) != 2 or opened[0] is None:
        opened = (errored("musicbrainz", "MusicBrainz", "MusicBrainz failed before returning a result."), None)
    partial, rest = opened
    links = dict(partial.links or {})
    # A MusicBrainz error with no saved URLs blocks Spotify, Instagram, and TikTok.
    # Cached relations, or a Spotify id already on the links, still let those cards run.
    if partial.status != "ok" and not _has_saved_links(links):
        links["_incomplete"] = True

    musicbrainz, apple, spotify, youtube, instagram, tiktok = await asyncio.gather(
        _finish_catalog(rest, partial),
        _bounded(fetch_apple(name, links, client, store), "apple", "Apple Music"),
        _bounded(fetch_spotify(name, links, env, client, store), "spotify", "Spotify"),
        _bounded(fetch_youtube(name, links, env, client, store), "youtube", "YouTube"),
        _bounded(fetch_instagram(name, links, client, store), "instagram", "Instagram"),
        _bounded(fetch_tiktok(name, links, client, store), "tiktok", "TikTok"),
    )
    sources = [musicbrainz]
    for platform, label, result in (
        ("apple", "Apple Music", apple),
        ("spotify", "Spotify", spotify),
        ("youtube", "YouTube", youtube),
        ("instagram", "Instagram", instagram),
        ("tiktok", "TikTok", tiktok),
    ):
        sources.append(result or errored(platform, label, "The source failed before returning a result."))
    payload = [source.to_dict() for source in sources]
    return {
        "sources": payload,
        "patterns": build_patterns(payload),
        "insights": build_insights(payload),
    }
