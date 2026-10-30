"""MusicBrainz artist, URL relationships, and the most precise public release date."""

from __future__ import annotations

import asyncio
import json
import random

from analytics import (
    EXCLUDED_SECONDARY,
    PATTERN_RELEASE_TYPES,
    classify_date,
    names_match,
    sanitize_artist_query,
)
from http_client import TransportError
from models import Metric, SourceResult, errored, make_item, sort_items, source_from_dict, unavailable
from providers.common import (
    apple_artist_id,
    cache_key,
    instagram_handle,
    spotify_artist_id,
    tiktok_handle,
    youtube_channel_id,
    youtube_handle,
)

MB = "https://musicbrainz.org/ws/2"
MB_USER_AGENT = "AverageAnalytics/1.0 ( callen@callensounds.com )"
MB_INTERVAL = 1.1
MB_TTL = 24 * 60 * 60
MB_RELATIONS_TTL = 7 * 24 * 60 * 60
MB_TIMEOUT = 4.5
# Search plus one lookup, inside the dashboard's ~10s deadline. The catalog gets what is left.
MB_OPEN_TIMEOUT = 5.5
# A request that would queue longer than this for a MusicBrainz slot gets an error card instead.
MB_MAX_QUEUE = 3.0
HEADERS = {"Accept": "application/json", "User-Agent": MB_USER_AGENT}
# Next free slot, in event-loop time, for this isolate. A plain float, not an asyncio
# primitive: no Lock or Semaphore is built at import time or shared across requests.
_mb_next = 0.0


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


class MusicBrainzBusy(TransportError):
    pass


# Catalog calls only take a slot when the queue is short, so identity lookups go first.
MB_CATALOG_MAX_QUEUE = 1.2
# Requests for an artist another request is already looking up poll the cache this often.
MB_PEER_POLL = 0.25
# query -> event-loop time after which the marker is stale. Plain data, no asyncio primitive.
_mb_inflight: dict[str, float] = {}
# At about one request a second, two lookups (search + artist) each fit in the identity
# budget. More owners at once would push every lookup past it and none would finish.
MB_MAX_OWNERS = 2


def _owners(now: float) -> int:
    return sum(1 for expiry in _mb_inflight.values() if expiry > now)


def _max_queue(http, limit: float = MB_MAX_QUEUE) -> float:
    remaining = getattr(http, "remaining", None)
    left = remaining() if callable(remaining) else None
    if left is not None:
        limit = min(limit, left - 0.5)
    return limit


async def _mb_slot(http=None, max_queue: float = MB_MAX_QUEUE) -> None:
    """At most about one MusicBrainz request per MB_INTERVAL, per isolate.

    The slot is reserved synchronously (read and write with no await in between),
    then the caller sleeps until it. A request that would wait past its budget
    does not reserve a slot and fails fast, so a burst cannot build an
    unbounded queue behind one shared lock.
    """
    global _mb_next
    now = asyncio.get_running_loop().time()
    slot = max(now, _mb_next)
    if slot - now > _max_queue(http, max_queue):
        raise MusicBrainzBusy(
            "MusicBrainz is busy: this server keeps to about one request per second and the queue is full. "
            "Refresh in a few seconds."
        )
    _mb_next = slot + MB_INTERVAL
    if slot > now:
        await asyncio.sleep(slot - now)


def _body_snippet(text: str) -> str:
    collapsed = " ".join((text or "").split())
    return collapsed[:240]


def _mb_message(status: int | None, body: str) -> str:
    snippet = _body_snippet(body)
    if status is None:
        return f"MusicBrainz request failed: {snippet or 'no response body'}"
    if snippet:
        return f"MusicBrainz HTTP {status}: {snippet}"
    return f"MusicBrainz HTTP {status}."


