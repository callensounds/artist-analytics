"""Daily watchlist digest from stored rows. No provider calls and no writes.

Artist set is the D1 watchlist when that session has saved artists, otherwise
the top tracked artists by the stored Spotify monthly listeners count in
``metric_latest``. Counts are the latest ``metric_snapshots`` row and the latest
row that is at least six days older. Each of those reads is one index seek per
audience metric (the same shape ``compare`` uses), batched into a single D1
round trip. A missing older row stays "not enough history". Nothing is
interpolated, and spike flags are computed in memory only.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timedelta, timezone

from analytics import parse_utc
from charts import CHART_LABELS, CHART_SOURCES
from compare import (
    METRICS,
    NOT_ENOUGH,
    UNAVAILABLE,
    _baseline_statements,
    compound_select_terms,
    growth_delta,
    like_glob_patterns,
    percent_tenths,
    timing_role,
)
from db import log_read_failure, missing_table, read_many, utc_now
from history import _playlist_url, _whole_number, present_alerts

# D1 rejects a UNION chain longer than this. Every digest statement stays at one
# SELECT arm; subqueries are not extra compound terms.
COMPOUND_TERM_LIMIT = 5
PATTERN_LIMIT = 50
# Free-plan CPU is about 1–2s. The artist list and every IN-list stay inside this.
DIGEST_ARTIST_CAP = 12
DIGEST_FALLBACK_CAP = 10
WINDOW_DAYS = 7
# A snapshot newer than this is inside the window, so it is not a 7-day baseline.
BASELINE_MIN_DAYS = 6
DEFAULT_THRESHOLD_PERCENT = 5.0
RELEASE_LIMIT = 80
CHART_LIMIT = 200
PLAYLIST_LIMIT = 80
ALERT_LIMIT = 80
PER_ARTIST_LIST = 10

PAGE_NOTE = (
    "Stored counts only. Nothing is estimated or interpolated, and this page does not call providers or write to the database."
)
BASELINE_NOTE = (
    f"{WINDOW_DAYS}-day change is the latest stored snapshot minus the latest stored snapshot "
    f"that is at least {BASELINE_MIN_DAYS} days older. Both timestamps are the times those rows were stored. "
    "If that older snapshot is missing, the cell says not enough history."
)
WATCHLIST_NOTE = f"Showing artists on your watchlist (up to {DIGEST_ARTIST_CAP})."
EMPTY_WATCHLIST_NOTE = (
    "Your watchlist is empty, so this shows the top tracked artists by stored Spotify monthly listeners."
)
UNAVAILABLE_WATCHLIST_NOTE = (
    "No watchlist is available for this login, so this shows the top tracked artists by stored Spotify monthly listeners."
)
UNREAD_WATCHLIST_NOTE = (
    "The watchlist could not be read, so this shows the top tracked artists by stored Spotify monthly listeners."
)
NONE_STORED_SUFFIX = " No tracked artist with a stored Spotify monthly listeners count was found."
DIGEST_READ_ERROR = "Data temporarily unavailable. Stored history could not be read on this request."
DIGEST_DB_NOTE = "The digest reads stored crawl data when D1 is bound. This page does not call providers."
RELEASE_NOTE = (
    "Official MusicBrainz albums, EPs, and singles with a day or an exact UTC time in the last "
    f"{WINDOW_DAYS} days. A month or a year is not placed in the window. A date-only value is not given a clock time."
)
CHART_NOTE = (
    "iTunes chart rows whose first stored chart date is in the last "
    f"{WINDOW_DAYS} days. A title that already had an earlier stored chart date is not listed again."
)
PLAYLIST_NOTE = f"Playlist appearances whose first stored time is in the last {WINDOW_DAYS} days."
ALERT_NOTE = (
    f"Growth alerts the crawler stored for these artists in the last {WINDOW_DAYS} days. "
    "Spike flags are computed while the page is read and are not saved."
)
DATE_ONLY_NOTE = "Date only. No clock time is stored."
EXACT_TIME_NOTE = "Exact UTC time."
NO_COUNTS_NOTE = "No exact audience count is stored yet."
SECTION_UNAVAILABLE = {
    "metrics": "Data temporarily unavailable. Stored audience counts could not be read on this request.",
    "releases": "Data temporarily unavailable. Stored releases could not be read on this request.",
    "charts": "Data temporarily unavailable. Stored chart entries could not be read on this request.",
    "playlists": "Data temporarily unavailable. Stored playlist adds could not be read on this request.",
    "alerts": "Data temporarily unavailable. Stored alerts could not be read on this request.",
}
_SOURCE_NOTES = {
    "watchlist": WATCHLIST_NOTE,
    "tracked-empty-watchlist": EMPTY_WATCHLIST_NOTE,
    "tracked-watchlist-unread": UNREAD_WATCHLIST_NOTE,
    "tracked-no-watchlist": UNAVAILABLE_WATCHLIST_NOTE,
}
_THRESHOLD_RE = re.compile(r"\d{1,3}(\.\d)?")


def parse_threshold(raw: str | float | None) -> float:
    """Percent flag, one decimal, from 0.1 to 100. Anything else is 5.

    ``?threshold=5`` means 5 percent. The value is only used in memory.
    """
    text = str(raw or "").strip()
    if text.endswith("%"):
        text = text[:-1].strip()
    if not text or not _THRESHOLD_RE.fullmatch(text):
        return DEFAULT_THRESHOLD_PERCENT
    tenths = int(round(float(text) * 10))
    if tenths < 1 or tenths > 1000:
        return DEFAULT_THRESHOLD_PERCENT
    return tenths / 10.0


def _threshold_input(value: float) -> str:
    tenths = int(round(value * 10))
    if tenths % 10 == 0:
        return str(tenths // 10)
    return f"{tenths // 10}.{tenths % 10}"


def _threshold_label(value: float) -> str:
    return f"{_threshold_input(value)}%"


def _as_utc(now: datetime | None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _window(now: datetime) -> dict:
    moment = _as_utc(now)
    start = moment - timedelta(days=WINDOW_DAYS)
    baseline = moment - timedelta(days=BASELINE_MIN_DAYS)
    return {
        "now": moment,
        "start_stamp": utc_now(start),
        "now_stamp": utc_now(moment),
        "baseline_cutoff": utc_now(baseline),
        "start_day": start.strftime("%Y-%m-%d"),
        "end_day": moment.strftime("%Y-%m-%d"),
        "end_exclusive": (moment + timedelta(days=1)).strftime("%Y-%m-%d"),
    }


def _ids(artist_ids: list[int]) -> list[int]:
    found: list[int] = []
    for value in artist_ids or []:
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if value < 1 or value in found:
            continue
        found.append(value)
        if len(found) >= DIGEST_ARTIST_CAP:
            break
    return found


def _clean_user_id(user_id: int | None) -> int | None:
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        return None
    return user_id


def _marks(count: int) -> str:
    return ",".join("?" * count)


def _check(sql: str, params: tuple) -> tuple[str, tuple]:
    if sql.count("?") != len(params):
        raise RuntimeError("digest placeholders do not match parameters")
    if compound_select_terms(sql) > COMPOUND_TERM_LIMIT:
        raise RuntimeError("digest SQL exceeds the D1 compound SELECT limit")
    for pattern in like_glob_patterns(sql):
        if len(pattern) > PATTERN_LIMIT:
            raise RuntimeError("digest SQL LIKE pattern is too long")
    return sql, params


def watch_statement(user_id: int) -> tuple[str, tuple]:
    """Newest saved artists. ``user_artists (user_id, created_at)`` seeks this."""
    sql = f"""
        SELECT artists.id AS id, artists.name AS name
        FROM user_artists
        JOIN artists ON artists.id = user_artists.artist_id
        WHERE user_artists.user_id = ?
        ORDER BY user_artists.created_at DESC, artists.id DESC
        LIMIT {int(DIGEST_ARTIST_CAP)}
    """
    return _check(sql, (int(user_id),))


def fallback_statement() -> tuple[str, tuple]:
    """Top tracked artists by the stored Spotify monthly listeners summary.

    ``metric_latest`` is one row per artist and metric. This does not scan
    ``metric_snapshots``. A rounded value that is not an integer is left out.
    """
    sql = f"""
        SELECT artists.id AS id, artists.name AS name
        FROM metric_latest
        JOIN artists ON artists.id = metric_latest.artist_id
        WHERE metric_latest.source = 'spotify'
          AND metric_latest.metric = 'Monthly listeners'
          AND metric_latest.value IS NOT NULL
          AND metric_latest.value >= 0
          AND metric_latest.value = CAST(metric_latest.value AS INTEGER)
          AND artists.tracked = 1
        ORDER BY metric_latest.value DESC, artists.id ASC
        LIMIT {int(DIGEST_FALLBACK_CAP)}
    """
    return _check(sql, ())


def _release_statement(artist_ids: list[int], start_day: str, end_exclusive: str) -> tuple[str, tuple]:
    sql = f"""
        SELECT artist_id, mbid, title, release_type, released_at, precision
        FROM musicbrainz_releases
        WHERE artist_id IN ({_marks(len(artist_ids))})
          AND released_at >= ?
          AND released_at < ?
          AND precision IN ('day', 'time')
        ORDER BY released_at DESC, artist_id ASC
        LIMIT {int(RELEASE_LIMIT)}
    """
    return _check(sql, (*artist_ids, start_day, end_exclusive))


def _chart_statement(artist_ids: list[int], start_day: str, end_day: str) -> tuple[str, tuple]:
    """Chart rows in the window that have no earlier stored date for that title.

    ``chart_entries (artist_id, chart_date)`` serves the outer range and the
    existence check. Ordered oldest-first so the row cap keeps the debut day.
    """
    sources = ", ".join("'" + source + "'" for source in CHART_SOURCES)
    sql = f"""
        SELECT artist_id, chart_source, territory, chart_date, position,
               track_or_album_name, collected_at, id
        FROM chart_entries
        WHERE artist_id IN ({_marks(len(artist_ids))})
          AND chart_source IN ({sources})
          AND chart_date >= ?
          AND chart_date <= ?
          AND NOT EXISTS (
              SELECT 1
              FROM chart_entries AS older
              WHERE older.artist_id = chart_entries.artist_id
                AND older.chart_source = chart_entries.chart_source
                AND older.territory = chart_entries.territory
                AND older.track_or_album_name = chart_entries.track_or_album_name
                AND older.chart_date < ?
          )
        ORDER BY chart_date ASC, position ASC, id ASC
        LIMIT {int(CHART_LIMIT)}
    """
    return _check(sql, (*artist_ids, start_day, end_day, start_day))


def _playlist_sql(artist_ids: list[int], *, with_source: bool) -> str:
    source_sql = ", source" if with_source else ""
    return f"""
        SELECT artist_id, playlist_id, playlist_name, owner_name, position,
               first_seen_at, last_seen_at{source_sql}
        FROM playlist_appearances
        WHERE artist_id IN ({_marks(len(artist_ids))})
          AND first_seen_at >= ?
          AND first_seen_at <= ?
          AND playlist_id != ''
          AND playlist_name != ''
        ORDER BY first_seen_at DESC, id DESC
        LIMIT {int(PLAYLIST_LIMIT)}
    """


def _playlist_statement(artist_ids: list[int], start_stamp: str, now_stamp: str) -> tuple[str, tuple]:
    return _check(_playlist_sql(artist_ids, with_source=True), (*artist_ids, start_stamp, now_stamp))


def _playlist_legacy_statement(artist_ids: list[int], start_stamp: str, now_stamp: str) -> tuple[str, tuple]:
    """Before ``source`` existed. A numeric id is not turned into a Spotify URL."""
    return _check(_playlist_sql(artist_ids, with_source=False), (*artist_ids, start_stamp, now_stamp))


def _alert_statement(artist_ids: list[int], start_stamp: str, now_stamp: str) -> tuple[str, tuple]:
    sql = f"""
        SELECT artists.name AS artist_name, metric_alerts.artist_id, metric_alerts.provider,
               metric_alerts.metric, metric_alerts.window, metric_alerts.delta, metric_alerts.delta_pct,
               metric_alerts.baseline_value, metric_alerts.latest_value, metric_alerts.detected_at
        FROM metric_alerts
        JOIN artists ON artists.id = metric_alerts.artist_id
        WHERE metric_alerts.artist_id IN ({_marks(len(artist_ids))})
          AND metric_alerts.detected_at >= ?
          AND metric_alerts.detected_at <= ?
        ORDER BY metric_alerts.detected_at DESC, metric_alerts.id DESC
        LIMIT {int(ALERT_LIMIT)}
    """
    return _check(sql, (*artist_ids, start_stamp, now_stamp))


def report_statements(artist_ids: list[int], now: datetime) -> list[tuple[str, tuple]]:
    """Every data read after the artist list is known. No UNION chain.

    The first ``len(METRICS)`` statements are the latest snapshot at or before
    now. The next block is the latest snapshot at or before the 6-day cutoff.
    Both use ``(artist_id, source, metric, captured_at)``.
    """
    ids = _ids(artist_ids)
    if not ids:
        return []
    window = _window(now)
    return [
        *_baseline_statements(ids, window["now_stamp"]),
        *_baseline_statements(ids, window["baseline_cutoff"]),
        _release_statement(ids, window["start_day"], window["end_exclusive"]),
        _chart_statement(ids, window["start_day"], window["end_day"]),
        _playlist_statement(ids, window["start_stamp"], window["now_stamp"]),
        _alert_statement(ids, window["start_stamp"], window["now_stamp"]),
    ]


def digest_sql_catalog(artist_ids: list[int] | None = None) -> list[str]:
    """SQL text the digest can send, including the pre-source playlist read."""
    ids = _ids(list(artist_ids or list(range(1, DIGEST_ARTIST_CAP + 1))))
    moment = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
    window = _window(moment)
    statements = [
        watch_statement(1),
        fallback_statement(),
        *report_statements(ids, moment),
        _playlist_legacy_statement(ids, window["start_stamp"], window["now_stamp"]),
    ]
    return [sql for sql, _params in statements]


def _export_href(kind: str, threshold: float) -> str:
    return f"/digest.{kind}?threshold={_threshold_input(threshold)}"


def _empty_overview() -> dict:
    return {
        "artist_count": 0,
        "spike_count": 0,
        "release_count": 0,
        "chart_count": 0,
        "playlist_count": 0,
        "alert_count": 0,
        "spikes": [],
    }


def empty_digest(
    message: str,
    *,
    failed: bool,
    threshold: float = DEFAULT_THRESHOLD_PERCENT,
    now: datetime | None = None,
    source: str = "",
    source_note: str = "",
) -> dict:
    """Page shell when the database is missing or the read did not finish."""
    moment = _as_utc(now)
    chosen = threshold if isinstance(threshold, float) else DEFAULT_THRESHOLD_PERCENT
    return {
        "failed": failed,
        "read_error": message if failed else "",
        "note": PAGE_NOTE,
        "source": source,
        "source_note": "" if failed else source_note,
        "baseline_note": BASELINE_NOTE,
        "release_note": RELEASE_NOTE,
        "chart_note": CHART_NOTE,
        "playlist_note": PLAYLIST_NOTE,
        "alert_note": ALERT_NOTE,
        "threshold_percent": chosen,
        "threshold_label": _threshold_label(chosen),
        "threshold_input": _threshold_input(chosen),
        "generated_at": utc_now(moment),
        "csv_href": "" if failed else _export_href("csv", chosen),
        "json_href": "" if failed else _export_href("json", chosen),
        "artists": [],
        "overview": _empty_overview(),
        "section_notes": {key: "" for key in SECTION_UNAVAILABLE},
    }


def _clean_artists(rows: list | None) -> list[dict]:
    found = []
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
        name = str(row.get("name") or "").strip()[:200]
        found.append({"id": artist_id, "name": name or "Artist"})
        if len(found) >= DIGEST_ARTIST_CAP:
            break
    return found


async def _select_artists(db, user_id: int | None) -> tuple[list[dict] | None, str]:
    """Watchlist, or the tracked fallback. None means the fallback read failed."""
    kind = "tracked-no-watchlist"
    if user_id is not None:
        try:
            watched = await db.query(*watch_statement(user_id))
        except Exception as exc:
            log_read_failure("digest watchlist", exc)
            kind = "tracked-watchlist-unread"
        else:
            artists = _clean_artists(watched)
            if artists:
                return artists, "watchlist"
            kind = "tracked-empty-watchlist"
    try:
        ranked = await db.query(*fallback_statement())
    except Exception as exc:
        log_read_failure("digest artists", exc)
        return None, kind
    return _clean_artists(ranked), kind


def _source_note(kind: str, *, empty: bool) -> str:
    note = _SOURCE_NOTES.get(kind, UNAVAILABLE_WATCHLIST_NOTE)
    if empty and kind != "watchlist":
        return note + NONE_STORED_SUFFIX
    return note


def _classify(found, label: str) -> tuple[list | None, str]:
    """Rows plus "" | "error" | "column".

    A missing table is an empty result: nothing has been stored yet.
    """
    if isinstance(found, Exception):
        if missing_table(found):
            return [], ""
        if "no such column" in str(found).lower():
            return None, "column"
        log_read_failure(label, found)
        return None, "error"
    if not isinstance(found, list):
        log_read_failure(label, RuntimeError("digest read returned no rows"))
        return None, "error"
    return found, ""


def _number_point(row: dict) -> dict | None:
    number = _whole_number(row.get("value"))
    captured = str(row.get("captured_at") or "")
    if number is None or not captured:
        return None
    return {"value": number, "captured_at": captured}


def _point_map(parts: list) -> tuple[dict, set[tuple[str, str]], bool]:
    points: dict[tuple[int, str, str], dict] = {}
    failed: set[tuple[str, str]] = set()
    hard = False
    for part, (_key, source, metric, _label) in zip(parts, METRICS):
        rows, problem = _classify(part, "digest snapshots")
        if problem:
            failed.add((source, metric))
            hard = True
            continue
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            point = _number_point(row)
            if point is None:
                continue
            try:
                artist_id = int(row["artist_id"])
            except (TypeError, ValueError, KeyError):
                continue
            points[(artist_id, str(row.get("source") or source), str(row.get("metric") or metric))] = point
    return points, failed, hard


def _count_text(value: int | None) -> str:
    if value is None:
        return ""
    return f"{value:,}"


def _metric_row(
    source: str,
    metric: str,
    label: str,
    latest: dict | None,
    baseline: dict | None,
    *,
    baseline_failed: bool,
    threshold_tenths: int,
) -> dict | None:
    if latest is None and baseline_failed:
        return None
    if latest is None:
        return None
    if baseline_failed:
        delta = {"status": "unavailable", "text": UNAVAILABLE}
    else:
        delta = growth_delta(latest, baseline)
    status = delta.get("status") or "missing"
    text = delta.get("text") or NOT_ENOUGH
    change = delta.get("change") if status == "ok" else None
    percent = delta.get("percent") if status == "ok" else None
    baseline_value = baseline["value"] if status == "ok" and baseline else None
    baseline_at = baseline["captured_at"] if status == "ok" and baseline else ""
    tenths = None
    if status == "ok" and isinstance(change, int) and isinstance(baseline_value, int):
        tenths = percent_tenths(change, baseline_value)
    spike = tenths is not None and abs(tenths) >= threshold_tenths
    return {
        "source": source,
        "metric": metric,
        "label": label,
        "latest_value": latest["value"],
        "latest_at": latest["captured_at"],
        "latest_text": _count_text(latest["value"]),
        "baseline_value": baseline_value,
        "baseline_at": baseline_at,
        "baseline_text": _count_text(baseline_value),
        "change": change,
        "percent": percent,
        "text": text,
        "status": "missing" if status not in {"ok", "unavailable"} else status,
        "spike": spike,
        "tenths": tenths,
    }


def _present_release(row: dict, window: dict) -> dict | None:
    title = str(row.get("title") or "").strip()
    released = str(row.get("released_at") or "").strip()
    if not title or not released:
        return None
    precision = str(row.get("precision") or "").strip()
    role = timing_role(precision, released)
    if role == "time":
        moment = parse_utc(released)
        start = parse_utc(window["start_stamp"])
        end = parse_utc(window["now_stamp"])
        if moment is None or start is None or end is None or moment < start or moment > end:
            return None
        display = released
        date_only = False
        note = EXACT_TIME_NOTE
        kind = "time"
    elif role == "day":
        day = released[:10]
        if day < window["start_day"] or day > window["end_day"]:
            return None
        display = day
        date_only = True
        note = DATE_ONLY_NOTE
        kind = "day"
    else:
        return None
    return {
        "title": title[:200],
        "mbid": str(row.get("mbid") or "")[:64],
        "release_type": str(row.get("release_type") or "")[:40],
        "released_at": display,
        "precision": kind,
        "date_only": date_only,
        "note": note,
    }


def _present_charts(rows: list) -> list[dict]:
    """One row per title: the earliest stored chart date, best position that day."""
    chosen: dict[tuple, dict] = {}
    order: list[tuple] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("track_or_album_name") or "").strip()
        chart_date = str(row.get("chart_date") or "").strip()
        source = str(row.get("chart_source") or "").strip()
        if not title or not chart_date or source not in CHART_SOURCES:
            continue
        try:
            artist_id = int(row["artist_id"])
        except (TypeError, ValueError, KeyError):
            continue
        territory = str(row.get("territory") or "")[:8]
        key = (artist_id, source, territory, title[:200])
        position = _whole_number(row.get("position"))
        if position is not None and position < 1:
            position = None
        item = {
            "artist_id": artist_id,
            "chart_source": source,
            "label": CHART_LABELS.get(source, source),
            "territory": territory,
            "chart_date": chart_date[:10],
            "position": position,
            "title": title[:200],
            "collected_at": str(row.get("collected_at") or "")[:40],
        }
        current = chosen.get(key)
        if current is None:
            chosen[key] = item
            order.append(key)
            continue
        if item["chart_date"] < current["chart_date"]:
            chosen[key] = item
            continue
        if item["chart_date"] == current["chart_date"] and _better_position(item["position"], current["position"]):
            chosen[key] = item
    found = [chosen[key] for key in order]
    # Newest chart day first. A lower position is the better rank that day.
    found.sort(key=lambda item: (item["chart_date"], -(item["position"] if item["position"] is not None else 10**9)))
    found.reverse()
    return found


def _better_position(new: int | None, old: int | None) -> bool:
    if new is None:
        return False
    if old is None:
        return True
    return new < old


def _present_playlist(row: dict) -> dict | None:
    if not isinstance(row, dict):
        return None
    name = str(row.get("playlist_name") or "").strip()
    playlist_id = str(row.get("playlist_id") or "").strip()
    first_seen = str(row.get("first_seen_at") or "").strip()
    if not name or not playlist_id or not first_seen:
        return None
    try:
        artist_id = int(row["artist_id"])
    except (TypeError, ValueError, KeyError):
        return None
    source = str(row.get("source") or "").strip().lower()
    position = _whole_number(row.get("position"))
    if position is not None and position < 1:
        position = None
    return {
        "artist_id": artist_id,
        "playlist_id": playlist_id[:80],
        "playlist_name": name[:200],
        "source": source[:40],
        "owner_name": str(row.get("owner_name") or "").strip()[:120],
        "position": position,
        "first_seen_at": first_seen[:40],
        "url": _playlist_url(source, playlist_id) or "",
    }


def _bucket(rows: list[dict], artist_ids: list[int], key: str = "artist_id") -> dict[int, list]:
    grouped = {artist_id: [] for artist_id in artist_ids}
    for row in rows:
        try:
            artist_id = int(row[key])
        except (TypeError, ValueError, KeyError):
            continue
        bucket = grouped.get(artist_id)
        if bucket is not None and len(bucket) < PER_ARTIST_LIST:
            bucket.append(row)
    return grouped


def _build_artists(
    artists: list[dict],
    latest: dict,
    baselines: dict,
    baseline_failed: set[tuple[str, str]],
    releases: dict[int, list],
    charts: dict[int, list],
    playlists: dict[int, list],
    alerts: dict[int, list],
    *,
    threshold_tenths: int,
) -> tuple[list[dict], list[dict]]:
    built = []
    spikes = []
    for artist in artists:
        metrics = []
        for _key, source, metric, label in METRICS:
            pair = (artist["id"], source, metric)
            row = _metric_row(
                source,
                metric,
                label,
                latest.get(pair),
                baselines.get(pair),
                baseline_failed=pair[1:] in baseline_failed,
                threshold_tenths=threshold_tenths,
            )
            if row is None:
                continue
            metrics.append(row)
            if row["spike"]:
                spikes.append(
                    {
                        "artist_id": artist["id"],
                        "artist_name": artist["name"],
                        "source": row["source"],
                        "metric": row["metric"],
                        "label": row["label"],
                        "change": row["change"],
                        "percent": row["percent"],
                        "text": row["text"],
                        "latest_value": row["latest_value"],
                        "latest_at": row["latest_at"],
                        "baseline_value": row["baseline_value"],
                        "baseline_at": row["baseline_at"],
                        "tenths": row["tenths"] or 0,
                    }
                )
        built.append(
            {
                "id": artist["id"],
                "name": artist["name"],
                "metrics": metrics,
                "metrics_note": "" if metrics else NO_COUNTS_NOTE,
                "releases": releases.get(artist["id"]) or [],
                "charts": charts.get(artist["id"]) or [],
                "playlists": playlists.get(artist["id"]) or [],
                "alerts": alerts.get(artist["id"]) or [],
            }
        )
    spikes.sort(key=lambda item: (-abs(int(item["tenths"])), item["artist_name"].casefold(), item["label"]))
    for item in spikes:
        item.pop("tenths", None)
    for artist in built:
        for metric in artist["metrics"]:
            metric.pop("tenths", None)
    return built, spikes


def _overview(artists: list[dict], spikes: list[dict]) -> dict:
    return {
        "artist_count": len(artists),
        "spike_count": len(spikes),
        "release_count": sum(len(artist["releases"]) for artist in artists),
        "chart_count": sum(len(artist["charts"]) for artist in artists),
        "playlist_count": sum(len(artist["playlists"]) for artist in artists),
        "alert_count": sum(len(artist["alerts"]) for artist in artists),
        "spikes": spikes,
    }


async def _playlist_rows(db, part, artist_ids: list[int], window: dict) -> tuple[list | None, str]:
    rows, problem = _classify(part, "digest playlists")
    if problem != "column":
        return rows, problem
    try:
        legacy = await db.query(
            *_playlist_legacy_statement(artist_ids, window["start_stamp"], window["now_stamp"])
        )
    except Exception as exc:
        if missing_table(exc):
            return [], ""
        log_read_failure("digest playlists", exc)
        return None, "error"
    return legacy, ""


async def _load_sections(db, artists: list[dict], now: datetime, threshold_tenths: int) -> dict:
    ids = [artist["id"] for artist in artists]
    window = _window(now)
    statements = report_statements(ids, now)
    found = await read_many(db, statements)
    if len(found) != len(statements):
        raise RuntimeError("digest read count")
    metric_count = len(METRICS)
    latest_map, _latest_failed_metrics, latest_hard = _point_map(found[:metric_count])
    baseline_map, baseline_failed, _baseline_hard = _point_map(found[metric_count : metric_count * 2])
    tail = found[metric_count * 2 :]
    notes = {key: "" for key in SECTION_UNAVAILABLE}
    if latest_hard:
        notes["metrics"] = SECTION_UNAVAILABLE["metrics"]

    release_rows, release_problem = _classify(tail[0], "digest releases")
    if release_problem:
        notes["releases"] = SECTION_UNAVAILABLE["releases"]
        release_rows = []
    presented_releases = []
    for row in release_rows or []:
        if not isinstance(row, dict):
            continue
        item = _present_release(row, window)
        if item is None:
            continue
        try:
            item["artist_id"] = int(row["artist_id"])
        except (TypeError, ValueError, KeyError):
            continue
        presented_releases.append(item)

    chart_rows, chart_problem = _classify(tail[1], "digest charts")
    if chart_problem:
        notes["charts"] = SECTION_UNAVAILABLE["charts"]
        chart_rows = []
    presented_charts = _present_charts(chart_rows or [])

    playlist_rows, playlist_problem = await _playlist_rows(db, tail[2], ids, window)
    if playlist_problem:
        notes["playlists"] = SECTION_UNAVAILABLE["playlists"]
        playlist_rows = []
    presented_playlists = []
    for row in playlist_rows or []:
        item = _present_playlist(row)
        if item is not None:
            presented_playlists.append(item)

    alert_rows, alert_problem = _classify(tail[3], "digest alerts")
    if alert_problem:
        notes["alerts"] = SECTION_UNAVAILABLE["alerts"]
        alert_rows = []
    presented_alerts = present_alerts(alert_rows or [])

    releases = _bucket(presented_releases, ids)
    charts = _bucket(presented_charts, ids)
    playlists = _bucket(presented_playlists, ids)
    alerts = _bucket(presented_alerts, ids)
    built, spikes = _build_artists(
        artists,
        latest_map,
        baseline_map,
        baseline_failed,
        releases,
        charts,
        playlists,
        alerts,
        threshold_tenths=threshold_tenths,
    )
    return {"artists": built, "spikes": spikes, "section_notes": notes}


async def _build_digest(db, *, user_id: int | None, threshold: float, now: datetime) -> dict:
    artists, kind = await _select_artists(db, user_id)
    if artists is None:
        return empty_digest(DIGEST_READ_ERROR, failed=True, threshold=threshold, now=now)
    source = "watchlist" if kind == "watchlist" else "tracked"
    note = _source_note(kind, empty=not artists)
    shell = empty_digest("", failed=False, threshold=threshold, now=now, source=source, source_note=note)
    if not artists:
        return shell
    loaded = await _load_sections(db, artists, now, int(round(threshold * 10)))
    shell["artists"] = loaded["artists"]
    shell["section_notes"] = loaded["section_notes"]
    shell["overview"] = _overview(loaded["artists"], loaded["spikes"])
    return shell


async def build_digest(
    db,
    *,
    user_id: int | None = None,
    threshold: str | float | None = None,
    now: datetime | None = None,
) -> dict:
    """One user's digest. Reads only. A failed read is an empty shell, not a raise."""
    chosen = parse_threshold(threshold)
    moment = _as_utc(now)
    try:
        return await _build_digest(db, user_id=_clean_user_id(user_id), threshold=chosen, now=moment)
    except Exception as exc:
        log_read_failure("digest", exc)
        return empty_digest(DIGEST_READ_ERROR, failed=True, threshold=chosen, now=moment)


