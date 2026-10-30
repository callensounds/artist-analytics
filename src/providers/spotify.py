"""Spotify from public pages. The Web API client-credentials path stays off unless both secrets are set."""

from __future__ import annotations

import base64
import html
import json
import re
import time

from analytics import compact_count, scrub, spotify_precision
from http_client import TransportError
from models import Metric, SourceResult, errored, make_item, sort_items, unavailable
from providers.common import cache_key, cached, drop_blocks, script_body, strip_tags
from runtime import env_get

TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
OEMBED = "https://open.spotify.com/oembed"
_TOKEN = {"value": "", "expires": 0.0}
SECTION_TITLES = {
    "Popular",
    "Popular releases",
    "Albums",
    "Singles and EPs",
    "Compilations",
    "Latest Release • Album",
    "Latest Release • Single",
    "Latest Release • EP",
    "Fans also like",
    "Appears On",
    "Followers",
}
KIND_LINE = re.compile(
    r"^(?:(Album|Single|EP|Compilation)\s*[•·]\s*((?:19|20)\d{2})|"
    r"((?:19|20)\d{2})\s*[•·]\s*(Album|Single|EP|Compilation))$"
)
LISTENERS_EXACT = re.compile(r"([\d,]{4,})\s+monthly listeners")
LISTENERS_ROUNDED = re.compile(r"([\d.]+[KMB])\s+monthly listeners", re.I)
PLAYS = re.compile(r"^[\d,]+$")
# Open artist pages are large. Keep the head, where the visible shelf and listener text sit.
PAGE_MAX_BYTES = 400_000
# Exact "1,473,027 monthly listeners" and "Fans also like" sit well past the first 64KB
# on a current artist page. Download far enough to reach them, then parse only a window.
FETCH_BYTES = 260_000
SHELF_WINDOW = 80_000
CRAWL_PAGE_BYTES = SHELF_WINDOW
CRAWL_EMBED_BYTES = 24_000


def visible_lines(page: str, limit: int | None = None) -> list[str]:
    text = page or ""
    if limit is not None and len(text) > limit:
        # Crawl only needs the head of the page. The dashboard still passes the whole body.
        text = text[:limit]
    text = drop_blocks(text, "<script", "</script>")
    text = drop_blocks(text, "<style", "</style>")
    text = strip_tags(text)
    lines = []
    for line in text.splitlines():
        cleaned = html.unescape(re.sub(r"\s+", " ", line).strip())
        if cleaned:
            lines.append(cleaned)
    return lines


def listener_slices(page: str) -> str:
    """Only the characters around each 'monthly listeners' phrase. The exact count is not in the head."""
    if not page:
        return ""
    needle = "monthly listeners"
    lowered = page.lower()
    parts = []
    start = 0
    while len(parts) < 8:
        at = lowered.find(needle, start)
        if at < 0:
            break
        parts.append(page[max(0, at - 48) : at + len(needle) + 8])
        start = at + len(needle)
    return "\n".join(parts)


def shelf_window(page: str) -> str:
    """Discography HTML starting at the listener label, not the document head."""
    if not page:
        return ""
    start = page.find("monthly-listeners-label")
    if start < 0:
        start = page.lower().find("monthly listeners")
    if start < 0:
        return page[:SHELF_WINDOW]
    return page[max(0, start - 2000) : start + SHELF_WINDOW]


def parse_monthly_listeners(page: str, lines: list[str] | None = None) -> tuple[str | None, bool]:
    """Return (display, exact). Rounded marketing text is kept as printed.

    Pass ``lines`` when the caller already ran ``visible_lines`` so the page is not scanned twice.
    """
    exact = [int(match.group(1).replace(",", "")) for match in LISTENERS_EXACT.finditer(page or "")]
    if exact:
        return f"{max(exact):,}", True
    if lines is None:
        lines = visible_lines(page)
    rounded = LISTENERS_ROUNDED.search("\n".join(lines)) or LISTENERS_ROUNDED.search(page or "")
    if rounded:
        return rounded.group(1), False
    return None, False


def parse_discography(lines: list[str]) -> list[tuple[str, str, str]]:
    found = []
    seen = set()
    previous = None
    for line in lines:
        match = KIND_LINE.match(line)
        if not match:
            previous = line
            continue
        if match.group(1):
            kind, year = match.group(1), match.group(2)
        else:
            year, kind = match.group(3), match.group(4)
        title = previous
        previous = line
        if not title or title in SECTION_TITLES or "•" in title:
            continue
        key = (title.casefold(), kind, year)
        if key in seen:
            continue
        seen.add(key)
        found.append((title, kind, year))
    return found


