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
SELECT to_name, to_spotify_id, to_artist_id, rank, first_seen_at,
       CASE
         WHEN related_edges.removed_at IS NULL
          AND freshness.absences_marked_at IS NOT NULL
          AND freshness.absences_marked_at > related_edges.last_seen_at
         THEN freshness.absences_marked_at
         ELSE related_edges.last_seen_at
       END AS last_seen_at
FROM related_edges
LEFT JOIN playlist_crawls AS freshness
  ON freshness.artist_id = related_edges.from_artist_id
 AND freshness.source = 'spotify_related'
WHERE related_edges.from_artist_id = ?
  AND (
    related_edges.removed_at IS NULL
    OR freshness.absences_marked_at IS NULL
  )
ORDER BY related_edges.rank ASC, last_seen_at DESC
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
SELECT t.source AS source, t.external_id AS external_id, t.track_name AS track_name,
       t.popularity AS popularity, t.album_name AS album_name, t.rank AS rank,
       t.first_seen_at AS first_seen_at,
       CASE
         WHEN t.removed_at IS NULL
          AND f.absences_marked_at IS NOT NULL
          AND f.absences_marked_at > t.last_seen_at
         THEN f.absences_marked_at
         ELSE t.last_seen_at
       END AS last_seen_at
FROM artist_tracks AS t
LEFT JOIN playlist_crawls AS f
  ON f.artist_id = t.artist_id AND f.source = t.source || '_tracks'
WHERE t.artist_id = ?
  AND (
    t.removed_at IS NULL
    OR f.absences_marked_at IS NULL
  )
ORDER BY last_seen_at DESC, CASE WHEN t.rank IS NULL THEN 1 ELSE 0 END, t.rank ASC, t.track_name ASC
LIMIT ?
"""

# One stored chart row per day and rank. captured_at in the CSV is the chart date.
_CHART_SQL = """
SELECT chart_source, territory, chart_date, position, track_or_album_name
FROM chart_entries
WHERE artist_id = ?
  AND position BETWEEN 1 AND 100
ORDER BY chart_date DESC, chart_source, position
LIMIT ?
"""

# idx_playlist_artist_seen. Follower count stays blank in the CSV when it was not stored.
_PLAYLIST_SQL = """
SELECT p.playlist_id AS playlist_id, p.playlist_name AS playlist_name,
       p.owner_name AS owner_name, p.follower_count AS follower_count, p.position AS position,
       p.first_seen_at AS first_seen_at,
       CASE
         WHEN p.removed_at IS NULL
          AND c.absences_marked_at IS NOT NULL
          AND c.absences_marked_at > p.last_seen_at
         THEN c.absences_marked_at
         ELSE p.last_seen_at
       END AS last_seen_at,
       p.source AS source, p.editorial AS editorial
FROM playlist_appearances AS p
LEFT JOIN playlist_crawls AS c
  ON c.artist_id = p.artist_id AND c.source = 'deezer'
WHERE p.artist_id = ?
ORDER BY
  CASE WHEN p.removed_at IS NULL AND c.absences_marked_at IS NOT NULL THEN 0 ELSE 1 END,
  p.last_seen_at DESC,
  p.id DESC
LIMIT ?
"""

_PLAYLIST_SQL_LEGACY = """
SELECT playlist_id, playlist_name, owner_name, follower_count, position, first_seen_at, last_seen_at
FROM playlist_appearances
WHERE artist_id = ?
ORDER BY last_seen_at DESC, id DESC
LIMIT ?
"""

# idx_track_stats_lookup starts with artist_id. The series is capped, not sampled.
_TRACK_STAT_SQL = """
SELECT s.source AS source, s.external_id AS external_id, s.metric AS metric,
       s.value AS value, s.captured_at AS captured_at, a.track_name AS track_name
FROM track_stats AS s
LEFT JOIN artist_tracks AS a
  ON a.artist_id = s.artist_id AND a.source = s.source AND a.external_id = s.external_id