async def _get_json(http, url: str, params: dict, max_queue: float = MB_MAX_QUEUE, max_bytes: int | None = None):
    """Two attempts. A 429, 503, or transport failure waits a short jitter, then tries once more."""
    last_error = TransportError("MusicBrainz request failed: no response body")
    for attempt in range(2):
        if attempt and _max_queue(http, max_queue) < 1.0:
            break
        await _mb_slot(http, max_queue)
        try:
            result, payload = await http.get_json(
                url,
                params=params,
                headers=HEADERS,
                timeout=MB_TIMEOUT,
                max_bytes=max_bytes,
            )
        except TransportError as exc:
            last_error = TransportError(_mb_message(None, str(exc)))
            if attempt == 0:
                await asyncio.sleep(random.uniform(0.25, 0.9))
                continue
            raise last_error from exc
        if result.ok and isinstance(payload, dict):
            return payload
        last_error = TransportError(_mb_message(result.status, result.text or ""))
        if result.status in {429, 503} and attempt == 0:
            await asyncio.sleep(random.uniform(0.25, 0.9))
            continue
        raise last_error
    raise last_error


def _relations_payload(result: SourceResult) -> dict:
    links = _public_links(result.links)
    return {
        "links": links,
        "name": result.profile_name or "",
        "mbid": (result.links or {}).get("_mbid") or "",
        "match": ((result.metrics or [None])[0].value if result.metrics else None) or "",
    }


async def _load_relations(cache, query: str) -> dict | None:
    if cache is None or not query:
        return None
    raw = await cache.get(cache_key("mb-rels", query))
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    links = payload.get("links") if isinstance(payload, dict) else None
    if not isinstance(links, dict):
        return None
    return links


async def _store_relations(cache, query: str, result: SourceResult) -> None:
    if cache is None or not query or result.status != "ok":
        return
    try:
        await cache.set(
            cache_key("mb-rels", query),
            json.dumps(_relations_payload(result)),
            MB_RELATIONS_TTL,
        )
    except Exception:
        return


def _public_links(links: dict | None) -> dict:
    return {key: value for key, value in (links or {}).items() if not str(key).startswith("_")}


async def _remember(cache, query: str, result: SourceResult) -> None:
    if cache is None or not query or not result.cacheable:
        return
    try:
        await cache.set(cache_key("musicbrainz", query), json.dumps(result.to_dict()), result.ttl)
    except Exception:
        return


async def _match(name: str, http) -> SourceResult:
    query = sanitize_artist_query(name)
    if not query:
        return unavailable("musicbrainz", "MusicBrainz", "Enter an artist name to search MusicBrainz.")
    try:
        search = await _get_json(
            http,
            f"{MB}/artist/",
            {"query": f'artist:"{query}"', "fmt": "json", "limit": "5", "inc": "url-rels"},
        )
    except MusicBrainzBusy:
        raise
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
            "No confident MusicBrainz artist match. Apple and YouTube can still be matched by name. "
            "Instagram and TikTok handles are not guessed.",
        )
    mbid = chosen["id"]
    # The search server usually ignores inc=url-rels. A lookup is the second call only then.
    if isinstance(chosen.get("relations"), list):
        artist_doc = chosen
    else:
        try:
            artist_doc = await _get_json(http, f"{MB}/artist/{mbid}", {"inc": "url-rels", "fmt": "json"})
        except MusicBrainzBusy:
            raise
        except TransportError as exc:
            return errored("musicbrainz", "MusicBrainz", str(exc))
    links = extract_links(artist_doc.get("relations") or [])
    links["_mbid"] = mbid
    profile = artist_doc.get("name") or chosen.get("name")
    disambiguation = artist_doc.get("disambiguation") or chosen.get("disambiguation") or ""
    return SourceResult(
        platform="musicbrainz",
        label="MusicBrainz",
        status="ok",
        profile_name=profile,
        profile_url=f"https://musicbrainz.org/artist/{mbid}",
        metrics=[
            Metric(label="Match", available=True, value=f"{profile}" + (f" ({disambiguation})" if disambiguation else "")),
        ],
        notices=[],
        links=links,
        ttl=MB_TTL,
    )


