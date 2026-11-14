"""Keyless Deezer artist data from the public api.deezer.com JSON API.

No token and no login. Fan counts are integers Deezer publishes as ``nb_fan``.
Release dates stay dates: a clock time is never added.
"""

from __future__ import annotations

import asyncio

from analytics import classify_date, names_match
from http_client import TransportError
from models import Metric, SourceResult, errored, make_item, sort_items, unavailable
from providers.common import deezer_artist_id

API = "https://api.deezer.com"
FANS = "Fans"
ALBUMS = "Albums"
TRACK_LIMIT = 10
ALBUM_LIMIT = 25
# A missing date on the albums list can be filled from the album endpoint.
# The list already carries release_date, so this stays a short fallback.
ALBUM_DETAIL_LIMIT = 2
CALL_TIMEOUT = 3.0
FETCH_BUDGET = 8.0
MAX_BYTES = 180_000
DATE_NOTICE = "Deezer publishes a release date, not a time of day. No hour was added."
STORED_NOTICE = "Using the Deezer artist id already stored for this artist."
FAN_NOTICE = "Matched by exact name. Among those, the artist with the most Deezer fans was kept."
UNMATCHED = "No Deezer artist with this exact name. A different name is not shown as this artist."
FAILED = "Deezer could not be read. Nothing was estimated."


class DeezerBusy(TransportError):
    """HTTP 429 or 503. The crawl message should be retried, not stored as a miss."""


def whole_count(value) -> int | None:
    """A published non-negative integer. Rounded text such as 1.2M is not a count."""
    if isinstance(value, bool) or value is None or isinstance(value, float):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def date_only(raw: str | None) -> str:
    """The calendar date Deezer sent. A time portion is dropped, not kept as midnight."""
    text = (raw or "").strip()
    if not text:
        return ""
    if "T" in text:
        text = text.split("T", 1)[0].strip()
    elif " " in text:
        left, right = text.split(" ", 1)
        if ":" in right:
            text = left.strip()
    return text


def _artist_id(value) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    text = str(value).strip()
    return text if text.isdigit() else ""


def _artist_row(row: dict) -> dict | None:
    if not isinstance(row, dict):
        return None
    artist_id = _artist_id(row.get("id"))
    name = " ".join(str(row.get("name") or "").split())
    if not artist_id or not name:
        return None
    picture = row.get("picture_medium") or row.get("picture") or row.get("picture_small")
    link = row.get("link") if isinstance(row.get("link"), str) else ""
    return {
        "id": artist_id,
        "name": name[:200],
        "nb_fan": whole_count(row.get("nb_fan")),
        "nb_album": whole_count(row.get("nb_album")),
        "link": link if link.startswith("https://www.deezer.com/artist/") else f"https://www.deezer.com/artist/{artist_id}",
        "picture": picture if isinstance(picture, str) and picture.startswith("https://") else None,
    }


def parse_search_artists(payload) -> list[dict]:
    """Artist rows from ``/search/artist``. An error object is not an empty list."""
    if not isinstance(payload, dict) or payload.get("error"):
        return []
    rows = payload.get("data")
    if not isinstance(rows, list):
        return []
    found = []
    seen = set()
    for row in rows:
        parsed = _artist_row(row) if isinstance(row, dict) else None
        if parsed is None or parsed["id"] in seen:
            continue
        seen.add(parsed["id"])
        found.append(parsed)
    return found


def choose_deezer_artist(candidates: list[dict] | None, name: str, preferred_id: str | None = None) -> dict | None:
    """Exact normalized name only.

    A Deezer id already tied to this artist (MusicBrainz URL or a stored id)
    wins when that row is one of the exact matches. Otherwise the exact-name
    row with the highest ``nb_fan`` wins. A missing fan count does not beat a
    published one, and a near name is not a match.
    """
    exact = []
    for row in candidates or []:
        parsed = _artist_row(row) if isinstance(row, dict) else None
        if parsed is None or not names_match(parsed["name"], name):
            continue
        exact.append(parsed)
    if not exact:
        return None
    preferred = str(preferred_id or "").strip()
    if preferred.isdigit():
        for row in exact:
            if str(row.get("id")) == preferred:
                return row
    def sort_key(row: dict) -> tuple:
        fans = row.get("nb_fan")
        known = isinstance(fans, int) and not isinstance(fans, bool)
        return (1 if known else 0, fans if known else 0, -int(row["id"]))

    return max(exact, key=sort_key)


