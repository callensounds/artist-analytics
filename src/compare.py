"""Side-by-side artist comparison from stored rows. No provider calls and no writes."""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import urlencode

from accounts import list_watched
from analytics import WEEKDAYS, WEEKDAY_SHORT, normalize_name, parse_utc, peak, stated_precision
from db import log_read_failure, missing_table, parse_utc as parse_snapshot, read_many, utc_now
from history import (
    TRACK_CURRENT_HOURS,
    _whole_number,
    downsample_points,
    list_tracked_artists,
    search_artists,
    stat_integer,
)
from momentum import percent_text
from scores import score_from_counts

UNAVAILABLE = "unavailable"
NOT_ENOUGH = "not enough history"
# D1's compound-SELECT limit is far below desktop SQLite. One SELECT is the
# whole statement. A UNION chain is what production rejected on /compare.
D1_COMPOUND_TERM_LIMIT = 1
# Character-class GLOB patterns are rejected as "too complex" well before this.
D1_PATTERN_LIMIT = 50
COMPARE_LIMIT = 4
SERIES_LIMIT = 90
CHART_POINT_CAP = 48
TRACK_RANK_CAP = 12
# Indexed seeks for the artists on the page, not a scan of every snapshot.
SLOTS = ("a", "b", "c", "d")
ARTIST_COLORS = ("#6ee7b7", "#7dd3fc", "#fcd34d", "#f9a8d4")
READ_ERROR = "Data temporarily unavailable. Stored history could not be read on this request."
PAGE_NOTE = "Stored counts only. Nothing is estimated, and this page does not call providers."
HOUR_NONE = "No exact UTC timestamp is stored. A date-only release is not given an hour."
WEEKDAY_NONE = "No stored day or exact time is available. A month or a year is not placed on a weekday."
CHART_EMPTY = "No exact stored readings for this metric."
PICK_NOTE = "Pick at least two tracked artists."

# Audience series that already have metric_snapshots rows. Order is the table order.
METRICS = (
    ("spotify-monthly-listeners", "spotify", "Monthly listeners", "Spotify monthly listeners"),
    ("spotify-followers", "spotify", "Followers", "Spotify followers"),
    ("instagram-followers", "instagram", "Followers", "Instagram followers"),
    ("tiktok-followers", "tiktok", "Followers", "TikTok followers"),
    ("tiktok-likes", "tiktok", "Likes", "TikTok likes"),
    ("youtube-subscribers", "youtube", "Subscribers", "YouTube subscribers"),
    ("deezer-fans", "deezer", "Fans", "Deezer fans"),
)
DEFAULT_METRIC = METRICS[0][0]
_METRIC_BY_KEY = {item[0]: item for item in METRICS}
_METRIC_BY_PAIR = {(item[1], item[2]): item for item in METRICS}

# Latest stat on a current top track. The track name is kept so the number stays tied to a row.
TRACK_STATS = (
    ("spotify-popularity", "spotify", "spotify_popularity", "Spotify popularity"),
    ("spotify-plays", "spotify", "spotify_playcount", "Spotify plays"),
    ("deezer-rank", "deezer", "deezer_rank", "Deezer rank score"),
)

_SCORE_FILTER = """
  AND value IS NOT NULL
  AND value >= 0
  AND value = CAST(value AS INTEGER)
  AND (
        (source = 'spotify' AND metric IN ('Monthly listeners', 'Followers'))
     OR (source = 'instagram' AND metric = 'Followers')
     OR (source = 'tiktok' AND metric IN ('Followers', 'Likes'))
     OR (source = 'youtube' AND metric = 'Subscribers')
     OR (source = 'deezer' AND metric = 'Fans')
  )
"""


def timing_role(precision: str | None, raw: str | None) -> str:
    """How a stored stamp may be counted.

    ``time`` has a real UTC clock, so it can fill an hour and a weekday.
    ``day`` has a calendar day and no usable clock. A date-only value is never
    ``time``, even when the stored precision says time. A month or a year is
    ``skip``: it is not given an hour or a weekday. The coarser of the stored
    precision and the text wins, matching the rest of the dashboard.
    """
    text = str(raw or "").strip()
    kind = stated_precision(text, precision)
    day = text[:10]
    if kind == "time":
        moment = parse_utc(text)
        if moment is not None and _real_day(day):
            return "time"
        if _real_day(day):
            return "day"
        return "skip"
    if kind == "day" and _real_day(day):
        return "day"
    return "skip"


def _real_day(day: str) -> bool:
    if len(day) != 10 or day[4] != "-" or day[7] != "-":
        return False
    moment = parse_utc(day + "T00:00:00Z")
    return moment is not None and moment.strftime("%Y-%m-%d") == day


