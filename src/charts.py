"""Stored iTunes chart rows. The request path does not fetch a feed."""

from __future__ import annotations

CHART_SOURCES = ("itunes_topsongs", "itunes_topalbums")
CHART_LABELS = {
    "itunes_topsongs": "iTunes Top Songs",
    "itunes_topalbums": "iTunes Top Albums",
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

_ARTIST_CHARTS_SQL = """
SELECT chart_source, territory, chart_date, position, track_or_album_name, artist_name, artist_id
FROM chart_entries AS c
WHERE c.artist_id = ?
  AND c.chart_date = (
      SELECT MAX(chart_date)
      FROM chart_entries
      WHERE artist_id = c.artist_id AND chart_source = c.chart_source AND territory = c.territory
  )
ORDER BY c.chart_source, c.position
LIMIT 40
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


def present_chart_rows(rows: list[dict] | None) -> list[dict]:
    presented = []
    for row in rows or []:
        source = str(row.get("chart_source") or "")
        if source not in CHART_LABELS:
            continue
        try:
            position = int(row.get("position"))
        except (TypeError, ValueError):
            continue
        if position < 1 or position > 100:
            continue
        title = str(row.get("track_or_album_name") or "").strip()
        artist_name = str(row.get("artist_name") or "").strip()
        if not title or not artist_name:
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
                "chart_date": str(row.get("chart_date") or ""),
            }
        )
    return presented


async def load_artist_charts(db, artist_id: int) -> list[dict]:
    rows = await db.query(_ARTIST_CHARTS_SQL, (artist_id,))
    return present_chart_rows(rows)


async def load_chart_board(db) -> dict:
    """Latest stored US songs and albums. No network."""
    songs = present_chart_rows(await db.query(_LATEST_CHART_SQL, ("itunes_topsongs", "itunes_topsongs")))
    albums = present_chart_rows(await db.query(_LATEST_CHART_SQL, ("itunes_topalbums", "itunes_topalbums")))
    return {
        "songs": songs,
        "albums": albums,
        "songs_date": songs[0]["chart_date"] if songs else "",
        "albums_date": albums[0]["chart_date"] if albums else "",
    }