def _json_threshold(value: float) -> int | float:
    tenths = int(round(float(value) * 10))
    if tenths % 10 == 0:
        return tenths // 10
    return tenths / 10.0


def _metric_document(metric: dict) -> dict:
    return {
        "source": metric["source"],
        "metric": metric["metric"],
        "label": metric["label"],
        "latest_value": metric["latest_value"],
        "latest_at": metric["latest_at"],
        "baseline_value": metric["baseline_value"],
        "baseline_at": metric["baseline_at"],
        "change": metric["change"],
        "percent": metric["percent"],
        "text": metric["text"],
        "status": metric["status"],
        "spike": bool(metric["spike"]),
    }


def _artist_document(artist: dict) -> dict:
    return {
        "id": artist["id"],
        "name": artist["name"],
        "metrics": [_metric_document(metric) for metric in artist["metrics"]],
        "releases": [
            {
                "title": item["title"],
                "mbid": item["mbid"],
                "release_type": item["release_type"],
                "released_at": item["released_at"],
                "precision": item["precision"],
                "date_only": item["date_only"],
                "note": item["note"],
            }
            for item in artist["releases"]
        ],
        "charts": [
            {
                "chart_source": item["chart_source"],
                "label": item["label"],
                "territory": item["territory"],
                "chart_date": item["chart_date"],
                "position": item["position"],
                "title": item["title"],
                "collected_at": item["collected_at"],
            }
            for item in artist["charts"]
        ],
        "playlists": [
            {
                "playlist_id": item["playlist_id"],
                "playlist_name": item["playlist_name"],
                "source": item["source"],
                "owner_name": item["owner_name"],
                "position": item["position"],
                "first_seen_at": item["first_seen_at"],
                "url": item["url"],
            }
            for item in artist["playlists"]
        ],
        "alerts": [
            {
                "provider": item.get("provider"),
                "metric": item.get("metric"),
                "window": item.get("window"),
                "delta": item.get("delta"),
                "delta_text": item.get("delta_text"),
                "percent": item.get("percent"),
                "baseline_value": item.get("baseline_value"),
                "latest_value": item.get("latest_value"),
                "detected_at": item.get("detected_at"),
            }
            for item in artist["alerts"]
        ],
    }