def parse_top_tracks(payload, limit: int = TRACK_LIMIT) -> list[dict]:
    """Top tracks in list order. Deezer's ``rank`` score is not a 1-based position."""
    if not isinstance(payload, dict) or payload.get("error"):
        return []
    rows = payload.get("data")
    if not isinstance(rows, list):
        return []
    cap = limit if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0 else TRACK_LIMIT
    tracks = []
    seen = set()
    for row in rows:
        if len(tracks) >= cap:
            break
        if not isinstance(row, dict):
            continue
        external = _artist_id(row.get("id"))
        name = " ".join(str(row.get("title") or "").split())
        if not external or not name or external in seen:
            continue
        seen.add(external)
        album = row.get("album") if isinstance(row.get("album"), dict) else {}
        album_name = " ".join(str(album.get("title") or "").split())
        # Deezer's `rank` is a public popularity score. The list position is separate.
        tracks.append(
            {
                "external_id": external,
                "name": name[:200],
                "rank": len(tracks) + 1,
                "deezer_rank": whole_count(row.get("rank")),
                "album_name": album_name[:200] or None,
                "popularity": None,
                "url": f"https://www.deezer.com/track/{external}",
            }
        )
    return tracks


def parse_album_rows(payload) -> tuple[list[dict], list[str]]:
    """Albums from ``/artist/{id}/albums`` or one ``/album/{id}`` object.

    Returns dated rows and ids that had no usable date. Dates are calendar
    precision only.
    """
    if not isinstance(payload, dict) or payload.get("error"):
        return [], []
    if _artist_id(payload.get("id")) and payload.get("title"):
        rows = [payload]
    else:
        raw_rows = payload.get("data")
        rows = raw_rows if isinstance(raw_rows, list) else []
    dated = []
    missing = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        album_id = _artist_id(row.get("id"))
        title = " ".join(str(row.get("title") or "").split())
        if not album_id or not title or album_id in seen:
            continue
        seen.add(album_id)
        kind = " ".join(str(row.get("record_type") or "album").split()) or "album"
        released = date_only(row.get("release_date") if isinstance(row.get("release_date"), str) else "")
        if classify_date(released) not in {"day", "month", "year"}:
            missing.append(album_id)
            continue
        dated.append(
            {
                "id": album_id,
                "title": title[:200],
                "kind": kind[:40],
                "release_date": released,
                "url": f"https://www.deezer.com/album/{album_id}",
            }
        )
    return dated, missing


def release_items(albums: list[dict]):
    items = []
    for album in albums:
        raw = date_only(album.get("release_date"))
        if classify_date(raw) not in {"day", "month", "year"}:
            continue
        item = make_item(
            title=album["title"],
            kind=album.get("kind") or "album",
            raw=raw,
            url=album.get("url"),
            detail=album.get("kind") or "album",
        )
        if item is None or item.precision == "time" or item.hour_utc is not None:
            continue
        items.append(item)
    return sort_items(items)


def _count_metric(label: str, number: int | None, missing: str) -> Metric:
    if number is None:
        return Metric(label=label, available=False, reason=missing)
    return Metric(label=label, available=True, value=f"{number:,}", exact=number)


def build_deezer_result(
    artist: dict | None,
    *,
    tracks: list[dict] | None = None,
    albums: list[dict] | None = None,
    matched: str = "fans",
    notices: list[str] | None = None,
    partial: str | None = None,
) -> SourceResult:
    if not artist or not artist.get("id"):
        return unavailable("deezer", "Deezer", UNMATCHED)
    items = release_items(albums or [])
    notes = []
    if matched == "stored":
        notes.append(STORED_NOTICE)
    else:
        notes.append(FAN_NOTICE)
    if items:
        notes.append(DATE_NOTICE)
    if artist.get("nb_album") is not None and len(albums or []) < int(artist["nb_album"]):
        notes.append("Album dates are the first page Deezer returned. Later pages were not fetched.")
    if partial:
        notes.append(partial)
    notes.extend(notices or [])
    return SourceResult(
        platform="deezer",
        label="Deezer",
        status="ok",
        profile_name=artist.get("name"),
        profile_url=artist.get("link") or f"https://www.deezer.com/artist/{artist['id']}",
        image_url=artist.get("picture"),
        metrics=[
            _count_metric(FANS, artist.get("nb_fan"), "Deezer did not publish a fan count."),
            _count_metric(ALBUMS, artist.get("nb_album"), "Deezer did not publish an album count."),
        ],
        items=items,
        tracks=list(tracks or []),
        links={"deezer_id": str(artist["id"])},
        notices=notes,
        cacheable=False,
    )


def _error_message(payload) -> str | None:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    message = " ".join(str(error.get("message") or "Deezer returned an error.").split())
    return message[:180] or "Deezer returned an error."


