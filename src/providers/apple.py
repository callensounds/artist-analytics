"""Apple release timestamps from the keyless public iTunes Search / Lookup API."""

from __future__ import annotations

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


async def _json(http, url: str, params: dict):
    try:
        result, payload = await http.get_json(url, params=params, timeout=12)
    except TransportError as exc:
        raise TransportError(str(exc)) from exc
    if not result.ok or not isinstance(payload, dict):
        raise TransportError(f"iTunes Search returned HTTP {result.status}.")
    return payload


async def fetch_apple(name: str, links: dict, http, cache) -> SourceResult:
    artist_id = (links or {}).get("apple_id") or ""

    async def produce() -> SourceResult:
        try:
            resolved = artist_id
            if not resolved:
                search = await _json(
                    http,
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
            payload = await _json(
                http,
                LOOKUP,
                {"id": resolved, "entity": "album", "limit": "200", "country": "us"},
            )
        except TransportError as exc:
            return errored("apple", "Apple Music", str(exc))
        artist, items = parse_collections(payload, resolved)
        if artist is None and not items:
            return unavailable("apple", "Apple Music", "iTunes lookup returned no albums for that artist id.")
        profile_name = (artist or {}).get("artistName") or name
        profile_url = (artist or {}).get("artistLinkUrl")
        timed = sum(1 for item in items if item.precision == "time")
        return SourceResult(
            platform="apple",
            label="Apple Music",
            status="ok",
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
            notices=[NOTICE],
        )

    return await cached(cache, cache_key("apple", name, artist_id), produce)
