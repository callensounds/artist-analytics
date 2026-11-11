"""Stored iTunes chart rows. The request path does not fetch a feed.

Artist history is one grouped read of ``chart_entries`` (peak, days, latest rank)
plus a second limited read of stored dates for the line. Missing dates are not filled in.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

CHART_SOURCES = ("itunes_topsongs", "itunes_topalbums")
CHART_LABELS = {
    "itunes_topsongs": "iTunes Top Songs",
    "itunes_topalbums": "iTunes Top Albums",
}
CHART_COLORS = {
    "itunes_topsongs": "#34d399",
    "itunes_topalbums": "#fb7185",
}
CHARTS_EMPTY = (
    "No Apple Music or iTunes chart position is stored for this artist. "
    "The crawler reads the public US iTunes top songs and top albums feeds. "
    "Spotify charts are not listed because there is no public chart JSON without a login."
)
CHARTS_PAGE_EMPTY = (
    "No iTunes chart has been stored yet. "
    "The crawler reads the public US top songs and top albums feeds. "
    "This page does not fetch them. "
    "Spotify charts are not listed because there is no public chart JSON without a login."
)

# One artist page stays on the indexed artist_id range. Titles and points are capped
# so a long history cannot turn into an unbounded D1 read.
ARTIST_CHART_TITLE_LIMIT = 40
ARTIST_CHART_POINT_LIMIT = 240
CHART_DATE_LIMIT = 90

_CHART_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_PEAK_TOKEN = re.compile(r"(\d{3})\|(\d{4}-\d{2}-\d{2})")
_LAST_TOKEN = re.compile(r"(\d{4}-\d{2}-\d{2})\|(\d{3})")
_DATE_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"

# peak_token is the best (lowest) position, zero-padded, then the first chart_date at that rank.
# last_token is the latest chart_date, then 1000 minus the best position on that date.
# ISO dates sort lexicographically, so MIN/MAX of these strings are the stored facts.
_ARTIST_CHART_SUMMARY_SQL = f"""
SELECT chart_source,
       territory,
       track_or_album_name,
       MIN(position) AS peak_position,
       COUNT(DISTINCT chart_date) AS days_on_chart,
       MAX(chart_date) AS last_date,
       MIN(printf('%03d|%s', position, chart_date)) AS peak_token,
       MAX(printf('%s|%03d', chart_date, 1000 - position)) AS last_token
FROM chart_entries
WHERE artist_id = ?
  AND position BETWEEN 1 AND 100
  AND chart_source IN ('itunes_topsongs', 'itunes_topalbums')
  AND chart_date GLOB '{_DATE_GLOB}'
GROUP BY chart_source, territory, track_or_album_name
ORDER BY last_date DESC, peak_position, chart_source, track_or_album_name
LIMIT {ARTIST_CHART_TITLE_LIMIT}
"""

_ARTIST_CHART_POINTS_SQL = f"""
SELECT chart_source, territory, chart_date, MIN(position) AS position, track_or_album_name
FROM chart_entries
WHERE artist_id = ?
  AND position BETWEEN 1 AND 100
  AND chart_source IN ('itunes_topsongs', 'itunes_topalbums')
  AND chart_date GLOB '{_DATE_GLOB}'
GROUP BY chart_source, territory, track_or_album_name, chart_date
ORDER BY chart_date DESC
LIMIT {ARTIST_CHART_POINT_LIMIT}
"""

_SOURCE_LATEST_SQL = """
SELECT chart_date
FROM chart_entries
WHERE chart_source = ? AND territory = 'us'
ORDER BY chart_date DESC
LIMIT 1
"""

_CHART_DATES_SQL = f"""
SELECT chart_date
FROM chart_entries
WHERE chart_source = ? AND territory = 'us'
GROUP BY chart_date
ORDER BY chart_date DESC
LIMIT {CHART_DATE_LIMIT}
"""

_LATEST_CHART_SQL = """
SELECT chart_source, territory, chart_date, position, track_or_album_name, artist_name, artist_id
FROM chart_entries
WHERE chart_source = ? AND territory = 'us'
  AND chart_date = (
      SELECT MAX(chart_date) FROM chart_entries
      WHERE chart_source = ? AND territory = 'us'
  )
ORDER BY position
LIMIT 100
"""

_CHART_ON_DATE_SQL = """
SELECT chart_source, territory, chart_date, position, track_or_album_name, artist_name, artist_id
FROM chart_entries
WHERE chart_source = ? AND territory = 'us' AND chart_date = ?
ORDER BY position
LIMIT 100
"""


def valid_chart_day(value: object) -> str | None:
    """A real calendar day in the crawler's YYYY-MM-DD form. Anything else is dropped."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not _CHART_DAY.fullmatch(text):
        return None
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    return text


