"""Persist audience counts, first-seen content, and Spotify related-artist edges."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from analytics import (
    PRECISION_LABELS,
    SOURCE_COLORS,
    build_posting_patterns,
    build_release_calendar,
    calendar_navigation,
    empty_posting_patterns,
    empty_release_calendar,
    normalize_name,
    release_years,
)
from charts import CHARTS_EMPTY, artist_chart_statements, charts_from_reads
from db import log_read_failure, missing_table, parse_utc, read_many, utc_now
from models import make_item
from scores import SCORE_EMPTY, SCORE_FORMULA, artist_score_statement, load_artist_score, load_scores, score_from_counts

# Only exact audience counts. Sample sizes and rounded "1.2M" text are not a growth series.
AUDIENCE = {
    ("spotify", "Monthly listeners"): "Spotify monthly listeners",
    ("spotify", "Followers"): "Spotify followers",
    ("instagram", "Followers"): "Instagram followers",
    ("tiktok", "Followers"): "TikTok followers",
    ("youtube", "Subscribers"): "YouTube subscribers",
    ("deezer", "Fans"): "Deezer fans",
}
SNAPSHOT_REPEAT_SECONDS = 3600
RELATED_RANK_LIMIT = 5
# Sightings are a crawl write, not a dashboard parse. Cap rows so one artist cannot
# turn into a prepare-per-item loop.
SIGHTINGS_PER_SOURCE = 8
SIGHTING_CAP = 24
GROWTH_ROW_LIMIT = 120
# One growth line. The earliest and latest readings stay so the delta is exact.
SERIES_POINT_CAP = 240
# Twelve month grids are most of the artist-page HTML. Older months stay on /calendar.
ARTIST_CALENDAR_MONTHS = 4
RELATED_READ_LIMIT = 20
SIGHTING_READ_LIMIT = 20
PLAYLIST_READ_LIMIT = 50
PLAYLIST_NEW_DAYS = 7
# A row refreshed once a day is still current. A row missing for longer than this ages out.
PLAYLIST_CURRENT_GRACE = timedelta(hours=36)
TRACK_CURRENT_HOURS = 36
PLAYLIST_NOT_CRAWLED = (
    "Deezer playlists have not been crawled yet. "
    "The crawler reads public playlists from Deezer. Nothing here is estimated."
)
# Spotify still has no placement list. The card keeps this as a note beside the Deezer rows.
PLAYLIST_SPOTIFY_NOTE = (
    "Spotify does not publish which playlists include an artist. "
    "The Web API lists albums and top tracks, and a playlist search only matches names, not track placements."
)
PLAYLIST_READ_ERROR = "Data temporarily unavailable. Stored playlists could not be read on this request."
TRACK_READ_LIMIT = 10
TRACKS_EMPTY = (
    "No Spotify top tracks have been stored yet. "
    "The crawler reads them from the public artist page. "
    "A popularity score stays blank until Spotify returns a 0–100 score. "
    "Nothing here is estimated."
)
TRACKS_READ_ERROR = "Data temporarily unavailable. Stored tracks could not be read on this request."
TRACK_STATS_EMPTY = (
    "Track stats are not yet collected. "
    "A Deezer rank score or a Spotify play count appears here only after a crawl stores the number that source published. "
    "Nothing here is estimated."
)
TRACK_STATS_READ_ERROR = "Data temporarily unavailable. Stored track stats could not be read on this request."
GROWTH_EMPTY = "No exact audience count has been stored yet."
GROWTH_READ_ERROR = "Data temporarily unavailable. Stored growth could not be read on this request."
ALERT_READ_LIMIT = 20
ALERTS_PAGE_LIMIT = 40
RELEASE_READ_LIMIT = 50
EXACT_TIME_READ_LIMIT = 80
RELEASE_SIGHTING_READ_LIMIT = 60
# One artist, one year. The jump list is distinct years, not every release row.
CALENDAR_YEAR_LIMIT = 100
CALENDAR_PERIOD_LIMIT = 200
CALENDAR_DATE_LIMIT = 500
RELEASES_EMPTY = "No official MusicBrainz albums, EPs, or singles with a release date have been stored yet."
POSTING_READ_ERROR = "Data temporarily unavailable. Stored timestamps could not be read on this request."
CALENDAR_READ_ERROR = "Data temporarily unavailable. Stored releases could not be read on this request."
ALERTS_EMPTY = (
    "No growth alerts yet. The crawler stores one when a 24h change reaches 2% "
    "or a 7d change reaches 5%, or the absolute jump is large. The same two readings are not stored twice."
)
ONE_READING = "one reading, no trend yet"
# D1 rejects a statement with more than 100 bound parameters.
_PARAM_BUDGET = 90


def _whole_number(value) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if value < 0 or not value.is_integer():
            return None
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[\d,]+", text):
            return int(text.replace(",", ""))
    return None


def popularity_score(value) -> int | None:
    """A stored 0–100 score. Missing stays missing, and a play count is not a score."""
    number = _whole_number(value)
    if number is None or number > 100:
        return None
    return number


# SQLite and the crawler share JavaScript's safe integer range. A bigger figure would round.
_MAX_STAT = 9_007_199_254_740_991
TRACK_STAT_METRICS = ("deezer_rank", "spotify_playcount", "spotify_popularity")


def stat_integer(value) -> int | None:
    """A published non-negative integer. Commas and floats are not a stored stat."""
    if isinstance(value, bool) or value is None or isinstance(value, float):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        return None
    if number < 0 or number > _MAX_STAT:
        return None
    return number


def audience_exact(metric: dict) -> int | None:
    """An audience point is an integer. Rounded text such as 1.2M is not one."""
    exact = _whole_number(metric.get("exact"))
    if exact is not None:
        return exact
    return _whole_number(metric.get("value"))


def format_span(span_ms: int) -> str:
    """Compact duration for a delta row. Hours stay whole when the span is exact."""
    seconds = max(0, int(span_ms // 1000))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 48 * 3600:
        if seconds % 3600 == 0:
            return f"{seconds // 3600}h"
        return f"{seconds / 3600:.1f}h"
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    return f"{seconds / 86400:.1f}d"


def format_delta(change: int, span_ms: int) -> str:
    return f"{change:+,} over {format_span(span_ms)}"


def _epoch_ms(captured: str | None) -> int | None:
    moment = parse_utc(captured)
    if moment is None:
        return None
    return int(moment.timestamp() * 1000)


def downsample_points(points: list[dict], limit: int = SERIES_POINT_CAP) -> list[dict]:
    """Keep the earliest and latest stored points and an even sample between them.

    Dropped points are omitted. Nothing is interpolated.
    """
    count = len(points)
    if count <= limit or limit < 2:
        return points
    last = count - 1
    chosen = {0, last}
    step = last / (limit - 1)
    for index in range(limit):
        chosen.add(min(last, int(round(index * step))))
    return [points[index] for index in sorted(chosen)]


def build_growth(rows: list[dict]) -> dict:
    """One dataset per source and metric, with only the stored exact readings.

    Points are ``{x, y}`` in epoch milliseconds. Series are not aligned onto one
    another, so a Spotify reading at :32 and an Instagram reading at :33 do not
    become nulls. Nothing is interpolated.
    """
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        key = (row.get("source"), row.get("metric"))
        number = _whole_number(row.get("value"))
        captured = row.get("captured_at")
        x = _epoch_ms(captured if isinstance(captured, str) else None)
        if key not in AUDIENCE or number is None or x is None:
            continue
        grouped.setdefault(key, []).append({"x": x, "y": number})
    datasets = []
    for key in AUDIENCE:
        items = grouped.get(key)
        if not items:
            continue
        items.sort(key=lambda point: point["x"])
        points: list[dict] = []
        for point in items:
            if points and points[-1]["x"] == point["x"]:
                points[-1] = point
            else:
                points.append(point)
        earliest = points[0]
        latest = points[-1]
        shown = downsample_points(points)
        if len(points) == 1:
            delta = {
                "latest": latest["y"],
                "earliest": earliest["y"],
                "change": None,
                "span_seconds": None,
                "latest_text": f"{latest['y']:,}",
                "text": ONE_READING,
            }
        else:
            change = latest["y"] - earliest["y"]
            span_ms = latest["x"] - earliest["x"]
            delta = {
                "latest": latest["y"],
                "earliest": earliest["y"],
                "change": change,
                "span_seconds": span_ms // 1000,
                "latest_text": f"{latest['y']:,}",
                "text": format_delta(change, span_ms),
            }
        datasets.append(
            {
                "label": AUDIENCE[key],
                "platform": key[0],
                "metric": key[1],
                "color": SOURCE_COLORS.get(key[0], "#94a3b8"),
                "data": shown,
                "capped": len(shown) < len(points),
                "delta": delta,
            }
        )
    return {"datasets": datasets}


def present_related(rows: list[dict]) -> list[dict]:
    """Rows for the Fans also like list. A name links only when to_artist_id is stored."""
    presented = []
    for row in rows or []:
        name = str(row.get("to_name") or "").strip()
        if not name:
            continue
        artist_id = row.get("to_artist_id")
        try:
            linked = int(artist_id) if artist_id is not None and str(artist_id) != "" else None
        except (TypeError, ValueError):
            linked = None
        presented.append(
            {
                "to_name": name[:120],
                "to_spotify_id": str(row.get("to_spotify_id") or ""),
                "to_artist_id": linked,
                "rank": row.get("rank"),
                "first_seen_at": str(row.get("first_seen_at") or ""),
                "last_seen_at": str(row.get("last_seen_at") or ""),
            }
        )
    return presented


def format_tenths(tenths: int | None) -> str | None:
    """Display for a percent stored as integer tenths. 20 is +2%."""
    if tenths is None:
        return None
    try:
        number = int(tenths)
    except (TypeError, ValueError):
        return None
    sign = "+" if number > 0 else ("-" if number < 0 else "")
    whole, frac = divmod(abs(number), 10)
    if frac == 0:
        return f"{sign}{whole}%"
    return f"{sign}{whole}.{frac}%"


def present_releases(rows: list[dict]) -> list[dict]:
    """Release-group dates at the stored precision. A year is not turned into a day."""
    presented = []
    for row in rows or []:
        title = str(row.get("title") or "").strip()
        mbid = str(row.get("mbid") or "").strip()
        if not title or not mbid:
            continue
        released = str(row.get("released_at") or "")
        precision = str(row.get("precision") or "")
        first_seen = str(row.get("first_seen_at") or "")
        presented.append(
            {
                "mbid": mbid[:64],
                "title": title[:200],
                "release_type": str(row.get("release_type") or ""),
                "released_at": released,
                "precision": precision,
                "precision_label": PRECISION_LABELS.get(precision, precision or "Unknown"),
                "first_seen_at": first_seen,
                "last_seen_at": str(row.get("last_seen_at") or ""),
                "_sort": timeline_instant(released, precision, first_seen),
            }
        )
    presented.sort(
        key=lambda item: ((item["_sort"] or datetime.min.replace(tzinfo=timezone.utc)).timestamp(), item["title"]),
        reverse=True,
    )
    for item in presented:
        item.pop("_sort", None)
    return presented


def present_alerts(rows: list[dict]) -> list[dict]:
    presented = []
    for row in rows or []:
        delta = _whole_number(row.get("delta"))
        if delta is None and row.get("delta") not in (0, "0"):
            signed = row.get("delta")
            if isinstance(signed, int) and not isinstance(signed, bool):
                delta = signed
            elif isinstance(signed, str) and re.fullmatch(r"-?\d+", signed.strip()):
                delta = int(signed.strip())
        if not isinstance(delta, int):
            continue
        baseline = _whole_number(row.get("baseline_value"))
        latest = _whole_number(row.get("latest_value"))
        if baseline is None or latest is None:
            continue
        pct = row.get("delta_pct")
        tenths = int(pct) if isinstance(pct, int) and not isinstance(pct, bool) else None
        if tenths is None and isinstance(pct, str) and re.fullmatch(r"-?\d+", pct.strip()):
            tenths = int(pct.strip())
        presented.append(
            {
                "artist_id": row.get("artist_id"),
                "artist_name": str(row.get("artist_name") or ""),
                "provider": str(row.get("provider") or ""),
                "metric": str(row.get("metric") or ""),
                "window": str(row.get("window") or ""),
                "delta": delta,
                "delta_text": f"{delta:+,}",
                "percent": format_tenths(tenths),
                "baseline_value": baseline,
                "latest_value": latest,
                "detected_at": str(row.get("detected_at") or ""),
            }
        )
    return presented


def _editorial(value) -> bool:
    if value is True or value == 1:
        return True
    if isinstance(value, str) and value.strip() == "1":
        return True
    return False


def _playlist_url(source: str, playlist_id: str) -> str | None:
    if source == "deezer" and playlist_id.isdigit():
        return f"https://www.deezer.com/playlist/{playlist_id}"
    if source in {"", "spotify"} and re.fullmatch(r"[A-Za-z0-9]+", playlist_id):
        return f"https://open.spotify.com/playlist/{playlist_id}"
    return None


def _as_utc(now: datetime | None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _playlist_watermark(rows: list[dict]) -> datetime | None:
    """Latest Deezer playlist crawl, or the newest stored last_seen when no crawl row exists."""
    crawled = []
    seen = []
    for row in rows or []:
        crawl = parse_utc(str(row.get("crawled_at") or ""))
        if crawl is not None:
            crawled.append(crawl)
        last = parse_utc(str(row.get("last_seen_at") or ""))
        if last is not None:
            seen.append(last)
    if crawled:
        return max(crawled)
    if seen:
        return max(seen)
    return None


def empty_playlist_counts() -> dict:
    return {"current": 0, "new": 0, "dropped": 0}


def present_playlist_rows(rows: list[dict] | None, now: datetime | None = None) -> tuple[list[dict], dict]:
    """Placements plus counts. A missing follower count stays missing, not zero.

    Current rows were seen within 36 hours of the latest playlist crawl, so a
    placement refreshed once a day stays current. New rows are current and were
    first stored within the last 7 days. A row that has been missing longer than
    that grace is dropped.
    """
    moment = _as_utc(now)
    cutoff = moment - timedelta(days=PLAYLIST_NEW_DAYS)
    watermark = _playlist_watermark(rows or [])
    current_after = watermark - PLAYLIST_CURRENT_GRACE if watermark is not None else None
    presented = []
    current = new = dropped = 0
    for row in rows or []:
        name = str(row.get("playlist_name") or "").strip()
        playlist_id = str(row.get("playlist_id") or "").strip()
        if not name or not playlist_id:
            continue
        followers = _whole_number(row.get("follower_count"))
        position = _whole_number(row.get("position"))
        if position is not None and position < 1:
            position = None
        source = str(row.get("source") or "").strip().lower()[:40]
        first_text = str(row.get("first_seen_at") or "")
        last_text = str(row.get("last_seen_at") or "")
        first = parse_utc(first_text)
        last = parse_utc(last_text)
        is_current = current_after is not None and last is not None and last >= current_after
        is_dropped = current_after is not None and last is not None and last < current_after
        if is_current:
            current += 1
            if first is not None and first >= cutoff:
                new += 1
        elif is_dropped:
            dropped += 1
        presented.append(
            {
                "playlist_id": playlist_id[:80],
                "name": name[:200],
                "owner_name": str(row.get("owner_name") or "").strip()[:120],
                "source": source,
                "url": _playlist_url(source, playlist_id[:80]),
                "editorial": _editorial(row.get("editorial")),
                "follower_count": followers,
                "follower_text": f"{followers:,}" if followers is not None else None,
                "position": position,
                "first_seen_at": first_text,
                "last_seen_at": last_text,
                "dropped": is_dropped,
                "current": is_current,
            }
        )
    return presented, {"current": current, "new": new, "dropped": dropped}


def present_playlists(rows: list[dict], now: datetime | None = None) -> list[dict]:
    """Stored playlist rows. A missing follower count stays missing, not zero."""
    playlists, _counts = present_playlist_rows(rows, now)
    return playlists


def playlist_counts(rows: list[dict], now: datetime | None = None) -> dict:
    """Current, new-in-7-days, and dropped counts for one bounded read."""
    _playlists, counts = present_playlist_rows(rows, now)
    return counts


def present_tracks(rows: list[dict]) -> list[dict]:
    """Stored top tracks. A missing popularity stays missing, not zero."""
    presented = []
    for row in rows or []:
        name = " ".join(str(row.get("track_name") or row.get("name") or "").split())
        external = str(row.get("external_id") or "").strip()
        if not name or not external:
            continue
        rank = _whole_number(row.get("rank"))
        if rank is not None and (rank < 1 or rank > 100):
            rank = None
        album = " ".join(str(row.get("album_name") or "").split())
        spotify_id = external if re.fullmatch(r"[A-Za-z0-9]{10,32}", external) else ""
        presented.append(
            {
                "name": name[:200],
                "external_id": external[:80],
                "popularity": popularity_score(row.get("popularity")),
                "album_name": album[:200] or None,
                "rank": rank,
                "first_seen_at": str(row.get("first_seen_at") or ""),
                "last_seen_at": str(row.get("last_seen_at") or ""),
                "url": f"https://open.spotify.com/track/{spotify_id}" if spotify_id else None,
            }
        )
    return presented


def _clean_tracks(raw) -> list[dict]:
    """Rows the crawler is willing to store. Play counts never become popularity."""
    cleaned = []
    seen = set()
    for item in raw or []:
        if len(cleaned) >= TRACK_READ_LIMIT or not isinstance(item, dict):
            continue
        name = " ".join(str(item.get("name") or item.get("track_name") or "").split())
        external = str(item.get("external_id") or "").strip()
        if not name or not external or len(external) > 80:
            continue
        external = external[:80]
        if external in seen:
            continue
        seen.add(external)
        rank = _whole_number(item.get("rank"))
        if rank is not None and (rank < 1 or rank > 100):
            rank = None
        album = " ".join(str(item.get("album_name") or "").split())
        cleaned.append(
            {
                "name": name[:200],
                "external_id": external,
                "popularity": popularity_score(item.get("popularity")),
                "album_name": album[:200] or None,
                "rank": rank,
                "deezer_rank": stat_integer(item.get("deezer_rank")),
                "playcount": stat_integer(item.get("playcount")),
            }
        )
    return cleaned


def timeline_instant(published: str | None, precision: str | None, first_seen: str | None) -> datetime | None:
    """Sort key. A year or a day stays at that precision and is not given a fake clock time."""
    text = (published or "").strip()
    kind = (precision or "").strip()
    if kind == "time":
        moment = parse_utc(text)
        if moment is not None:
            return moment
    elif kind == "day" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    elif kind == "month" and re.fullmatch(r"\d{4}-\d{2}", text):
        return datetime.strptime(text + "-01", "%Y-%m-%d").replace(tzinfo=timezone.utc)
    elif kind == "year" and re.fullmatch(r"\d{4}", text):
        return datetime(int(text), 1, 1, tzinfo=timezone.utc)
    return parse_utc(first_seen)


def present_sightings(rows: list[dict]) -> list[dict]:
    """Timeline order: source published time when it is usable, otherwise first seen."""
    presented = []
    for row in rows or []:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        published = str(row.get("published_at") or "")
        precision = str(row.get("precision") or "")
        first_seen = str(row.get("first_seen_at") or "")
        presented.append(
            {
                "source": str(row.get("source") or ""),
                "title": title[:200],
                "kind": str(row.get("kind") or ""),
                "published_at": published,
                "precision": precision,
                "precision_label": PRECISION_LABELS.get(precision, precision or "Unknown"),
                "first_seen_at": first_seen,
                "_sort": timeline_instant(published, precision, first_seen),
            }
        )
    presented.sort(key=lambda item: ((item["_sort"] or datetime.min.replace(tzinfo=timezone.utc)).timestamp(), item["first_seen_at"]), reverse=True)
    for item in presented:
        item.pop("_sort", None)
    return presented


def empty_history(reason: str) -> dict:
    return {
        "available": False,
        "reason": reason,
        "growth": {"labels": [], "datasets": []},
        "related": [],
        "sightings": [],
        "playlists": [],
        "playlist_counts": empty_playlist_counts(),
        "playlists_reason": PLAYLIST_NOT_CRAWLED,
        "playlists_note": PLAYLIST_SPOTIFY_NOTE,
        "tracks": [],
        "tracks_reason": TRACKS_EMPTY,
        "track_stats": [],
        "track_stats_reason": TRACK_STATS_EMPTY,
        "momentum": [],
        "momentum_reason": None,
        "alerts": [],
        "alerts_reason": ALERTS_EMPTY,
        "releases": [],
        "releases_reason": RELEASES_EMPTY,
        "charts": [],
        "charts_reason": CHARTS_EMPTY,
        "score_text": None,
        "score_components": [],
        "score_formula": SCORE_FORMULA,
        "score_reason": SCORE_EMPTY,
        "posting": empty_posting_patterns(),
        "calendar": empty_release_calendar(),
        "deezer": None,
        "artist_id": None,
    }


def _deezer_identity(bundle: dict, links: dict) -> tuple[str | None, int | None]:
    """Deezer id from a confirmed provider match, else a MusicBrainz URL. Album count is not a snapshot."""
    deezer_id = _blank(links.get("deezer_id"))
    albums = None
    for source in bundle.get("sources") or []:
        if source.get("platform") != "deezer" or source.get("status") == "error":
            continue
        source_id = _blank((source.get("links") or {}).get("deezer_id"))
        if source_id:
            deezer_id = source_id
        for metric in source.get("metrics") or []:
            if metric.get("label") == "Albums" and metric.get("available"):
                albums = audience_exact(metric)
    return deezer_id, albums


def _links(bundle: dict) -> dict:
    for source in bundle.get("sources") or []:
        if source.get("platform") == "musicbrainz":
            return dict(source.get("links") or {})
    return {}


def _mbid(bundle: dict) -> str | None:
    for source in bundle.get("sources") or []:
        if source.get("platform") != "musicbrainz":
            continue
        match = re.search(r"musicbrainz.org/artist/([0-9a-f-]{36})", source.get("profile_url") or "")
        return match.group(1) if match else None
    return None


def _blank(value) -> str | None:
    text = (value or "").strip() if isinstance(value, str) else ""
    return text or None


def _value_statements(prefix: str, rows: list[tuple], suffix: str = "", fixed: tuple = ()) -> list[tuple[str, tuple]]:
    """Multi-row INSERT statements, each under D1's bound-parameter cap.

    Placeholders in ``prefix`` and ``suffix`` count against the same budget.
    ``fixed`` is bound in source order around the row values.
    """
    if not rows:
        return []
    width = len(rows[0])
    outside = prefix.count("?") + suffix.count("?")
    if len(fixed) != outside:
        raise ValueError("fixed parameters must match placeholders outside the row")
    room = _PARAM_BUDGET - outside
    if width < 1 or width > room:
        raise ValueError("one row exceeds the D1 parameter budget")
    per = max(1, room // width)
    marker = "(" + ",".join("?" for _ in range(width)) + ")"
    tail = f" {suffix}" if suffix else ""
    head = fixed[: prefix.count("?")]
    foot = fixed[prefix.count("?") :]
    statements = []
    for offset in range(0, len(rows), per):
        chunk = rows[offset : offset + per]
        sql = prefix + " VALUES " + ",".join(marker for _ in chunk) + tail
        params = head + tuple(value for row in chunk for value in row) + foot
        statements.append((sql, params))
    return statements


async def _latest_track_stat_map(db, artist_id: int, pairs: list[tuple[str, str, str]]) -> dict[tuple[str, str, str], dict]:
    """Latest row per track metric. One indexed statement per source, not a UNION.

    SQLite returns ``value`` from the row that holds ``MAX(captured_at)``.
    A UNION of per-track LIMIT 1 reads exceeds D1's compound SELECT term limit.
    """
    ids_by_source: dict[str, list[str]] = {}
    metrics_by_source: dict[str, list[str]] = {}
    wanted: set[tuple[str, str, str]] = set()
    for source, external_id, metric in pairs:
        key = (source, external_id, metric)
        if key in wanted or metric not in TRACK_STAT_METRICS:
            continue
        wanted.add(key)
        external_ids = ids_by_source.setdefault(source, [])
        if external_id not in external_ids:
            external_ids.append(external_id)
        metrics = metrics_by_source.setdefault(source, [])
        if metric not in metrics:
            metrics.append(metric)
    found: dict[tuple[str, str, str], dict] = {}
    for source, external_ids in ids_by_source.items():
        metrics = metrics_by_source[source]
        rows = await db.query(
            "SELECT source, external_id, metric, value, MAX(captured_at) AS captured_at "
            "FROM track_stats "
            "WHERE artist_id = ? AND source = ? "
            f"AND metric IN ({','.join('?' * len(metrics))}) "
            f"AND external_id IN ({','.join('?' * len(external_ids))}) "
            "GROUP BY external_id, metric",
            (artist_id, source, *metrics, *external_ids),
        )
        for row in rows:
            key = (row.get("source"), row.get("external_id"), row.get("metric"))
            if key in wanted:
                found[key] = row
    return found


async def _track_stat_rows(db, artist_id: int, candidates: list[tuple], stamp: str) -> list[tuple]:
    """At most one row per track and metric per UTC day, and none when the value is unchanged."""
    if not candidates:
        return []
    prior = await _latest_track_stat_map(db, artist_id, [(item[0], item[1], item[2]) for item in candidates])
    day = stamp[:10]
    rows = []
    for source, external_id, metric, value in candidates:
        previous = prior.get((source, external_id, metric))
        if previous:
            previous_day = str(previous.get("captured_at") or "")[:10]
            previous_value = stat_integer(previous.get("value"))
            if previous_day == day or previous_value == value:
                continue
        rows.append((artist_id, source, external_id, metric, value, stamp))
    return rows


async def _latest_track_scores(db, artist_id: int) -> dict[str, dict]:
    rows = await db.query(
        """
        SELECT external_id, popularity, captured_at
        FROM track_snapshots
        WHERE artist_id = ? AND source = 'spotify'
        ORDER BY captured_at DESC, id DESC
        LIMIT 40
        """,
        (artist_id,),
    )
    newest = {}
    for row in rows:
        external = row.get("external_id")
        if external and external not in newest:
            newest[external] = row
    return newest


async def _latest_by_metric(db, artist_id: int) -> dict[tuple[str, str], dict]:
    rows = await db.query(
        """
        SELECT source, metric, value, captured_at
        FROM metric_latest
        WHERE artist_id = ?
        """,
        (artist_id,),
    )
    return {(row["source"], row["metric"]): row for row in rows}


async def remember(
    db,
    name: str,
    bundle: dict,
    *,
    origin: str = "search",
    now: datetime | None = None,
    related_cap: int = 25,
    artist_row: dict | None = None,
    extra=None,
) -> dict:
    moment = now or datetime.now(timezone.utc)
    stamp = utc_now(moment)
    normalized = normalize_name(name)
    links = _links(bundle)
    channel_ids = links.get("youtube_channel_ids") or []
    if artist_row is None:
        await db.execute(
            """
            INSERT INTO artists (name, normalized_name, origin, tracked, created_at)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(normalized_name) DO UPDATE SET
                name = excluded.name,
                tracked = 1,
                origin = CASE WHEN excluded.origin = 'search' THEN 'search' ELSE artists.origin END
            """,
            (name.strip()[:120], normalized, origin, stamp),
        )
        artist = await db.query_one("SELECT * FROM artists WHERE normalized_name = ?", (normalized,))
    else:
        artist = artist_row
    artist_id = int(artist["id"])
    sources = bundle.get("sources") or []
    musicbrainz_failed = any(
        source.get("platform") == "musicbrainz" and source.get("status") == "error" for source in sources
    )
    statements = [
        (
            """
            UPDATE artists SET
                musicbrainz_id = COALESCE(?, musicbrainz_id),
                spotify_id = COALESCE(?, spotify_id),
                apple_id = COALESCE(?, apple_id),
                youtube_channel_id = COALESCE(?, youtube_channel_id),
                instagram_handle = COALESCE(?, instagram_handle),
                tiktok_handle = COALESCE(?, tiktok_handle),
                deezer_id = COALESCE(?, deezer_id),
                deezer_nb_album = COALESCE(?, deezer_nb_album),
                last_crawled_at = COALESCE(?, last_crawled_at)
            WHERE id = ?
            """,
            (
                _mbid(bundle),
                _blank(links.get("spotify_id")),
                _blank(links.get("apple_id")),
                _blank(channel_ids[0] if channel_ids else None),
                _blank(links.get("instagram")),
                _blank(links.get("tiktok")),
                *_deezer_identity(bundle, links),
                None if musicbrainz_failed else stamp,
                artist_id,
            ),
        )
    ]
    snapshots = 0
    snapshot_rows = []
    sighting_rows = []
    latest = await _latest_by_metric(db, artist_id)
    for source in sources:
        platform = source.get("platform") or ""
        if source.get("status") == "error":
            continue
        for metric in source.get("metrics") or []:
            label = metric.get("label") or ""
            if (platform, label) not in AUDIENCE or not metric.get("available"):
                continue
            number = audience_exact(metric)
            if number is None:
                continue
            previous = latest.get((platform, label))
            previous_value = _whole_number(previous.get("value")) if previous else None
            if previous and previous_value is not None and previous_value == number:
                previous_at = parse_utc(previous.get("captured_at"))
                if previous_at and moment - previous_at < timedelta(seconds=SNAPSHOT_REPEAT_SECONDS):
                    continue
            snapshot_rows.append((artist_id, platform, label, number, str(metric.get("value") or number), stamp))
            snapshots += 1
        taken = 0
        for item in source.get("items") or []:
            sighting_cap = 25 if platform == "deezer" else SIGHTINGS_PER_SOURCE
            if taken >= sighting_cap or (platform != "deezer" and len(sighting_rows) >= SIGHTING_CAP):
                break
            if platform == "deezer" and (item.get("precision") == "time" or item.get("hour_utc") is not None):
                continue
            external = (item.get("url") or f"{item.get('kind')}:{item.get('title')}:{item.get('sort_key')}")[:300]
            if not external or not item.get("title"):
                continue
            sighting_rows.append(
                (
                    artist_id,
                    platform,
                    external,
                    str(item.get("title"))[:200],
                    item.get("kind"),
                    item.get("sort_key") or item.get("display"),
                    item.get("precision"),
                    stamp,
                )
            )
            taken += 1
    stored_tracks = []
    track_values = []
    stat_candidates = []
    for source in sources:
        platform = source.get("platform")
        if source.get("status") == "error" or platform not in {"spotify", "deezer"}:
            continue
        cleaned = _clean_tracks(source.get("tracks"))[:TRACK_READ_LIMIT]
        if platform == "deezer":
            for track in cleaned:
                track["popularity"] = None
                if track["deezer_rank"] is not None:
                    stat_candidates.append((platform, track["external_id"], "deezer_rank", track["deezer_rank"]))
        if platform == "spotify":
            stored_tracks.extend(cleaned)
            for track in cleaned:
                if track["playcount"] is not None:
                    stat_candidates.append((platform, track["external_id"], "spotify_playcount", track["playcount"]))
        track_values.extend(
            (
                artist_id,
                platform,
                track["external_id"],
                track["name"],
                track["popularity"],
                track["album_name"],
                track["rank"],
                stamp,
                stamp,
            )
            for track in cleaned
        )
    snapshot_values = []
    if stored_tracks:
        previous_scores = await _latest_track_scores(db, artist_id)
        for track in stored_tracks:
            score = track["popularity"]
            if score is None:
                continue
            prior = previous_scores.get(track["external_id"])
            if prior and popularity_score(prior.get("popularity")) == score:
                prior_at = parse_utc(prior.get("captured_at"))
                if prior_at and moment - prior_at < timedelta(seconds=SNAPSHOT_REPEAT_SECONDS):
                    continue
            snapshot_values.append((artist_id, "spotify", track["external_id"], score, stamp))
    stat_values = await _track_stat_rows(db, artist_id, stat_candidates, stamp)
    statements.extend(
        _value_statements(
            """
            INSERT INTO artist_tracks (
                artist_id, source, external_id, track_name, popularity, album_name, rank, first_seen_at, last_seen_at
            )
            """.strip(),
            track_values,
            """
            ON CONFLICT(artist_id, source, external_id) DO UPDATE SET
                track_name = excluded.track_name,
                popularity = COALESCE(excluded.popularity, artist_tracks.popularity),
                album_name = COALESCE(excluded.album_name, artist_tracks.album_name),
                rank = excluded.rank,
                last_seen_at = excluded.last_seen_at
            """.strip(),
        )
    )
    statements.extend(
        _value_statements(
            """
            INSERT INTO track_snapshots (artist_id, source, external_id, popularity, captured_at)
            """.strip(),
            snapshot_values,
        )
    )
    statements.extend(
        _value_statements(
            "INSERT INTO track_stats (artist_id, source, external_id, metric, value, captured_at)",
            stat_values,
        )
    )
    statements.extend(
        _value_statements(
            "INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)",
            snapshot_rows,
        )
    )
    statements.extend(
        _value_statements(
            """
            INSERT OR IGNORE INTO content_sightings
                (artist_id, source, external_id, title, kind, published_at, precision, first_seen_at)
            """.strip(),
            sighting_rows,
        )
    )
    related_sql, edges, discovered = await _store_related(db, artist_id, bundle, stamp, related_cap)
    statements.extend(related_sql)
    if extra is not None:
        statements.extend(extra(snapshots, edges, discovered))
    # One round trip for every write after the artist row exists.
    await db.batch(statements)
    return {
        "artist_id": artist_id,
        "snapshots": snapshots,
        "edges": edges,
        "discovered": discovered,
    }


async def _related_tracked(db) -> int:
    """Cached count of auto-tracked related artists. One COUNT fills the cache."""
    row = await db.query_one("SELECT related_tracked FROM crawl_state WHERE id = 1")
    if row is not None and row.get("related_tracked") is not None:
        return int(row["related_tracked"])
    counted = await db.query_one("SELECT COUNT(*) AS n FROM artists WHERE origin = 'related' AND tracked = 1")
    total = int((counted or {}).get("n") or 0)
    await db.execute(
        """
        INSERT INTO crawl_state (id, next_artist_id, updated_at, related_tracked)
        VALUES (1, 0, ?, ?)
        ON CONFLICT(id) DO UPDATE SET related_tracked = excluded.related_tracked
        """,
        (utc_now(), total),
    )
    return total


async def _store_related(db, artist_id: int, bundle: dict, stamp: str, related_cap: int) -> tuple[list[tuple[str, tuple]], int, int]:
    related = []
    for source in bundle.get("sources") or []:
        if source.get("platform") == "spotify":
            related = list(source.get("related") or [])[:12]
            break
    edge_rows = []
    for item in related:
        spotify_id = _blank(item.get("spotify_id"))
        to_name = _blank(item.get("name"))
        if not spotify_id or not to_name:
            continue
        rank = int(item.get("rank") or len(edge_rows) + 1)
        edge_rows.append((artist_id, spotify_id, to_name[:120], "spotify", rank, stamp, stamp))
    statements = _value_statements(
        """
        INSERT INTO related_edges
            (from_artist_id, to_spotify_id, to_name, source, rank, first_seen_at, last_seen_at)
        """.strip(),
        edge_rows,
        """
        ON CONFLICT(from_artist_id, to_spotify_id, source) DO UPDATE SET
            to_name = excluded.to_name,
            rank = excluded.rank,
            last_seen_at = excluded.last_seen_at
        """.strip(),
    )
    written = len(edge_rows)
    discovered = 0
    if related_cap <= 0 or not edge_rows:
        return statements, written, discovered
    before = await _related_tracked(db)
    room = related_cap - before
    if room <= 0:
        return statements, written, discovered
    self_spotify = _blank(_links(bundle).get("spotify_id"))
    candidates = []
    for item in related:
        if int(item.get("rank") or 99) > RELATED_RANK_LIMIT:
            continue
        spotify_id = _blank(item.get("spotify_id"))
        to_name = _blank(item.get("name"))
        if not spotify_id or not to_name or spotify_id == self_spotify:
            continue
        candidates.append((spotify_id, to_name[:120], normalize_name(to_name)))
    if not candidates:
        return statements, written, discovered
    marks = ",".join("?" for _ in candidates)
    existing_rows = await db.query(
        f"""
        SELECT id, spotify_id, normalized_name FROM artists
        WHERE spotify_id IN ({marks}) OR normalized_name IN ({marks})
        """,
        tuple([item[0] for item in candidates] + [item[2] for item in candidates]),
    )
    by_spotify: dict[str, int] = {}
    by_name: dict[str, int] = {}
    for existing in existing_rows:
        if existing.get("spotify_id"):
            by_spotify.setdefault(existing["spotify_id"], int(existing["id"]))
        if existing.get("normalized_name"):
            by_name.setdefault(existing["normalized_name"], int(existing["id"]))
    link_updates = []
    new_rows = []
    for spotify_id, to_name, normalized in candidates:
        if room <= 0:
            break
        found = by_spotify.get(spotify_id) or by_name.get(normalized)
        if found:
            link_updates.append((found, artist_id, spotify_id))
            continue
        new_rows.append((to_name, normalized, spotify_id, "related", 1, stamp))
        room -= 1
    if new_rows:
        statements.extend(
            _value_statements(
                "INSERT INTO artists (name, normalized_name, spotify_id, origin, tracked, created_at)",
                new_rows,
                "ON CONFLICT(normalized_name) DO NOTHING",
            )
        )
        discovered = len(new_rows)
        statements.append(
            (
                "UPDATE crawl_state SET related_tracked = COALESCE(related_tracked, 0) + ? WHERE id = 1",
                (discovered,),
            )
        )
        for to_name, normalized, spotify_id, _, _, _ in new_rows:
            link_updates.append((normalized, spotify_id, artist_id, spotify_id))
    if link_updates:
        for params in link_updates:
            if len(params) == 3:
                statements.append(
                    (
                        """
                        UPDATE related_edges SET to_artist_id = ?
                        WHERE from_artist_id = ? AND to_spotify_id = ? AND source = 'spotify'
                        """,
                        params,
                    )
                )
            else:
                # Runs after the insert in this same batch, so the new row is visible.
                statements.append(
                    (
                        """
                        UPDATE related_edges SET to_artist_id = (
                            SELECT id FROM artists
                            WHERE normalized_name = ? OR spotify_id = ?
                            LIMIT 1
                        )
                        WHERE from_artist_id = ? AND to_spotify_id = ? AND source = 'spotify'
                        """,
                        params,
                    )
                )
    return statements, written, discovered


# deezer_id is on this same row. The card does not look the artist up again.
ARTIST_LOOKUP_SQL = (
    "SELECT id, name, normalized_name, deezer_id, deezer_nb_album "
    "FROM artists WHERE normalized_name = ?"
)
_ARTIST_LOOKUP_BARE_SQL = "SELECT id, name, normalized_name FROM artists WHERE normalized_name = ?"
_ARTIST_BY_ID_SQL = (
    "SELECT id, name, normalized_name, tracked, last_crawled_at, deezer_id, deezer_nb_album "
    "FROM artists WHERE id = ?"
)
_ARTIST_BY_ID_BARE_SQL = (
    "SELECT id, name, normalized_name, tracked, last_crawled_at FROM artists WHERE id = ?"
)

SNAPSHOT_SQL = """
SELECT source, metric, value, captured_at
FROM metric_snapshots
WHERE artist_id = ? AND value IS NOT NULL
ORDER BY captured_at DESC, id DESC
LIMIT ?
"""

# Ordered by rank, not by an exact last_seen match, so a daily refresh stays visible.
RELATED_SQL = """
SELECT to_name, to_spotify_id, to_artist_id, rank, first_seen_at, last_seen_at
FROM related_edges
WHERE from_artist_id = ?
ORDER BY rank ASC, last_seen_at DESC
LIMIT ?
"""

SIGHTING_SQL = """
SELECT source, title, kind, published_at, precision, first_seen_at
FROM content_sightings
WHERE artist_id = ?
ORDER BY COALESCE(NULLIF(published_at, ''), first_seen_at) DESC, id DESC
LIMIT ?
"""

# idx_playlist_artist_seen is (artist_id, last_seen_at). The crawl time is one primary-key lookup.
# Current vs dropped is applied in Python with PLAYLIST_CURRENT_GRACE, not in this read.
PLAYLIST_SQL = """
SELECT playlist_id, playlist_name, owner_name, follower_count, position,
       first_seen_at, last_seen_at, source, editorial,
       (SELECT crawled_at FROM playlist_crawls WHERE artist_id = ? AND source = 'deezer') AS crawled_at
