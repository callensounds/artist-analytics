"""Collect every public source for one artist. One failure does not blank the others."""

from __future__ import annotations

import asyncio

from analytics import build_insights, build_patterns
from cache import cache_from_env
from http_client import RequestsClient
from models import errored
from providers.apple import fetch_apple
from providers.instagram import fetch_instagram
from providers.musicbrainz import fetch_musicbrainz
from providers.spotify import fetch_spotify
from providers.tiktok import fetch_tiktok
from providers.youtube import fetch_youtube


async def collect_artist(name: str, env=None, http=None, cache=None) -> dict:
    client = http or RequestsClient()
    store = cache or cache_from_env(env)
    musicbrainz = await fetch_musicbrainz(name, client, store)
    links = dict(musicbrainz.links or {})
    if musicbrainz.status != "ok":
        links["_incomplete"] = True

    async def run(fetcher):
        try:
            return await fetcher()
        except Exception:
            return None

    apple, spotify, youtube, instagram, tiktok = await asyncio.gather(
        run(lambda: fetch_apple(name, links, client, store)),
        run(lambda: fetch_spotify(name, links, env, client, store)),
        run(lambda: fetch_youtube(name, links, env, client, store)),
        run(lambda: fetch_instagram(name, links, client, store)),
        run(lambda: fetch_tiktok(name, links, client, store)),
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
