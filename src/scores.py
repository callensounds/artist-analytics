"""Audience score from the latest exact stored counts. No estimates and no network."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, localcontext

from db import missing_table

# Order is the breakdown order. A metric that is not stored is left out.
SCORE_METRICS = (
    ("spotify", "Monthly listeners", "Spotify monthly listeners"),
    ("spotify", "Followers", "Spotify followers"),
    ("instagram", "Followers", "Instagram followers"),
    ("tiktok", "Followers", "TikTok followers"),
    ("tiktok", "Likes", "TikTok likes"),
    ("youtube", "Subscribers", "YouTube subscribers"),
)
SCORE_FORMULA = (
    "Score = sum of log10(count), each term rounded to 2 decimal places, "
    "using only the latest exact stored count for each source. "
    "A source is included only when that exact integer is stored: "
    "Spotify monthly listeners, Spotify followers, Instagram followers, "
    "TikTok followers, TikTok likes, and YouTube subscribers. "
    "A missing source is left out. Zero and rounded text are left out. "
    "Nothing is estimated."
)
SCORE_EMPTY = (
    "No exact audience count is stored yet, so there is no score. "
    "Missing sources are left out and nothing is estimated."
)
SCORE_RANK_LIMIT = 50
SCORE_RANK_EMPTY = "No tracked artist has an exact audience count stored yet, so there is nothing to rank."

_FILTER = """
    metric_latest.value IS NOT NULL
    AND (
          (metric_latest.source = 'spotify' AND metric_latest.metric IN ('Monthly listeners', 'Followers'))
       OR (metric_latest.source = 'instagram' AND metric_latest.metric = 'Followers')
       OR (metric_latest.source = 'tiktok' AND metric_latest.metric IN ('Followers', 'Likes'))
       OR (metric_latest.source = 'youtube' AND metric_latest.metric = 'Subscribers')
    )
"""

# Used only when metric_latest has not been created yet.
_LEGACY_FILTER = """
    value IS NOT NULL
    AND (
          (source = 'spotify' AND metric IN ('Monthly listeners', 'Followers'))
       OR (source = 'instagram' AND metric = 'Followers')
       OR (source = 'tiktok' AND metric IN ('Followers', 'Likes'))
       OR (source = 'youtube' AND metric = 'Subscribers')
    )