FROM playlist_appearances
WHERE artist_id = ?
ORDER BY last_seen_at DESC, id DESC
LIMIT ?
"""

# idx_artist_tracks_seen is (artist_id, source, last_seen_at). A track refreshed
# once a day stays in this window. One that has been off the list longer drops out.
# strftime keeps the same YYYY-MM-DDTHH:MM:SSZ form as the stored stamps.
TRACK_SQL = f"""
SELECT external_id, track_name, popularity, album_name, rank, first_seen_at, last_seen_at
FROM artist_tracks
WHERE artist_id = ? AND source = 'spotify'
  AND last_seen_at >= (
      SELECT strftime('%Y-%m-%dT%H:%M:%SZ', MAX(last_seen_at), '-{TRACK_CURRENT_HOURS} hours')
      FROM artist_tracks
      WHERE artist_id = ? AND source = 'spotify'
  )
ORDER BY CASE WHEN rank IS NULL THEN 1 ELSE 0 END, rank ASC, track_name ASC
LIMIT ?
"""

# One read for the current top tracks: latest value, the newest value on or
# before the cutoff, and the first stored value. idx_track_stats_lookup is
# (artist_id, source, external_id, metric, captured_at).
TRACK_STATS_SQL = f"""
WITH current_tracks AS (
    SELECT source, external_id, track_name, rank AS list_rank
    FROM (
        SELECT source, external_id, track_name, rank
        FROM artist_tracks
        WHERE artist_id = ? AND source = 'deezer'
          AND last_seen_at >= (
              SELECT strftime('%Y-%m-%dT%H:%M:%SZ', MAX(last_seen_at), '-{TRACK_CURRENT_HOURS} hours')
              FROM artist_tracks
              WHERE artist_id = ? AND source = 'deezer'
          )
        ORDER BY CASE WHEN rank IS NULL THEN 1 ELSE 0 END, rank ASC, track_name ASC
        LIMIT 12
    )
    UNION ALL
    SELECT source, external_id, track_name, rank AS list_rank
    FROM (
        SELECT source, external_id, track_name, rank
        FROM artist_tracks
        WHERE artist_id = ? AND source = 'spotify'
          AND last_seen_at >= (
              SELECT strftime('%Y-%m-%dT%H:%M:%SZ', MAX(last_seen_at), '-{TRACK_CURRENT_HOURS} hours')
              FROM artist_tracks
              WHERE artist_id = ? AND source = 'spotify'
          )
        ORDER BY CASE WHEN rank IS NULL THEN 1 ELSE 0 END, rank ASC, track_name ASC
        LIMIT 12
    )
)
SELECT source, external_id, metric, value, captured_at, track_name, list_rank,
       rn_latest, rn_first, rn_ago