async def _finish(partial: SourceResult, http) -> SourceResult:
    links = dict(partial.links or {})
    mbid = links.get("_mbid") or ""
    public = _public_links(links)
    if partial.status != "ok" or not mbid:
        partial.links = public
        return partial
    try:
        groups, releases, release_notice = await _catalog(http, mbid)
    except TransportError as exc:
        partial.links = public
        partial.notices = list(partial.notices or []) + [str(exc)]
        # Keep the match on the page, but do not cache a dashboard without its catalog.
        partial.cacheable = False
        return partial
    items = aggregate_catalog(groups.get("release-groups") or [], releases.get("releases") or [])
    day_level = sum(1 for item in items if item.precision in {"day", "time"})
    month_or_year = sum(1 for item in items if item.precision in {"month", "year"})
    notices = [
        "Release dates use the earliest MusicBrainz date, refined to a full day when a release event has one. "
        "Month- and year-only dates stay partial and are excluded from the weekday chart. "
        "Showing up to 100 release groups and 100 official releases."
    ]
    if release_notice:
        notices.append(release_notice)
    match = (partial.metrics or [None])[0]
    return SourceResult(
        platform="musicbrainz",
        label="MusicBrainz",
        status="ok",
        profile_name=partial.profile_name,
        profile_url=partial.profile_url,
        metrics=[
            match or Metric(label="Match", available=True, value=partial.profile_name),
            Metric(label="Catalog rows", available=True, value=str(len(items))),
            Metric(label="Exact days", available=True, value=str(day_level)),
            Metric(label="Partial dates", available=True, value=str(month_or_year)),
        ],
        items=items,
        notices=notices,
        links=public,
        ttl=MB_TTL,
    )


async def _peer_result(cache, query: str) -> SourceResult | None:
    """What another request already stored for this query: the full result, or its URL relations."""
    if cache is None or not query:
        return None
    raw = await cache.get(cache_key("musicbrainz", query))
    if raw:
        try:
            return source_from_dict(json.loads(raw))
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    raw = await cache.get(cache_key("mb-rels", query))
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("links"), dict):
        return None
    name = payload.get("name") or query
    mbid = payload.get("mbid") or ""
    return SourceResult(
        platform="musicbrainz",
        label="MusicBrainz",
        status="ok",
        profile_name=name,
        profile_url=f"https://musicbrainz.org/artist/{mbid}" if mbid else None,
        metrics=[Metric(label="Match", available=True, value=payload.get("match") or name)],
        notices=["The release catalog is still loading in another request. Refresh for MusicBrainz release dates."],
        links=dict(payload["links"]),
        ttl=MB_TTL,
        cacheable=False,
    )


async def _match_or_wait(name: str, http, cache, query: str) -> tuple[SourceResult, bool]:
    """Look the artist up, or wait for the request that already is. Returns (result, did_the_lookup).

    Concurrent requests for one artist share the lookup through the cache, which holds
    plain strings. No future, lock, or JS object crosses from one request to another.
    """
    loop = asyncio.get_running_loop()
    remaining = getattr(http, "remaining", None)
    left = remaining() if callable(remaining) else None
    budget = MB_OPEN_TIMEOUT if left is None else min(MB_OPEN_TIMEOUT, left)
    give_up = loop.time() + budget - 0.3
    last_busy: MusicBrainzBusy | None = None
    waited = False
    while True:
        if waited:
            # Check what a peer stored before taking over its marker.
            peer = await _peer_result(cache, query)
            if peer is not None:
                return peer, False
        now = loop.time()
        if _mb_inflight.get(query, 0.0) <= now and _owners(now) < MB_MAX_OWNERS:
            marker = now + MB_OPEN_TIMEOUT + 1.0
            _mb_inflight[query] = marker
            try:
                result = await _match(name, http)
                if result.status == "ok":
                    await _store_relations(cache, query, result)
                return result, True
            except MusicBrainzBusy as exc:
                last_busy = exc
            finally:
                if _mb_inflight.get(query) == marker:
                    _mb_inflight.pop(query, None)
        if loop.time() + MB_PEER_POLL >= give_up:
            raise last_busy or MusicBrainzBusy(
                "MusicBrainz is busy with other lookups on this server. Refresh in a few seconds."
            )
        await asyncio.sleep(MB_PEER_POLL)
        waited = True