WHERE s.artist_id = ?
ORDER BY s.captured_at DESC, s.source, s.external_id, s.metric
LIMIT ?
"""

_TRACK_STAT_METRICS = {"deezer_rank", "spotify_playcount", "spotify_popularity"}


def _blank(value) -> str:
    if value is None:
        return ""
    return str(value)


def _row(section: str, **fields: object) -> list:
    values = {key: _blank(value) for key, value in fields.items()}
    values["section"] = section
    return [values.get(column, "") for column in COLUMNS]


def _chart_position(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        return None
    if number < 1 or number > 100:
        return None
    return number


def _editorial(value: object) -> bool:
    if value is True or value == 1:
        return True
    if isinstance(value, str) and value.strip() == "1":
        return True
    return False


def render_export_csv(
    metrics: list[dict],
    sightings: list[dict],
    edges: list[dict],
    releases: list[dict] | None = None,
    tracks: list[dict] | None = None,
    charts: list[dict] | None = None,
    playlists: list[dict] | None = None,
    track_stats: list[dict] | None = None,
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
    for row in charts or []:
        position = _chart_position(row.get("position"))
        title = str(row.get("track_or_album_name") or row.get("title") or "").strip()
        chart_date = str(row.get("chart_date") or "").strip()
        source = str(row.get("chart_source") or "").strip()
        if position is None or not title or not chart_date or not source:
            continue
        writer.writerow(
            _row(
                "chart_entries",
                captured_at=chart_date,
                provider=source,
                metric="position",
                value=position,
                title=title,
                kind=row.get("territory"),
                rank=position,
            )
        )
    for row in playlists or []:
        playlist_id = str(row.get("playlist_id") or "").strip()
        title = str(row.get("playlist_name") or row.get("title") or "").strip()
        if not playlist_id or not title:
            continue
        followers = _whole_number(row.get("follower_count"))
        position = _whole_number(row.get("position"))
        if position is not None and position < 1:
            position = None
        writer.writerow(
            _row(
                "playlist",
                provider=row.get("source") or "",
                title=title,
                value="" if followers is None else followers,
                first_seen_at=row.get("first_seen_at"),
                last_seen_at=row.get("last_seen_at"),
                rank="" if position is None else position,
                to_name=row.get("owner_name"),
                external_id=playlist_id,
                kind="editorial" if _editorial(row.get("editorial")) else "",
            )
        )
    for row in track_stats or []:
        metric = str(row.get("metric") or "").strip()
        if metric not in _TRACK_STAT_METRICS:
            continue
        number = _whole_number(row.get("value"))
        external = str(row.get("external_id") or "").strip()
        captured = str(row.get("captured_at") or "").strip()
        if number is None or not external or not captured:
            continue
        writer.writerow(
            _row(
                "track_stat",
                captured_at=captured,
                provider=row.get("source"),
                metric=metric,
                value=number,
                title=row.get("track_name") or row.get("title"),
                external_id=external,
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
    try:
        charts = await db.query(_CHART_SQL, (artist_id, cap))
    except Exception:
        charts = []
    playlists = await _playlist_export_rows(db, artist_id, cap)
    try:
        track_stats = await db.query(_TRACK_STAT_SQL, (artist_id, cap))
    except Exception as exc:
        text = str(exc).lower()
        if "no such column" not in text and "no such table" not in text:
            raise
        track_stats = []
    return {
        "metrics": metrics,
        "sightings": sightings,
        "edges": edges,
        "releases": releases,
        "tracks": tracks,
        "charts": charts,
        "playlists": playlists,
        "track_stats": track_stats,
    }


async def _playlist_export_rows(db, artist_id: int, cap: int) -> list[dict]:
    try:
        return await db.query(_PLAYLIST_SQL, (artist_id, cap))
    except Exception as exc:
        text = str(exc).lower()
        if "no such column" not in text and "no such table" not in text:
            raise
    try:
        return await db.query(_PLAYLIST_SQL_LEGACY, (artist_id, cap))
    except Exception:
        return []