def parse_popular(lines: list[str]) -> list[tuple[str, int | None]]:
    if "Popular" not in lines:
        return []
    rows = []
    index = lines.index("Popular") + 1
    while index < len(lines) and lines[index] not in {"Popular releases", "Albums", "Singles and EPs"}:
        title = lines[index]
        plays = lines[index + 1] if index + 1 < len(lines) else ""
        if title not in SECTION_TITLES and PLAYS.fullmatch(plays or ""):
            rows.append((title, int(plays.replace(",", ""))))
            index += 2
            continue
        index += 1
    return rows


def parse_embed_state(page: str) -> dict:
    raw = script_body(page or "", 'id="__NEXT_DATA__"')
    if not raw:
        return {}
    if len(raw) > CRAWL_EMBED_BYTES:
        return {}
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, MemoryError, RecursionError, ValueError):
        return {}
    entity = (
        ((payload.get("props") or {}).get("pageProps") or {})
        .get("state", {})
        .get("data", {})
        .get("entity")
        or {}
    )
    if not isinstance(entity, dict):
        return {}
    tracks = []
    for track in entity.get("trackList") or []:
        if isinstance(track, dict) and track.get("title"):
            tracks.append(str(track["title"]))
    return {"name": entity.get("name") or entity.get("title"), "tracks": tracks}


def parse_spotify_followers(lines: list[str]) -> int | None:
    for index, line in enumerate(lines):
        if line != "Followers" or index == 0:
            continue
        previous = lines[index - 1]
        if PLAYS.fullmatch(previous):
            return int(previous.replace(",", ""))
    return None


def _spotify_id_at(chunk: str, start: int) -> tuple[str, int] | None:
    end = start
    limit = min(len(chunk), start + 32)
    while end < limit:
        char = chunk[end]
        if not (("A" <= char <= "Z") or ("a" <= char <= "z") or ("0" <= char <= "9")):
            break
        end += 1
    if end == start or end >= len(chunk) or chunk[end] != '"':
        return None
    return chunk[start:end], end + 1


def parse_related_artists(page: str) -> list[dict]:
    """Names and ids from the public 'Fans also like' cards. Order is Spotify's rank.

    Linear scan of a short slice. The dashboard does not call this; the crawl does.
    """
    if not page:
        return []
    start = page.find("Fans also like")
    if start < 0:
        return []
    end = page.find("Appears On", start)
    chunk = page[start:end if end > start else min(len(page), start + 8000)]
    found = []
    seen = set()
    cursor = 0
    marker = 'href="/artist/'
    while len(found) < 12:
        at = chunk.find(marker, cursor)
        if at < 0:
            break
        parsed = _spotify_id_at(chunk, at + len(marker))
        if parsed is None:
            cursor = at + len(marker)
            continue
        artist_id, after = parsed
        window = chunk[after:after + 900]
        close = window.find("</span>")
        if close < 0:
            cursor = after
            continue
        open_gt = window.rfind(">", 0, close)
        if open_gt < 0:
            cursor = after
            continue
        name = " ".join(html.unescape(window[open_gt + 1 : close]).split())
        cursor = after + close + len("</span>")
        if not name or "<" in name or len(name) > 80 or artist_id in seen:
            continue
        seen.add(artist_id)
        found.append({"name": name, "spotify_id": artist_id, "rank": len(found) + 1})
    return found