FROM (
    SELECT stats.source AS source,
           stats.external_id AS external_id,
           stats.metric AS metric,
           stats.value AS value,
           stats.captured_at AS captured_at,
           current_tracks.track_name AS track_name,
           current_tracks.list_rank AS list_rank,
           ROW_NUMBER() OVER (
               PARTITION BY stats.source, stats.external_id, stats.metric
               ORDER BY stats.captured_at DESC
           ) AS rn_latest,
           ROW_NUMBER() OVER (
               PARTITION BY stats.source, stats.external_id, stats.metric
               ORDER BY stats.captured_at ASC
           ) AS rn_first,
           ROW_NUMBER() OVER (
               PARTITION BY stats.source, stats.external_id, stats.metric
               ORDER BY CASE WHEN stats.captured_at <= ? THEN 0 ELSE 1 END, stats.captured_at DESC
           ) AS rn_ago
    FROM track_stats AS stats
    JOIN current_tracks
      ON current_tracks.source = stats.source
     AND current_tracks.external_id = stats.external_id
    WHERE stats.artist_id = ?
) AS ranked
WHERE rn_latest = 1
   OR rn_first = 1
   OR (rn_ago = 1 AND captured_at <= ?)
LIMIT 80
"""


def _stat_flag(value) -> bool:
    return value == 1 or value == "1"


def present_track_stats(rows: list[dict] | None, cutoff: str) -> list[dict]:
    """Latest Deezer rank and Spotify play count, plus the change since a real earlier row."""
    grouped: dict[tuple[str, str, str], dict] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        source = str(row.get("source") or "")
        external = str(row.get("external_id") or "").strip()
        metric = str(row.get("metric") or "")
        if source not in {"deezer", "spotify"} or metric not in TRACK_STAT_METRICS or not external:
            continue
        value = stat_integer(row.get("value"))
        captured = str(row.get("captured_at") or "")
        if value is None or not captured:
            continue
        slot = grouped.setdefault((source, external, metric), {})
        item = {
            "value": value,
            "captured_at": captured,
            "name": " ".join(str(row.get("track_name") or "").split())[:200],
            "list_rank": _whole_number(row.get("list_rank")),
        }
        if item["list_rank"] is not None and (item["list_rank"] < 1 or item["list_rank"] > 100):
            item["list_rank"] = None
        if _stat_flag(row.get("rn_latest")):
            slot["latest"] = item
        if _stat_flag(row.get("rn_first")):
            slot["first"] = item
        if _stat_flag(row.get("rn_ago")) and captured <= cutoff:
            slot["ago"] = item
    presented = []
    for (source, external, metric), slot in grouped.items():
        latest = slot.get("latest")
        if not latest or not latest["name"]:
            continue
        ago = slot.get("ago")
        first = slot.get("first")
        baseline = None
        used_first = False
        if ago and ago["captured_at"] < latest["captured_at"]:
            baseline = ago
        elif first and first["captured_at"] < latest["captured_at"]:
            baseline = first
            used_first = True
        if baseline is None:
            change = "No earlier value"
        else:
            delta = latest["value"] - baseline["value"]
            day = baseline["captured_at"][:10]
            if delta > 0:
                change = f"+{delta:,} since {day}"
            else:
                change = f"{delta:,} since {day}"
            if used_first:
                change = f"{change} (first stored value)"
        if source == "deezer" and external.isdigit():
            url = f"https://www.deezer.com/track/{external}"
        elif re.fullmatch(r"[A-Za-z0-9]{10,32}", external):
            url = f"https://open.spotify.com/track/{external}"
        else:
            url = None
        labels = {
            "deezer_rank": "Rank score",
            "spotify_playcount": "Plays",
            "spotify_popularity": "Popularity",
        }
        presented.append(
            {
                "name": latest["name"],
                "url": url,
                "source": source,
                "source_label": "Deezer" if source == "deezer" else "Spotify",
                "metric": metric,
                "metric_label": labels[metric],
                "value": latest["value"],
                "value_text": f"{latest['value']:,}",
                "change_text": change,
                "captured_at": latest["captured_at"],
                "list_rank": latest["list_rank"],
            }
        )
    presented.sort(
        key=lambda item: (
            0 if item["source"] == "deezer" else 1,
            item["list_rank"] is None,
            item["list_rank"] or 0,
            item["name"].casefold(),
            item["metric"],
        )
    )
    return presented


# Every stored release group. last_seen_at is not a current/dropped filter.
RELEASE_SQL = """
SELECT mbid, title, release_type, released_at, precision, first_seen_at, last_seen_at
FROM musicbrainz_releases
WHERE artist_id = ?
LIMIT ?
"""

# published_at is the source clock. first_seen_at is when this app stored the row.
EXACT_TIME_SQL = """
SELECT source, published_at, precision
FROM content_sightings
WHERE artist_id = ? AND precision = 'time'
  AND source IN ('instagram', 'tiktok', 'youtube', 'apple')
