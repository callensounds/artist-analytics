"""CSV of one artist's stored rows. No provider calls."""

from __future__ import annotations

import csv
import io

from history import _whole_number

# Newest rows only. Three indexed reads, each stopped at this cap.
EXPORT_ROW_CAP = 5000

COLUMNS = (
    "section",
    "captured_at",
    "provider",
    "metric",
    "value",
    "title",
    "kind",
    "published_at",
    "precision",
    "first_seen_at",
    "last_seen_at",
    "rank",
    "to_name",
    "to_spotify_id",
    "to_artist_id",
)

_METRIC_SQL = """
SELECT captured_at, source, metric, value
FROM metric_snapshots
WHERE artist_id = ? AND value IS NOT NULL
ORDER BY captured_at DESC, id DESC
LIMIT ?
"""

_SIGHTING_SQL = """
SELECT source, title, kind, published_at, precision, first_seen_at
FROM content_sightings
WHERE artist_id = ?
ORDER BY COALESCE(NULLIF(published_at, ''), first_seen_at) DESC, id DESC
LIMIT ?
"""

_EDGE_SQL = """
SELECT to_name, to_spotify_id, to_artist_id, rank, first_seen_at, last_seen_at
FROM related_edges
WHERE from_artist_id = ?
ORDER BY rank ASC, last_seen_at DESC
LIMIT ?
"""


def _blank(value) -> str:
    if value is None:
        return ""
    return str(value)


def render_export_csv(metrics: list[dict], sightings: list[dict], edges: list[dict]) -> str:
    """One CSV. A metric is included only when value is an exact integer."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    for row in metrics:
        number = _whole_number(row.get("value"))
        if number is None:
            continue
        writer.writerow(
            [
                "metric",
                _blank(row.get("captured_at")),
                _blank(row.get("source")),
                _blank(row.get("metric")),
                number,
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
            ]
        )
    for row in sightings:
        writer.writerow(
            [
                "sighting",
                "",
                _blank(row.get("source")),
                "",
                "",
                _blank(row.get("title")),
                _blank(row.get("kind")),
                _blank(row.get("published_at")),
                _blank(row.get("precision")),
                _blank(row.get("first_seen_at")),
                "",
                "",
                "",
                "",
                "",
            ]
        )
    for row in edges:
        artist_id = row.get("to_artist_id")
        writer.writerow(
            [
                "related",
                "",
                "spotify",
                "",
                "",
                "",
                "",
                "",
                "",
                _blank(row.get("first_seen_at")),
                _blank(row.get("last_seen_at")),
                _blank(row.get("rank")),
                _blank(row.get("to_name")),
                _blank(row.get("to_spotify_id")),
                "" if artist_id is None else _blank(artist_id),
            ]
        )
    return buffer.getvalue()


async def load_export_rows(db, artist_id: int, limit: int = EXPORT_ROW_CAP) -> dict[str, list[dict]]:
    cap = max(1, min(int(limit), EXPORT_ROW_CAP))
    metrics = await db.query(_METRIC_SQL, (artist_id, cap))
    sightings = await db.query(_SIGHTING_SQL, (artist_id, cap))
    edges = await db.query(_EDGE_SQL, (artist_id, cap))
    return {"metrics": metrics, "sightings": sightings, "edges": edges}
