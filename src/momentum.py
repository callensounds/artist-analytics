"""24h and 7d changes from stored exact counts. No provider calls."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from db import parse_utc, utc_now
from history import _whole_number, format_span

# Points inside the window. Two readings are required. The span shown is the
# time between those readings, not a filled-in 24h or 7d.
WINDOW_24H = 24 * 60 * 60
WINDOW_7D = 7 * 24 * 60 * 60
WINDOWS = (("24h", WINDOW_24H), ("7d", WINDOW_7D))

# TikTok likes are included when that exact series has two points. Other
# metrics are not momentum, even if a snapshot exists.
MOMENTUM_METRICS = (
    ("spotify", "Monthly listeners", "Spotify monthly listeners"),
    ("spotify", "Followers", "Spotify followers"),
    ("instagram", "Followers", "Instagram followers"),
    ("tiktok", "Followers", "TikTok followers"),
    ("tiktok", "Likes", "TikTok likes"),
)
MOMENTUM_ROW_CAP = 2000
MOMENTUM_EMPTY = "Not enough stored readings in the last 24 hours or 7 days. One reading is not a trend."

# One indexed range per series. A single OR across sources cannot use
# (artist_id, source, metric, captured_at) and would read every older row.
_MOMENTUM_SQL = """
SELECT source, metric, value, captured_at, id FROM metric_snapshots
WHERE artist_id = ? AND source = 'spotify' AND metric = 'Monthly listeners' AND captured_at >= ? AND value IS NOT NULL
UNION ALL
SELECT source, metric, value, captured_at, id FROM metric_snapshots
WHERE artist_id = ? AND source = 'spotify' AND metric = 'Followers' AND captured_at >= ? AND value IS NOT NULL
UNION ALL
SELECT source, metric, value, captured_at, id FROM metric_snapshots
WHERE artist_id = ? AND source = 'instagram' AND metric = 'Followers' AND captured_at >= ? AND value IS NOT NULL
UNION ALL
SELECT source, metric, value, captured_at, id FROM metric_snapshots
WHERE artist_id = ? AND source = 'tiktok' AND metric = 'Followers' AND captured_at >= ? AND value IS NOT NULL
UNION ALL
SELECT source, metric, value, captured_at, id FROM metric_snapshots
WHERE artist_id = ? AND source = 'tiktok' AND metric = 'Likes' AND captured_at >= ? AND value IS NOT NULL
ORDER BY captured_at ASC, id ASC
LIMIT ?
"""


def percent_text(change: int, baseline: int) -> str | None:
    """Percent from exact integers, to a tenth. A zero or negative baseline has no percent."""
    if baseline <= 0:
        return None
    numerator = change * 1000
    if numerator >= 0:
        tenths = (numerator + baseline // 2) // baseline
    else:
        tenths = -(((-numerator) + baseline // 2) // baseline)
    sign = "+" if tenths > 0 else ("-" if tenths < 0 else "")
    whole, frac = divmod(abs(tenths), 10)
    if frac == 0:
        return f"{sign}{whole}%"
    return f"{sign}{whole}.{frac}%"


def _spark_bars(points: list[dict], limit: int = 16) -> list[int]:
    """Bar heights from real points. No interpolated readings."""
    if not points:
        return []
    if len(points) == 1:
        chosen = points
    elif len(points) <= limit:
        chosen = points
    else:
        indexes = {0, len(points) - 1}
        step = (len(points) - 1) / (limit - 1)
        for index in range(limit):
            indexes.add(min(len(points) - 1, int(round(index * step))))
        chosen = [points[index] for index in sorted(indexes)]
    values = [point["value"] for point in chosen]
    low, high = min(values), max(values)
    if low == high:
        return [50] * len(chosen)
    return [int(round((value - low) * 100 / (high - low))) for value in values]


def window_delta(points: list[dict], now: datetime, window_seconds: int) -> dict | None:
    """Latest minus earliest exact point inside the window. One point is omitted."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    start = now - timedelta(seconds=window_seconds)
    chosen: dict[datetime, int] = {}
    for point in points:
        moment = point.get("at")
        value = point.get("value")
        if not isinstance(moment, datetime) or not isinstance(value, int) or isinstance(value, bool):
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if moment < start or moment > now:
            continue
        chosen[moment] = value
    if len(chosen) < 2:
        return None
    ordered = sorted(chosen)
    earliest_at, latest_at = ordered[0], ordered[-1]
    earliest, latest = chosen[earliest_at], chosen[latest_at]
    change = latest - earliest
    span_ms = int((latest_at - earliest_at).total_seconds() * 1000)
    return {
        "change": change,
        "change_text": f"{change:+,}",
        "earliest": earliest,
        "latest": latest,
        "from": earliest_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": latest_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "span": format_span(span_ms),
        "percent": percent_text(change, earliest),
    }


def build_momentum(rows: list[dict], now: datetime) -> list[dict]:
    grouped: dict[tuple[str, str], list[dict]] = {}
    allowed = {(source, metric) for source, metric, _label in MOMENTUM_METRICS}
    for row in rows:
        key = (row.get("source"), row.get("metric"))
        if key not in allowed:
            continue
        number = _whole_number(row.get("value"))
        moment = parse_utc(row.get("captured_at") if isinstance(row.get("captured_at"), str) else None)
        if number is None or moment is None:
            continue
        grouped.setdefault(key, []).append({"at": moment, "value": number})
    series = []
    for source, metric, label in MOMENTUM_METRICS:
        points = grouped.get((source, metric)) or []
        windows = []
        for name, seconds in WINDOWS:
            delta = window_delta(points, now, seconds)
            if delta is None:
                continue
            windows.append({"name": name, **delta})
        if not windows:
            continue
        widest = windows[-1]
        in_wide = []
        start = now - timedelta(seconds=dict(WINDOWS)[widest["name"]])
        seen: dict[datetime, int] = {}
        for point in points:
            moment = point["at"]
            if start <= moment <= now:
                seen[moment] = point["value"]
        for moment in sorted(seen):
            in_wide.append({"at": moment, "value": seen[moment]})
        series.append(
            {
                "label": label,
                "source": source,
                "metric": metric,
                "windows": windows,
                "bars": _spark_bars(in_wide),
            }
        )
    return series


async def load_momentum(db, artist_id: int, now: datetime | None = None) -> list[dict]:
    moment = now or datetime.now(timezone.utc)
    cutoff = utc_now(moment - timedelta(seconds=WINDOW_7D))
    rows = await db.query(
        _MOMENTUM_SQL,
        (artist_id, cutoff, artist_id, cutoff, artist_id, cutoff, artist_id, cutoff, artist_id, cutoff, MOMENTUM_ROW_CAP),
    )
    return build_momentum(rows, moment)