def parse_oembed(payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        return {}
    return {
        "title": payload.get("title"),
        "thumbnail": payload.get("thumbnail_url"),
    }


def _release_items(rows: list[tuple[str, str, str]]):
    items = []
    for title, kind, year in rows:
        item = make_item(title=title, kind="release", raw=year, precision="year", detail=kind)
        if item is not None:
            items.append(item)
    return sort_items(items)


def _basic(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


async def _token(http, client_id: str, client_secret: str) -> str:
    now = time.monotonic()
    if _TOKEN["value"] and now < _TOKEN["expires"]:
        return _TOKEN["value"]
    try:
        result, payload = await http.post_form(
            TOKEN_URL,
            {"grant_type": "client_credentials"},
            headers={"Authorization": _basic(client_id, client_secret)},
            timeout=10,
        )
    except TransportError as exc:
        raise TransportError(str(exc)) from exc
    if not result.ok or not isinstance(payload, dict) or not payload.get("access_token"):
        message = ""
        if isinstance(payload, dict):
            message = payload.get("error_description") or payload.get("error") or ""
        text = scrub(str(message or f"HTTP {result.status}"), [client_id, client_secret])
        raise TransportError(f"Spotify rejected the client credentials ({text}).")
    _TOKEN["value"] = payload["access_token"]
    _TOKEN["expires"] = now + max(int(payload.get("expires_in") or 3600) - 60, 30)
    return _TOKEN["value"]


async def _api(http, token: str, path: str, params: dict, secrets: list[str]):
    try:
        result, payload = await http.get_json(
            f"{API}{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except TransportError as exc:
        raise TransportError(str(exc)) from exc
    if result.status == 401:
        _TOKEN["value"] = ""
        raise TransportError("Spotify refused the access token.")
    if not result.ok or not isinstance(payload, dict):
        message = ""
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = error.get("message") or ""
        raise TransportError(scrub(message or f"Spotify returned HTTP {result.status}.", secrets))
    return payload


def parse_albums(payload: dict) -> list:
    items = []
    for album in payload.get("items") or []:
        precision = spotify_precision(album.get("release_date") or "", album.get("release_date_precision"))
        item = make_item(
            title=album.get("name") or "Untitled",
            kind="release",
            raw=album.get("release_date") or "",
            precision=precision,
            url=(album.get("external_urls") or {}).get("spotify"),
            detail=album.get("album_type") or album.get("album_group"),
        )
        if item is not None:
            items.append(item)
    return sort_items(items)


async def _fetch_api(name: str, artist_id: str, env_secrets: tuple[str, str], http, cache) -> SourceResult:
    client_id, client_secret = env_secrets
    secrets = [client_id, client_secret]

    async def produce() -> SourceResult:
        from analytics import names_match

        try:
            token = await _token(http, client_id, client_secret)
            resolved = artist_id
            if not resolved:
                search = await _api(
                    http,
                    token,
                    "/search",
                    {"q": f'artist:"{name}"', "type": "artist", "limit": "5"},
                    secrets,
                )
                for artist in (search.get("artists") or {}).get("items") or []:
                    if names_match(artist.get("name") or "", name):
                        resolved = artist.get("id") or ""
                        break
                if not resolved:
                    return unavailable("spotify", "Spotify", "No exact Spotify artist match for that name.")
            artist = await _api(http, token, f"/artists/{resolved}", {}, secrets)
            albums = await _api(
                http,
                token,
                f"/artists/{resolved}/albums",
                {"include_groups": "album,single", "limit": "50", "market": "US"},
                secrets,
            )
            top = await _api(http, token, f"/artists/{resolved}/top-tracks", {"market": "US"}, secrets)
        except TransportError as exc:
            return errored("spotify", "Spotify", str(exc))
        items = parse_albums(albums)
        followers = (artist.get("followers") or {}).get("total")
        follower_exact = followers if isinstance(followers, int) and not isinstance(followers, bool) and followers >= 0 else None
        tracks = []
        for track in (top.get("tracks") or [])[:5]:
            score = track.get("popularity")
            tracks.append(f"{track.get('name')} ({score})" if score is not None else track.get("name"))
        return SourceResult(
            platform="spotify",
            label="Spotify",
            status="ok",
            profile_name=artist.get("name"),
            profile_url=(artist.get("external_urls") or {}).get("spotify"),
            metrics=[
                Metric(
                    label="Followers",
                    available=follower_exact is not None,
                    value=f"{follower_exact:,}" if follower_exact is not None else None,
                    exact=follower_exact,
                    reason=None if follower_exact is not None else "Spotify did not return a follower count.",
                ),
                Metric(
                    label="Top tracks",
                    available=bool(tracks),
                    value=", ".join(track for track in tracks if track) or None,
                    reason=None if tracks else "Spotify did not return top tracks.",
                ),
            ],
            items=items,
            notices=[
                "Optional Web API client-credentials path. release_date_precision is preserved. "
                "This path is unused unless both Spotify secrets are set."
            ],
        )

    return await cached(cache, cache_key("spotify-api", name, artist_id), produce)


def _top_track_metric(embed_tracks: list[str], popular: list[tuple[str, int | None]]) -> Metric:
    plays = {title.casefold(): count for title, count in popular}
    labels = []
    names = embed_tracks or [title for title, _ in popular]
    for title in names[:5]:
        count = plays.get(title.casefold())
        pretty = compact_count(count) if count is not None else None
        labels.append(f"{title} ({pretty} plays)" if pretty else title)
    if not labels:
        return Metric(
            label="Top tracks",
            available=False,
            reason="The public artist and embed pages did not include a top-track list.",
        )
    return Metric(label="Top tracks", available=True, value=", ".join(labels))


async def _fetch_public(artist_id: str, http, cache, *, include_related: bool = False) -> SourceResult:
    async def produce() -> SourceResult:
        artist_url = f"https://open.spotify.com/artist/{artist_id}"
        embed_url = f"https://open.spotify.com/embed/artist/{artist_id}"
        try:
            oembed_result, oembed_payload = await http.get_json(
                OEMBED,
                params={"url": artist_url},
                timeout=12,
            )
        except TransportError:
            oembed_result, oembed_payload = None, None
        page_cap = FETCH_BYTES
        embed_cap = CRAWL_EMBED_BYTES
        try:
            artist_page = await http.get_text(
                artist_url,
                timeout=15,
                allow_redirects=False,
                max_bytes=page_cap,
            )
        except TransportError as exc:
            return unavailable("spotify", "Spotify", str(exc))
        try:
            embed_page = await http.get_text(
                embed_url,
                timeout=12,
                allow_redirects=False,
                max_bytes=embed_cap,
            )
        except TransportError:
            embed_page = None

        if artist_page.status in {301, 302, 303, 307, 308} and "login" in artist_page.header("location").lower():
            return unavailable(
                "spotify",
                "Spotify",
                "Spotify redirected the public artist page to a login wall. Release dates were not read.",
            )
        if artist_page.status in {403, 429}:
            return unavailable("spotify", "Spotify", f"Spotify blocked the public artist page (HTTP {artist_page.status}).")
        if not artist_page.ok and (embed_page is None or not embed_page.ok):
            return unavailable("spotify", "Spotify", f"The public Spotify page returned HTTP {artist_page.status}.")

        oembed = parse_oembed(oembed_payload if oembed_result and oembed_result.ok else None)
        page_text = artist_page.text if artist_page.ok else ""
        shelf = shelf_window(page_text)
        lines = visible_lines(shelf)
        listeners, listeners_exact = parse_monthly_listeners(listener_slices(page_text), lines)
        follower_count = parse_spotify_followers(lines)
        discography = parse_discography(lines)
        popular = parse_popular(lines)
        # Related-artist HTML is crawl-only. The dashboard keeps the 9f16bbd card path.
        related = parse_related_artists(page_text) if include_related else []
        embed = parse_embed_state(embed_page.text) if embed_page is not None and embed_page.ok else {}
        items = _release_items(discography)
        listener_exact = int(listeners.replace(",", "")) if listeners_exact and listeners else None
        if not items and listeners is None and not (embed.get("tracks") or popular) and not oembed.get("title"):
            return unavailable(
                "spotify",
                "Spotify",
                "The public Spotify pages did not include listeners, top tracks, or release years. The markup may have changed.",
            )
        notices = [
            "Read from open.spotify.com oEmbed plus the public artist and embed pages. "
            "No Web API token is used. Album and single rows keep the year Spotify prints; they are not given a day or clock time. "
            "Exact release timestamps come from Apple Music."
        ]
        if not listeners_exact and listeners:
            notices.append("Monthly listeners are Spotify's rounded public figure, not an exact count.")
        if not items:
            notices.append("The public artist page did not include a year-labeled discography.")
        return SourceResult(
            platform="spotify",
            label="Spotify",
            status="ok",
            profile_name=oembed.get("title") or embed.get("name"),
            profile_url=artist_url,
            image_url=oembed.get("thumbnail"),
            metrics=[
                Metric(
                    label="Monthly listeners",
                    available=listeners is not None,
                    value=listeners,
                    exact=listener_exact,
                    reason=None if listeners is not None else "The public artist page did not include a monthly listener count.",
                ),
                Metric(
                    label="Followers",
                    available=follower_count is not None,
                    value=f"{follower_count:,}" if follower_count is not None else None,
                    exact=follower_count,
                    reason=None if follower_count is not None else "The public artist page did not include a follower count.",
                ),
                _top_track_metric(embed.get("tracks") or [], popular),
                Metric(label="Releases with a year", available=True, value=str(len(items))),
            ],
            related=related,
            items=items[:40],
            notices=notices,
        )

    # A separate key so a dashboard cache entry (no related list) is not reused by a crawl.
    cache_parts = (artist_id, "related") if include_related else (artist_id,)
    return await cached(cache, cache_key("spotify-public", *cache_parts), produce)


async def fetch_spotify(name: str, links: dict, env, http, cache, include_related: bool = False) -> SourceResult:
    artist_id = (links or {}).get("spotify_id") or ""
    client_id = env_get(env, "SPOTIFY_CLIENT_ID")
    client_secret = env_get(env, "SPOTIFY_CLIENT_SECRET")
    # The public search HTML shell does not include artist ids, and oEmbed
    # rejects search URLs. Guessing an id would show the wrong artist.
    if not artist_id and not (client_id and client_secret):
        reason = (
            "MusicBrainz has no official Spotify URL for this artist. "
            "The public search page does not include artist ids, so an id is not guessed."
        )
        if (links or {}).get("_incomplete"):
            reason = (
                "MusicBrainz did not return URL relationships. "
                "The public Spotify search page does not include artist ids, so an id is not guessed."
            )
        return unavailable("spotify", "Spotify", reason, ttl=120)
    if client_id and client_secret:
        return await _fetch_api(name, artist_id, (client_id, client_secret), http, cache)
    return await _fetch_public(artist_id, http, cache, include_related=include_related)