ORDER BY published_at DESC, id DESC
LIMIT ?
"""

RELEASE_SIGHTING_SQL = """
SELECT source, title, kind, published_at, precision
FROM content_sightings
WHERE artist_id = ? AND source IN ('apple', 'spotify', 'deezer')
ORDER BY published_at DESC, id DESC
LIMIT ?
"""

# idx_mb_releases_artist is (artist_id, released_at). The range stays on that index.
# ':' sorts after every digit, so it includes 2006. The text '10000' sorts before '2000'.
MB_CALENDAR_YEARS_SQL = """
SELECT substr(released_at, 1, 4) AS year
FROM musicbrainz_releases
WHERE artist_id = ?
  AND released_at >= '1000'
  AND released_at < ':'
GROUP BY substr(released_at, 1, 4)
ORDER BY year DESC
LIMIT ?
"""

MB_CALENDAR_PERIOD_SQL = """
SELECT mbid, title, release_type, released_at, precision
FROM musicbrainz_releases
WHERE artist_id = ?
  AND released_at >= ?
  AND released_at < ?
ORDER BY released_at DESC
LIMIT ?
"""

# UNIQUE (artist_id, source, external_id) seeks one source. published_at is filtered inside that set.
# Dates are placed in Python so a UTC offset can land in the next year without a guessed day.
SIGHTING_CALENDAR_DATES_SQL = """
SELECT published_at, precision
FROM content_sightings
WHERE artist_id = ?
  AND source = ?
  AND published_at >= '1000'
  AND published_at < ':'
