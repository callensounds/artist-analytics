"""CSV of one artist's stored rows. No provider calls."""

from __future__ import annotations

import csv
import io

from history import _whole_number, popularity_score

# Newest rows only. Each section stops at this cap.
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
    "mbid",
    "release_type",
    "external_id",
    "album_name",
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

_RELEASE_SQL = """
SELECT mbid, title, release_type, released_at, precision, first_seen_at, last_seen_at
FROM musicbrainz_releases
WHERE artist_id = ?
ORDER BY released_at DESC, mbid
LIMIT ?
"""

_TRACK_SQL = """
SELECT source, external_id, track_name, popularity, album_name, rank, first_seen_at, last_seen_at
FROM artist_tracks
WHERE artist_id = ?
ORDER BY last_seen_at DESC, CASE WHEN rank IS NULL THEN 1 ELSE 0 END, rank ASC, track_name ASC
LIMIT ?
"""


def _blank(value) -> str:
    if value is None:
        return ""
    return str(value)


def _row(section: str, **fields: object) -> list:
    values = {key: _blank(value) for key, value in fields.items()}
    values["section"] = section
    return [values.get(column, "") for column in COLUMNS]


def render_export_csv(
    metrics: list[dict],
    sightings: list[dict],
    edges: list[dict],
    releases: list[dict] | None = None,
    tracks: list[dict] | None = None,
) -> str:
    """One CSV. A metric is included only when value is an exact integer."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    for row in metrics:
        number = _whole_number(row.get("value"))
        if number is None:
            continue
        writer.writerow(
            _row(
                "metric",
                captured_at=row.get("captured_at"),
                provider=row.get("source"),
                metric=row.get("metric"),
                value=number,
            )
        )
    for row in sightings:
        writer.writerow(
            _row(
                "sighting",
                provider=row.get("source"),
                title=row.get("title"),
                kind=row.get("kind"),
                published_at=row.get("published_at"),
                precision=row.get("precision"),
                first_seen_at=row.get("first_seen_at"),
            )
        )
    for row in edges:
        artist_id = row.get("to_artist_id")
        writer.writerow(
            _row(
                "related",
                provider="spotify",
                first_seen_at=row.get("first_seen_at"),
                last_seen_at=row.get("last_seen_at"),
                rank=row.get("rank"),
                to_name=row.get("to_name"),
                to_spotify_id=row.get("to_spotify_id"),
                to_artist_id="" if artist_id is None else artist_id,
            )
        )
    for row in releases or []:
        writer.writerow(
            _row(
                "release",
                provider="musicbrainz",
                title=row.get("title"),
                published_at=row.get("released_at"),
                precision=row.get("precision"),
                first_seen_at=row.get("first_seen_at"),
                last_seen_at=row.get("last_seen_at"),
                mbid=row.get("mbid"),
                release_type=row.get("release_type"),
            )
        )
    for row in tracks or []:
        score = popularity_score(row.get("popularity"))
        writer.writerow(
            _row(
                "track",
                provider=row.get("source") or "spotify",
                title=row.get("track_name") or row.get("title"),
                value="" if score is None else score,
                first_seen_at=row.get("first_seen_at"),
                last_seen_at=row.get("last_seen_at"),
                rank=row.get("rank"),
                external_id=row.get("external_id"),
                album_name=row.get("album_name"),
            )
        )
    return buffer.getvalue()


async def load_export_rows(db, artist_id: int, limit: int = EXPORT_ROW_CAP) -> dict[str, list[dict]]:
    cap = max(1, min(int(limit), EXPORT_ROW_CAP))
    metrics = await db.query(_METRIC_SQL, (artist_id, cap))
    sightings = await db.query(_SIGHTING_SQL, (artist_id, cap))
    edges = await db.query(_EDGE_SQL, (artist_id, cap))
    releases = await db.query(_RELEASE_SQL, (artist_id, cap))
    try:
        tracks = await db.query(_TRACK_SQL, (artist_id, cap))
    except Exception:
        tracks = []
    return {"metrics": metrics, "sightings": sightings, "edges": edges, "releases": releases, "tracks": tracks}
