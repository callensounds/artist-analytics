"""Cross-artist release and post timing from stored rows. No provider calls and no writes.

Hours and weekdays are UTC. A stored precision is never made finer: an hour
comes only from an exact timestamp, and a month or a year is not placed on a
day. The same release title on the same UTC day is counted once.

All stored dates has no time bound, so that window reads only the newest
``ALL_TIME_ROW_CAP`` rows per table. The page says so when the cap is hit.
Text columns are shortened in SQL so a huge stored string never lands in the
isolate, and each stamp is parsed once.
"""

from __future__ import annotations

import csv
import io
import json
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

from analytics import (
    PRECISION_RANK,
    WEEKDAYS,
    WEEKDAY_SHORT,
    calendar_parts,
    normalize_name,
    parse_utc,
    peak,
    stated_precision,
)
from compare import compound_select_terms, like_glob_patterns
from db import log_read_failure, missing_table, read_many, utc_now
from export_csv import formula_safe

# D1 rejects a long UNION chain. Every statement here is one SELECT. Digest's
# ceiling is five compound terms; this page does not use a compound SELECT at all.
COMPOUND_TERM_LIMIT = 5
PATTERN_LIMIT = 50
# Bound the IN list and the rows each statement returns. The extra row is only
# a probe so the page can say the cap was hit without a second query.
ARTIST_CAP = 40
ROW_CAP = 2000
# Days=all has no timestamp predicate. Two 2,000-row reads, plus a parsed copy
# of each row, were enough to trip the 128MB isolate limit (error 1102) while
# CPU was still ~150ms. The all-time window keeps the newest rows only.
ALL_TIME_ROW_CAP = 400
# Match the slices below. SQLite returns the shortened text, not the stored blob.
_TEXT_SOURCE = 40
_TEXT_ID = 80
_TEXT_TITLE = 200
_TEXT_KIND = 40
_TEXT_STAMP = 80
_TEXT_PRECISION = 16
MODAL_MIN_EXACT = 3
NOT_ENOUGH_EXACT = "not enough exact timestamps"
PAGE_NOTE = (
    "Stored timestamps only, in UTC. Nothing is estimated or interpolated, "
    "and this page does not call providers or write to the database."
)
HOUR_NOTE = "The grid counts exact UTC timestamps only. A date-only release is not given an hour."
WEEKDAY_NOTE = "Weekday totals use a stored day or an exact UTC time. A month or a year is not placed."
DEDUPE_NOTE = (
    "The same release title on the same UTC day from more than one source is counted once in the charts. "
    "The most precise timestamp is kept. When both are exact, the later UTC time is kept. "
    "Source counts are the stored rows before that collapse."
)
WINDOW_NOTE = (
    "A month or a year is included when that period overlaps the window. "
    "It is counted as not placeable and is not given a day or an hour."
)
HOUR_EMPTY = (
    "No exact UTC timestamp is stored in this window. "
    "A date-only row is not given an hour, and a month or a year is not placed."
)
WEEKDAY_EMPTY = "No stored day or exact time is in this window. A month or a year is not placed on a weekday."
EMPTY_EVENTS = "No stored release or post in this window has a timestamp. Nothing was estimated or filled in."
EMPTY_WATCHLIST = (
    "Your watchlist is empty. This page does not look artists up. "
    "Switch scope to all tracked artists to read everyone who is already stored."
)
EMPTY_TRACKED = "No tracked artists are stored."
NO_WATCHLIST_SESSION = (
    "No watchlist is available for this login. Scope all reads tracked artists stored in the database."
)
TIMING_READ_ERROR = "Data temporarily unavailable. Stored timing could not be read on this request."
TIMING_DB_NOTE = "Timing reads stored crawl data when D1 is bound. This page does not call providers."
NO_EXACT_LATEST = "No exact UTC timestamp is stored."
NO_DATED_SHARE = "No stored day or exact time."

POST_SOURCES = ("instagram", "tiktok", "youtube")
POST_KINDS = ("post", "video")
SOURCE_LABELS = {
    "spotify": "Spotify",
    "apple": "Apple Music",
    "deezer": "Deezer",
    "musicbrainz": "MusicBrainz",
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "youtube": "YouTube",
}
_SOURCE_ORDER = ("spotify", "apple", "deezer", "musicbrainz", "instagram", "tiktok", "youtube")
_MIN_MOMENT = datetime(1, 1, 1, tzinfo=timezone.utc)
_SCOPES = ("watchlist", "all")
_DAYS = ("90", "365", "all")
_KINDS = ("release", "post", "all")
_WEEKDAY_KEYS = tuple(short.lower() for short in WEEKDAY_SHORT)


def parse_scope(raw: str | None) -> str:
    text = str(raw or "").strip().lower()
    if text == "all":
        return "all"
    return "watchlist"


def parse_days(raw: str | None) -> str:
    text = str(raw or "").strip().lower()
    if text in _DAYS:
        return text
    return "365"


def parse_kind(raw: str | None) -> str:
    text = str(raw or "").strip().lower()
    if text in _KINDS:
        return text
    return "all"


def timing_path(scope: str, days: str, kind: str, export: str = "") -> str:
    """Timing URL for the page or an export. The query is the view that was built."""
    suffix = f".{export}" if export in {"csv", "json"} else ""
    query = urlencode((("scope", scope), ("days", days), ("kind", kind)))
    return f"/timing{suffix}?{query}"