def percent_tenths(change: int, baseline: int) -> int | None:
    """Change in tenths of a percent, half-up. None when the baseline is not positive.

    The same rounding as ``momentum.percent_text``. A zero baseline is not a percent.
    """
    if isinstance(change, bool) or isinstance(baseline, bool):
        return None
    if not isinstance(change, int) or not isinstance(baseline, int) or baseline <= 0:
        return None
    numerator = change * 1000
    if numerator >= 0:
        return (numerator + baseline // 2) // baseline
    return -(((-numerator) + baseline // 2) // baseline)


def growth_delta(latest: dict | None, baseline: dict | None) -> dict:
    """Exact change between two stored endpoints.

    The caller passes the latest snapshot and the latest snapshot at or before
    the cutoff. A point inside the window is not a baseline. If either endpoint
    is missing, or they are not two different times, there is not enough history.
    Nothing is interpolated.
    """
    latest_value = _endpoint_value(latest)
    baseline_value = _endpoint_value(baseline)
    latest_at = _endpoint_at(latest)
    baseline_at = _endpoint_at(baseline)
    if latest_value is None or baseline_value is None or not latest_at or not baseline_at or baseline_at >= latest_at:
        return {"text": NOT_ENOUGH, "status": "missing", "from": "", "to": ""}
    change = latest_value - baseline_value
    percent = percent_text(change, baseline_value)
    text = f"{change:+,}"
    if percent:
        text = f"{text} ({percent})"
    return {
        "text": text,
        "status": "ok",
        "change": change,
        "percent": percent,
        "from": baseline_at,
        "to": latest_at,
    }


def _endpoint_value(point: dict | None):
    if not isinstance(point, dict):
        return None
    value = point.get("value")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _endpoint_at(point: dict | None) -> str:
    if not isinstance(point, dict):
        return ""
    return str(point.get("captured_at") or "")


def normalise_growth(series: dict[int, list[dict]]) -> dict[int, list[dict]]:
    """Percent change from the first common snapshot.

    The common start is the latest of each artist's first stored point, so the
    overlay begins where every included artist has history. Each artist's
    baseline is their earliest stored point at or after that instant. Later
    points are the percent change from that baseline. Points before it are
    omitted. No timestamp is invented for an artist who has no reading there.
    A baseline that is not a positive count is omitted rather than divided.
    """
    prepared: dict[int, list[dict]] = {}
    for artist_id, points in (series or {}).items():
        cleaned = []
        for point in points or []:
            if not isinstance(point, dict):
                continue
            moment = point.get("at")
            value = point.get("value")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                continue
            if not isinstance(moment, datetime):
                continue
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            else:
                moment = moment.astimezone(timezone.utc)
            cleaned.append({"at": moment, "value": value, "id": point.get("id") or 0})
        cleaned.sort(key=lambda item: (item["at"], item["id"]))
        deduped: list[dict] = []
        for item in cleaned:
            if deduped and deduped[-1]["at"] == item["at"]:
                deduped[-1] = item
            else:
                deduped.append(item)
        if deduped:
            prepared[int(artist_id)] = deduped
    if not prepared:
        return {}
    common_start = max(points[0]["at"] for points in prepared.values())
    plotted: dict[int, list[dict]] = {}
    for artist_id, points in prepared.items():
        eligible = [point for point in points if point["at"] >= common_start]
        if not eligible:
            continue
        baseline = eligible[0]["value"]
        if baseline <= 0:
            continue
        series_points = []
        for point in eligible:
            tenths = percent_tenths(point["value"] - baseline, baseline)
            if tenths is None:
                continue
            series_points.append(
                {
                    "at": point["at"],
                    "value": point["value"],
                    "baseline": baseline,
                    "tenths": tenths,
                }
            )
        if series_points:
            plotted[artist_id] = series_points
    return plotted


def compare_path(tokens: list[str], *, metric: str = "", q: str = "", csv: bool = False) -> str:
    """A compare URL. Empty tokens are left out. Values are query-encoded."""
    pairs = []
    for slot, token in zip(SLOTS, tokens):
        text = str(token or "").strip()
        if text:
            pairs.append((slot, text))
    if metric:
        pairs.append(("metric", metric))
    if q:
        pairs.append(("q", q))
    path = "/compare.csv" if csv else "/compare"
    if not pairs:
        return path
    return path + "?" + urlencode(pairs)


def parse_slot_values(values: list[str]) -> list[str]:
    """Non-empty a, b, c, d values, in order, each capped like the artist route."""
    parsed = []
    for value in values[:COMPARE_LIMIT]:
        text = str(value or "").strip()[:120]
        if text:
            parsed.append(text)
    return parsed


def _marks(count: int) -> str:
    return ",".join("?" * count)


def _check(sql: str, params: tuple) -> tuple[str, tuple]:
    if sql.count("?") != len(params):
        raise RuntimeError("compare placeholders do not match parameters")
    return sql, params


def _latest_statement(artist_ids: list[int]) -> tuple[str, tuple]:
    sql = f"""
        SELECT artist_id, source, metric, value, captured_at
        FROM metric_latest
        WHERE artist_id IN ({_marks(len(artist_ids))})
        {_SCORE_FILTER}
    """
    return _check(sql, tuple(artist_ids))


def _legacy_latest_statement(artist_ids: list[int]) -> tuple[str, tuple]:
    """One latest exact row per series when metric_latest has not been created."""
    sql = f"""
        SELECT artist_id, source, metric, value, captured_at
        FROM (
            SELECT artist_id, source, metric, value, captured_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY artist_id, source, metric
                       ORDER BY captured_at DESC, id DESC
                   ) AS rn
            FROM metric_snapshots
            WHERE artist_id IN ({_marks(len(artist_ids))})
            {_SCORE_FILTER}
        ) AS ranked
        WHERE rn = 1
    """
    return _check(sql, tuple(artist_ids))


def compound_select_terms(sql: str) -> int:
    """SELECT arms joined by UNION, INTERSECT, or EXCEPT.

    A plain statement is one term. Each compound keyword adds one. Nested
    subqueries are not compound terms.
    """
    keywords = len(re.findall(r"\b(?:UNION|INTERSECT|EXCEPT)\b", sql or "", re.I))
    if keywords:
        return keywords + 1
    if re.search(r"\bSELECT\b", sql or "", re.I):
        return 1
    return 0


def like_glob_patterns(sql: str) -> list[str]:
    """Literals used as LIKE or GLOB patterns. D1 rejects a long character class."""
    return re.findall(r"\b(?:LIKE|GLOB)\s+'([^']*)'", sql or "", re.I)


def _digit_checks(column: str, positions: tuple[int, ...]) -> str:
    return " AND ".join(f"substr({column}, {pos}, 1) BETWEEN '0' AND '9'" for pos in positions)


def _day_prefix_sql(column: str) -> str:
    """A real YYYY-MM-DD prefix. Digit and hyphen checks, not a GLOB class."""
    return " AND ".join(
        [
            f"length({column}) >= 10",
            f"substr({column}, 5, 1) = '-'",
            f"substr({column}, 8, 1) = '-'",
            _digit_checks(column, (1, 2, 3, 4, 6, 7, 9, 10)),
            f"strftime('%w', substr({column}, 1, 10)) IS NOT NULL",
        ]
    )


def _clock_sql(column: str) -> str:
    """An exact clock. A date-only string fails the length and separator checks."""
    return " AND ".join(
        [
            f"length({column}) >= 19",
            f"(substr({column}, 11, 1) = 'T' OR substr({column}, 11, 1) = ' ')",
            f"substr({column}, 14, 1) = ':'",
            f"substr({column}, 17, 1) = ':'",
            _digit_checks(column, (12, 13, 15, 16, 18, 19)),
            f"CAST(substr({column}, 12, 2) AS INTEGER) BETWEEN 0 AND 23",
            _day_prefix_sql(column),
        ]
    )


def _baseline_statements(artist_ids: list[int], cutoff: str) -> list[tuple[str, tuple]]:
    """One index seek per artist, per audience metric. No compound SELECT.

    Source and metric are bound, so ``(artist_id, source, metric, captured_at)``
    can stop at the newest row at or before the cutoff.
    """
    if not artist_ids:
        return []
    marks = _marks(len(artist_ids))
    statements = []
    for _key, source, metric, _label in METRICS:
        sql = f"""
            SELECT artists.id AS artist_id, s.source AS source, s.metric AS metric,
                   s.value AS value, s.captured_at AS captured_at
            FROM artists
            JOIN metric_snapshots AS s ON s.id = (
                SELECT metric_snapshots.id
                FROM metric_snapshots
                WHERE metric_snapshots.artist_id = artists.id
                  AND metric_snapshots.source = ?
                  AND metric_snapshots.metric = ?
                  AND metric_snapshots.captured_at <= ?
                  AND metric_snapshots.value IS NOT NULL
                  AND metric_snapshots.value >= 0
                  AND metric_snapshots.value = CAST(metric_snapshots.value AS INTEGER)
                ORDER BY metric_snapshots.captured_at DESC, metric_snapshots.id DESC
                LIMIT 1
            )
            WHERE artists.id IN ({marks})
        """
        statements.append(_check(sql, (source, metric, cutoff, *artist_ids)))
    return statements


def _series_statements(artist_ids: list[int], source: str, metric: str) -> list[tuple[str, tuple]]:
    """At most SERIES_LIMIT newest exact points per artist. One seek each, no UNION."""
    sql = f"""
        SELECT artist_id, value, captured_at, id
        FROM metric_snapshots
        WHERE artist_id = ? AND source = ? AND metric = ?
          AND value IS NOT NULL AND value >= 0
          AND value = CAST(value AS INTEGER)
        ORDER BY captured_at DESC, id DESC
        LIMIT {int(SERIES_LIMIT)}
    """
    return [_check(sql, (artist_id, source, metric)) for artist_id in artist_ids]


def _playlist_statement(artist_ids: list[int]) -> tuple[str, tuple]:
    hours = int(TRACK_CURRENT_HOURS)
    marks = _marks(len(artist_ids))
    sql = f"""
        SELECT p.artist_id AS artist_id, COUNT(*) AS n, MAX(p.last_seen_at) AS captured_at
        FROM playlist_appearances AS p
        JOIN (
            SELECT artist_id, MAX(last_seen_at) AS watermark
            FROM playlist_appearances
            WHERE artist_id IN ({marks})
            GROUP BY artist_id
        ) AS w ON w.artist_id = p.artist_id
        WHERE p.artist_id IN ({marks})
          AND p.playlist_id != ''
          AND p.playlist_name != ''
          AND p.removed_at IS NULL
          AND (
                (SELECT c.absences_marked_at FROM playlist_crawls AS c
                  WHERE c.artist_id = p.artist_id AND c.source = 'deezer') IS NOT NULL
                OR p.last_seen_at >= strftime(
                      '%Y-%m-%dT%H:%M:%SZ',
                      replace(replace(w.watermark, 'T', ' '), 'Z', ''),
                      '-{hours} hours'
                   )
          )
        GROUP BY p.artist_id
    """
    return _check(sql, (*artist_ids, *artist_ids))


def _playlist_legacy_statement(artist_ids: list[int]) -> tuple[str, tuple]:
    """Before removed_at existed, a row seen within the grace window was current."""
    hours = int(TRACK_CURRENT_HOURS)
    marks = _marks(len(artist_ids))
    sql = f"""
        SELECT p.artist_id AS artist_id, COUNT(*) AS n, MAX(p.last_seen_at) AS captured_at
        FROM playlist_appearances AS p
        JOIN (
            SELECT artist_id, MAX(last_seen_at) AS watermark
            FROM playlist_appearances
            WHERE artist_id IN ({marks})
            GROUP BY artist_id
        ) AS w ON w.artist_id = p.artist_id
        WHERE p.artist_id IN ({marks})
          AND p.playlist_id != ''
          AND p.playlist_name != ''
          AND p.last_seen_at >= strftime(
                '%Y-%m-%dT%H:%M:%SZ',
                replace(replace(w.watermark, 'T', ' '), 'Z', ''),
                '-{hours} hours'
              )
        GROUP BY p.artist_id
    """
    return _check(sql, (*artist_ids, *artist_ids))


def _release_statement(artist_ids: list[int], start90: str, start365: str, end: str) -> tuple[str, tuple]:
    """Day or exact-time MusicBrainz releases. A month or a year is not placed in the window."""
    sql = f"""
        SELECT artist_id,
               SUM(CASE WHEN released_at >= ? THEN 1 ELSE 0 END) AS d90,
               MAX(CASE WHEN released_at >= ? THEN released_at END) AS newest90,
               COUNT(*) AS d365,
               MAX(released_at) AS newest365
        FROM musicbrainz_releases
        WHERE artist_id IN ({_marks(len(artist_ids))})
          AND released_at >= ?
          AND released_at < ?
          AND precision IN ('day', 'time')
          AND {_day_prefix_sql("released_at")}
        GROUP BY artist_id
    """
    return _check(sql, (start90, start90, *artist_ids, start365, end))


def _track_statement(artist_ids: list[int]) -> tuple[str, tuple]:
    """Latest stored stat for current top tracks. Rank 1 is preferred in Python, over at most a page of rows."""
    hours = int(TRACK_CURRENT_HOURS)
    sql = f"""
        SELECT t.artist_id AS artist_id, t.source AS source, t.external_id AS external_id,
               t.track_name AS track_name, t.rank AS rank,
               s.metric AS metric, s.value AS value, s.captured_at AS captured_at
        FROM artist_tracks AS t
        JOIN track_stats AS s
          ON s.artist_id = t.artist_id
         AND s.source = t.source
         AND s.external_id = t.external_id
         AND s.rowid = (
              SELECT track_stats.rowid FROM track_stats
              WHERE track_stats.artist_id = t.artist_id
                AND track_stats.source = t.source
                AND track_stats.external_id = t.external_id
                AND track_stats.metric = s.metric
              ORDER BY track_stats.captured_at DESC, track_stats.rowid DESC
              LIMIT 1
         )
        WHERE t.artist_id IN ({_marks(len(artist_ids))})
          AND t.track_name != ''
          AND (t.rank IS NULL OR (t.rank >= 1 AND t.rank <= {TRACK_RANK_CAP}))
          AND (
                (t.source = 'spotify' AND s.metric IN ('spotify_popularity', 'spotify_playcount'))
                OR (t.source = 'deezer' AND s.metric = 'deezer_rank')
          )
          AND t.removed_at IS NULL
          AND (
                (SELECT absences_marked_at FROM playlist_crawls
                  WHERE artist_id = t.artist_id AND source = t.source || '_tracks') IS NOT NULL
                OR t.last_seen_at >= (
                    SELECT strftime(
                        '%Y-%m-%dT%H:%M:%SZ',
                        replace(replace(MAX(last_seen_at), 'T', ' '), 'Z', ''),
                        '-{hours} hours'
                    )
                    FROM artist_tracks
                    WHERE artist_id = t.artist_id AND source = t.source
                )
          )
        ORDER BY t.artist_id,
                 CASE WHEN t.rank IS NULL THEN 1 ELSE 0 END,
                 t.rank ASC,
                 t.track_name ASC
        LIMIT 200
    """
    return _check(sql, tuple(artist_ids))


def _hour_statement(artist_ids: list[int], *, table: str, column: str) -> tuple[str, tuple]:
    """Hour buckets from a real clock only. A bare date never becomes 00."""
    if table not in {"content_sightings", "musicbrainz_releases"} or column not in {"published_at", "released_at"}:
        raise ValueError("unsupported timing read")
    sql = f"""
        SELECT artist_id,
               CAST(substr({column}, 12, 2) AS INTEGER) AS hour_utc,
               COUNT(*) AS n
        FROM {table}
        WHERE artist_id IN ({_marks(len(artist_ids))})
          AND precision = 'time'
          AND {_clock_sql(column)}
        GROUP BY artist_id, hour_utc
    """
    return _check(sql, tuple(artist_ids))


def _weekday_statement(artist_ids: list[int], *, table: str, column: str) -> tuple[str, tuple]:
    """Weekday of a stored day or exact time. Month and year precision are excluded."""
    if table not in {"content_sightings", "musicbrainz_releases"} or column not in {"published_at", "released_at"}:
        raise ValueError("unsupported timing read")
    sql = f"""
        SELECT artist_id,
               CAST(strftime('%w', substr({column}, 1, 10)) AS INTEGER) AS dow,
               COUNT(*) AS n
        FROM {table}
        WHERE artist_id IN ({_marks(len(artist_ids))})
          AND precision IN ('day', 'time')
          AND {_day_prefix_sql(column)}
        GROUP BY artist_id, dow
    """
    return _check(sql, tuple(artist_ids))


def report_statements(artist_ids: list[int], metric_key: str, now: datetime) -> list[tuple[str, tuple]]:
    """Every read the comparison batch sends. Callers keep this list free of UNION chains."""
    spec = _METRIC_BY_KEY.get(metric_key) or _METRIC_BY_KEY[DEFAULT_METRIC]
    moment = _as_utc(now)
    cutoff_7 = utc_now(moment - timedelta(days=7))
    cutoff_28 = utc_now(moment - timedelta(days=28))
    start90 = _day(moment - timedelta(days=90))
    start365 = _day(moment - timedelta(days=365))
    end = _day(moment + timedelta(days=1))
    return [
        _latest_statement(artist_ids),
        *_baseline_statements(artist_ids, cutoff_7),
        *_baseline_statements(artist_ids, cutoff_28),
        *_series_statements(artist_ids, spec[1], spec[2]),
        _playlist_statement(artist_ids),
        _release_statement(artist_ids, start90, start365, end),
        _track_statement(artist_ids),
        _hour_statement(artist_ids, table="content_sightings", column="published_at"),
        _hour_statement(artist_ids, table="musicbrainz_releases", column="released_at"),
        _weekday_statement(artist_ids, table="content_sightings", column="published_at"),
        _weekday_statement(artist_ids, table="musicbrainz_releases", column="released_at"),
    ]


def compare_sql_catalog(artist_ids: list[int] | None = None) -> list[str]:
    """SQL text a four-artist comparison can send, including the legacy fallbacks."""
    ids = list(artist_ids or [1, 2, 3, 4])
    moment = datetime(2026, 11, 15, tzinfo=timezone.utc)
    statements = list(report_statements(ids, DEFAULT_METRIC, moment))
    statements.append(_legacy_latest_statement(ids))
    statements.append(_playlist_legacy_statement(ids))
    for _key, source, metric, _label in METRICS:
        statements.extend(_series_statements(ids, source, metric))
    return [sql for sql, _params in statements]


def _soft_error(exc: BaseException) -> bool:
    return missing_table(exc) or "no such column" in str(exc).lower()


def _as_utc(now: datetime | None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _day(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _rows_or_none(found, *, label: str) -> tuple[list[dict] | None, bool]:
    """Rows, and whether a real read failed. A missing table is unavailable, not a crash."""
    if isinstance(found, Exception):
        if not _soft_error(found):
            log_read_failure(label, found)
            return None, True
        return None, False
    if not isinstance(found, list):
        return None, True
    return found, False


async def _resolve(db, raw: list[str]) -> dict:
    """Match each slot to a tracked artist. Ids and normalized names are two indexed reads."""
    id_keys = []
    name_keys = []
    for key in raw:
        if key.isdigit() and len(key) <= 12:
            number = int(key)
            if number >= 1:
                id_keys.append(number)
                continue
        normalized = normalize_name(key)
        if normalized:
            name_keys.append(normalized)
    by_id: dict[int, dict] = {}
    by_name: dict[str, dict] = {}
    if id_keys:
        unique_ids = tuple(dict.fromkeys(id_keys))
        rows = await db.query(
            f"""
            SELECT id, name, normalized_name
            FROM artists
            WHERE tracked = 1 AND id IN ({_marks(len(unique_ids))})
            """,
            unique_ids,
        )
        for row in rows:
            by_id[int(row["id"])] = row
    if name_keys:
        unique_names = tuple(dict.fromkeys(name_keys))
        rows = await db.query(
            f"""
            SELECT id, name, normalized_name
            FROM artists
            WHERE tracked = 1 AND normalized_name IN ({_marks(len(unique_names))})
            """,
            unique_names,
        )
        for row in rows:
            by_name[str(row["normalized_name"])] = row
    artists = []
    missing = []
    canonical = []
    seen: set[int] = set()
    for key in raw:
        row = None
        if key.isdigit() and len(key) <= 12 and int(key) >= 1:
            row = by_id.get(int(key))
        else:
            row = by_name.get(normalize_name(key))
        if row is None:
            missing.append(key)
            canonical.append(key)
            continue
        artist_id = int(row["id"])
        if artist_id in seen:
            continue
        seen.add(artist_id)
        artists.append(
            {
                "id": artist_id,
                "name": str(row.get("name") or ""),
                "normalized_name": str(row.get("normalized_name") or ""),
            }
        )
        canonical.append(str(artist_id))
    return {"artists": artists, "missing": missing, "canonical": canonical}


def _blank_delta() -> dict:
    return {"text": NOT_ENOUGH, "status": "missing", "from": "", "to": ""}


def _na_delta() -> dict:
    return {"text": UNAVAILABLE, "status": "na", "from": "", "to": ""}


def _cell(artist_id: int, value_text: str, captured_at: str, detail: str, delta_7: dict, delta_28: dict) -> dict:
    return {
        "artist_id": artist_id,
        "value_text": value_text,
        "captured_at": captured_at,
        "detail": detail,
        "delta_7": delta_7["text"],
        "delta_7_status": delta_7["status"],
        "delta_7_from": delta_7.get("from") or "",
        "delta_7_to": delta_7.get("to") or "",
        "delta_28": delta_28["text"],
        "delta_28_status": delta_28["status"],
        "delta_28_from": delta_28.get("from") or "",
        "delta_28_to": delta_28.get("to") or "",
    }


def _number_point(row: dict | None) -> dict | None:
    if not isinstance(row, dict):
        return None
    number = _whole_number(row.get("value"))
    captured = str(row.get("captured_at") or "")
    if number is None or not captured:
        return None
    return {"value": number, "captured_at": captured}


def _index_points(rows: list[dict] | None) -> dict[tuple[int, str, str], dict]:
    found = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        point = _number_point(row)
        if point is None:
            continue
        key = (int(row["artist_id"]), str(row.get("source") or ""), str(row.get("metric") or ""))
        found[key] = point
    return found


def _pick_track_stats(rows: list[dict] | None) -> dict[tuple[int, str], dict]:
    """Best current track per artist and stat. A removed track never reaches this list."""
    best: dict[tuple[int, str], tuple] = {}
    chosen: dict[tuple[int, str], dict] = {}
    allowed = {(source, metric) for _key, source, metric, _label in TRACK_STATS}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        source = str(row.get("source") or "")
        metric = str(row.get("metric") or "")
        if (source, metric) not in allowed:
            continue
        value = stat_integer(row.get("value"))
        captured = str(row.get("captured_at") or "")
        name = " ".join(str(row.get("track_name") or "").split())
        if value is None or not captured or not name:
            continue
        rank = _whole_number(row.get("rank"))
        if rank is None or rank < 1 or rank > TRACK_RANK_CAP:
            rank_key = (1, 0)
        else:
            rank_key = (0, rank)
        key = (int(row["artist_id"]), metric)
        sort = (rank_key, name.casefold())
        current = best.get(key)
        if current is not None and current <= sort:
            continue
        best[key] = sort
        chosen[key] = {"value": value, "captured_at": captured, "detail": name[:200]}
    return chosen


def _count_map(rows: list[dict] | None, count_key: str, time_key: str) -> dict[int, dict]:
    found = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        number = _whole_number(row.get(count_key))
        if number is None:
            continue
        captured = str(row.get(time_key) or "")
        found[int(row["artist_id"])] = {"value": number, "captured_at": captured}
    return found


def _fill_buckets(rows: list[dict] | None, buckets: dict[int, list[int]], *, width: int, column: str) -> None:
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        artist_id = int(row["artist_id"])
        count = _whole_number(row.get("n"))
        slot = row.get(column)
        if isinstance(slot, bool) or not isinstance(slot, int):
            try:
                slot = int(slot)
            except (TypeError, ValueError):
                continue
        if count is None or slot < 0 or slot >= width:
            continue
        target = buckets.setdefault(artist_id, [0] * width)
        target[slot] += count


def _bars(counts: list[int], labels: list[str], *, hour: bool) -> list[dict]:
    peak_count = max(counts) if counts else 0
    bars = []
    for index, count in enumerate(counts):
        if count <= 0 or peak_count <= 0:
            height = 0
        else:
            height = max(8, int(round(count * 100 / peak_count)))
        if hour:
            bars.append(
                {
                    "hour": index,
                    "count": count,
                    "height": height,
                    "label": f"{index:02d}:00 UTC",
                }
            )
        else:
            bars.append(
                {
                    "index": index,
                    "count": count,
                    "height": height,
                    "label": labels[index],
                    "short": WEEKDAY_SHORT[index],
                }
            )
    return bars


def _summary(counts: list[int], labels: list[str], *, hour: bool) -> str:
    found = peak(counts, labels)
    if not found:
        return ""
    kind = "hour" if hour else "weekday"
    return f"Most common {kind} is {found['label']} ({found['count']} of {found['total']})."


def _sqlite_dow_to_monday(dow: int) -> int:
    """SQLite ``%w`` is Sunday = 0. The dashboard week starts on Monday."""
    return (int(dow) - 1) % 7


def build_overlay_svg(series: list[dict], label: str) -> str:
    """One polyline per artist. Vertices are stored points, downsampled without new values."""
    drawn = []
    for item in series:
        points = [
            point
            for point in item.get("points") or []
            if isinstance(point.get("at"), datetime) and isinstance(point.get("tenths"), int)
        ]
        if not points:
            continue
        points = downsample_points(points, CHART_POINT_CAP)
        drawn.append({**item, "points": points})
    if not drawn:
        return ""
    moments = [point["at"] for item in drawn for point in item["points"]]
    tenths = [point["tenths"] for item in drawn for point in item["points"]]
    t0, t1 = min(moments), max(moments)
    v0, v1 = min(tenths), max(tenths)
    width, height = 640, 220
    left, right, top, bottom = 46, 10, 16, 20
    inner_w = width - left - right
    inner_h = height - top - bottom
    span = (t1 - t0).total_seconds()
    value_span = v1 - v0

    def x_of(moment: datetime) -> float:
        if span <= 0:
            return left + inner_w / 2
        return left + (moment - t0).total_seconds() / span * inner_w

    def y_of(value: int) -> float:
        if value_span == 0:
            return top + inner_h / 2
        return top + (1 - (value - v0) / value_span) * inner_h

    def coord(point: dict) -> str:
        return f"{x_of(point['at']):.1f},{y_of(point['tenths']):.1f}"

    title = escape(label)
    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{title}" class="compare-svg">',
        f"<title>{title}</title>",
    ]
    zero_y = y_of(0) if v0 <= 0 <= v1 else None
    if zero_y is not None:
        parts.append(
            f'<line x1="{left}" y1="{zero_y:.1f}" x2="{width - right}" y2="{zero_y:.1f}" stroke="#334155" stroke-width="1"/>'
        )
    for anchor in (v0, v1):
        parts.append(
            f'<text x="4" y="{y_of(anchor) + 4:.1f}" fill="#c5d0de" font-size="11">{_percent_label(anchor)}</text>'
        )
    for item in drawn:
        color = item["color"]
        points = item["points"]
        if len(points) >= 2:
            poly = " ".join(coord(point) for point in points)
            parts.append(
                f'<polyline data-artist="{int(item["id"])}" fill="none" stroke="{color}" stroke-width="2" '
                f'stroke-linejoin="round" stroke-linecap="round" points="{poly}"/>'
            )
        marks = [points[0], points[-1]] if len(points) > 1 else [points[0]]
        seen = set()
        for point in marks:
            key = (point["at"], point["tenths"])
            if key in seen:
                continue
            seen.add(key)
            parts.append(
                f'<circle data-artist="{int(item["id"])}" data-tenths="{int(point["tenths"])}" '
                f'cx="{x_of(point["at"]):.1f}" cy="{y_of(point["tenths"]):.1f}" r="3" fill="{color}"/>'
            )
    parts.append("</svg>")
    return "".join(parts)


def _percent_label(tenths: int) -> str:
    sign = "-" if tenths < 0 else ""
    whole, frac = divmod(abs(int(tenths)), 10)
    if frac:
        return f"{sign}{whole}.{frac}%"
    return f"{sign}{whole}%"


def _chart_from_rows(artists: list[dict], rows: list[dict] | None, spec: tuple) -> dict:
    _key, source, metric, label = spec
    grouped: dict[int, list[dict]] = {artist["id"]: [] for artist in artists}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        artist_id = int(row["artist_id"])
        if artist_id not in grouped:
            continue
        number = _whole_number(row.get("value"))
        moment = parse_snapshot(str(row.get("captured_at") or ""))
        if number is None or moment is None:
            continue
        grouped[artist_id].append({"at": moment, "value": number, "id": row.get("id") or 0})
    plotted = normalise_growth(grouped)
    names = {artist["id"]: artist["name"] for artist in artists}
    colors = {artist["id"]: artist["color"] for artist in artists}
    series = []
    for index, artist in enumerate(artists):
        points = plotted.get(artist["id"])
        if not points:
            continue
        series.append(
            {
                "id": artist["id"],
                "name": names[artist["id"]],
                "color": colors[artist["id"]],
                "points": points,
            }
        )
        if index >= COMPARE_LIMIT:
            break
    note = (
        "Percent change from the first common snapshot: the earliest stored point "
        "at or after every included artist's first reading. Earlier points are omitted. "
        "Nothing is interpolated. A series whose first point in that window is zero is omitted."
    )
    svg = build_overlay_svg(series, f"{label}, percent change from the first common snapshot")
    return {
        "svg": svg,
        "note": note if svg else CHART_EMPTY,
        "series": [{"id": item["id"], "name": item["name"], "color": item["color"]} for item in series],
    }


def _baseline_from_parts(parts: list) -> tuple[dict[tuple[int, str, str], dict], set[tuple[str, str]]]:
    """Points that were read, and the metrics whose statement failed.

    One failed metric leaves the others alone. It does not blank the page.
    """
    points: dict[tuple[int, str, str], dict] = {}
    failed: set[tuple[str, str]] = set()
    for part, (_key, source, metric, _label) in zip(parts, METRICS):
        rows, bad = _rows_or_none(part, label="compare baseline")
        if bad or rows is None:
            failed.add((source, metric))
            continue
        for row in rows:
            point = _number_point(row)
            if point is None or not isinstance(row, dict):
                continue
            points[(int(row["artist_id"]), str(row.get("source") or source), str(row.get("metric") or metric))] = point
    return points, failed


def _series_from_parts(parts: list) -> list[dict] | None:
    rows: list[dict] = []
    failed = 0
    for part in parts:
        found, bad = _rows_or_none(part, label="compare chart")
        if bad or found is None:
            failed += 1
            continue
        rows.extend(row for row in found if isinstance(row, dict))
    if parts and failed == len(parts):
        return None
    return rows


async def _load_report(db, artists: list[dict], metric_key: str, now: datetime) -> dict:
    artist_ids = [artist["id"] for artist in artists]
    spec = _METRIC_BY_KEY.get(metric_key) or _METRIC_BY_KEY[DEFAULT_METRIC]
    statements = report_statements(artist_ids, spec[0], now)
    base_count = len(METRICS)
    series_count = len(artist_ids)
    found = await read_many(db, statements)
    index = 0
    latest_found = found[index]
    index += 1
    baseline_7_parts = found[index : index + base_count]
    index += base_count
    baseline_28_parts = found[index : index + base_count]
    index += base_count
    series_parts = found[index : index + series_count]
    index += series_count
    tail = found[index:]
    latest_rows, latest_failed = _rows_or_none(latest_found, label="compare latest")
    if latest_rows is None and isinstance(latest_found, Exception) and _soft_error(latest_found):
        try:
            latest_rows = await db.query(*_legacy_latest_statement(artist_ids))
            latest_failed = False
        except Exception as exc:
            latest_rows = None
            latest_failed = not _soft_error(exc)
            if latest_failed:
                log_read_failure("compare latest", exc)
    prior_7, failed_7 = _baseline_from_parts(baseline_7_parts)
    prior_28, failed_28 = _baseline_from_parts(baseline_28_parts)
    series_rows = _series_from_parts(series_parts)
    playlist_rows, _playlist_failed = _rows_or_none(tail[0], label="compare playlists")
    if playlist_rows is None and isinstance(tail[0], Exception) and "no such column" in str(tail[0]).lower():
        try:
            playlist_rows = await db.query(*_playlist_legacy_statement(artist_ids))
        except Exception as exc:
            playlist_rows = None
            if not _soft_error(exc):
                log_read_failure("compare playlists", exc)
    release_rows, _release_failed = _rows_or_none(tail[1], label="compare releases")
    track_rows, _track_failed = _rows_or_none(tail[2], label="compare track stats")
    hour_rows, _hour_read_failed = _rows_or_none(tail[3], label="compare hours")
    hour_release_rows, _hour_release_failed = _rows_or_none(tail[4], label="compare release hours")
    weekday_rows, _weekday_read_failed = _rows_or_none(tail[5], label="compare weekdays")
    weekday_release_rows, _weekday_release_failed = _rows_or_none(tail[6], label="compare release weekdays")

    latest = _index_points(latest_rows)
    tracks = _pick_track_stats(track_rows) if track_rows is not None else None
    playlists = _count_map(playlist_rows, "n", "captured_at") if playlist_rows is not None else None
    releases = {}
    if release_rows is not None:
        for row in release_rows:
            if not isinstance(row, dict):
                continue
            releases[int(row["artist_id"])] = row

    score_rows: dict[int, list[dict]] = {}
    if latest_rows is not None:
        for row in latest_rows:
            if isinstance(row, dict):
                score_rows.setdefault(int(row["artist_id"]), []).append(row)
    scores = {artist["id"]: score_from_counts(score_rows.get(artist["id"]) or []) for artist in artists}
    ranked = []
    for artist in artists:
        score = scores[artist["id"]]
        if score.get("score_hundredths") is None:
            continue
        ranked.append((int(score["score_hundredths"]), artist["name"].casefold(), artist["id"]))
    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    rank_of = {artist_id: index for index, (_score, _name, artist_id) in enumerate(ranked, start=1)}

    rows = []
    for key, source, metric, label in METRICS:
        cells = []
        for artist in artists:
            if latest_rows is None:
                cells.append(_cell(artist["id"], UNAVAILABLE, "", "", _na_delta(), _na_delta()))
                continue
            pair = (artist["id"], source, metric)
            current = latest.get(pair)
            if current is None:
                cells.append(_cell(artist["id"], UNAVAILABLE, "", "", _blank_delta(), _blank_delta()))
                continue
            delta_7 = _na_delta() if (source, metric) in failed_7 else growth_delta(current, prior_7.get(pair))
            delta_28 = _na_delta() if (source, metric) in failed_28 else growth_delta(current, prior_28.get(pair))
            cells.append(
                _cell(artist["id"], f"{current['value']:,}", current["captured_at"], "", delta_7, delta_28)
            )
        rows.append({"key": key, "label": label, "cells": cells})

    for key, _source, metric, label in TRACK_STATS:
        cells = []
        for artist in artists:
            chosen = None if tracks is None else tracks.get((artist["id"], metric))
            if tracks is None or chosen is None:
                cells.append(_cell(artist["id"], UNAVAILABLE, "", "", _na_delta(), _na_delta()))
                continue
            cells.append(
                _cell(
                    artist["id"],
                    f"{chosen['value']:,}",
                    chosen["captured_at"],
                    chosen["detail"],
                    _na_delta(),
                    _na_delta(),
                )
            )
        rows.append({"key": key, "label": label, "cells": cells})

    cells = []
    for artist in artists:
        chosen = None if playlists is None else playlists.get(artist["id"])
        if playlists is None:
            cells.append(_cell(artist["id"], UNAVAILABLE, "", "", _na_delta(), _na_delta()))
        elif chosen is None:
            cells.append(_cell(artist["id"], "0", "", "", _na_delta(), _na_delta()))
        else:
            cells.append(
                _cell(artist["id"], f"{chosen['value']:,}", chosen["captured_at"], "", _na_delta(), _na_delta())
            )
    rows.append({"key": "playlists", "label": "Playlist appearances", "cells": cells})

    cells = []
    rank_cells = []
    for artist in artists:
        score = scores[artist["id"]]
        newest = ""
        if latest_rows is not None:
            for row in score_rows.get(artist["id"]) or []:
                pair = (str(row.get("source") or ""), str(row.get("metric") or ""))
                if pair not in _METRIC_BY_PAIR:
                    continue
                number = _whole_number(row.get("value"))
                captured = str(row.get("captured_at") or "")
                if number is None or number < 1 or not captured:
                    continue
                if captured > newest:
                    newest = captured
        if not score.get("score_text"):
            cells.append(_cell(artist["id"], UNAVAILABLE, "", "", _na_delta(), _na_delta()))
            rank_cells.append(_cell(artist["id"], UNAVAILABLE, "", "", _na_delta(), _na_delta()))
            continue
        cells.append(_cell(artist["id"], score["score_text"], newest, "", _na_delta(), _na_delta()))
        rank_cells.append(_cell(artist["id"], str(rank_of[artist["id"]]), "", "", _na_delta(), _na_delta()))
    rows.append({"key": "score", "label": "Audience score", "cells": cells})
    rows.append({"key": "score-rank", "label": "Score rank", "cells": rank_cells})

    for key, label, count_name, time_name in (
        ("releases-90", "MusicBrainz releases (90 days)", "d90", "newest90"),
        ("releases-365", "MusicBrainz releases (365 days)", "d365", "newest365"),
    ):
        cells = []
        for artist in artists:
            if release_rows is None:
                cells.append(_cell(artist["id"], UNAVAILABLE, "", "", _na_delta(), _na_delta()))
                continue
            row = releases.get(artist["id"])
            if row is None:
                cells.append(_cell(artist["id"], "0", "", "", _na_delta(), _na_delta()))
                continue
            number = _whole_number(row.get(count_name))
            if number is None:
                cells.append(_cell(artist["id"], UNAVAILABLE, "", "", _na_delta(), _na_delta()))
                continue
            captured = str(row.get(time_name) or "")
            cells.append(_cell(artist["id"], f"{number:,}", captured, "", _na_delta(), _na_delta()))
        rows.append({"key": key, "label": label, "cells": cells})

    hours: dict[int, list[int]] = {}
    weekdays_sqlite: dict[int, list[int]] = {}
    hour_failed = hour_rows is None and hour_release_rows is None
    weekday_failed = weekday_rows is None and weekday_release_rows is None
    if hour_rows is not None:
        _fill_buckets(hour_rows, hours, width=24, column="hour_utc")
    if hour_release_rows is not None:
        _fill_buckets(hour_release_rows, hours, width=24, column="hour_utc")
    if weekday_rows is not None:
        _fill_buckets(weekday_rows, weekdays_sqlite, width=7, column="dow")
    if weekday_release_rows is not None:
        _fill_buckets(weekday_release_rows, weekdays_sqlite, width=7, column="dow")
    timing = []
    hour_labels = [f"{hour:02d}:00 UTC" for hour in range(24)]
    for artist in artists:
        hour_counts = hours.get(artist["id"], [0] * 24) if not hour_failed else None
        weekday_counts = [0] * 7
        if not weekday_failed:
            raw_days = weekdays_sqlite.get(artist["id"], [0] * 7)
            for dow, count in enumerate(raw_days):
                weekday_counts[_sqlite_dow_to_monday(dow)] += count
        else:
            weekday_counts = None
        hour_total = sum(hour_counts) if hour_counts is not None else 0
        weekday_total = sum(weekday_counts) if weekday_counts is not None else 0
        timing.append(
            {
                "id": artist["id"],
                "name": artist["name"],
                "color": artist["color"],
                "hour_total": hour_total,
                "weekday_total": weekday_total,
                "hour_reason": READ_ERROR if hour_failed else ("" if hour_total else HOUR_NONE),
                "weekday_reason": READ_ERROR if weekday_failed else ("" if weekday_total else WEEKDAY_NONE),
                "hour_summary": _summary(hour_counts, hour_labels, hour=True) if hour_counts is not None else "",
                "weekday_summary": _summary(weekday_counts, WEEKDAYS, hour=False) if weekday_counts is not None else "",
                "hour_bars": _bars(hour_counts, hour_labels, hour=True) if hour_counts is not None and hour_total else [],
                "weekday_bars": _bars(weekday_counts or [], WEEKDAYS, hour=False)
                if weekday_counts is not None and weekday_total
                else [],
            }
        )
    chart = _chart_from_rows(artists, series_rows, spec)
    if series_rows is None:
        chart = {"svg": "", "note": READ_ERROR, "series": []}
    # The page banner stays clear. Only the audience table says its own read failed.
    table_note = READ_ERROR if latest_rows is None and latest_failed else ""
    return {"rows": rows, "timing": timing, "chart": chart, "table_note": table_note}


def _decorate(artists: list[dict], metric_key: str) -> list[dict]:
    ids = [artist["id"] for artist in artists]
    decorated = []
    for index, artist in enumerate(artists):
        decorated.append(
            {
                **artist,
                "slot": SLOTS[index],
                "color": ARTIST_COLORS[index % len(ARTIST_COLORS)],
                "remove_href": compare_path(
                    [str(item) for item in ids if item != artist["id"]],
                    metric=metric_key if metric_key != DEFAULT_METRIC else "",
                ),
            }
        )
    return decorated


def _picker_entry(ids: list[int], artist_id: int, name: str, metric_key: str) -> dict:
    added = artist_id in ids
    chosen = ids if added or len(ids) >= COMPARE_LIMIT else [*ids, artist_id]
    href = compare_path(
        [str(item) for item in chosen],
        metric=metric_key if metric_key and metric_key != DEFAULT_METRIC else "",
    )
    return {"id": artist_id, "name": name, "href": href, "added": added}


async def _load_picker(db, artists: list[dict], *, query: str, user_id: int | None, metric_key: str) -> dict:
    ids = [artist["id"] for artist in artists]
    show = len(artists) < COMPARE_LIMIT
    mode = "full" if len(artists) < 2 else "add"
    matches = []
    tracked = []
    watching = []
    note = ""
    if not show:
        return {"show": False, "mode": "none", "query": query, "matches": [], "tracked": [], "watching": [], "note": ""}
    if user_id:
        try:
            watched = await list_watched(db, int(user_id))
        except Exception as exc:
            log_read_failure("compare watchlist", exc)
            watched = []
            note = READ_ERROR
        watching = [_picker_entry(ids, int(row["id"]), str(row.get("name") or ""), metric_key) for row in watched or []]
    if query:
        try:
            found = await search_artists(db, query)
        except Exception as exc:
            log_read_failure("compare search", exc)
            found = []
            note = READ_ERROR
        matches = [_picker_entry(ids, int(row["id"]), str(row.get("name") or ""), metric_key) for row in found or []]
    elif mode == "full":
        try:
            listing = await list_tracked_artists(db, page=1, sort="name")
        except Exception as exc:
            log_read_failure("compare directory", exc)
            listing = {"artists": []}
            note = READ_ERROR
        tracked = [
            _picker_entry(ids, int(row["id"]), str(row.get("name") or ""), metric_key)
            for row in listing.get("artists") or []
        ]
    return {
        "show": True,
        "mode": mode,
        "query": query,
        "matches": matches,
        "tracked": tracked,
        "watching": watching,
        "note": note,
    }


def render_compare_csv(artists: list[dict], rows: list[dict]) -> str:
    """The same table as the page. Missing cells stay the words unavailable or not enough history."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        ["metric", "artist", "artist_id", "value", "detail", "captured_at", "delta_7d", "delta_28d"]
    )
    names = {artist["id"]: artist["name"] for artist in artists}
    for row in rows:
        for cell in row["cells"]:
            writer.writerow(
                [
                    row["label"],
                    names.get(cell["artist_id"], ""),
                    cell["artist_id"],
                    cell["value_text"],
                    cell["detail"],
                    cell["captured_at"] or UNAVAILABLE,
                    cell["delta_7"],
                    cell["delta_28"],
                ]
            )
    return buffer.getvalue()


def _metric_key(raw: str) -> str:
    text = str(raw or "").strip()[:80]
    if text in _METRIC_BY_KEY:
        return text
    return DEFAULT_METRIC


async def build_compare_view(
    db,
    *,
    slots: list[str],
    metric: str = "",
    query: str = "",
    user_id: int | None = None,
    now: datetime | None = None,
    redirect: bool = True,
) -> dict:
    """Artists, table, chart, and picker. Reads only.

    ``slots`` is the raw a, b, c, d list. A slug redirects to the numeric id
    when ``redirect`` is set. Fewer than two tracked artists returns the picker.
    """
    moment = _as_utc(now)
    raw = parse_slot_values(slots)
    metric_key = _metric_key(metric)
    metric_explicit = str(metric or "").strip()[:80] in _METRIC_BY_KEY
    needle = str(query or "").strip()[:120]
    resolved = await _resolve(db, raw)
    artists = resolved["artists"]
    if redirect and resolved["canonical"] != raw:
        target_metric = metric_key if metric_explicit else ""
        return {"redirect": compare_path(resolved["canonical"], metric=target_metric, q=needle)}
    decorated = _decorate(artists, metric_key if metric_explicit else "")
    ready = len(decorated) >= 2
    report = {"rows": [], "timing": [], "chart": {"svg": "", "note": "", "series": []}, "read_error": ""}
    if ready:
        report = await _load_report(db, decorated, metric_key, moment)
    picker = await _load_picker(
        db,
        decorated,
        query=needle,
        user_id=user_id,
        metric_key=metric_key if metric_explicit else "",
    )
    missing = resolved["missing"]
    missing_note = ""
    if missing:
        shown = ", ".join(missing[:COMPARE_LIMIT])
        missing_note = f"Not tracked: {shown}. This page does not look them up on a provider."
    form_metric = metric_key if ready or metric_explicit else ""
    cards = [
        {
            "artist": artist,
            "rows": [
                {"key": row["key"], "label": row["label"], "cell": row["cells"][index]}
                for row in report["rows"]
            ],
        }
        for index, artist in enumerate(decorated)
    ]
    return {
        "redirect": "",
        "ready": ready,
        "note": PAGE_NOTE,
        "read_error": "",
        "table_note": report.get("table_note") or "",
        "missing_note": missing_note,
        "artists": decorated,
        "rows": report["rows"],
        "cards": cards,
        "timing": report["timing"],
        "chart_svg": report["chart"]["svg"] if ready else "",
        "chart_note": report["chart"]["note"] if ready else "",
        "chart_series": report["chart"]["series"] if ready else [],
        "metric_options": [
            {"key": item[0], "label": item[3], "selected": item[0] == metric_key}
            for item in METRICS
        ],
        "form_slots": [{"slot": artist["slot"], "value": str(artist["id"])} for artist in decorated],
        "form_metric": form_metric,
        "csv_href": compare_path([str(artist["id"]) for artist in decorated], csv=True) if ready else "",
        "picker": picker,
        "pick_note": "" if ready else PICK_NOTE,
        "hour_note": "Hours are UTC and use an exact timestamp only. A date-only release is not given an hour.",
        "weekday_note": "Weekdays use a stored day or an exact time. A month or a year is left out.",
        "release_note": (
            "Release counts are official MusicBrainz albums, EPs, and singles with a day or an exact time. "
            "A month or a year is not placed in the 90-day or 365-day window."
        ),
        "score_note": "Score rank is among the artists on this page. The full list is on Scores.",
        "delta_note": (
            "7d and 28d subtract the latest stored snapshot at or before that cutoff from the latest snapshot. "
            "If either stored endpoint is missing, the cell says not enough history."
        ),
    }


def empty_compare_view(message: str) -> dict:
    """The picker shell when the database cannot be read. No estimated counts."""
    return {
        "redirect": "",
        "ready": False,
        "note": PAGE_NOTE,
        "read_error": message,
        "table_note": "",
        "missing_note": "",
        "artists": [],
        "rows": [],
        "cards": [],
        "timing": [],
        "chart_svg": "",
        "chart_note": "",
        "chart_series": [],
        "metric_options": [],
        "form_slots": [],
        "form_metric": "",
        "csv_href": "",
        "picker": {"show": True, "mode": "full", "query": "", "matches": [], "tracked": [], "watching": [], "note": ""},
        "pick_note": PICK_NOTE,
        "hour_note": "",
        "weekday_note": "",
        "release_note": "",
        "score_note": "",
        "delta_note": "",
    }