"""


def log10_hundredths(value: int) -> int:
    """log10(value) in hundredths, half-up. 1000 is 300, which the UI shows as 3.00."""
    if value < 1:
        raise ValueError("log10 is only defined here for a positive count")
    with localcontext() as ctx:
        ctx.prec = 28
        points = (Decimal(value).ln() / Decimal(10).ln()) * 100
        return int(points.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_points(hundredths: int) -> str:
    sign = "-" if hundredths < 0 else ""
    whole, frac = divmod(abs(int(hundredths)), 100)
    return f"{sign}{whole}.{frac:02d}"


def score_from_counts(rows: list[dict]) -> dict:
    """Sum log10 of the latest exact counts. Rows may repeat; the caller passes one row per metric."""
    from history import _whole_number

    latest: dict[tuple[str, str], int] = {}
    for row in rows or []:
        key = (row.get("source"), row.get("metric"))
        number = _whole_number(row.get("value"))
        if key not in {(item[0], item[1]) for item in SCORE_METRICS} or number is None or number < 1:
            continue
        latest[key] = number
    components = []
    total = 0
    for source, metric, label in SCORE_METRICS:
        number = latest.get((source, metric))
        if number is None:
            continue
        points = log10_hundredths(number)
        total += points
        components.append(
            {
                "source": source,
                "metric": metric,
                "label": label,
                "count": number,
                "count_text": f"{number:,}",
                "points_text": format_points(points),
            }
        )
    if not components:
        return {
            "score_hundredths": None,
            "score_text": None,
            "components": [],
            "formula": SCORE_FORMULA,
        }
    return {
        "score_hundredths": total,
        "score_text": format_points(total),
        "components": components,
        "formula": SCORE_FORMULA,
    }


def _latest_sql(where_artist: str) -> str:
    return f"""
        SELECT metric_latest.artist_id AS artist_id,
               artists.name AS name,
               metric_latest.source AS source,
               metric_latest.metric AS metric,
               metric_latest.value AS value
        FROM metric_latest
        JOIN artists ON artists.id = metric_latest.artist_id AND artists.tracked = 1
        WHERE {where_artist} {_FILTER}
    """


def _legacy_window_sql(where_artist: str) -> str:
    return f"""
        SELECT artist_id, name, source, metric, value
        FROM (
            SELECT metric_snapshots.artist_id AS artist_id,
                   artists.name AS name,
                   metric_snapshots.source AS source,
                   metric_snapshots.metric AS metric,
                   metric_snapshots.value AS value,
                   ROW_NUMBER() OVER (
                       PARTITION BY metric_snapshots.artist_id, metric_snapshots.source, metric_snapshots.metric
                       ORDER BY metric_snapshots.captured_at DESC, metric_snapshots.id DESC
                   ) AS rn
            FROM metric_snapshots
            JOIN artists ON artists.id = metric_snapshots.artist_id AND artists.tracked = 1
            WHERE {where_artist} {_LEGACY_FILTER}
        ) AS ranked
        WHERE rn = 1
    """


async def _score_rows(db, where_new: str, where_old: str, params: tuple) -> list[dict]:
    legacy = False
    try:
        rows = await db.query(_latest_sql(where_new), params)
    except Exception as exc:
        if not missing_table(exc):
            raise
        legacy = True
    if legacy:
        rows = await db.query(_legacy_window_sql(where_old), params)
    return rows


async def load_scores(db, artist_ids: list[int]) -> dict[int, dict]:
    """Scores for a small set of artists. One indexed read of the latest row per metric."""
    if not artist_ids:
        return {}
    marks = ",".join("?" for _ in artist_ids)
    rows = await _score_rows(
        db,
        f"metric_latest.artist_id IN ({marks}) AND",
        f"metric_snapshots.artist_id IN ({marks}) AND",
        tuple(artist_ids),
    )
    return _group_scores(rows)


async def load_artist_score(db, artist_id: int) -> dict:
    found = await load_scores(db, [artist_id])
    score = found.get(artist_id)
    if score is not None:
        return score
    return score_from_counts([])


async def rank_scores(db, limit: int = SCORE_RANK_LIMIT) -> list[dict]:
    """Tracked artists with a score, highest first. Capped so the request stays one read plus a sort."""
    cap = max(1, min(int(limit), SCORE_RANK_LIMIT))
    rows = await _score_rows(db, "", "", ())
    names: dict[int, str] = {}
    buckets: dict[int, list[dict]] = {}
    for row in rows:
        artist_id = int(row["artist_id"])
        names[artist_id] = str(row.get("name") or "")
        buckets.setdefault(artist_id, []).append(row)
    ranked = []
    for artist_id, metrics in buckets.items():
        score = score_from_counts(metrics)
        if score["score_hundredths"] is None:
            continue
        ranked.append(
            {
                "artist_id": artist_id,
                "name": names.get(artist_id) or "",
                **score,
            }
        )
    ranked.sort(key=lambda item: (-int(item["score_hundredths"]), item["name"].casefold(), item["artist_id"]))
    chosen = ranked[:cap]
    for index, item in enumerate(chosen, start=1):
        item["rank"] = index
    return chosen


def _group_scores(rows: list[dict]) -> dict[int, dict]:
    buckets: dict[int, list[dict]] = {}
    for row in rows:
        buckets.setdefault(int(row["artist_id"]), []).append(row)
    return {artist_id: score_from_counts(metrics) for artist_id, metrics in buckets.items()}