LIMIT ?
"""

SIGHTING_CALENDAR_YEARS_SQL = """
SELECT substr(published_at, 1, 4) AS year
FROM content_sightings
WHERE artist_id = ?
  AND source = ?
  AND published_at >= '1000'
  AND published_at < ':'
GROUP BY substr(published_at, 1, 4)
ORDER BY year DESC
LIMIT ?
"""

SIGHTING_CALENDAR_PERIOD_SQL = """
SELECT source, title, kind, published_at, precision
FROM content_sightings
WHERE artist_id = ?
  AND source = ?
  AND published_at >= ?
  AND published_at < ?
ORDER BY published_at DESC
LIMIT ?
"""

ALERT_SQL = """
SELECT provider, metric, window, delta, delta_pct, baseline_value, latest_value,
       baseline_at, latest_at, detected_at
FROM metric_alerts
WHERE artist_id = ?
ORDER BY detected_at DESC, id DESC
LIMIT ?
"""

ALERTS_FEED_SQL = """
SELECT artists.name AS artist_name, metric_alerts.artist_id, metric_alerts.provider,
       metric_alerts.metric, metric_alerts.window, metric_alerts.delta, metric_alerts.delta_pct,
       metric_alerts.baseline_value, metric_alerts.latest_value, metric_alerts.detected_at
