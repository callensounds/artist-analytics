"""Collect every public source for one artist. One failure does not blank the others."""

from __future__ import annotations

import asyncio

from analytics import build_insights, build_patterns
from cache import cache_from_env
from http_client import HttpClient
from models import SourceResult, errored
from providers.apple import fetch_apple
from providers.instagram import fetch_instagram
from providers.musicbrainz import MB_CRAWL_MAX_QUEUE, MB_CRAWL_OPEN_TIMEOUT, MB_OPEN_TIMEOUT, open_musicbrainz, timed_out
from providers.spotify import fetch_spotify
from providers.tiktok import fetch_tiktok
from providers.youtube import fetch_youtube

# The whole dashboard answers within about 10s. Rendering gets the last half second.
DASHBOARD_DEADLINE = 9.5
# The queue consumer is the only request on its invocation, so MusicBrainz can wait
# out a dashboard burst instead of giving up at the page budget.
CRAWL_DEADLINE = 28.0
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


def _skipped(platform: str, label: str) -> SourceResult:
    return SourceResult(
        platform=platform,
        label=label,
        status="ok",
        notices=["Skipped; a snapshot from the last few hours is already stored."],
        cacheable=False,
    )


async def _passthrough(value):
    return value


async def _links_from_database(env, name: str) -> dict:
    """One indexed artists read. Used when MusicBrainz did not return URL relations."""
    if env is None or not name:
        return {}
    # Imported lazily: crawl imports collect_artist while this module is loading.
    from crawl import known_links, open_database

    database = None
    try:
        database = await open_database(env, migrate_schema=False)
    except Exception:
        database = None
    if database is None:
        return {}
    row = None
    try:
        from analytics import normalize_name

        row = await database.query_one(
            """
            SELECT spotify_id, apple_id, youtube_channel_id, instagram_handle, tiktok_handle
            FROM artists WHERE normalized_name = ?
            """,
            (normalize_name(name),),
        )
    except Exception:
        row = None
    if not row:
        return {}
    return known_links(row)


async def collect_artist(
    name: str,
    env=None,
    http=None,
    cache=None,
    known_links=None,
    deadline_s: float = DASHBOARD_DEADLINE,
    include_related: bool = False,
    skip_platforms=None,
) -> dict:
    client = http or HttpClient()
    light = bool(getattr(client, "crawl_light", False))
    if light and deadline_s == DASHBOARD_DEADLINE:
        deadline_s = CRAWL_DEADLINE
    deadline = asyncio.get_running_loop().time() + deadline_s
    if http is None:
        client.deadline = deadline - RENDER_MARGIN
    elif light and getattr(client, "deadline", None) is None:
        try:
            client.deadline = deadline - RENDER_MARGIN
        except Exception:
            pass
    if light:
        try:
            if getattr(client, "mb_open_timeout", None) is None:
                client.mb_open_timeout = MB_CRAWL_OPEN_TIMEOUT
            if getattr(client, "mb_max_queue", None) is None:
                client.mb_max_queue = MB_CRAWL_MAX_QUEUE
        except Exception:
            pass
    store = cache or cache_from_env(env)
    skipped = {platform for platform in (skip_platforms or set()) if platform}
    open_cap = MB_CRAWL_OPEN_TIMEOUT if light else MB_OPEN_TIMEOUT
    # Identity (search + URL relations) has to finish first. The catalog does not.
    if "musicbrainz" in skipped:
        partial, rest = _skipped("musicbrainz", "MusicBrainz"), None
    else:
        # Await the timeout fallback only after TimeoutError has finished propagating.
        # timed_out() reads the cache, and that read is a JS promise on Workers.
        opened = None
        timed_out_open = False
        failed_open = False
        try:
            opened = await asyncio.wait_for(open_musicbrainz(name, client, store), _budget(deadline, open_cap))
        except asyncio.TimeoutError:
            timed_out_open = True
        except Exception:
            failed_open = True
        if timed_out_open:
            opened = (await timed_out(name, store), None)
        elif failed_open or not isinstance(opened, tuple) or len(opened) != 2 or opened[0] is None:
            opened = (errored("musicbrainz", "MusicBrainz", "MusicBrainz failed before returning a result."), None)
        partial, rest = opened
        # The release catalog is a large JSON parse. Audience snapshots do not need it.
        if light and rest is not None:
            rest.close()
            rest = None
    links = dict(partial.links or {})
    # Ids already stored for this artist fill gaps. They are not guessed.
    for key, value in (known_links or {}).items():
        if value and not links.get(key):
            links[key] = value
    # A MusicBrainz timeout used to mark Spotify, Instagram, and TikTok incomplete
    # whenever the cache had no URL relations. The artists row is the same ids,
    # stored from an earlier crawl, and one indexed read recovers them.
    if partial.status != "ok" and not _has_saved_links(links):
        stored = await _links_from_database(env, name)
        for key, value in stored.items():
            if value and not links.get(key):
                links[key] = value
        if _has_saved_links(links):
            partial.notices = list(partial.notices or []) + [
                "MusicBrainz did not return URL relations. Spotify, Instagram, and TikTok are using ids stored for this artist."
            ]
    # A MusicBrainz error with no saved URLs blocks Spotify, Instagram, and TikTok.
    # Cached relations, a previous artists row, or a Spotify id already on the links still let those cards run.
    if partial.status != "ok" and not _has_saved_links(links):
        links["_incomplete"] = True

    async def _run(platform: str, label: str, coro):
        if platform in skipped:
            coro.close()
            return _skipped(platform, label)
        return await _bounded(coro, platform, label, deadline)

    musicbrainz, apple, spotify, youtube, instagram, tiktok = await asyncio.gather(
        _passthrough(partial) if "musicbrainz" in skipped else _finish_catalog(rest, partial, deadline),
        _run("apple", "Apple Music", fetch_apple(name, links, client, store)),
        _run("spotify", "Spotify", fetch_spotify(name, links, env, client, store, include_related=include_related)),
        _run("youtube", "YouTube", fetch_youtube(name, links, env, client, store)),
        _run("instagram", "Instagram", fetch_instagram(name, links, client, store)),
        _run("tiktok", "TikTok", fetch_tiktok(name, links, client, store)),
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
