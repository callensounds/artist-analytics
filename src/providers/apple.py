"""Apple release timestamps from the keyless public iTunes Search / Lookup API."""

from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime, timezone
from urllib.parse import urlencode

from analytics import names_match
from http_client import TransportError
from models import Metric, SourceResult, TimedItem, errored, make_item, sort_items, unavailable
from providers.common import cache_key, cached

LOOKUP = "https://itunes.apple.com/lookup"
SEARCH = "https://itunes.apple.com/search"
NOTICE = (
    "releaseDate is the exact UTC timestamp iTunes publishes. "
    "For the US storefront it is often midnight Pacific (07:00 or 08:00 UTC), not a separate local clock time. "
    "Follower counts are not part of this API."
)
NAME_MATCH = "Matched by name on the public iTunes Search API, not from a MusicBrainz URL."
FRESH_TTL = 6 * 60 * 60
STALE_TTL = 7 * 24 * 60 * 60


class ItunesLimited(Exception):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"iTunes returned HTTP {status}.")


def _stamp(moment: datetime | None = None) -> str:
    value = moment or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def itunes_url(base: str, params: dict) -> str:
    return f"{base}?{urlencode(params)}"


def _limited_message(status: int, fetched_at: str | None) -> str:
    if fetched_at:
        return f"Apple is rate-limiting; showing cached data from {fetched_at}."
    if status == 429:
        return "Apple is rate-limiting this lookup. Try again shortly."
    return "Apple refused the lookup from this network (HTTP 403). Try again shortly."


def parse_collections(payload: dict, artist_id: str) -> tuple[dict | None, list[TimedItem]]:
    artist = None
    items: list[TimedItem] = []
    seen = set()
    for row in payload.get("results") or []:
        if row.get("wrapperType") == "artist" and str(row.get("artistId")) == str(artist_id):
            artist = row
            continue
        if row.get("wrapperType") != "collection":
            continue
        if str(row.get("artistId")) != str(artist_id):
            continue
        collection_id = row.get("collectionId")
        if collection_id in seen:
            continue
        seen.add(collection_id)
        item = make_item(
            title=row.get("collectionName") or "Untitled",
            kind="release",
            raw=row.get("releaseDate") or "",
            url=row.get("collectionViewUrl"),
            detail=" · ".join(
                part
                for part in (
                    row.get("collectionType"),
                    f"{row['trackCount']} tracks" if row.get("trackCount") else None,
                    row.get("country"),
                )
                if part
            ),
        )
        if item is not None:
            items.append(item)
    return artist, sort_items(items)


async def _read_cached(cache, key: str) -> dict | None:
    if cache is None:
        return None
    raw = await cache.get(key)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("body"), dict):
        return None
    return payload


async def _store_body(cache, url: str, body: dict, fetched_at: str) -> None:
    if cache is None:
        return
    raw = json.dumps({"at": fetched_at, "body": body})
    await cache.set(cache_key("itunes-body", url), raw, FRESH_TTL)
    await cache.set(cache_key("itunes-stale", url), raw, STALE_TTL)


async def itunes_json(http, cache, base: str, params: dict) -> tuple[dict, str | None, bool]:
    """One iTunes call, cached. 429 is retried once. 429/403 can return a stale body."""
    url = itunes_url(base, params)
    fresh = await _read_cached(cache, cache_key("itunes-body", url))
    if fresh is not None:
        return fresh["body"], fresh.get("at"), False

    last_status = 429
    for attempt in range(2):
        try:
            result, payload = await http.get_json(url, timeout=8)
        except TransportError as exc:
            raise TransportError(str(exc)) from exc
        if result.status == 429 and attempt == 0:
            await asyncio.sleep(1.5 + random.uniform(0, 0.4))
            continue
        if result.status in {429, 403}:
            last_status = result.status
            break
        if not result.ok or not isinstance(payload, dict):
            raise TransportError(f"iTunes Search returned HTTP {result.status}.")
        fetched_at = _stamp()
        await _store_body(cache, url, payload, fetched_at)
        return payload, fetched_at, False

    stale = await _read_cached(cache, cache_key("itunes-stale", url))
    if stale is not None:
        return stale["body"], stale.get("at"), True
    raise ItunesLimited(last_status)


async def fetch_apple(name: str, links: dict, http, cache) -> SourceResult:
    artist_id = (links or {}).get("apple_id") or ""

    async def produce() -> SourceResult:
        matched_by_name = False
        limited_at = None
        try:
            resolved = artist_id
            if not resolved:
                search, _, _search_stale = await itunes_json(
                    http,
                    cache,
                    SEARCH,
                    {"term": name, "entity": "musicArtist", "limit": "5", "country": "us"},
                )
                for row in search.get("results") or []:
                    if names_match(row.get("artistName") or "", name):
                        resolved = str(row.get("artistId"))
                        break
                if not resolved:
                    return unavailable(
                        "apple",
                        "Apple Music",
                        "No exact iTunes artist match. A nearby name is not shown as this artist.",
                    )
                matched_by_name = True
            payload, fetched_at, lookup_stale = await itunes_json(
                http,
                cache,
                LOOKUP,
                {"id": resolved, "entity": "album", "limit": "100", "country": "us"},
            )
            if lookup_stale:
                limited_at = fetched_at
        except ItunesLimited as exc:
            return unavailable("apple", "Apple Music", _limited_message(exc.status, None), ttl=120)
        except TransportError as exc:
            return errored("apple", "Apple Music", str(exc))
        artist, items = parse_collections(payload, resolved)
        if artist is None and not items:
            return unavailable("apple", "Apple Music", "iTunes lookup returned no albums for that artist id.")
        profile_name = (artist or {}).get("artistName") or name
        profile_url = (artist or {}).get("artistLinkUrl")
        timed = sum(1 for item in items if item.precision == "time")
        notices = [NOTICE]
        if matched_by_name:
            notices.append(NAME_MATCH)
        reason = _limited_message(429, limited_at) if limited_at else None
        return SourceResult(
            platform="apple",
            label="Apple Music",
            status="ok",
            reason=reason,
            profile_name=profile_name,
            profile_url=profile_url,
            metrics=[
                Metric(label="Albums returned", available=True, value=str(len(items))),
                Metric(label="Exact timestamps", available=True, value=str(timed)),
                Metric(
                    label="Followers",
                    available=False,
                    reason="The public iTunes Search API does not publish follower counts.",
                ),
            ],
            items=items[:100],
            notices=notices,
        )

    return await cached(cache, cache_key("apple", name, artist_id), produce)