FROM metric_alerts
JOIN artists ON artists.id = metric_alerts.artist_id
ORDER BY metric_alerts.detected_at DESC, metric_alerts.id DESC
LIMIT ?
"""


async def _artist_row(db, sql: str, bare: str, params: tuple) -> dict | None:
    """One artists row. A database that has not applied 0012 still resolves the artist.

    The fallback read stays outside ``except``. Awaiting a D1 promise while an
    exception is still propagating breaks the Workers runtime.
    """
    try:
        return await db.query_one(sql, params)
    except Exception as exc:
        if "no such column" not in str(exc).lower():
            raise
    return await db.query_one(bare, params)


async def find_artist(db, name: str) -> dict | None:
    """Lookup by the same normalized_name the crawler stores. Display case does not matter."""
    normalized = normalize_name(name)
    if not normalized:
        return None
    return await _artist_row(db, ARTIST_LOOKUP_SQL, _ARTIST_LOOKUP_BARE_SQL, (normalized,))


async def load_growth(db, name: str) -> dict:
    """Audience points only. The dashboard uses load_dashboard_history, which also reads edges."""
    history = await load_dashboard_history(db, name)
    if not history.get("growth", {}).get("datasets"):
        history["reason"] = history.get("reason") or "No exact audience count has been stored yet."
    return history


def calendar_period_bounds(year: int) -> tuple[str, str]:
    """Text bounds for one calendar year, widened by the day a UTC offset can cross.

    Placement still uses the stored precision. A neighbor month is dropped unless
    the UTC day actually falls in `year`.
    """
    if year <= 1000:
        start = f"{year:04d}"
    else:
        start = f"{year - 1:04d}-12"
    if year >= 9999:
        end = ":"
    else:
        end = f"{year + 1:04d}-02"
    return start, end


def _calendar_year_numbers(rows: list[dict] | None) -> list[int]:
    found = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        text = str(row.get("year") or "").strip()
        if len(text) != 4 or not text.isdigit():
            continue
        number = int(text)
        if 1000 <= number <= 9999:
            found.append(number)
    return found


async def _safe_rows(db, sql: str, params: tuple) -> list[dict] | None:
    try:
        return await db.query(sql, params)
    except Exception:
        return None


async def _read_posting(db, artist_id: int) -> dict:
    try:
        posting_rows = await db.query(EXACT_TIME_SQL, (artist_id, EXACT_TIME_READ_LIMIT))
    except Exception as exc:
        log_read_failure("posting times", exc)
        return empty_posting_patterns(POSTING_READ_ERROR)
    return build_posting_patterns(posting_rows or [])


def _musicbrainz_calendar_rows(rows: list[dict] | None) -> list[dict]:
    return [
        {
            "title": row.get("title"),
            "source": "musicbrainz",
            "released_at": row.get("released_at"),
            "precision": row.get("precision"),
            "kind": row.get("release_type"),
            "mbid": row.get("mbid"),
        }
        for row in rows or []
        if isinstance(row, dict)
    ]


async def _sighting_years(db, artist_id: int, source: str) -> list[int] | None:
    """Placed years for one source. A full read that hits the cap also keeps the grouped years."""
    dates = await _safe_rows(db, SIGHTING_CALENDAR_DATES_SQL, (artist_id, source, CALENDAR_DATE_LIMIT))
    if dates is None:
        grouped = await _safe_rows(db, SIGHTING_CALENDAR_YEARS_SQL, (artist_id, source, CALENDAR_YEAR_LIMIT))
        if grouped is None:
            return None
        return _calendar_year_numbers(grouped)
    found = release_years(dates)
    if len(dates) >= CALENDAR_DATE_LIMIT:
        grouped = await _safe_rows(db, SIGHTING_CALENDAR_YEARS_SQL, (artist_id, source, CALENDAR_YEAR_LIMIT))
        found.extend(_calendar_year_numbers(grouped))
    return found


async def load_release_calendar(db, artist_id: int, year: int | None = None) -> dict:
    """One stored year, plus the years that actually have a release. No provider calls."""
    mb_years = await _safe_rows(db, MB_CALENDAR_YEARS_SQL, (artist_id, CALENDAR_YEAR_LIMIT))
    apple_years = await _sighting_years(db, artist_id, "apple")
    spotify_years = await _sighting_years(db, artist_id, "spotify")
    deezer_years = await _sighting_years(db, artist_id, "deezer")
    if mb_years is None and apple_years is None and spotify_years is None and deezer_years is None:
        calendar = empty_release_calendar(CALENDAR_READ_ERROR)
        calendar["navigation"] = calendar_navigation([], None, artist_id)
        return calendar
    years = sorted(
        set(_calendar_year_numbers(mb_years) + (apple_years or []) + (spotify_years or []) + (deezer_years or [])),
        reverse=True,
    )
    if not years:
        calendar = empty_release_calendar()
        calendar["navigation"] = calendar_navigation([], None, artist_id)
        return calendar
    selected = year if isinstance(year, int) and not isinstance(year, bool) and 1000 <= year <= 9999 else years[0]
    navigation = calendar_navigation(years, selected, artist_id)
    start, end = calendar_period_bounds(selected)
    mb_rows = await _safe_rows(db, MB_CALENDAR_PERIOD_SQL, (artist_id, start, end, CALENDAR_PERIOD_LIMIT))
    apple_rows = await _safe_rows(db, SIGHTING_CALENDAR_PERIOD_SQL, (artist_id, "apple", start, end, CALENDAR_PERIOD_LIMIT))
    spotify_rows = await _safe_rows(
        db, SIGHTING_CALENDAR_PERIOD_SQL, (artist_id, "spotify", start, end, CALENDAR_PERIOD_LIMIT)
    )
    deezer_rows = await _safe_rows(
        db, SIGHTING_CALENDAR_PERIOD_SQL, (artist_id, "deezer", start, end, CALENDAR_PERIOD_LIMIT)
    )
    if mb_rows is None and apple_rows is None and spotify_rows is None and deezer_rows is None:
        calendar = empty_release_calendar(CALENDAR_READ_ERROR)
        calendar["navigation"] = navigation
        calendar["period_year"] = selected
        calendar["period_label"] = str(selected)
        calendar["empty_period"] = False
        return calendar
    calendar_rows = _musicbrainz_calendar_rows(mb_rows)
    if apple_rows:
        calendar_rows.extend(apple_rows)
    if spotify_rows:
        calendar_rows.extend(spotify_rows)
    if deezer_rows:
        calendar_rows.extend(deezer_rows)
    calendar = build_release_calendar(calendar_rows, year=selected)
    calendar["navigation"] = navigation
    calendar["row_cap_hit"] = any(
        rows is not None and len(rows) >= CALENDAR_PERIOD_LIMIT
        for rows in (mb_rows, apple_rows, spotify_rows, deezer_rows)
    )
    if calendar["empty_period"]:
        calendar["reason"] = (
            f"No stored MusicBrainz, Apple, Spotify, or Deezer release falls in {selected}. "
            "The years above are the ones with a stored date."
        )
    return calendar


async def load_calendar_page(db, artist: dict, year: int | None = None) -> dict:
    """Posting hours and one release year. Skips snapshots, charts, and the other artist cards."""
    artist_id = int(artist["id"])
    history = empty_history("No exact audience count has been stored yet.")
    history["artist_id"] = artist_id
    history["name"] = artist.get("name")
    history["available"] = True
    history["posting"] = await _read_posting(db, artist_id)
    history["calendar"] = await load_release_calendar(db, artist_id, year)
    return history


async def find_artist_by_id(db, artist_id: int) -> dict | None:
    if not isinstance(artist_id, int) or isinstance(artist_id, bool) or artist_id < 1:
        return None
    return await _artist_row(db, _ARTIST_BY_ID_SQL, _ARTIST_BY_ID_BARE_SQL, (artist_id,))


DEEZER_FANS_SQL = """
SELECT value, value_text, captured_at
FROM metric_latest
WHERE artist_id = ? AND source = 'deezer' AND metric = 'Fans'
"""

DEEZER_TRACKS_SQL = f"""
SELECT external_id, track_name, album_name, rank, last_seen_at
FROM artist_tracks
WHERE artist_id = ? AND source = 'deezer'
  AND last_seen_at >= (
      SELECT strftime('%Y-%m-%dT%H:%M:%SZ', MAX(last_seen_at), '-{TRACK_CURRENT_HOURS} hours')
      FROM artist_tracks
      WHERE artist_id = ? AND source = 'deezer'
  )