def digest_document(view: dict) -> dict:
    """JSON-ready digest. The same fields feed the CSV."""
    overview = view.get("overview") or _empty_overview()
    return {
        "generated_at": view.get("generated_at") or "",
        "source": view.get("source") or "",
        "source_note": view.get("source_note") or "",
        "read_error": view.get("read_error") or "",
        "threshold_percent": _json_threshold(view.get("threshold_percent") or DEFAULT_THRESHOLD_PERCENT),
        "window_days": WINDOW_DAYS,
        "baseline_min_days": BASELINE_MIN_DAYS,
        "sections": dict(view.get("section_notes") or {}),
        "overview": {
            "artist_count": overview.get("artist_count") or 0,
            "spike_count": overview.get("spike_count") or 0,
            "release_count": overview.get("release_count") or 0,
            "chart_count": overview.get("chart_count") or 0,
            "playlist_count": overview.get("playlist_count") or 0,
            "alert_count": overview.get("alert_count") or 0,
            "spikes": list(overview.get("spikes") or []),
        },
        "artists": [_artist_document(artist) for artist in view.get("artists") or []],
    }


def render_digest_json(view: dict) -> str:
    return json.dumps(digest_document(view), ensure_ascii=False)


def _blank(value) -> str:
    if value is None:
        return ""
    return str(value)


