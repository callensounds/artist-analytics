"""MusicBrainz artist, URL relationships, and the most precise public release date."""

from __future__ import annotations

from analytics import (
    EXCLUDED_SECONDARY,
    PATTERN_RELEASE_TYPES,
    classify_date,
    names_match,
    sanitize_artist_query,
)
from http_client import TransportError
from models import Metric, SourceResult, errored, make_item, sort_items, unavailable
from providers.common import (
    apple_artist_id,
    cached,
    cache_key,
    instagram_handle,
    spotify_artist_id,
    tiktok_handle,
    youtube_channel_id,
    youtube_handle,
)

MB = "https://musicbrainz.org/ws/2"
HEADERS = {"Accept": "application/json"}


def extract_links(relations: list[dict]) -> dict:
    links = {
        "spotify_id": "",
        "apple_id": "",
        "youtube_channel_ids": [],
        "youtube_handles": [],
        "instagram": "",
        "tiktok": "",
    }
    seen_channels = set()
    for relation in relations or []:
        url = ((relation or {}).get("url") or {}).get("resource") or ""
        if not links["spotify_id"]:
            links["spotify_id"] = spotify_artist_id(url) or ""
        if not links["apple_id"]:
            links["apple_id"] = apple_artist_id(url) or ""
        if not links["instagram"]:
            links["instagram"] = instagram_handle(url) or ""
        if not links["tiktok"]:
            links["tiktok"] = tiktok_handle(url) or ""
        channel = youtube_channel_id(url)
        if channel and channel not in seen_channels:
            seen_channels.add(channel)
            links["youtube_channel_ids"].append(channel)
        handle = youtube_handle(url)
        if handle and handle not in links["youtube_handles"]:
            links["youtube_handles"].append(handle)
    return links


def _add_candidate(group: dict, raw: str | None, place: str | None) -> None:
    if classify_date(raw or "") == "unknown":
        return
    group["candidates"].append(((raw or "").strip(), place or None))


def aggregate_catalog(release_groups: list[dict], releases: list[dict]) -> list[dict]:
    from analytics import choose_best_date

    groups: dict[str, dict] = {}

    def ensure(group_id: str, title: str, primary: str, secondary: list[str]) -> dict | None:
        if not group_id:
            return None
        if EXCLUDED_SECONDARY.intersection(secondary or []):
            return None
        row = groups.get(group_id)
        if row is None:
            row = {
                "id": group_id,
                "title": title or "Untitled",
                "primary_type": primary or "Unknown",
                "secondary": list(secondary or []),
                "candidates": [],
            }
            groups[group_id] = row
        elif title and row["title"] == "Untitled":
            row["title"] = title
        return row

    for group in release_groups or []:
        row = ensure(
            group.get("id") or "",
            group.get("title") or "",
            group.get("primary-type") or "Unknown",
            group.get("secondary-types") or [],
        )
        if row is not None:
            _add_candidate(row, group.get("first-release-date"), None)

    for release in releases or []:
        embedded = release.get("release-group") or {}
        row = ensure(
            embedded.get("id") or release.get("id") or "",
            embedded.get("title") or release.get("title") or "",
            embedded.get("primary-type") or "Unknown",
            embedded.get("secondary-types") or [],
        )
        if row is None:
            continue
        _add_candidate(row, embedded.get("first-release-date"), None)
        _add_candidate(row, release.get("date"), release.get("country"))
        for event in release.get("release-events") or []:
            area = ((event or {}).get("area") or {}).get("name")
            _add_candidate(row, (event or {}).get("date"), area)

    items = []
    for row in groups.values():
        best, place = choose_best_date(row["candidates"])
        if not best:
            continue
        primary = row["primary_type"]
        if primary not in PATTERN_RELEASE_TYPES:
            continue
        label = primary
        if row["secondary"]:
            label = f"{primary} · {', '.join(row['secondary'])}"
        item = make_item(
            title=row["title"],
            kind="release",
            raw=best,
            detail=place or None,
        )
        if item is None:
            continue
        item.detail = " · ".join(part for part in (label, place) if part)
        items.append(item)
    return sort_items(items)