ORDER BY CASE WHEN rank IS NULL THEN 1 ELSE 0 END, rank ASC, track_name ASC
LIMIT ?
"""

DEEZER_RELEASES_SQL = """
SELECT external_id, title, kind, published_at, precision
FROM content_sightings
WHERE artist_id = ? AND source = 'deezer'
ORDER BY published_at DESC, id DESC
LIMIT ?
"""

DEEZER_IDENTITY_SQL = "SELECT deezer_id, deezer_nb_album FROM artists WHERE id = ?"
DEEZER_PAGE_NOTICE = "Stored by the crawler. This page did not call Deezer."
DEEZER_UNAVAILABLE = (
    "No Deezer artist is stored for this name. A different name is not shown, and nothing was estimated."
)
DEEZER_READ_ERROR = "Data temporarily unavailable. Stored Deezer data could not be read on this request."


def _date_only_text(raw: str) -> str:
    text = (raw or "").strip()
    if "T" in text:
        return text.split("T", 1)[0].strip()
    if " " in text:
        left, right = text.split(" ", 1)
        if ":" in right:
            return left.strip()
    return text


def present_deezer_card(
    name: str,
    identity: dict | None,
    fans: dict | None,
    tracks: list[dict] | None,
    releases: list[dict] | None,
    *,
    failed: bool = False,
) -> dict:
    """Artist-page card from stored rows. A missing count stays unavailable."""
    deezer_id = str((identity or {}).get("deezer_id") or "").strip()
    deezer_id = deezer_id if deezer_id.isdigit() else ""
    fans_number = _whole_number((fans or {}).get("value")) if fans else None
    albums = _whole_number((identity or {}).get("deezer_nb_album")) if identity else None
    track_rows = []
    for row in tracks or []:
        if not isinstance(row, dict):
            continue
        title = " ".join(str(row.get("track_name") or row.get("name") or "").split())
        external = str(row.get("external_id") or "").strip()
        if not title or not external:
            continue
        rank = _whole_number(row.get("rank"))
        if rank is not None and (rank < 1 or rank > 100):
            rank = None
        album = " ".join(str(row.get("album_name") or "").split())
        track_rows.append(
            {
                "name": title[:200],
                "rank": rank,
                "url": f"https://www.deezer.com/track/{external}" if external.isdigit() else None,
                "album_name": album[:200] or None,
            }
        )
    item_dicts = []
    for row in releases or []:
        if not isinstance(row, dict):
            continue
        raw = _date_only_text(str(row.get("published_at") or ""))
        precision = str(row.get("precision") or "")
        if precision == "time":
            precision = "day"
        item = make_item(
            title=str(row.get("title") or ""),
            kind=str(row.get("kind") or "album"),
            raw=raw,
            precision=precision or None,
        )
        if item is None or item.precision == "time" or item.hour_utc is not None:
            continue
        external = str(row.get("external_id") or "")
        album_id = external.rstrip("/").split("/")[-1] if "deezer.com/album/" in external else ""
        if album_id.isdigit():
            item.url = f"https://www.deezer.com/album/{album_id}"
        item_dicts.append(
            {
                "title": item.title,
                "url": item.url,
                "detail": item.detail or item.kind,
                "display": item.display,
                "precision_label": item.precision_label,
                "weekday": item.weekday,
                "kind": item.kind,
                "hour_utc": None,
            }
        )
    profile_name = str(name or "").strip()[:120] or None if deezer_id else None
    profile_url = f"https://www.deezer.com/artist/{deezer_id}" if deezer_id else None
    # A failed read must not be described as an empty catalog.
    if failed:
        return {
            "platform": "deezer",
            "label": "Deezer",
            "status": "unavailable",
            "reason": DEEZER_READ_ERROR,
            "profile_name": profile_name,
            "profile_url": profile_url,
            "metrics": [],
            "items": [],
            "tracks": [],
            "notices": [DEEZER_PAGE_NOTICE],
        }
    has_profile = bool(deezer_id or fans_number is not None or track_rows or item_dicts or albums is not None)
    if not has_profile:
        return {
            "platform": "deezer",
            "label": "Deezer",
            "status": "unavailable",
            "reason": DEEZER_UNAVAILABLE,
            "profile_name": None,
            "profile_url": None,
            "metrics": [],
            "items": [],
            "tracks": [],
            "notices": [DEEZER_PAGE_NOTICE],
        }
    metrics = []
    if fans_number is not None:
        metrics.append(
            {"label": "Fans", "available": True, "value": f"{fans_number:,}", "exact": fans_number, "reason": None}
        )
    elif deezer_id:
        metrics.append(
            {
                "label": "Fans",
                "available": False,
                "value": None,
                "exact": None,
                "reason": "No fan count has been stored yet.",
            }
        )
    if albums is not None:
        metrics.append(
            {"label": "Albums", "available": True, "value": f"{albums:,}", "exact": albums, "reason": None}
        )
    elif deezer_id:
        metrics.append(
            {
                "label": "Albums",
                "available": False,
                "value": None,
                "exact": None,
                "reason": "No album count has been stored yet.",
            }
        )
    notices = [DEEZER_PAGE_NOTICE]
    if item_dicts:
        notices.append("Deezer dates have no time of day. No hour was added.")
    # An id from MusicBrainz can land before the Deezer crawl stores fans.
    pending = fans_number is None and not track_rows and not item_dicts
    return {
        "platform": "deezer",
        "label": "Deezer",
        "status": "pending" if pending else "ok",
        "reason": None,
        "profile_name": profile_name,
        "profile_url": profile_url,
        "metrics": metrics,
        "items": item_dicts,
        "tracks": track_rows[:10],
        "notices": notices,
    }


def _deezer_read_failed(exc: BaseException) -> bool:
    text = str(exc).lower()
    if "no such column" in text or "no such table" in text:
        return False
    log_read_failure("deezer card", exc)
    return True


def _row_deezer_identity(artist: dict | None) -> dict | None:
    """Identity already loaded with the artist, so the card does not select that row again."""
    if not isinstance(artist, dict):
        return None
    if "deezer_id" not in artist and "deezer_nb_album" not in artist:
        return None
    return {"deezer_id": artist.get("deezer_id"), "deezer_nb_album": artist.get("deezer_nb_album")}


def _stored_deezer_id(identity: dict | None) -> str:
    text = str((identity or {}).get("deezer_id") or "").strip()
    return text if text.isdigit() else ""


async def load_deezer_card(db, artist_id: int, name: str | None, artist: dict | None = None) -> dict:
    """Stored Deezer card. No Deezer request.

    Fan, track, and release rows are written with the artist id. When that id is
    missing, those queries are skipped so an empty catalog cannot add D1 round
    trips. The artist page calls this after history is already in hand.
    """
    identity = _row_deezer_identity(artist)
    failed = False
    if identity is None:
        try:
            identity = await db.query_one(DEEZER_IDENTITY_SQL, (artist_id,))
        except Exception as exc:
            failed = _deezer_read_failed(exc) or failed
            identity = None
    if failed or not _stored_deezer_id(identity):
        return present_deezer_card(str(name or ""), identity, None, [], [], failed=failed)
    # Fans, tracks, and releases are independent. One batch, not three round trips.
    # A real failure still blanks the card. A database without these tables does not.
    values = await read_many(
        db,
        [
            (DEEZER_FANS_SQL, (artist_id,)),
            (DEEZER_TRACKS_SQL, (artist_id, artist_id, 10)),
            (DEEZER_RELEASES_SQL, (artist_id, 20)),
        ],
    )
    rows: list[list[dict]] = []
    for value in values:
        if isinstance(value, BaseException):
            failed = _deezer_read_failed(value) or failed
            rows.append([])
        else:
            rows.append(list(value or []))
    if failed:
        return present_deezer_card(str(name or ""), identity, None, [], [], failed=True)
    fans = rows[0][0] if rows[0] else None
    return present_deezer_card(str(name or ""), identity, fans, rows[1], rows[2], failed=False)


def _logged_rows(scope: str, value) -> list[dict] | None:
    """Rows from ``read_many``. None means that statement failed and was logged."""
    if isinstance(value, BaseException):
        log_read_failure(scope, value)
        return None
    return list(value or [])


async def load_history_for_artist(db, artist: dict, on_growth=None) -> dict:
    """Snapshots, related edges, and sightings for one known artists row. No network.

    Audience points are one indexed read, reported through ``on_growth`` before
    anything else. The remaining cards are a single batch: each used to be its
    own D1 round trip, and the sum of those waits exceeded the page budget once
    a few dozen extra snapshot rows had to be decoded on every trip. The Deezer
    card is not in this batch. The artist page loads it afterwards so a slow
    card cannot discard this history.
    """
    artist_id = int(artist["id"])
    growth_failed = False
    try:
        rows = await db.query(SNAPSHOT_SQL, (artist_id, GROWTH_ROW_LIMIT))
    except Exception as exc:
        growth_failed = True
        rows = []
        log_read_failure("growth snapshots", exc)
    history = empty_history(GROWTH_EMPTY)
    history["artist_id"] = artist_id
    history["name"] = artist.get("name")
    history["growth"] = build_growth(rows)
    if growth_failed:
        history["reason"] = GROWTH_READ_ERROR
    elif history["growth"]["datasets"]:
        history["reason"] = None
    if on_growth is not None:
        on_growth(
            {
                "artist_id": artist_id,
                "name": artist.get("name"),
                "reason": history["reason"],
                "growth": history["growth"],
            }
        )
    from momentum import MOMENTUM_EMPTY, MOMENTUM_ROW_CAP, WINDOW_7D, _MOMENTUM_SQL, build_momentum

    moment = datetime.now(timezone.utc)
    cutoff = utc_now(moment - timedelta(seconds=WINDOW_7D))
    charts = artist_chart_statements(artist_id)
    labeled = [
        ("related edges", RELATED_SQL, (artist_id, RELATED_READ_LIMIT)),
        ("content sightings", SIGHTING_SQL, (artist_id, SIGHTING_READ_LIMIT)),
        ("playlists", PLAYLIST_SQL, (artist_id, artist_id, PLAYLIST_READ_LIMIT)),
        ("tracks", TRACK_SQL, (artist_id, artist_id, TRACK_READ_LIMIT)),
        ("momentum", _MOMENTUM_SQL, (artist_id, cutoff) * 5 + (MOMENTUM_ROW_CAP,)),
        ("alerts", ALERT_SQL, (artist_id, ALERT_READ_LIMIT)),
        ("musicbrainz releases", RELEASE_SQL, (artist_id, RELEASE_READ_LIMIT)),
        ("posting times", EXACT_TIME_SQL, (artist_id, EXACT_TIME_READ_LIMIT)),
        ("release sightings", RELEASE_SIGHTING_SQL, (artist_id, RELEASE_SIGHTING_READ_LIMIT)),
        (
            "track stats",
            TRACK_STATS_SQL,
            (artist_id, artist_id, artist_id, artist_id, cutoff, artist_id, cutoff),
        ),
    ]
    body_count = len(labeled)
    labeled.extend(("chart positions", sql, params) for sql, params in charts)
    score_sql, score_params = artist_score_statement(artist_id)
    labeled.append(("audience score", score_sql, score_params))
    raw = await read_many(db, [(sql, params) for _scope, sql, params in labeled])
    loaded = [
        _logged_rows(scope, value)
        for (scope, _sql, _params), value in zip(labeled[: body_count - 1], raw[: body_count - 1])
    ]
    stat_raw = raw[body_count - 1] if len(raw) >= body_count else RuntimeError("track stats were not read")
    chart_values = [
        _logged_rows(scope, value)
        for (scope, _sql, _params), value in zip(labeled[body_count:-1], raw[body_count:-1])
    ]
    score_value = raw[-1]
    related = loaded[0] or []
    sightings = loaded[1] or []
    playlists = loaded[2]
    track_rows = loaded[3]
    momentum_rows = loaded[4]
    alert_rows = loaded[5]
    release_rows = loaded[6]
    posting_rows = loaded[7]
    release_sightings = loaded[8]
    history["available"] = bool(rows or related or sightings)
    history["related"] = present_related(related)
    history["sightings"] = present_sightings(sightings)
    if playlists is None:
        history["playlists"] = []
        history["playlist_counts"] = empty_playlist_counts()
        history["playlists_reason"] = PLAYLIST_READ_ERROR
    else:
        history["playlists"], history["playlist_counts"] = present_playlist_rows(playlists, moment)
        history["playlists_reason"] = None if history["playlists"] else PLAYLIST_NOT_CRAWLED
    history["playlists_note"] = PLAYLIST_SPOTIFY_NOTE
    if track_rows is None:
        history["tracks"] = []
        history["tracks_reason"] = TRACKS_READ_ERROR
    else:
        history["tracks"] = present_tracks(track_rows)
        history["tracks_reason"] = None if history["tracks"] else TRACKS_EMPTY
    if isinstance(stat_raw, BaseException):
        if missing_table(stat_raw):
            history["track_stats"] = []
            history["track_stats_reason"] = TRACK_STATS_EMPTY
        else:
            log_read_failure("track stats", stat_raw)
            history["track_stats"] = []
            history["track_stats_reason"] = TRACK_STATS_READ_ERROR
    else:
        history["track_stats"] = present_track_stats(list(stat_raw or []), cutoff)
        history["track_stats_reason"] = None if history["track_stats"] else TRACK_STATS_EMPTY
    if momentum_rows is None:
        history["momentum"] = []
        history["momentum_reason"] = "Data temporarily unavailable. Stored momentum could not be read on this request."
    else:
        history["momentum"] = build_momentum(momentum_rows, moment)
        history["momentum_reason"] = None if history["momentum"] else MOMENTUM_EMPTY
    if alert_rows is None:
        history["alerts"] = []
        history["alerts_reason"] = "Data temporarily unavailable. Stored alerts could not be read on this request."
    else:
        history["alerts"] = present_alerts(alert_rows)
        history["alerts_reason"] = None if history["alerts"] else ALERTS_EMPTY
    releases_failed = release_rows is None
    if releases_failed:
        history["releases"] = []
        history["releases_reason"] = "Data temporarily unavailable. Stored MusicBrainz releases could not be read on this request."
    else:
        history["releases"] = present_releases(release_rows)
        history["releases_reason"] = None if history["releases"] else RELEASES_EMPTY
    if posting_rows is None:
        history["posting"] = empty_posting_patterns(POSTING_READ_ERROR)
    else:
        history["posting"] = build_posting_patterns(posting_rows)
    release_sightings_failed = release_sightings is None
    calendar_rows = [
        {
            "title": release.get("title"),
            "source": "musicbrainz",
            "released_at": release.get("released_at"),
            "precision": release.get("precision"),
            "kind": release.get("release_type"),
            "mbid": release.get("mbid"),
        }
        for release in history["releases"]
    ]
    if not release_sightings_failed:
        calendar_rows.extend(release_sightings or [])
    history["calendar"] = build_release_calendar(calendar_rows, month_cap=ARTIST_CALENDAR_MONTHS)
    if not history["calendar"]["available"] and (releases_failed or release_sightings_failed):
        if releases_failed and release_sightings_failed:
            history["calendar"] = empty_release_calendar(CALENDAR_READ_ERROR)
        elif releases_failed:
            history["calendar"]["reason"] = "Data temporarily unavailable. Stored MusicBrainz releases could not be read on this request."
        else:
            history["calendar"]["reason"] = "Data temporarily unavailable. Stored Apple, Spotify, and Deezer releases could not be read on this request."
    if any(value is None for value in chart_values):
        history["charts"] = []
        history["charts_reason"] = "Data temporarily unavailable. Stored chart positions could not be read on this request."
    else:
        history["charts"] = charts_from_reads(*chart_values)
        history["charts_reason"] = None if history["charts"] else CHARTS_EMPTY
    score = None
    score_failed = False
    if isinstance(score_value, BaseException):
        # 0011 is what creates metric_latest. Until then the snapshot window still scores.
        if missing_table(score_value):
            try:
                score = await load_artist_score(db, artist_id)
            except Exception as exc:
                score_failed = True
                log_read_failure("audience score", exc)
        else:
            score_failed = True
            log_read_failure("audience score", score_value)
    else:
        score = score_from_counts(list(score_value or []))
    if score_failed or score is None:
        history["score_text"] = None
        history["score_components"] = []
        history["score_formula"] = SCORE_FORMULA
        history["score_reason"] = "Data temporarily unavailable. Stored audience counts could not be read on this request." if score_failed else SCORE_EMPTY
    else:
        history["score_text"] = score["score_text"]
        history["score_components"] = score["components"]
        history["score_formula"] = score["formula"]
        history["score_reason"] = None if score["score_text"] else SCORE_EMPTY
    return history


async def load_dashboard_history(db, name: str) -> dict:
    """Indexed reads only: the artist row, recent snapshots, related edges, and sightings.

    The old request path called load_growth, which selected metric_snapshots and left
    related and sightings as empty lists. The template then showed the crawler placeholders
    even when related_edges and content_sightings had rows.
    """
    artist = await find_artist(db, name)
    if not artist:
        return empty_history("This artist has no stored snapshots yet.")
    return await load_history_for_artist(db, artist)


async def load_artist_history(db, name: str) -> dict:
    return await load_dashboard_history(db, name)


TRENDING_CAP = 50
_TRENDING_WINDOWS = {"24h", "7d"}
_TRENDING_PROVIDERS = {"spotify", "instagram", "tiktok"}


async def list_trending(db, *, window: str = "", provider: str = "") -> list[dict]:
    """Recent stored alerts, largest percent first. No network."""
    clauses: list[str] = []
    params: list = []
    chosen_window = window if window in _TRENDING_WINDOWS else ""
    chosen_provider = provider if provider in _TRENDING_PROVIDERS else ""
    if chosen_window:
        clauses.append("metric_alerts.window = ?")
        params.append(chosen_window)
    if chosen_provider:
        clauses.append("metric_alerts.provider = ?")
        params.append(chosen_provider)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(TRENDING_CAP)
    rows = await db.query(
        f"""
        SELECT artists.name AS artist_name, metric_alerts.artist_id, metric_alerts.provider,
               metric_alerts.metric, metric_alerts.window, metric_alerts.delta, metric_alerts.delta_pct,
               metric_alerts.baseline_value, metric_alerts.latest_value, metric_alerts.detected_at
        FROM metric_alerts
        JOIN artists ON artists.id = metric_alerts.artist_id
        {where}
        ORDER BY CASE WHEN metric_alerts.delta_pct IS NULL THEN 1 ELSE 0 END,
                 ABS(metric_alerts.delta_pct) DESC,
                 ABS(metric_alerts.delta) DESC,
                 metric_alerts.detected_at DESC,
                 metric_alerts.id DESC
        LIMIT ?
        """,
        tuple(params),
    )
    return present_alerts(rows)


async def load_alert_feed(db, limit: int = ALERTS_PAGE_LIMIT) -> list[dict]:
    rows = await db.query(ALERTS_FEED_SQL, (max(1, min(int(limit), ALERTS_PAGE_LIMIT)),))
    return present_alerts(rows)


DIRECTORY_PAGE_SIZE = 25

_DIRECTORY_NAME_SQL = """
SELECT id, name, normalized_name
FROM artists
WHERE tracked = 1
ORDER BY name COLLATE NOCASE, id
LIMIT ? OFFSET ?
"""

_DIRECTORY_LISTENER_SQL = """
SELECT a.id, a.name, a.normalized_name, listeners.value AS monthly_listeners
FROM artists AS a
LEFT JOIN metric_latest AS listeners
  ON listeners.artist_id = a.id
 AND listeners.source = 'spotify'
 AND listeners.metric = 'Monthly listeners'
 AND listeners.value IS NOT NULL