def _as_utc(now: datetime | None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _ids(artist_ids: list[int]) -> list[int]:
    found: list[int] = []
    for value in artist_ids or []:
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if value < 1 or value in found:
            continue
        found.append(value)
        if len(found) >= ARTIST_CAP:
            break
    return found


def row_cap(days: str) -> int:
    """Rows read per table. All stored dates uses the smaller newest-N cap."""
    if parse_days(days) == "all":
        return ALL_TIME_ROW_CAP
    return ROW_CAP


def _marks(count: int) -> str:
    return ",".join("?" * count)


def _short(column: str, width: int, alias: str = "") -> str:
    """Projected text, capped. The ORDER BY column stays bare so it can use an index."""
    name = alias or column
    return f"substr({column}, 1, {int(width)}) AS {name}"


def _check(sql: str, params: tuple) -> tuple[str, tuple]:
    if sql.count("?") != len(params):
        raise RuntimeError("timing placeholders do not match parameters")
    folded = f" {sql.upper()} "
    if compound_select_terms(sql) > COMPOUND_TERM_LIMIT:
        raise RuntimeError("timing SQL exceeds the D1 compound SELECT limit")
    if " UNION " in folded or " INTERSECT " in folded or " EXCEPT " in folded:
        raise RuntimeError("timing SQL must not use a compound SELECT")
    if " LIKE " in folded or " GLOB " in folded:
        raise RuntimeError("timing SQL must not use LIKE or GLOB")
    for pattern in like_glob_patterns(sql):
        if len(pattern) > PATTERN_LIMIT:
            raise RuntimeError("timing SQL LIKE pattern is too long")
    return sql, params


def _sql_bounds(now: datetime, days: str) -> tuple[str, str] | None:
    """Coarse year span for the SQL seek. Python applies the exact window."""
    if days == "all":
        return None
    moment = _as_utc(now)
    start = moment - timedelta(days=int(days))
    return (f"{start.year:04d}", f"{moment.year + 1:04d}")


def _range_sql(column: str, bounds: tuple[str, str] | None) -> str:
    if bounds is None:
        return ""
    return f"AND {column} >= ? AND {column} < ?"


def _kind_sql(kind: str) -> str:
    sources = ", ".join("'" + source + "'" for source in POST_SOURCES)
    kinds = ", ".join("'" + name + "'" for name in POST_KINDS)
    if kind == "post":
        return f"AND (source IN ({sources}) OR IFNULL(kind, '') IN ({kinds}))"
    if kind == "release":
        return f"AND source NOT IN ({sources}) AND IFNULL(kind, '') NOT IN ({kinds})"
    return ""


def watch_statement(user_id: int) -> tuple[str, tuple]:
    """Newest saved artists. ``user_artists (user_id, created_at)`` seeks this."""
    sql = f"""
        SELECT artists.id AS id, artists.name AS name
        FROM user_artists
        JOIN artists ON artists.id = user_artists.artist_id
        WHERE user_artists.user_id = ?
        ORDER BY user_artists.created_at DESC, artists.id DESC
        LIMIT {int(ARTIST_CAP) + 1}
    """
    return _check(sql, (int(user_id),))


def tracked_statement() -> tuple[str, tuple]:
    """Tracked artists, oldest id first, capped. The directory is the small table."""
    sql = f"""
        SELECT id, name
        FROM artists
        WHERE tracked = 1
        ORDER BY id ASC
        LIMIT {int(ARTIST_CAP) + 1}
    """
    return _check(sql, ())


def _sighting_statement(
    artist_ids: list[int], kind: str, bounds: tuple[str, str] | None, cap: int
) -> tuple[str, tuple]:
    params: tuple = tuple(artist_ids)
    if bounds is not None:
        params = (*params, bounds[0], bounds[1])
    # ``published_at`` is ordered as stored text. Wrapping it would block a seek.
    sql = f"""
        SELECT artist_id,
               {_short("source", _TEXT_SOURCE)},
               {_short("external_id", _TEXT_ID)},
               {_short("title", _TEXT_TITLE)},
               {_short("kind", _TEXT_KIND)},
               {_short("published_at", _TEXT_STAMP)},
               {_short("precision", _TEXT_PRECISION)}
        FROM content_sightings
        WHERE artist_id IN ({_marks(len(artist_ids))})
          AND published_at IS NOT NULL
          AND published_at != ''
          {_range_sql("published_at", bounds)}
          {_kind_sql(kind)}
        ORDER BY published_at DESC
        LIMIT {int(cap) + 1}
    """
    return _check(sql, params)


def _release_statement(artist_ids: list[int], bounds: tuple[str, str] | None, cap: int) -> tuple[str, tuple]:
    """``idx_mb_releases_artist`` is (artist_id, released_at). Order by that column, bare."""
    params: tuple = tuple(artist_ids)
    if bounds is not None:
        params = (*params, bounds[0], bounds[1])
    sql = f"""
        SELECT artist_id,
               {_short("mbid", _TEXT_ID)},
               {_short("title", _TEXT_TITLE)},
               {_short("release_type", _TEXT_KIND, "release_type")},
               {_short("released_at", _TEXT_STAMP)},
               {_short("precision", _TEXT_PRECISION)}
        FROM musicbrainz_releases
        WHERE artist_id IN ({_marks(len(artist_ids))})
          AND released_at IS NOT NULL
          AND released_at != ''
          {_range_sql("released_at", bounds)}
        ORDER BY released_at DESC
        LIMIT {int(cap) + 1}
    """
    return _check(sql, params)


def event_statements(artist_ids: list[int], *, kind: str, days: str, now: datetime) -> list[tuple[str, tuple]]:
    """Sightings, then MusicBrainz when posts are not the only kind. No UNION."""
    ids = _ids(artist_ids)
    if not ids:
        return []
    chosen_kind = parse_kind(kind)
    chosen_days = parse_days(days)
    bounds = _sql_bounds(now, chosen_days)
    cap = row_cap(chosen_days)
    statements = [_sighting_statement(ids, chosen_kind, bounds, cap)]
    if chosen_kind != "post":
        statements.append(_release_statement(ids, bounds, cap))
    return statements


def timing_sql_catalog(artist_ids: list[int] | None = None) -> list[str]:
    """SQL text the timing page can send."""
    ids = list(artist_ids or list(range(1, ARTIST_CAP + 5)))
    moment = datetime(2026, 11, 15, 18, 0, tzinfo=timezone.utc)
    found = [watch_statement(1)[0], tracked_statement()[0]]
    for days in _DAYS:
        for kind in _KINDS:
            found.extend(sql for sql, _params in event_statements(ids, kind=kind, days=days, now=moment))
    return found


def _window(now: datetime, days: str) -> dict | None:
    if days == "all":
        return None
    moment = _as_utc(now)
    start = moment - timedelta(days=int(days))
    return {
        "start": start,
        "end": moment,
        "start_day": start.strftime("%Y-%m-%d"),
        "end_day": moment.strftime("%Y-%m-%d"),
    }


def _real_day(day: str) -> bool:
    """A real YYYY-MM-DD. Invalid days fail the same way ``parse_utc`` does, without a datetime."""
    if len(day) != 10 or day[4] != "-" or day[7] != "-":
        return False
    year_s, month_s, dom_s = day[0:4], day[5:7], day[8:10]
    if not (year_s.isdigit() and month_s.isdigit() and dom_s.isdigit()):
        return False
    year, month, dom = int(year_s), int(month_s), int(dom_s)
    if not (1 <= year <= 9999 and 1 <= month <= 12):
        return False
    return 1 <= dom <= monthrange(year, month)[1]


def _weekday_of_day(day: str) -> int | None:
    """Weekday of a calendar date. The midnight instant is not an hour bucket."""
    if not _real_day(day):
        return None
    return date(int(day[0:4]), int(day[5:7]), int(day[8:10])).weekday()


def _place_stamp(raw: str, precision: str):
    """One parse of a stored stamp.

    Returns role, stated precision, UTC moment, weekday, hour, UTC day, coarse
    key, and rank. A date-only value has no hour. A month or a year is coarse.
    """
    stated = stated_precision(raw, precision)
    if stated == "time":
        moment = parse_utc(raw)
        day = raw[:10]
        if moment is not None and _real_day(day):
            moment = moment.astimezone(timezone.utc)
            return (
                "time",
                "time",
                moment,
                moment.weekday(),
                moment.hour,
                moment.strftime("%Y-%m-%d"),
                "",
                PRECISION_RANK["time"],
            )
        weekday = _weekday_of_day(day)
        if weekday is None:
            return None
        return ("day", "day", None, weekday, None, day, "", PRECISION_RANK["day"])
    if stated == "day":
        day = raw[:10]
        weekday = _weekday_of_day(day)
        if weekday is None:
            return None
        return ("day", "day", None, weekday, None, day, "", PRECISION_RANK["day"])
    if stated not in {"month", "year"}:
        return None
    coarse_key = _coarse_key(stated, raw)
    if not coarse_key:
        return None
    return ("coarse", stated, None, None, None, "", coarse_key, PRECISION_RANK[stated])


def _coarse_key(stated: str, raw: str) -> str:
    parts = calendar_parts(raw)
    if stated == "year" and parts and 1 <= parts[0] <= 9999:
        return f"{parts[0]:04d}"
    if stated == "month" and len(parts) >= 2 and 1 <= parts[0] <= 9999 and 1 <= parts[1] <= 12:
        return f"{parts[0]:04d}-{parts[1]:02d}"
    return ""


def _month_overlaps(raw: str, window: dict) -> bool:
    parts = calendar_parts(raw)
    if len(parts) < 2:
        return False
    year, month = parts[0], parts[1]
    if not (1 <= year <= 9999 and 1 <= month <= 12):
        return False
    last_day = monthrange(year, month)[1]
    first = datetime(year, month, 1, tzinfo=timezone.utc)
    last = datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc)
    return first <= window["end"] and last >= window["start"]