def _whole(value: object) -> int | None:
    """An exact integer. Fractional ranks and booleans are not chart positions."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            return None
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        return int(value.strip())
    return None


def _position(value: object) -> int | None:
    number = _whole(value)
    if number is None or number < 1 or number > 100:
        return None
    return number


def _day_ms(day: str) -> int:
    moment = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def _parse_peak_token(token: object, peak_position: int) -> str | None:
    if not isinstance(token, str):
        return None
    match = _PEAK_TOKEN.fullmatch(token)
    if not match:
        return None
    position = int(match.group(1))
    day = valid_chart_day(match.group(2))
    if position != peak_position or day is None:
        return None
    return day


def _parse_last_token(token: object, last_date: str) -> int | None:
    if not isinstance(token, str):
        return None
    match = _LAST_TOKEN.fullmatch(token)
    if not match:
        return None
    day = valid_chart_day(match.group(1))
    if day != last_date:
        return None
    position = 1000 - int(match.group(2))
    return _position(position)


def _title_key(source: str, territory: str, title: str) -> tuple[str, str, str]:
    return (source, territory, title)


def _cap_series(points: list[dict], limit: int) -> list[dict]:
    """Keep both ends of a stored line and sample the readings between them."""
    count = len(points)
    if count <= limit or limit < 2:
        return points
    last = count - 1
    chosen = {0, last}
    step = last / (limit - 1)
    for index in range(limit):
        chosen.add(min(last, int(round(index * step))))
    return [points[index] for index in sorted(chosen)]


def _series_for(
    points: list[dict] | None,
) -> dict[tuple[str, str, str], list[dict]]:
    """One point per stored chart date. The best rank wins when a title is stored twice that day."""
    grouped: dict[tuple[str, str, str], dict[str, int]] = {}
    for row in points or []:
        source = str(row.get("chart_source") or "")
        if source not in CHART_LABELS:
            continue
        day = valid_chart_day(row.get("chart_date"))
        position = _position(row.get("position"))
        raw_title = row.get("track_or_album_name")
        territory = str(row.get("territory") or "").strip().lower()
        if day is None or position is None or not isinstance(raw_title, str) or not raw_title.strip() or not territory:
            continue
        bucket = grouped.setdefault(_title_key(source, territory, raw_title), {})
        previous = bucket.get(day)
        if previous is None or position < previous:
            bucket[day] = position
    series: dict[tuple[str, str, str], list[dict]] = {}
    for key, ranks in grouped.items():
        points = [{"x": _day_ms(day), "y": ranks[day]} for day in sorted(ranks)]
        series[key] = _cap_series(points, ARTIST_CHART_POINT_LIMIT)
    return series


def present_chart_rows(rows: list[dict] | None) -> list[dict]:
    presented = []
    for row in rows or []:
        source = str(row.get("chart_source") or "")
        if source not in CHART_LABELS:
            continue
        position = _position(row.get("position"))
        if position is None:
            continue
        title = str(row.get("track_or_album_name") or "").strip()
        artist_name = str(row.get("artist_name") or "").strip()
        chart_date = valid_chart_day(row.get("chart_date"))
        if not title or not artist_name or chart_date is None:
            continue
        raw_id = row.get("artist_id")
        linked = None
        if raw_id not in (None, ""):
            try:
                linked = int(raw_id)
            except (TypeError, ValueError):
                linked = None
        presented.append(
            {
                "position": position,
                "title": title[:200],
                "artist_name": artist_name[:200],
                "artist_id": linked if linked and linked > 0 else None,
                "chart_source": source,
                "chart_label": CHART_LABELS[source],
                "territory": "US",
                "chart_date": chart_date,
            }
        )
    return presented


def present_chart_history(
    summaries: list[dict] | None,
    points: list[dict] | None = None,
    latest_dates: dict[tuple[str, str], str] | None = None,
) -> list[dict]:
    """One row per stored title. Ranks and dates come only from the summary and point rows.

    ``current_position`` is set only when the title's last chart_date is the latest
    stored chart date for that source and territory. A title that has fallen off
    stays in the list with its peak and days, and current_position is None.
    """
    series_by_title = _series_for(points)
    latest = latest_dates or {}
    presented = []
    for row in summaries or []:
        source = str(row.get("chart_source") or "")
        if source not in CHART_LABELS:
            continue
        raw_title = row.get("track_or_album_name")
        if not isinstance(raw_title, str):
            continue
        title = raw_title.strip()
        territory = str(row.get("territory") or "").strip().lower()
        if not title or not territory:
            continue
        peak_position = _position(row.get("peak_position"))
        last_date = valid_chart_day(row.get("last_date"))
        days = _whole(row.get("days_on_chart"))
        if peak_position is None or last_date is None or days is None or days < 1:
            continue
        peak_date = _parse_peak_token(row.get("peak_token"), peak_position)
        last_position = _parse_last_token(row.get("last_token"), last_date)
        if peak_date is None or last_position is None:
            continue
        board_date = latest.get((source, territory))
        current = last_position if board_date is not None and last_date == board_date else None
        key = _title_key(source, territory, raw_title)
        series = series_by_title.get(key, [])
        presented.append(
            {
                "title": title[:200],
                "chart_source": source,
                "chart_label": CHART_LABELS[source],
                "territory": "US" if territory == "us" else territory.upper(),
                "chart_date": last_date,
                "current_position": current,
                "peak_position": peak_position,
                "peak_date": peak_date,
                "days_on_chart": days,
                "series": series,
                "series_capped": len(series) < days,
                "color": CHART_COLORS[source],
            }
        )
    presented.sort(
        key=lambda item: (
            CHART_SOURCES.index(item["chart_source"]) if item["chart_source"] in CHART_SOURCES else len(CHART_SOURCES),
            0 if item["current_position"] is not None else 1,
            item["current_position"] if item["current_position"] is not None else 999,
            item["peak_position"],
            item["title"].casefold(),
        )
    )
    return presented


async def _latest_us_dates(db) -> dict[tuple[str, str], str]:
    found: dict[tuple[str, str], str] = {}
    for source in CHART_SOURCES:
        row = await db.query_one(_SOURCE_LATEST_SQL, (source,))
        if not row:
            continue
        day = valid_chart_day(row.get("chart_date"))
        if day:
            found[(source, "us")] = day
    return found


async def load_stored_chart_dates(db) -> list[str]:
    """Distinct stored US chart days, newest first. Songs and albums share one list."""
    found: set[str] = set()
    for source in CHART_SOURCES:
        rows = await db.query(_CHART_DATES_SQL, (source,))
        for row in rows or []:
            day = valid_chart_day(row.get("chart_date"))
            if day:
                found.add(day)
    return sorted(found, reverse=True)[:CHART_DATE_LIMIT]


def artist_chart_statements(artist_id: int) -> list[tuple[str, tuple]]:
    """Summary, points, and the latest stored day for each US chart. One batch."""
    return [
        (_ARTIST_CHART_SUMMARY_SQL, (artist_id,)),
        (_ARTIST_CHART_POINTS_SQL, (artist_id,)),
        (_SOURCE_LATEST_SQL, (CHART_SOURCES[0],)),
        (_SOURCE_LATEST_SQL, (CHART_SOURCES[1],)),
    ]


def charts_from_reads(summaries, points, song_latest, album_latest) -> list[dict]:
    """The same card ``load_artist_charts`` builds, from rows already read."""
    latest: dict[tuple[str, str], str] = {}
    for source, rows in ((CHART_SOURCES[0], song_latest), (CHART_SOURCES[1], album_latest)):
        if not rows:
            continue
        day = valid_chart_day((rows[0] or {}).get("chart_date"))
        if day:
            latest[(source, "us")] = day
    return present_chart_history(summaries or [], points or [], latest)


async def load_artist_charts(db, artist_id: int) -> list[dict]:
    summaries = await db.query(_ARTIST_CHART_SUMMARY_SQL, (artist_id,))
    points = await db.query(_ARTIST_CHART_POINTS_SQL, (artist_id,))
    latest = await _latest_us_dates(db)
    return present_chart_history(summaries, points, latest)


async def load_chart_board(db, chart_date: str | None = None) -> dict:
    """US songs and albums for one stored date, or the latest stored date. No network."""
    dates = await load_stored_chart_dates(db)
    selected = valid_chart_day(chart_date) or ""
    if selected not in dates:
        selected = ""
    if selected:
        songs = present_chart_rows(await db.query(_CHART_ON_DATE_SQL, ("itunes_topsongs", selected)))
        albums = present_chart_rows(await db.query(_CHART_ON_DATE_SQL, ("itunes_topalbums", selected)))
        songs_date = selected
        albums_date = selected
    else:
        songs = present_chart_rows(await db.query(_LATEST_CHART_SQL, ("itunes_topsongs", "itunes_topsongs")))
        albums = present_chart_rows(await db.query(_LATEST_CHART_SQL, ("itunes_topalbums", "itunes_topalbums")))
        songs_date = songs[0]["chart_date"] if songs else ""
        albums_date = albums[0]["chart_date"] if albums else ""
    return {
        "songs": songs,
        "albums": albums,
        "songs_date": songs_date,
        "albums_date": albums_date,
        "dates": dates,
        "selected_date": selected,
    }