WHERE a.tracked = 1
ORDER BY CASE WHEN listeners.value IS NULL THEN 1 ELSE 0 END, listeners.value DESC, a.name COLLATE NOCASE, a.id
LIMIT ? OFFSET ?
"""

_DIRECTORY_LISTENER_FALLBACK_SQL = """
SELECT a.id, a.name, a.normalized_name, listeners.value AS monthly_listeners
FROM artists AS a
LEFT JOIN (
    SELECT artist_id, value
    FROM (
        SELECT artist_id, value,
               ROW_NUMBER() OVER (
                   PARTITION BY artist_id
                   ORDER BY captured_at DESC, id DESC
               ) AS rn
        FROM metric_snapshots
        WHERE source = 'spotify' AND metric = 'Monthly listeners' AND value IS NOT NULL
    ) AS ranked
    WHERE rn = 1
) AS listeners ON listeners.artist_id = a.id
WHERE a.tracked = 1
ORDER BY CASE WHEN listeners.value IS NULL THEN 1 ELSE 0 END, listeners.value DESC, a.name COLLATE NOCASE, a.id
LIMIT ? OFFSET ?
"""


async def list_tracked_artists(db, *, page: int = 1, sort: str = "name") -> dict:
    """One page of tracked artists and their two latest exact counts. No network."""
    mode = "listeners" if sort == "listeners" else "name"
    total_row = await db.query_one("SELECT COUNT(*) AS n FROM artists WHERE tracked = 1")
    total = int((total_row or {}).get("n") or 0)
    pages = max(1, (total + DIRECTORY_PAGE_SIZE - 1) // DIRECTORY_PAGE_SIZE) if total else 1
    try:
        requested = int(page or 1)
    except (TypeError, ValueError):
        requested = 1
    safe_page = min(max(requested, 1), pages)
    offset = (safe_page - 1) * DIRECTORY_PAGE_SIZE
    sql = _DIRECTORY_LISTENER_SQL if mode == "listeners" else _DIRECTORY_NAME_SQL
    legacy_listeners = False
    try:
        rows = await db.query(sql, (DIRECTORY_PAGE_SIZE, offset))
    except Exception as exc:
        if mode != "listeners" or not missing_table(exc):
            raise
        legacy_listeners = True
    if legacy_listeners:
        rows = await db.query(_DIRECTORY_LISTENER_FALLBACK_SQL, (DIRECTORY_PAGE_SIZE, offset))
    ids = [int(row["id"]) for row in rows]
    metrics = await _latest_metrics(db, ids)
    linked: set[int] = set()
    if ids:
        marks = ",".join("?" for _ in ids)
        edges = await db.query(
            f"SELECT DISTINCT from_artist_id AS id FROM related_edges WHERE from_artist_id IN ({marks})",
            tuple(ids),
        )
        linked = {int(row["id"]) for row in edges}
    scores: dict[int, dict] = {}
    if ids:
        try:
            scores = await load_scores(db, ids)
        except Exception:
            scores = {}
    artists = []
    for row in rows:
        artist_id = int(row["id"])
        score = scores.get(artist_id) or {}
        artists.append(
            {
                "id": artist_id,
                "name": row.get("name") or "",
                "metrics": metrics.get(artist_id, []),
                "has_related": artist_id in linked,
                "score_text": score.get("score_text"),
            }
        )
    return {
        "artists": artists,
        "page": safe_page,
        "pages": pages,
        "total": total,
        "sort": mode,
        "has_prev": safe_page > 1,
        "has_next": safe_page < pages,
    }


SEARCH_LIMIT = 15
SHORT_SEARCH_LIMIT = 8
METRIC_PREVIEW = 2
# One character is a prefix scan on the unique normalized_name index.
# A leading-wildcard substring waits until the query is at least this long.
SUBSTRING_MIN = 2

_SEARCH_PREFIX_SQL = """
SELECT id, name, normalized_name, tracked
FROM artists
WHERE tracked = 1 AND normalized_name LIKE ? ESCAPE '\\'
ORDER BY
    CASE WHEN normalized_name = ? THEN 0 ELSE 1 END,
    length(normalized_name),
    name
LIMIT ?
"""

_SEARCH_SUBSTR_SQL = """
SELECT id, name, normalized_name, tracked
FROM artists
WHERE tracked = 1
  AND normalized_name LIKE ? ESCAPE '\\'
  AND normalized_name NOT LIKE ? ESCAPE '\\'
ORDER BY instr(normalized_name, ?), length(normalized_name), name
LIMIT ?
"""


def _like_literal(fragment: str) -> str:
    return fragment.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _preview_metrics(rows: list[dict]) -> list[dict]:
    """Up to two latest exact audience counts. Rounded text is not a metric."""
    newest: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row.get("source"), row.get("metric"))
        number = _whole_number(row.get("value"))
        if key not in AUDIENCE or number is None:
            continue
        current = newest.get(key)
        captured = str(row.get("captured_at") or "")
        if current is None or captured > str(current.get("captured_at") or ""):
            newest[key] = {"value": number, "captured_at": captured}
    preview = []
    for key, label in AUDIENCE.items():
        item = newest.get(key)
        if item is None:
            continue
        preview.append(
            {
                "label": label,
                "value": item["value"],
                "text": f"{item['value']:,}",
                "captured_at": item["captured_at"],
            }
        )
        if len(preview) >= METRIC_PREVIEW:
            break
    return preview


async def _latest_metrics(db, artist_ids: list[int]) -> dict[int, list[dict]]:
    if not artist_ids:
        return {}
    marks = ",".join("?" for _ in artist_ids)
    legacy = False
    try:
        rows = await db.query(
            f"""
            SELECT artist_id, source, metric, value, captured_at
            FROM metric_latest
            WHERE artist_id IN ({marks}) AND value IS NOT NULL
            """,
            tuple(artist_ids),
        )
    except Exception as exc:
        if not missing_table(exc):
            raise
        legacy = True
    if legacy:
        rows = await db.query(
            f"""
            SELECT artist_id, source, metric, value, captured_at
            FROM (
                SELECT artist_id, source, metric, value, captured_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY artist_id, source, metric
                           ORDER BY captured_at DESC, id DESC
                       ) AS rn
                FROM metric_snapshots
                WHERE artist_id IN ({marks}) AND value IS NOT NULL
            ) AS ranked
            WHERE rn = 1
            """,
            tuple(artist_ids),
        )
    grouped: dict[int, list[dict]] = {}
    for row in rows:
        grouped.setdefault(int(row["artist_id"]), []).append(row)
    return {artist_id: _preview_metrics(items) for artist_id, items in grouped.items()}


async def search_artists(db, query: str) -> list[dict]:
    """Prefix match on normalized_name, then a bounded substring match.

    An empty or one-symbol query does not scan the table. A single character
    is prefix-only. Two or more characters may add a substring pass, still limited.
    """
    needle = normalize_name(query)[:80]
    if not needle:
        return []
    literal = _like_literal(needle)
    limit = SHORT_SEARCH_LIMIT if len(needle) < SUBSTRING_MIN else SEARCH_LIMIT
    rows = await db.query(_SEARCH_PREFIX_SQL, (literal + "%", needle, limit))
    if len(needle) >= SUBSTRING_MIN and len(rows) < limit:
        extra = await db.query(
            _SEARCH_SUBSTR_SQL,
            ("%" + literal + "%", literal + "%", needle, limit - len(rows)),
        )
        rows = list(rows) + list(extra)
    metrics = await _latest_metrics(db, [int(row["id"]) for row in rows])
    results = []
    for row in rows:
        artist_id = int(row["id"])
        results.append(
            {
                "id": artist_id,
                "name": row.get("name") or "",
                "normalized_name": row.get("normalized_name") or "",
                "tracked": row.get("tracked"),
                "metrics": metrics.get(artist_id, []),
            }
        )
    return results