def _year_overlaps(raw: str, window: dict) -> bool:
    parts = calendar_parts(raw)
    if not parts or not (1 <= parts[0] <= 9999):
        return False
    year = parts[0]
    first = datetime(year, 1, 1, tzinfo=timezone.utc)
    last = datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    return first <= window["end"] and last >= window["start"]


def _in_window(stated: str, raw: str, moment: datetime | None, window: dict | None) -> bool:
    if window is None:
        return True
    if stated == "time" and moment is not None:
        return window["start"] <= moment <= window["end"]
    if stated == "day":
        day = raw.strip()[:10]
        return window["start_day"] <= day <= window["end_day"]
    if stated == "month":
        return _month_overlaps(raw, window)
    if stated == "year":
        return _year_overlaps(raw, window)
    return False


def event_group(source: str, kind: str) -> str:
    """Releases and posts. MusicBrainz, Spotify, Apple, and Deezer are releases."""
    if source in POST_SOURCES or kind in POST_KINDS:
        return "post"
    return "release"


def source_label(source: str) -> str:
    key = str(source or "").strip().lower()
    if key in SOURCE_LABELS:
        return SOURCE_LABELS[key]
    cleaned = " ".join(key.replace("_", " ").split())
    if not cleaned:
        return "Unknown"
    return cleaned[:40].title()


@dataclass(slots=True)
class _Event:
    artist_id: int
    source: str
    external_id: str
    title: str
    group: str
    role: str
    stated: str
    raw: str
    utc_day: str
    weekday: int | None
    hour: int | None
    moment: datetime | None
    coarse_key: str
    rank: int


