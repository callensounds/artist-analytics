"""Cross-artist release and post timing from stored rows. No provider calls and no writes.

Hours and weekdays are UTC. A stored precision is never made finer: an hour
comes only from an exact timestamp, and a month or a year is not placed on a
day. The same release title on the same UTC day is counted once.
"""

from __future__ import annotations

import csv
import io
import json
from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
from compare import compound_select_terms, like_glob_patterns, timing_role
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


def _marks(count: int) -> str:
    return ",".join("?" * count)


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


def _sighting_statement(artist_ids: list[int], kind: str, bounds: tuple[str, str] | None) -> tuple[str, tuple]:
    params: tuple = tuple(artist_ids)
    if bounds is not None:
        params = (*params, bounds[0], bounds[1])
    sql = f"""
        SELECT artist_id, source, external_id, title, kind, published_at, precision
        FROM content_sightings
        WHERE artist_id IN ({_marks(len(artist_ids))})
          AND published_at IS NOT NULL
          AND published_at != ''
          {_range_sql("published_at", bounds)}
          {_kind_sql(kind)}
        ORDER BY published_at DESC
        LIMIT {int(ROW_CAP) + 1}
    """
    return _check(sql, params)


def _release_statement(artist_ids: list[int], bounds: tuple[str, str] | None) -> tuple[str, tuple]:
    """``idx_mb_releases_artist`` is (artist_id, released_at)."""
    params: tuple = tuple(artist_ids)
    if bounds is not None:
        params = (*params, bounds[0], bounds[1])
    sql = f"""
        SELECT artist_id, mbid, title, release_type, released_at, precision
        FROM musicbrainz_releases
        WHERE artist_id IN ({_marks(len(artist_ids))})
          AND released_at IS NOT NULL
          AND released_at != ''
          {_range_sql("released_at", bounds)}
        ORDER BY released_at DESC
        LIMIT {int(ROW_CAP) + 1}
    """
    return _check(sql, params)


def event_statements(artist_ids: list[int], *, kind: str, days: str, now: datetime) -> list[tuple[str, tuple]]:
    """Sightings, then MusicBrainz when posts are not the only kind. No UNION."""
    ids = _ids(artist_ids)
    if not ids:
        return []
    chosen_kind = parse_kind(kind)
    bounds = _sql_bounds(now, parse_days(days))
    statements = [_sighting_statement(ids, chosen_kind, bounds)]
    if chosen_kind != "post":
        statements.append(_release_statement(ids, bounds))
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


def _weekday_of_day(day: str) -> int | None:
    """Weekday of a calendar date. The midnight instant is not an hour bucket."""
    moment = parse_utc(day + "T00:00:00Z")
    if moment is None or moment.strftime("%Y-%m-%d") != day:
        return None
    return moment.weekday()


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