async def open_musicbrainz(name: str, http, cache):
    """URL relations first. The catalog coroutine can run beside the other providers."""
    query = sanitize_artist_query(name)
    if cache is not None and query:
        raw = await cache.get(cache_key("musicbrainz", query))
        if raw:
            try:
                return source_from_dict(json.loads(raw)), None
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
    try:
        partial, owner = await _match_or_wait(name, http, cache, query)
    except MusicBrainzBusy as exc:
        partial, owner = errored("musicbrainz", "MusicBrainz", str(exc)), True
    if not owner:
        return partial, None
    if partial.status != "ok":
        cached_links = await _load_relations(cache, query)
        if cached_links:
            partial.links = dict(cached_links)
            partial.notices = list(partial.notices or []) + [
                "MusicBrainz did not answer. Spotify, Instagram, and TikTok are using URL relations cached for 7 days."
            ]
        else:
            partial.links = _public_links(partial.links)
        await _remember(cache, query, partial)
        return partial, None

    async def finish():
        done = await _finish(partial, http)
        await _remember(cache, query, done)
        return done

    return partial, finish()


async def timed_out(name: str, cache) -> SourceResult:
    """The identity step ran out of time. Cached URL relations still let the other cards run."""
    result = errored(
        "musicbrainz",
        "MusicBrainz",
        "MusicBrainz took too long and was skipped so the rest of the page could load.",
    )
    try:
        cached_links = await asyncio.wait_for(_load_relations(cache, sanitize_artist_query(name)), 1.0)
    except Exception:
        cached_links = None
    if cached_links:
        result.links = dict(cached_links)
        result.notices = [
            "MusicBrainz did not answer in time. Spotify, Instagram, and TikTok are using URL relations cached for 7 days."
        ]
    return result


async def fetch_musicbrainz(name: str, http, cache) -> SourceResult:
    partial, rest = await open_musicbrainz(name, http, cache)
    if rest is None:
        return partial
    return await rest


# One page. Offset pages are not followed on a dashboard or a crawl.
CATALOG_LIMIT = "25"
CATALOG_MAX_BYTES = 100_000


def _groups_need_release_events(groups_result: dict) -> bool:
    """Release-group dates are enough when several already include a calendar day."""
    dated = 0
    for row in (groups_result or {}).get("release-groups") or []:
        raw = str((row or {}).get("first-release-date") or "")
        if len(raw) >= 10:
            dated += 1
            if dated >= 5:
                return False
    return True


async def _catalog(http, mbid: str):
    async def groups():
        return await _get_json(
            http,
            f"{MB}/release-group",
            {"artist": mbid, "fmt": "json", "limit": CATALOG_LIMIT},
            MB_CATALOG_MAX_QUEUE,
            max_bytes=CATALOG_MAX_BYTES,
        )

    async def releases():
        return await _get_json(
            http,
            f"{MB}/release",
            {
                "artist": mbid,
                "fmt": "json",
                "limit": CATALOG_LIMIT,
                "status": "official",
                "inc": "release-groups",
            },
            MB_CATALOG_MAX_QUEUE,
            max_bytes=CATALOG_MAX_BYTES,
        )

    try:
        groups_result = await groups()
    except Exception as groups_error:
        if isinstance(groups_error, TransportError):
            raise
        raise TransportError("MusicBrainz release groups could not be loaded.") from groups_error
    if not _groups_need_release_events(groups_result):
        return groups_result, {"releases": []}, None
    try:
        releases_result = await releases()
    except Exception:
        return (
            groups_result,
            {"releases": []},
            "Official release events could not be loaded, so dates fall back to release-group first-release-date values.",
        )
    return groups_result, releases_result, None