async def _get_json(http, url: str, params: dict):
    import asyncio

    last_status = None
    for attempt in range(2):
        try:
            result, payload = await http.get_json(url, params=params, headers=HEADERS, timeout=10)
        except TransportError as exc:
            raise TransportError(str(exc)) from exc
        last_status = result.status
        if result.status in {429, 503} and attempt == 0:
            await asyncio.sleep(2)
            continue
        if result.status in {429, 503}:
            raise TransportError("MusicBrainz is rate-limiting requests. Try again shortly.")
        if not result.ok or not isinstance(payload, dict):
            raise TransportError(f"MusicBrainz returned HTTP {result.status}.")
        return payload
    raise TransportError(f"MusicBrainz returned HTTP {last_status}.")


async def fetch_musicbrainz(name: str, http, cache) -> SourceResult:
    query = sanitize_artist_query(name)
    if not query:
        return unavailable("musicbrainz", "MusicBrainz", "Enter an artist name to search MusicBrainz.")

    async def produce() -> SourceResult:
        try:
            search = await _get_json(
                http,
                f"{MB}/artist/",
                {"query": f'artist:"{query}"', "fmt": "json", "limit": "5"},
            )
        except TransportError as exc:
            return errored("musicbrainz", "MusicBrainz", str(exc))
        artists = search.get("artists") or []
        chosen = None
        for artist in artists:
            score = int(artist.get("score") or 0)
            if score >= 80 or names_match(artist.get("name") or "", query):
                chosen = artist
                break
        if chosen is None:
            return unavailable(
                "musicbrainz",
                "MusicBrainz",
                "No confident MusicBrainz artist match. Social profiles are not guessed from the name.",
            )
        mbid = chosen["id"]
        try:
            artist_doc = await _get_json(http, f"{MB}/artist/{mbid}", {"inc": "url-rels", "fmt": "json"})
            groups = await _get_json(
                http,
                f"{MB}/release-group",
                {"artist": mbid, "fmt": "json", "limit": "100"},
            )
        except TransportError as exc:
            return errored("musicbrainz", "MusicBrainz", str(exc))
        release_notice = None
        try:
            releases = await _get_json(
                http,
                f"{MB}/release",
                {
                    "artist": mbid,
                    "fmt": "json",
                    "limit": "100",
                    "status": "official",
                    "inc": "release-groups",
                },
            )
        except TransportError:
            releases = {"releases": []}
            release_notice = "Official release events could not be loaded, so dates fall back to release-group first-release-date values."
        items = aggregate_catalog(groups.get("release-groups") or [], releases.get("releases") or [])
        day_level = sum(1 for item in items if item.precision in {"day", "time"})
        partial = sum(1 for item in items if item.precision in {"month", "year"})
        disambiguation = artist_doc.get("disambiguation") or chosen.get("disambiguation") or ""
        profile = artist_doc.get("name") or chosen.get("name")
        notices = [
            "Release dates use the earliest MusicBrainz date, refined to a full day when a release event has one. "
            "Month- and year-only dates stay partial and are excluded from the weekday chart. "
            "Showing up to 100 release groups and 100 official releases."
        ]
        if release_notice:
            notices.append(release_notice)
        return SourceResult(
            platform="musicbrainz",
            label="MusicBrainz",
            status="ok",
            profile_name=profile,
            profile_url=f"https://musicbrainz.org/artist/{mbid}",
            metrics=[
                Metric(label="Match", available=True, value=f"{profile}" + (f" ({disambiguation})" if disambiguation else "")),
                Metric(label="Catalog rows", available=True, value=str(len(items))),
                Metric(label="Exact days", available=True, value=str(day_level)),
                Metric(
                    label="Partial dates",
                    available=True,
                    value=str(partial),
                    reason=None,
                ),
            ],
            items=items,
            notices=notices,
            links=extract_links(artist_doc.get("relations") or []),
        )

    return await cached(cache, cache_key("musicbrainz", query), produce)