async def _get_json(http, url: str, params: dict | None, deadline: float):
    left = deadline - asyncio.get_running_loop().time()
    if left < 0.2:
        raise TransportError("Deezer timed out before the next request.")
    try:
        result, payload = await http.get_json(
            url,
            params=params,
            timeout=min(CALL_TIMEOUT, left),
            max_bytes=MAX_BYTES,
            headers={"Accept": "application/json"},
        )
    except TransportError:
        raise
    if result.status in {429, 503}:
        raise DeezerBusy(f"Deezer returned HTTP {result.status}.")
    return result, payload


async def _load_artist(http, artist_id: str, deadline: float) -> dict | None:
    result, payload = await _get_json(http, f"{API}/artist/{artist_id}", None, deadline)
    if not result.ok or _error_message(payload):
        return None
    return _artist_row(payload) if isinstance(payload, dict) else None


async def _search(http, name: str, deadline: float) -> tuple[list[dict], str | None]:
    result, payload = await _get_json(
        http,
        f"{API}/search/artist",
        {"q": name.strip()[:120], "limit": "25"},
        deadline,
    )
    if result.status == 404:
        return [], None
    if not result.ok:
        return [], f"Deezer search returned HTTP {result.status}."
    message = _error_message(payload)
    if message:
        return [], message
    if not isinstance(payload, dict):
        return [], "Deezer search did not return JSON."
    return parse_search_artists(payload), None


async def _top_and_albums(http, artist_id: str, deadline: float) -> tuple[list[dict], list[dict], str | None]:
    notes = []
    tracks: list[dict] = []
    albums: list[dict] = []
    missing: list[str] = []
    try:
        result, payload = await _get_json(http, f"{API}/artist/{artist_id}/top", {"limit": str(TRACK_LIMIT)}, deadline)
        if result.ok and not _error_message(payload):
            tracks = parse_top_tracks(payload)
        else:
            notes.append("Deezer top tracks were not available.")
    except DeezerBusy:
        raise
    except TransportError:
        notes.append("Deezer top tracks timed out.")
    try:
        result, payload = await _get_json(
            http,
            f"{API}/artist/{artist_id}/albums",
            {"limit": str(ALBUM_LIMIT)},
            deadline,
        )
        if result.ok and not _error_message(payload):
            albums, missing = parse_album_rows(payload)
        else:
            notes.append("Deezer album dates were not available.")
    except DeezerBusy:
        raise
    except TransportError:
        notes.append("Deezer album dates timed out.")
    filled = list(albums)
    for album_id in missing[:ALBUM_DETAIL_LIMIT]:
        try:
            result, payload = await _get_json(http, f"{API}/album/{album_id}", None, deadline)
        except DeezerBusy:
            raise
        except TransportError:
            notes.append("One Deezer album page timed out.")
            break
        if not result.ok or _error_message(payload):
            continue
        extra, _missing = parse_album_rows(payload if isinstance(payload, dict) else {})
        filled.extend(extra)
    return tracks, filled, " ".join(notes) or None


async def fetch_deezer(name: str, links: dict | None, http, cache=None) -> SourceResult:
    """Resolve one artist and read fans, albums, top tracks, and release dates.

    A stored Deezer id skips search. ``cache`` is unused: the crawler stores
    the snapshot, and the artist page does not call this.
    """
    del cache
    cleaned = " ".join((name or "").split())
    if not cleaned:
        return unavailable("deezer", "Deezer", "Enter an artist name to search Deezer.")
    deadline = asyncio.get_running_loop().time() + FETCH_BUDGET
    preferred = str((links or {}).get("deezer_id") or "").strip()
    preferred = preferred if preferred.isdigit() else ""
    matched = "fans"
    artist = None
    try:
        if preferred:
            artist = await _load_artist(http, preferred, deadline)
            if artist is not None and names_match(artist["name"], cleaned):
                matched = "stored"
            else:
                artist = None
        if artist is None:
            candidates, problem = await _search(http, cleaned, deadline)
            if problem:
                return errored("deezer", "Deezer", problem)
            chosen = choose_deezer_artist(candidates, cleaned, preferred or None)
            if chosen is None:
                return unavailable("deezer", "Deezer", UNMATCHED)
            profile = await _load_artist(http, chosen["id"], deadline)
            if profile is not None and names_match(profile["name"], cleaned):
                artist = profile
            elif profile is not None:
                return unavailable("deezer", "Deezer", UNMATCHED)
            else:
                artist = chosen
        tracks, albums, partial = await _top_and_albums(http, artist["id"], deadline)
    except DeezerBusy:
        raise
    except TransportError as exc:
        return errored("deezer", "Deezer", str(exc) or FAILED)
    except Exception:
        return errored("deezer", "Deezer", FAILED)
    return build_deezer_result(artist, tracks=tracks, albums=albums, matched=matched, partial=partial)