def render_digest_csv(view: dict) -> str:
    """Flat export of the same digest. Missing history stays the words, not a zero."""
    doc = digest_document(view)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    header = [
        "section",
        "artist_set",
        "threshold_percent",
        "artist",
        "artist_id",
        "provider",
        "metric",
        "latest_value",
        "latest_at",
        "baseline_value",
        "baseline_at",
        "change",
        "percent",
        "status",
        "spike",
        "title",
        "detail",
        "occurred_at",
    ]
    writer.writerow(header)
    artist_set = doc["source"]
    threshold = doc["threshold_percent"]
    for artist in doc["artists"]:
        name = artist["name"]
        artist_id = artist["id"]
        for metric in artist["metrics"]:
            writer.writerow(
                [
                    "metric",
                    artist_set,
                    threshold,
                    name,
                    artist_id,
                    metric["source"],
                    metric["metric"],
                    _blank(metric["latest_value"]),
                    metric["latest_at"],
                    _blank(metric["baseline_value"]),
                    metric["baseline_at"],
                    _blank(metric["change"]),
                    _blank(metric["percent"]),
                    metric["status"],
                    "yes" if metric["spike"] else "no",
                    "",
                    metric["text"],
                    metric["latest_at"],
                ]
            )
        for release in artist["releases"]:
            writer.writerow(
                [
                    "release",
                    artist_set,
                    threshold,
                    name,
                    artist_id,
                    "musicbrainz",
                    release["release_type"],
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    release["precision"],
                    "",
                    release["title"],
                    release["note"],
                    release["released_at"],
                ]
            )
        for chart in artist["charts"]:
            detail = "" if chart["position"] is None else f"#{chart['position']}"
            writer.writerow(
                [
                    "chart",
                    artist_set,
                    threshold,
                    name,
                    artist_id,
                    chart["chart_source"],
                    chart["label"],
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    chart["territory"],
                    "",
                    chart["title"],
                    detail,
                    chart["chart_date"],
                ]
            )
        for playlist in artist["playlists"]:
            writer.writerow(
                [
                    "playlist",
                    artist_set,
                    threshold,
                    name,
                    artist_id,
                    playlist["source"],
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    playlist["playlist_name"],
                    playlist["url"],
                    playlist["first_seen_at"],
                ]
            )
        for alert in artist["alerts"]:
            writer.writerow(
                [
                    "alert",
                    artist_set,
                    threshold,
                    name,
                    artist_id,
                    alert.get("provider") or "",
                    alert.get("metric") or "",
                    _blank(alert.get("latest_value")),
                    "",
                    _blank(alert.get("baseline_value")),
                    "",
                    _blank(alert.get("delta")),
                    _blank(alert.get("percent")),
                    "stored",
                    "",
                    alert.get("window") or "",
                    alert.get("delta_text") or "",
                    alert.get("detected_at") or "",
                ]
            )
    for spike in doc["overview"]["spikes"]:
        writer.writerow(
            [
                "spike",
                artist_set,
                threshold,
                spike.get("artist_name") or "",
                spike.get("artist_id") or "",
                spike.get("source") or "",
                spike.get("metric") or "",
                _blank(spike.get("latest_value")),
                spike.get("latest_at") or "",
                _blank(spike.get("baseline_value")),
                spike.get("baseline_at") or "",
                _blank(spike.get("change")),
                _blank(spike.get("percent")),
                "spike",
                "yes",
                spike.get("label") or "",
                spike.get("text") or "",
                spike.get("latest_at") or "",
            ]
        )
    return buffer.getvalue()