def _classify_row(row: dict, *, kind: str, window: dict | None, allowed: set[int]) -> _Event | None:
    if not isinstance(row, dict):
        return None
    try:
        artist_id = int(row["artist_id"])
    except (TypeError, ValueError, KeyError):
        return None
    if artist_id not in allowed:
        return None
    # MusicBrainz rows stay in their own shape. Copying them into sighting dicts
    # held a second string for every release while the original was still live.
    if "released_at" in row and "published_at" not in row:
        source = "musicbrainz"
        raw_kind = str(row.get("release_type") or "release").strip().lower()[:_TEXT_KIND]
        external_id = str(row.get("mbid") or "").strip()[:_TEXT_ID]
        raw = str(row.get("released_at") or "").strip()[:_TEXT_STAMP]
    else:
        source = str(row.get("source") or "").strip().lower()[:_TEXT_SOURCE]
        raw_kind = str(row.get("kind") or "").strip().lower()[:_TEXT_KIND]
        external_id = str(row.get("external_id") or "").strip()[:_TEXT_ID]
        raw = str(row.get("published_at") or "").strip()[:_TEXT_STAMP]
    if not raw:
        return None
    group = event_group(source, raw_kind)
    if kind != "all" and group != kind:
        return None
    placed = _place_stamp(raw, str(row.get("precision") or ""))
    if placed is None:
        return None
    role, stated, moment, weekday, hour, utc_day, coarse_key, rank = placed
    window_kind = stated if role == "coarse" else role
    if not _in_window(window_kind, raw, moment, window):
        return None
    title = " ".join(str(row.get("title") or "").split())[:_TEXT_TITLE]
    return _Event(
        artist_id=artist_id,
        source=source,
        external_id=external_id,
        title=title,
        group=group,
        role=role,
        stated=stated,
        raw=raw,
        utc_day=utc_day,
        weekday=weekday,
        hour=hour,
        moment=moment,
        coarse_key=coarse_key,
        rank=rank,
    )


def _prefer(candidate: _Event, current: _Event) -> bool:
    """Higher precision wins. A later exact UTC time wins a precision tie."""
    if candidate.rank != current.rank:
        return candidate.rank > current.rank
    cand_moment = candidate.moment or _MIN_MOMENT
    curr_moment = current.moment or _MIN_MOMENT
    if cand_moment != curr_moment:
        return cand_moment > curr_moment
    return (candidate.source, candidate.external_id) < (current.source, current.external_id)


def _source_sort(source: str) -> tuple:
    try:
        return (_SOURCE_ORDER.index(source), source)
    except ValueError:
        return (len(_SOURCE_ORDER), source)