@dataclass
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
    source = str(row.get("source") or "").strip().lower()[:40]
    raw_kind = str(row.get("kind") or "").strip().lower()[:40]
    group = event_group(source, raw_kind)
    if kind != "all" and group != kind:
        return None
    raw = str(row.get("published_at") or "").strip()[:80]
    if not raw:
        return None
    precision = str(row.get("precision") or "")
    # timing_role already applies stated_precision, so a clock is never invented.
    role = timing_role(precision, raw)
    stated = stated_precision(raw, precision)
    title = " ".join(str(row.get("title") or "").split())[:200]
    external_id = str(row.get("external_id") or "").strip()[:80]
    if role == "time":
        moment = parse_utc(raw)
        if moment is None:
            return None
        moment = moment.astimezone(timezone.utc)
        if not _in_window("time", raw, moment, window):
            return None
        return _Event(
            artist_id=artist_id,
            source=source,
            external_id=external_id,
            title=title,
            group=group,
            role="time",
            stated="time",
            raw=raw,
            utc_day=moment.strftime("%Y-%m-%d"),
            weekday=moment.weekday(),
            hour=moment.hour,
            moment=moment,
            coarse_key="",
            rank=PRECISION_RANK["time"],
        )
    if role == "day":
        day = raw[:10]
        weekday = _weekday_of_day(day)
        if weekday is None or not _in_window("day", raw, None, window):
            return None
        return _Event(
            artist_id=artist_id,
            source=source,
            external_id=external_id,
            title=title,
            group=group,
            role="day",
            stated="day",
            raw=raw,
            utc_day=day,
            weekday=weekday,
            hour=None,
            moment=None,
            coarse_key="",
            rank=PRECISION_RANK["day"],
        )
    if stated not in {"month", "year"}:
        return None
    coarse_key = _coarse_key(stated, raw)
    if not coarse_key or not _in_window(stated, raw, None, window):
        return None
    return _Event(
        artist_id=artist_id,
        source=source,
        external_id=external_id,
        title=title,
        group=group,
        role="coarse",
        stated=stated,
        raw=raw,
        utc_day="",
        weekday=None,
        hour=None,
        moment=None,
        coarse_key=coarse_key,
        rank=PRECISION_RANK[stated],
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


def _dedupe(events: list[_Event]) -> tuple[list[_Event], int]:
    """Collapse the same release. Posts stay as stored. A month is not given a day."""
    kept: list[_Event] = []
    dated: dict[tuple, _Event] = {}
    coarse: dict[tuple, _Event] = {}
    for event in events:
        if event.group != "release":
            kept.append(event)
            continue
        title_key = normalize_name(event.title)
        if not title_key:
            kept.append(event)
            continue
        if event.role in {"time", "day"} and event.utc_day:
            key = (event.artist_id, title_key, event.utc_day)
            bucket = dated
        elif event.role == "coarse" and event.coarse_key:
            key = (event.artist_id, title_key, "coarse", event.coarse_key)
            bucket = coarse
        else:
            kept.append(event)
            continue
        current = bucket.get(key)
        if current is None or _prefer(event, current):
            bucket[key] = event
    kept.extend(dated.values())
    kept.extend(coarse.values())
    collapsed = len(events) - len(kept)
    return kept, max(collapsed, 0)


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


def _artist_row(artist: dict, events: list[_Event]) -> dict:
    weekday_counts = [0] * 7
    hour_counts = [0] * 24
    exact_days = [0] * 7
    exact = 0
    dated = 0
    friday = 0
    latest = ""
    latest_moment: datetime | None = None
    for event in events:
        if event.role == "time" and event.hour is not None and event.weekday is not None:
            exact += 1
            hour_counts[event.hour] += 1
            weekday_counts[event.weekday] += 1
            exact_days[event.weekday] += 1
            dated += 1
            if event.weekday == 4:
                friday += 1
            if event.moment is not None and (
                latest_moment is None
                or event.moment > latest_moment
                or (event.moment == latest_moment and event.raw > latest)
            ):
                latest_moment = event.moment
                latest = event.raw
        elif event.role == "day" and event.weekday is not None:
            weekday_counts[event.weekday] += 1
            dated += 1
            if event.weekday == 4:
                friday += 1
    if exact >= MODAL_MIN_EXACT:
        # Weekday and hour both come from exact timestamps. A date-only row is not a mode.
        day_peak = peak(exact_days, list(WEEKDAYS))
        hour_peak = peak(hour_counts, [f"{hour:02d}:00" for hour in range(24)])
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
        "latest_exact_at": latest,
        "weekday_counts": weekday_counts,
        "hour_counts": hour_counts,
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
    notes = []
    if artist_cap_hit:
        notes.append(f"The artist list stopped at {ARTIST_CAP}. Further artists were not read.")
    if sighting_cap_hit:
        notes.append(
            f"Content rows stopped at {ROW_CAP}, newest timestamp first. Older rows in this window were not included."
        )
    if release_cap_hit:
        notes.append(
            f"MusicBrainz rows stopped at {ROW_CAP}, newest timestamp first. Older rows in this window were not included."
        )
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
    classified: list[_Event] = []
    for row in list(sightings or []) + list(releases or []):
        event = _classify_row(row, kind=chosen_kind, window=window, allowed=allowed)
        if event is not None:
            classified.append(event)
    kept, collapsed = _dedupe(classified)
    if collapsed:
        noun = "row" if collapsed == 1 else "rows"
        notes.append(
            f"{collapsed} duplicate stored {noun} for the same release were counted once. "
            "The most precise timestamp was kept."
        )

    heatmap = _zero_heatmap()
    weekday_counts = [0] * 7
    hour_counts = [0] * 24
    exact = day = coarse = 0
    release_dated = release_friday = post_dated = post_friday = 0
    by_artist: dict[int, list[_Event]] = {artist["id"]: [] for artist in cleaned}
    for event in kept:
        bucket = by_artist.get(event.artist_id)
        if bucket is None:
            continue
        bucket.append(event)
        if event.role == "coarse":
            coarse += 1
            continue
        if event.weekday is None:
            continue
        if event.role == "time" and event.hour is not None:
            exact += 1
            heatmap[event.weekday][event.hour] += 1
            weekday_counts[event.weekday] += 1
            hour_counts[event.hour] += 1
        elif event.role == "day":
            day += 1
            weekday_counts[event.weekday] += 1
        else:
            continue
        friday = event.weekday == 4
        if event.group == "release":
            release_dated += 1
            release_friday += int(friday)
        elif event.group == "post":
            post_dated += 1
            post_friday += int(friday)

    sources: dict[str, dict] = {}
    for event in classified:
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
    source_rows = [sources[key] for key in sorted(sources, key=_source_sort)]

    artist_rows = [_artist_row(artist, by_artist[artist["id"]]) for artist in cleaned]
    artist_rows.sort(key=lambda item: (-item["exact_count"], item["name"].casefold(), item["id"]))
    counted = exact + day + coarse
    counts = {
        "artists": len(cleaned),
        "stored_rows": len(classified),
        "counted": counted,
        "exact": exact,
        "day": day,
        "dated": exact + day,
        "coarse": coarse,
        "collapsed": collapsed,
    }
    nonzero = [
        {"hour": hour, "label": f"{hour:02d}:00 UTC", "count": count}
        for hour, count in enumerate(hour_counts)
        if count
    ]
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
            "insights": _insights(release_friday, release_dated, post_friday, post_dated, chosen_kind),
            "sources": source_rows,
            "artists": artist_rows,
        }
    )
    return view