def _level(count: int, top: int) -> int:
    if count <= 0 or top <= 0:
        return 0
    return min(4, 1 + (count * 3) // top)


def _bar_height(count: int, top: int) -> int:
    if count <= 0 or top <= 0:
        return 0
    return max(8, int(round(count * 100 / top)))


def _zero_heatmap() -> list[list[int]]:
    return [[0 for _ in range(24)] for _ in range(7)]


def _blank_counts() -> dict:
    return {
        "artists": 0,
        "stored_rows": 0,
        "counted": 0,
        "exact": 0,
        "day": 0,
        "dated": 0,
        "coarse": 0,
        "collapsed": 0,
    }


def _options(selected: str, pairs: tuple[tuple[str, str], ...]) -> list[dict]:
    return [{"value": value, "label": label, "selected": value == selected} for value, label in pairs]


def _chrome(scope: str, days: str, kind: str, now: datetime) -> dict:
    scope_pairs = (("watchlist", "Watchlist"), ("all", "All tracked"))
    day_pairs = (("90", "90 days"), ("365", "365 days"), ("all", "All stored dates"))
    kind_pairs = (("all", "Releases and posts"), ("release", "Releases"), ("post", "Posts"))
    return {
        "generated_at": utc_now(now),
        "timezone": "UTC",
        "scope": scope,
        "days": days,
        "kind": kind,
        "page_note": PAGE_NOTE,
        "hour_note": HOUR_NOTE,
        "weekday_note": WEEKDAY_NOTE,
        "dedupe_note": DEDUPE_NOTE,
        "window_note": WINDOW_NOTE,
        "scope_options": _options(scope, scope_pairs),
        "days_options": _options(days, day_pairs),
        "kind_options": _options(kind, kind_pairs),
        "csv_href": timing_path(scope, days, kind, "csv"),
        "json_href": timing_path(scope, days, kind, "json"),
        "not_enough_exact": NOT_ENOUGH_EXACT,
        "no_exact_latest": NO_EXACT_LATEST,
        "no_dated_share": NO_DATED_SHARE,
    }


def empty_timing(
    message: str,
    *,
    failed: bool,
    scope: str = "watchlist",
    days: str = "365",
    kind: str = "all",
    now: datetime | None = None,
    source_note: str = "",
    empty_reason: str = "",
) -> dict:
    """Page shell when the database is missing or the read did not finish."""
    chosen_scope = parse_scope(scope)
    chosen_days = parse_days(days)
    chosen_kind = parse_kind(kind)
    moment = _as_utc(now)
    view = _chrome(chosen_scope, chosen_days, chosen_kind, moment)
    if failed:
        view["csv_href"] = ""
        view["json_href"] = ""
    view.update(
        {
            "failed": failed,
            "read_error": message if failed else "",
            "empty": True,
            "empty_reason": "" if failed else empty_reason,
            "source_note": "" if failed else source_note,
            "cap_notes": [],
            "counts": _blank_counts(),
            "heatmap": _zero_heatmap(),
            "heatmap_rows": [],
            "weekday_counts": [0] * 7,
            "weekday_bars": [],
            "hour_counts": [0] * 24,
            "nonzero_hours": [],
            "hour_reason": HOUR_EMPTY if not failed else "",
            "weekday_reason": WEEKDAY_EMPTY if not failed else "",
            "insights": [],
            "sources": [],
            "artists": [],
        }
    )
    return view


def _insights(release_friday: int, release_dated: int, post_friday: int, post_dated: int, kind: str) -> list[str]:
    lines = []
    if kind in {"release", "all"} and release_dated:
        lines.append(f"{release_friday} of {release_dated} dated releases landed on Friday UTC.")
    if kind in {"post", "all"} and post_dated:
        lines.append(f"{post_friday} of {post_dated} dated posts landed on Friday UTC.")
    return lines


class _Tally:
    """Per-artist counters. Events are not kept once they have been counted."""

    __slots__ = ("exact", "dated", "friday", "weekday", "hour", "exact_days", "latest", "latest_moment")

    def __init__(self):
        self.exact = 0
        self.dated = 0
        self.friday = 0
        self.weekday = [0] * 7
        self.hour = [0] * 24
        self.exact_days = [0] * 7
        self.latest = ""
        self.latest_moment = None


class _Fold:
    """Running totals. Dedupe maps hold one winning release, not every row."""

    __slots__ = (
        "classified",
        "immediate",
        "dated",
        "coarse_kept",
        "sources",
        "tallies",
        "heatmap",
        "weekday_counts",
        "hour_counts",
        "exact",
        "day",
        "coarse",
        "release_dated",
        "release_friday",
        "post_dated",
        "post_friday",
    )

    def __init__(self, artist_ids: list[int]):
        self.classified = 0
        self.immediate = 0
        self.dated: dict[tuple, _Event] = {}
        self.coarse_kept: dict[tuple, _Event] = {}
        self.sources: dict[str, dict] = {}
        self.tallies = {artist_id: _Tally() for artist_id in artist_ids}
        self.heatmap = _zero_heatmap()
        self.weekday_counts = [0] * 7
        self.hour_counts = [0] * 24
        self.exact = 0
        self.day = 0
        self.coarse = 0
        self.release_dated = 0
        self.release_friday = 0
        self.post_dated = 0
        self.post_friday = 0


def _bump_source(sources: dict[str, dict], event: _Event) -> None:
    row = sources.get(event.source)
    if row is None:
        row = {"source": event.source, "label": source_label(event.source), "exact": 0, "day": 0, "coarse": 0}
        sources[event.source] = row
    if event.role == "time":
        row["exact"] += 1
    elif event.role == "day":
        row["day"] += 1
    else:
        row["coarse"] += 1


def _apply_kept(state: _Fold, event: _Event) -> None:
    tally = state.tallies.get(event.artist_id)
    if tally is None:
        return
    if event.role == "coarse":
        state.coarse += 1
        return
    if event.weekday is None:
        return
    friday = event.weekday == 4
    if event.role == "time" and event.hour is not None:
        state.exact += 1
        state.heatmap[event.weekday][event.hour] += 1
        state.weekday_counts[event.weekday] += 1
        state.hour_counts[event.hour] += 1
        tally.exact += 1
        tally.hour[event.hour] += 1
        tally.weekday[event.weekday] += 1
        tally.exact_days[event.weekday] += 1
        tally.dated += 1
        if friday:
            tally.friday += 1
        if event.moment is not None and (
            tally.latest_moment is None
            or event.moment > tally.latest_moment
            or (event.moment == tally.latest_moment and event.raw > tally.latest)
        ):
            tally.latest_moment = event.moment
            tally.latest = event.raw
    elif event.role == "day":
        state.day += 1
        state.weekday_counts[event.weekday] += 1
        tally.weekday[event.weekday] += 1
        tally.dated += 1
        if friday:
            tally.friday += 1
    else:
        return
    if event.group == "release":
        state.release_dated += 1
        state.release_friday += int(friday)
    elif event.group == "post":
        state.post_dated += 1
        state.post_friday += int(friday)


def _keep_release(state: _Fold, event: _Event, title_key: str) -> None:
    if event.role in {"time", "day"} and event.utc_day:
        key = (event.artist_id, title_key, event.utc_day)
        bucket = state.dated
    elif event.role == "coarse" and event.coarse_key:
        key = (event.artist_id, title_key, event.coarse_key)
        bucket = state.coarse_kept
    else:
        state.immediate += 1
        _apply_kept(state, event)
        return
    current = bucket.get(key)
    if current is None or _prefer(event, current):
        # The title is only the dedupe key. Drop it so the winner does not keep a second copy.
        event.title = ""
        bucket[key] = event


def _consume(state: _Fold, rows, *, kind: str, window: dict | None, allowed: set[int]) -> None:
    for row in rows or []:
        event = _classify_row(row, kind=kind, window=window, allowed=allowed)
        if event is None:
            continue
        state.classified += 1
        _bump_source(state.sources, event)
        if event.group != "release":
            state.immediate += 1
            _apply_kept(state, event)
            continue
        title_key = normalize_name(event.title)
        if not title_key:
            state.immediate += 1
            _apply_kept(state, event)
            continue
        _keep_release(state, event, title_key)


def _finish_fold(state: _Fold) -> int:
    collapsed = state.classified - state.immediate - len(state.dated) - len(state.coarse_kept)
    winners = list(state.dated.values())
    winners.extend(state.coarse_kept.values())
    state.dated.clear()
    state.coarse_kept.clear()
    for event in winners:
        _apply_kept(state, event)
    del winners
    return max(collapsed, 0)


def _artist_row(artist: dict, tally: _Tally) -> dict:
    exact = tally.exact
    dated = tally.dated
    friday = tally.friday
    if exact >= MODAL_MIN_EXACT:
        # Weekday and hour both come from exact timestamps. A date-only row is not a mode.
        day_peak = peak(tally.exact_days, list(WEEKDAYS))
        hour_peak = peak(tally.hour, [f"{hour:02d}:00" for hour in range(24)])
        modal_weekday = day_peak["label"] if day_peak else ""
        modal_hour = hour_peak["label"] if hour_peak else ""
        modal_note = ""
    else:
        modal_weekday = ""
        modal_hour = ""
        modal_note = NOT_ENOUGH_EXACT
    return {
        "id": artist["id"],
        "name": artist["name"],
        "exact_count": exact,
        "modal_weekday": modal_weekday,
        "modal_hour_utc": modal_hour,
        "modal_note": modal_note,
        "friday_count": friday,
        "dated_count": dated,
        "friday_share": f"{friday} of {dated}" if dated else "",
        "latest_exact_at": tally.latest,
        "weekday_counts": tally.weekday,
        "hour_counts": tally.hour,
    }


def _present_heatmap(heatmap: list[list[int]]) -> list[dict]:
    top = max((count for row in heatmap for count in row), default=0)
    rows = []
    for index, label in enumerate(WEEKDAYS):
        cells = []
        for hour, count in enumerate(heatmap[index]):
            cells.append(
                {
                    "hour": hour,
                    "count": count,
                    "label": f"{hour:02d}:00 UTC",
                    "level": _level(count, top),
                }
            )
        rows.append({"label": label, "short": WEEKDAY_SHORT[index], "cells": cells})
    return rows


def _present_weekday_bars(counts: list[int]) -> list[dict]:
    top = max(counts) if counts else 0
    bars = []
    for index, count in enumerate(counts):
        bars.append(
            {
                "label": WEEKDAYS[index],
                "short": WEEKDAY_SHORT[index],
                "count": count,
                "height": _bar_height(count, top),
            }
        )
    return bars


def assemble_timing(
    artists: list[dict],
    sightings: list[dict] | None,
    releases: list[dict] | None,
    *,
    scope: str,
    days: str,
    kind: str,
    now: datetime,
    artist_cap_hit: bool = False,
    sighting_cap_hit: bool = False,
    release_cap_hit: bool = False,
    source_note: str = "",
    section_notes: list[str] | None = None,
) -> dict:
    """Charts and per-artist rows from rows already read. No database access."""
    chosen_scope = parse_scope(scope)
    chosen_days = parse_days(days)
    chosen_kind = parse_kind(kind)
    moment = _as_utc(now)
    cleaned = []
    seen: set[int] = set()
    for row in artists or []:
        if not isinstance(row, dict):
            continue
        try:
            artist_id = int(row["id"])
        except (TypeError, ValueError, KeyError):
            continue
        if artist_id < 1 or artist_id in seen:
            continue
        seen.add(artist_id)
        name = " ".join(str(row.get("name") or "").split())[:200] or "Artist"
        cleaned.append({"id": artist_id, "name": name})
        if len(cleaned) >= ARTIST_CAP:
            break
    view = _chrome(chosen_scope, chosen_days, chosen_kind, moment)
    cap = row_cap(chosen_days)
    sightings, sighting_cap_hit = _bound_rows(sightings, cap, sighting_cap_hit)
    releases, release_cap_hit = _bound_rows(releases, cap, release_cap_hit)
    notes = []
    if artist_cap_hit:
        notes.append(f"The artist list stopped at {ARTIST_CAP}. Further artists were not read.")
    if sighting_cap_hit:
        notes.append(_row_cap_note("content", cap, all_time=chosen_days == "all"))
    if release_cap_hit:
        notes.append(_row_cap_note("MusicBrainz", cap, all_time=chosen_days == "all"))
    for note in section_notes or []:
        if note:
            notes.append(note)
    if not cleaned:
        empty_reason = EMPTY_WATCHLIST if chosen_scope == "watchlist" else EMPTY_TRACKED
        if source_note == NO_WATCHLIST_SESSION:
            empty_reason = NO_WATCHLIST_SESSION
        shell = empty_timing(
            "",
            failed=False,
            scope=chosen_scope,
            days=chosen_days,
            kind=chosen_kind,
            now=moment,
            source_note=source_note,
            empty_reason=empty_reason,
        )
        shell["cap_notes"] = notes
        return shell

    allowed = {artist["id"] for artist in cleaned}
    window = _window(moment, chosen_days)
    state = _Fold([artist["id"] for artist in cleaned])
    _consume(state, sightings, kind=chosen_kind, window=window, allowed=allowed)
    # The sighting dicts are the caller's until this function returns. Drop our
    # reference before the release pass so both tables are not live together
    # with the dedupe map.
    del sightings
    _consume(state, releases, kind=chosen_kind, window=window, allowed=allowed)
    del releases
    collapsed = _finish_fold(state)
    if collapsed:
        noun = "row" if collapsed == 1 else "rows"
        notes.append(
            f"{collapsed} duplicate stored {noun} for the same release were counted once. "
            "The most precise timestamp was kept."
        )

    source_rows = [state.sources[key] for key in sorted(state.sources, key=_source_sort)]
    artist_rows = [_artist_row(artist, state.tallies[artist["id"]]) for artist in cleaned]
    state.tallies.clear()
    artist_rows.sort(key=lambda item: (-item["exact_count"], item["name"].casefold(), item["id"]))
    exact = state.exact
    day = state.day
    coarse = state.coarse
    counted = exact + day + coarse
    counts = {
        "artists": len(cleaned),
        "stored_rows": state.classified,
        "counted": counted,
        "exact": exact,
        "day": day,
        "dated": exact + day,
        "coarse": coarse,
        "collapsed": collapsed,
    }
    nonzero = [
        {"hour": hour, "label": f"{hour:02d}:00 UTC", "count": count}
        for hour, count in enumerate(state.hour_counts)
        if count
    ]
    heatmap = state.heatmap
    weekday_counts = state.weekday_counts
    hour_counts = state.hour_counts
    view.update(
        {
            "failed": False,
            "read_error": "",
            "empty": counted == 0,
            "empty_reason": EMPTY_EVENTS if counted == 0 else "",
            "source_note": source_note,
            "cap_notes": notes,
            "counts": counts,
            "heatmap": heatmap,
            "heatmap_rows": _present_heatmap(heatmap) if exact else [],
            "weekday_counts": weekday_counts,
            "weekday_bars": _present_weekday_bars(weekday_counts) if exact + day else [],
            "hour_counts": hour_counts,
            "nonzero_hours": nonzero,
            "hour_reason": "" if exact else HOUR_EMPTY,
            "weekday_reason": "" if exact + day else WEEKDAY_EMPTY,
            "insights": _insights(
                state.release_friday, state.release_dated, state.post_friday, state.post_dated, chosen_kind
            ),
            "sources": source_rows,
            "artists": artist_rows,
        }
    )
    return view


def _row_cap_note(label: str, cap: int, *, all_time: bool) -> str:
    if all_time:
        noun = "content rows" if label == "content" else "MusicBrainz rows"
        return (
            f"All stored dates is capped at the newest {cap} {noun}. "
            "Older rows were not included."
        )
    noun = "Content rows" if label == "content" else "MusicBrainz rows"
    return f"{noun} stopped at {cap}, newest timestamp first. Older rows in this window were not included."


def _bound_rows(rows, cap: int, hit: bool) -> tuple[list, bool]:
    """Keep the leading rows, which the SQL returns newest first. Say so when the rest is dropped."""
    if not rows:
        return [], hit
    if len(rows) > cap:
        return list(rows[:cap]), True
    return rows, hit


def _cap_rows(rows: list, cap: int) -> tuple[list, bool]:
    return _bound_rows(rows, cap, False)


def _clean_loaded_artists(rows: list | None) -> tuple[list[dict], bool]:
    cleaned = []
    seen: set[int] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            artist_id = int(row["id"])
        except (TypeError, ValueError, KeyError):
            continue
        if artist_id < 1 or artist_id in seen:
            continue
        seen.add(artist_id)
        cleaned.append(row)
    cap_hit = len(cleaned) > ARTIST_CAP
    return cleaned[:ARTIST_CAP], cap_hit


def _read_rows(found, label: str) -> tuple[list | None, str]:
    """Rows, or None when the read failed. A missing table is an empty store."""
    if isinstance(found, Exception):
        if missing_table(found):
            return [], "missing"
        log_read_failure(label, found)
        return None, "error"
    if not isinstance(found, list):
        log_read_failure(label, RuntimeError("timing read returned no rows"))
        return None, "error"
    return [row for row in found if isinstance(row, dict)], ""


async def _load_artists(db, *, user_id: int | None, scope: str) -> tuple[list[dict] | None, bool, str]:
    if scope == "watchlist":
        if user_id is None:
            return [], False, NO_WATCHLIST_SESSION
        try:
            rows = await db.query(*watch_statement(user_id))
        except Exception as exc:
            if missing_table(exc):
                return [], False, EMPTY_WATCHLIST
            log_read_failure("timing watchlist", exc)
            return None, False, ""
        artists, cap_hit = _clean_loaded_artists(rows)
        note = "Showing artists on your watchlist." if artists else EMPTY_WATCHLIST
        return artists, cap_hit, note
    try:
        rows = await db.query(*tracked_statement())
    except Exception as exc:
        log_read_failure("timing artists", exc)
        return None, False, ""
    artists, cap_hit = _clean_loaded_artists(rows)
    note = "Showing tracked artists." if artists else EMPTY_TRACKED
    return artists, cap_hit, note


async def _build(db, *, user_id: int | None, scope: str, days: str, kind: str, now: datetime) -> dict:
    artists, artist_cap_hit, source_note = await _load_artists(db, user_id=user_id, scope=scope)
    if artists is None:
        return empty_timing(TIMING_READ_ERROR, failed=True, scope=scope, days=days, kind=kind, now=now)
    if not artists:
        return empty_timing(
            "",
            failed=False,
            scope=scope,
            days=days,
            kind=kind,
            now=now,
            source_note=source_note,
            empty_reason=source_note or EMPTY_EVENTS,
        )
    statements = event_statements([artist["id"] for artist in artists], kind=kind, days=days, now=now)
    found = await read_many(db, statements)
    if len(found) != len(statements):
        raise RuntimeError("timing read count")
    sighting_rows, sighting_problem = _read_rows(found[0], "timing sightings")
    found[0] = None
    release_rows: list | None = []
    release_problem = ""
    if kind != "post":
        release_rows, release_problem = _read_rows(found[1], "timing releases")
        found[1] = None
    del found
    if sighting_rows is None or release_rows is None:
        return empty_timing(TIMING_READ_ERROR, failed=True, scope=scope, days=days, kind=kind, now=now)
    cap = row_cap(days)
    sightings, sighting_cap_hit = _cap_rows(sighting_rows, cap)
    del sighting_rows
    releases, release_cap_hit = _cap_rows(release_rows, cap)
    del release_rows
    section_notes = []
    if sighting_problem == "missing":
        section_notes.append("Content sightings are not stored yet.")
    if release_problem == "missing":
        section_notes.append("MusicBrainz releases are not stored yet.")
    return assemble_timing(
        artists,
        sightings,
        releases,
        scope=scope,
        days=days,
        kind=kind,
        now=now,
        artist_cap_hit=artist_cap_hit,
        sighting_cap_hit=sighting_cap_hit,
        release_cap_hit=release_cap_hit,
        source_note=source_note,
        section_notes=section_notes,
    )


async def build_timing(
    db,
    *,
    user_id: int | None = None,
    scope: str = "",
    days: str = "",
    kind: str = "",
    now: datetime | None = None,
) -> dict:
    """One timing view. Reads only. A failed read is an empty shell, not a raise."""
    chosen_scope = parse_scope(scope)
    chosen_days = parse_days(days)
    chosen_kind = parse_kind(kind)
    moment = _as_utc(now)
    cleaned_user = user_id
    if isinstance(cleaned_user, bool) or not isinstance(cleaned_user, int) or cleaned_user < 1:
        cleaned_user = None
    try:
        return await _build(
            db,
            user_id=cleaned_user,
            scope=chosen_scope,
            days=chosen_days,
            kind=chosen_kind,
            now=moment,
        )
    except Exception as exc:
        log_read_failure("timing", exc)
        return empty_timing(
            TIMING_READ_ERROR,
            failed=True,
            scope=chosen_scope,
            days=chosen_days,
            kind=chosen_kind,
            now=moment,
        )


def _artist_document(artist: dict) -> dict:
    return {
        "id": artist["id"],
        "name": artist["name"],
        "exact_count": artist["exact_count"],
        "modal_weekday": artist["modal_weekday"],
        "modal_hour_utc": artist["modal_hour_utc"],
        "modal_note": artist["modal_note"],
        "friday_count": artist["friday_count"],
        "dated_count": artist["dated_count"],
        "friday_share": artist["friday_share"],
        "latest_exact_at": artist["latest_exact_at"],
        "weekday_counts": list(artist["weekday_counts"]),
        "hour_counts": list(artist["hour_counts"]),
    }


def timing_document(view: dict) -> dict:
    """JSON-ready timing view. The same artist fields feed the CSV."""
    counts = view.get("counts") or _blank_counts()
    return {
        "generated_at": view.get("generated_at") or "",
        "timezone": "UTC",
        "scope": view.get("scope") or "",
        "days": view.get("days") or "",
        "kind": view.get("kind") or "",
        "read_error": view.get("read_error") or "",
        "empty": bool(view.get("empty")),
        "empty_reason": view.get("empty_reason") or "",
        "source_note": view.get("source_note") or "",
        "cap_notes": list(view.get("cap_notes") or []),
        "counts": {
            "artists": counts.get("artists") or 0,
            "stored_rows": counts.get("stored_rows") or 0,
            "counted": counts.get("counted") or 0,
            "exact": counts.get("exact") or 0,
            "day": counts.get("day") or 0,
            "dated": counts.get("dated") or 0,
            "coarse": counts.get("coarse") or 0,
            "collapsed": counts.get("collapsed") or 0,
        },
        "heatmap": view.get("heatmap") or _zero_heatmap(),
        "weekday_counts": list(view.get("weekday_counts") or [0] * 7),
        "weekday_labels": list(WEEKDAYS),
        "hour_counts": list(view.get("hour_counts") or [0] * 24),
        "insights": list(view.get("insights") or []),
        "sources": [
            {
                "source": row.get("source") or "",
                "label": row.get("label") or "",
                "exact": row.get("exact") or 0,
                "day": row.get("day") or 0,
                "coarse": row.get("coarse") or 0,
            }
            for row in view.get("sources") or []
        ],
        "artists": [_artist_document(artist) for artist in view.get("artists") or []],
    }


def render_timing_json(view: dict) -> str:
    return json.dumps(timing_document(view), ensure_ascii=False)


def timing_csv_columns() -> list[str]:
    return [
        "scope",
        "days",
        "kind",
        "artist_id",
        "artist",
        "exact_count",
        "modal_weekday",
        "modal_hour_utc",
        "modal_note",
        "friday_count",
        "dated_count",
        "friday_share",
        "latest_exact_at",
        *[f"weekday_{name}" for name in _WEEKDAY_KEYS],
        *[f"hour_{hour:02d}" for hour in range(24)],
    ]


_HTML_ONLY = ("heatmap_rows", "weekday_bars", "scope_options", "days_options", "kind_options", "nonzero_hours")


def release_render_fields(view: dict) -> None:
    """Drop HTML-only cells before CSV or JSON so both are not live with the document."""
    for key in _HTML_ONLY:
        view.pop(key, None)


def render_timing_csv(view: dict) -> str:
    """One row per artist. Counts are the deduped chart counts. Formula text is escaped.

    Reads the view that was already built. It does not classify the rows again.
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(timing_csv_columns())
    scope = formula_safe(view.get("scope") or "")
    days = formula_safe(view.get("days") or "")
    kind = formula_safe(view.get("kind") or "")
    for artist in view.get("artists") or []:
        writer.writerow(
            [
                scope,
                days,
                kind,
                artist["id"],
                formula_safe(artist["name"]),
                artist["exact_count"],
                formula_safe(artist["modal_weekday"]),
                formula_safe(artist["modal_hour_utc"]),
                formula_safe(artist["modal_note"]),
                artist["friday_count"],
                artist["dated_count"],
                formula_safe(artist["friday_share"]),
                formula_safe(artist["latest_exact_at"]),
                *artist["weekday_counts"],
                *artist["hour_counts"],
            ]
        )
    return buffer.getvalue()