def _cap_rows(rows: list) -> tuple[list, bool]:
    if len(rows) > ROW_CAP:
        return rows[:ROW_CAP], True
    return rows, False


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


def _as_sighting(row: dict, *, source: str, external_id: str, kind: str, published_at: str) -> dict:
    return {
        "artist_id": row.get("artist_id"),
        "source": source,
        "external_id": external_id,
        "title": row.get("title"),
        "kind": kind,
        "published_at": published_at,
        "precision": row.get("precision"),
    }


def _release_rows(rows: list) -> list[dict]:
    found = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        found.append(
            _as_sighting(
                row,
                source="musicbrainz",
                external_id=str(row.get("mbid") or ""),
                kind=str(row.get("release_type") or "release"),
                published_at=str(row.get("released_at") or ""),
            )
        )
    return found


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
    release_rows: list | None = []
    release_problem = ""
    if kind != "post":
        release_rows, release_problem = _read_rows(found[1], "timing releases")
    if sighting_rows is None or release_rows is None:
        return empty_timing(TIMING_READ_ERROR, failed=True, scope=scope, days=days, kind=kind, now=now)
    sightings, sighting_cap_hit = _cap_rows(sighting_rows)
    releases, release_cap_hit = _cap_rows(_release_rows(release_rows))
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


def render_timing_csv(view: dict) -> str:
    """One row per artist. Counts are the deduped chart counts. Formula text is escaped."""
    document = timing_document(view)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(timing_csv_columns())
    for artist in document["artists"]:
        writer.writerow(
            [
                formula_safe(document["scope"]),
                formula_safe(document["days"]),
                formula_safe(document["kind"]),
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
