"""Persist audience counts, first-seen content, and Spotify related-artist edges."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from analytics import SOURCE_COLORS, normalize_name
from db import parse_utc, utc_now

# Only exact audience counts. Sample sizes and rounded "1.2M" text are not a growth series.
AUDIENCE = {
    ("spotify", "Monthly listeners"): "Spotify monthly listeners",
    ("spotify", "Followers"): "Spotify followers",
    ("instagram", "Followers"): "Instagram followers",
    ("tiktok", "Followers"): "TikTok followers",
    ("youtube", "Subscribers"): "YouTube subscribers",
}
SNAPSHOT_REPEAT_SECONDS = 3600
RELATED_RANK_LIMIT = 5
# Sightings are a crawl write, not a dashboard parse. Cap rows so one artist cannot
# turn into a prepare-per-item loop.
SIGHTINGS_PER_SOURCE = 8
SIGHTING_CAP = 24
GROWTH_ROW_LIMIT = 120
RELATED_READ_LIMIT = 20
SIGHTING_READ_LIMIT = 20
ONE_READING = "one reading, no trend yet"
# D1 rejects a statement with more than 100 bound parameters.
_PARAM_BUDGET = 90


def _whole_number(value) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if value < 0 or not value.is_integer():
            return None
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[\d,]+", text):
            return int(text.replace(",", ""))
    return None


def audience_exact(metric: dict) -> int | None:
    """An audience point is an integer. Rounded text such as 1.2M is not one."""
    exact = _whole_number(metric.get("exact"))
    if exact is not None:
        return exact
    return _whole_number(metric.get("value"))


def format_span(span_ms: int) -> str:
    """Compact duration for a delta row. Hours stay whole when the span is exact."""
    seconds = max(0, int(span_ms // 1000))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 48 * 3600:
        if seconds % 3600 == 0:
            return f"{seconds // 3600}h"
        return f"{seconds / 3600:.1f}h"
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    return f"{seconds / 86400:.1f}d"


def format_delta(change: int, span_ms: int) -> str:
    return f"{change:+,} over {format_span(span_ms)}"


def _epoch_ms(captured: str | None) -> int | None:
    moment = parse_utc(captured)
    if moment is None:
        return None
    return int(moment.timestamp() * 1000)


def build_growth(rows: list[dict]) -> dict:
    """One dataset per source and metric, with only the stored exact readings.

    Points are ``{x, y}`` in epoch milliseconds. Series are not aligned onto one
    another, so a Spotify reading at :32 and an Instagram reading at :33 do not
    become nulls. Nothing is interpolated.
    """
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        key = (row.get("source"), row.get("metric"))
        number = _whole_number(row.get("value"))
        captured = row.get("captured_at")
        x = _epoch_ms(captured if isinstance(captured, str) else None)
        if key not in AUDIENCE or number is None or x is None:
            continue
        grouped.setdefault(key, []).append({"x": x, "y": number})
    datasets = []
    for key in AUDIENCE:
        items = grouped.get(key)
        if not items:
            continue
        items.sort(key=lambda point: point["x"])
        points: list[dict] = []
        for point in items:
            if points and points[-1]["x"] == point["x"]:
                points[-1] = point
            else:
                points.append(point)
        earliest = points[0]
        latest = points[-1]
        if len(points) == 1:
            delta = {
                "latest": latest["y"],
                "earliest": earliest["y"],
                "change": None,
                "span_seconds": None,
                "latest_text": f"{latest['y']:,}",
                "text": ONE_READING,
            }
        else:
            change = latest["y"] - earliest["y"]
            span_ms = latest["x"] - earliest["x"]
            delta = {
                "latest": latest["y"],
                "earliest": earliest["y"],
                "change": change,
                "span_seconds": span_ms // 1000,
                "latest_text": f"{latest['y']:,}",
                "text": format_delta(change, span_ms),
            }
        datasets.append(
            {
                "label": AUDIENCE[key],
                "platform": key[0],
                "metric": key[1],
                "color": SOURCE_COLORS.get(key[0], "#94a3b8"),
                "data": points,
                "delta": delta,
            }
        )
    return {"datasets": datasets}


def present_related(rows: list[dict]) -> list[dict]:
    """Rows for the Fans also like list. A name links only when to_artist_id is stored."""
    presented = []
    for row in rows or []:
        name = str(row.get("to_name") or "").strip()
        if not name:
            continue
        artist_id = row.get("to_artist_id")
        try:
            linked = int(artist_id) if artist_id is not None and str(artist_id) != "" else None
        except (TypeError, ValueError):
            linked = None
        presented.append(
            {
                "to_name": name[:120],
                "to_spotify_id": str(row.get("to_spotify_id") or ""),
                "to_artist_id": linked,
                "rank": row.get("rank"),
                "first_seen_at": str(row.get("first_seen_at") or ""),
                "last_seen_at": str(row.get("last_seen_at") or ""),
            }
        )
    return presented


def present_sightings(rows: list[dict]) -> list[dict]:
    presented = []
    for row in rows or []:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        presented.append(
            {
                "source": str(row.get("source") or ""),
                "title": title[:200],
                "kind": str(row.get("kind") or ""),
                "published_at": str(row.get("published_at") or ""),
                "precision": str(row.get("precision") or ""),
                "first_seen_at": str(row.get("first_seen_at") or ""),
            }
        )
    return presented


def empty_history(reason: str) -> dict:
    return {
        "available": False,
        "reason": reason,
        "growth": {"labels": [], "datasets": []},
        "related": [],
        "sightings": [],
        "artist_id": None,
    }


def _links(bundle: dict) -> dict:
    for source in bundle.get("sources") or []:
        if source.get("platform") == "musicbrainz":
            return dict(source.get("links") or {})
    return {}


def _mbid(bundle: dict) -> str | None:
    for source in bundle.get("sources") or []:
        if source.get("platform") != "musicbrainz":
            continue
        match = re.search(r"musicbrainz.org/artist/([0-9a-f-]{36})", source.get("profile_url") or "")
        return match.group(1) if match else None
    return None


def _blank(value) -> str | None:
    text = (value or "").strip() if isinstance(value, str) else ""
    return text or None


def _value_statements(prefix: str, rows: list[tuple], suffix: str = "") -> list[tuple[str, tuple]]:
    """Multi-row INSERT statements, each under D1's bound-parameter cap."""
    if not rows:
        return []
    width = len(rows[0])
    per = max(1, _PARAM_BUDGET // width)
    marker = "(" + ",".join("?" for _ in range(width)) + ")"
    tail = f" {suffix}" if suffix else ""
    statements = []
    for offset in range(0, len(rows), per):
        chunk = rows[offset : offset + per]
        sql = prefix + " VALUES " + ",".join(marker for _ in chunk) + tail
        params = tuple(value for row in chunk for value in row)
        statements.append((sql, params))
    return statements


async def _latest_by_metric(db, artist_id: int) -> dict[tuple[str, str], dict]:
    rows = await db.query(
        """
        SELECT source, metric, value, captured_at
        FROM (
            SELECT source, metric, value, captured_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY source, metric
                       ORDER BY captured_at DESC, id DESC
                   ) AS rn
            FROM metric_snapshots
            WHERE artist_id = ?
        ) AS ranked
        WHERE rn = 1
        """,
        (artist_id,),
    )
    return {(row["source"], row["metric"]): row for row in rows}


async def remember(
    db,
    name: str,
    bundle: dict,
    *,
    origin: str = "search",
    now: datetime | None = None,
    related_cap: int = 25,
    artist_row: dict | None = None,
    extra=None,
) -> dict:
    moment = now or datetime.now(timezone.utc)
    stamp = utc_now(moment)
    normalized = normalize_name(name)
    links = _links(bundle)
    channel_ids = links.get("youtube_channel_ids") or []
    if artist_row is None:
        await db.execute(
            """
            INSERT INTO artists (name, normalized_name, origin, tracked, created_at)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(normalized_name) DO UPDATE SET
                name = excluded.name,
                tracked = 1,
                origin = CASE WHEN excluded.origin = 'search' THEN 'search' ELSE artists.origin END
            """,
            (name.strip()[:120], normalized, origin, stamp),
        )
        artist = await db.query_one("SELECT * FROM artists WHERE normalized_name = ?", (normalized,))
    else:
        artist = artist_row
    artist_id = int(artist["id"])
    sources = bundle.get("sources") or []
    musicbrainz_failed = any(
        source.get("platform") == "musicbrainz" and source.get("status") == "error" for source in sources
    )
    statements = [
        (
            """
            UPDATE artists SET
                musicbrainz_id = COALESCE(?, musicbrainz_id),
                spotify_id = COALESCE(?, spotify_id),
                apple_id = COALESCE(?, apple_id),
                youtube_channel_id = COALESCE(?, youtube_channel_id),
                instagram_handle = COALESCE(?, instagram_handle),
                tiktok_handle = COALESCE(?, tiktok_handle),
                last_crawled_at = COALESCE(?, last_crawled_at)
            WHERE id = ?
            """,
            (
                _mbid(bundle),
                _blank(links.get("spotify_id")),
                _blank(links.get("apple_id")),
                _blank(channel_ids[0] if channel_ids else None),
                _blank(links.get("instagram")),
                _blank(links.get("tiktok")),
                None if musicbrainz_failed else stamp,
                artist_id,
            ),
        )
    ]
    snapshots = 0
    snapshot_rows = []
    sighting_rows = []
    latest = await _latest_by_metric(db, artist_id)
    for source in sources:
        platform = source.get("platform") or ""
        if source.get("status") == "error":
            continue
        for metric in source.get("metrics") or []:
            label = metric.get("label") or ""
            if (platform, label) not in AUDIENCE or not metric.get("available"):
                continue
            number = audience_exact(metric)
            if number is None:
                continue
            previous = latest.get((platform, label))
            previous_value = _whole_number(previous.get("value")) if previous else None
            if previous and previous_value is not None and previous_value == number:
                previous_at = parse_utc(previous.get("captured_at"))
                if previous_at and moment - previous_at < timedelta(seconds=SNAPSHOT_REPEAT_SECONDS):
                    continue
            snapshot_rows.append((artist_id, platform, label, number, str(metric.get("value") or number), stamp))
            snapshots += 1
        taken = 0
        for item in source.get("items") or []:
            if taken >= SIGHTINGS_PER_SOURCE or len(sighting_rows) >= SIGHTING_CAP:
                break
            external = (item.get("url") or f"{item.get('kind')}:{item.get('title')}:{item.get('sort_key')}")[:300]
            if not external or not item.get("title"):
                continue
            sighting_rows.append(
                (
                    artist_id,
                    platform,
                    external,
                    str(item.get("title"))[:200],
                    item.get("kind"),
                    item.get("sort_key") or item.get("display"),
                    item.get("precision"),
                    stamp,
                )
            )
            taken += 1
    statements.extend(
        _value_statements(
            "INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)",
            snapshot_rows,
        )
    )
    statements.extend(
        _value_statements(
            """
            INSERT OR IGNORE INTO content_sightings
                (artist_id, source, external_id, title, kind, published_at, precision, first_seen_at)
            """.strip(),
            sighting_rows,
        )
    )
    related_sql, edges, discovered = await _store_related(db, artist_id, bundle, stamp, related_cap)
    statements.extend(related_sql)
    if extra is not None:
        statements.extend(extra(snapshots, edges, discovered))
    # One round trip for every write after the artist row exists.
    await db.batch(statements)
    return {
        "artist_id": artist_id,
        "snapshots": snapshots,
        "edges": edges,
        "discovered": discovered,
    }


async def _store_related(db, artist_id: int, bundle: dict, stamp: str, related_cap: int) -> tuple[list[tuple[str, tuple]], int, int]:
    related = []
    for source in bundle.get("sources") or []:
        if source.get("platform") == "spotify":
            related = list(source.get("related") or [])[:12]
            break
    edge_rows = []
    for item in related:
        spotify_id = _blank(item.get("spotify_id"))
        to_name = _blank(item.get("name"))
        if not spotify_id or not to_name:
            continue
        rank = int(item.get("rank") or len(edge_rows) + 1)
        edge_rows.append((artist_id, spotify_id, to_name[:120], "spotify", rank, stamp, stamp))
    statements = _value_statements(
        """
        INSERT INTO related_edges
            (from_artist_id, to_spotify_id, to_name, source, rank, first_seen_at, last_seen_at)
        """.strip(),
        edge_rows,
        """
        ON CONFLICT(from_artist_id, to_spotify_id, source) DO UPDATE SET
            to_name = excluded.to_name,
            rank = excluded.rank,
            last_seen_at = excluded.last_seen_at
        """.strip(),
    )
    written = len(edge_rows)
    discovered = 0
    if related_cap <= 0 or not edge_rows:
        return statements, written, discovered
    row = await db.query_one("SELECT COUNT(*) AS n FROM artists WHERE origin = 'related' AND tracked = 1")
    before = int((row or {}).get("n") or 0)
    room = related_cap - before
    if room <= 0:
        return statements, written, discovered
    self_spotify = _blank(_links(bundle).get("spotify_id"))
    candidates = []
    for item in related:
        if int(item.get("rank") or 99) > RELATED_RANK_LIMIT:
            continue
        spotify_id = _blank(item.get("spotify_id"))
        to_name = _blank(item.get("name"))
        if not spotify_id or not to_name or spotify_id == self_spotify:
            continue
        candidates.append((spotify_id, to_name[:120], normalize_name(to_name)))
    if not candidates:
        return statements, written, discovered
    marks = ",".join("?" for _ in candidates)
    existing_rows = await db.query(
        f"""
        SELECT id, spotify_id, normalized_name FROM artists
        WHERE spotify_id IN ({marks}) OR normalized_name IN ({marks})
        """,
        tuple([item[0] for item in candidates] + [item[2] for item in candidates]),
    )
    by_spotify: dict[str, int] = {}
    by_name: dict[str, int] = {}
    for existing in existing_rows:
        if existing.get("spotify_id"):
            by_spotify.setdefault(existing["spotify_id"], int(existing["id"]))
        if existing.get("normalized_name"):
            by_name.setdefault(existing["normalized_name"], int(existing["id"]))
    link_updates = []
    new_rows = []
    for spotify_id, to_name, normalized in candidates:
        if room <= 0:
            break
        found = by_spotify.get(spotify_id) or by_name.get(normalized)
        if found:
            link_updates.append((found, artist_id, spotify_id))
            continue
        new_rows.append((to_name, normalized, spotify_id, "related", 1, stamp))
        room -= 1
    if new_rows:
        statements.extend(
            _value_statements(
                "INSERT INTO artists (name, normalized_name, spotify_id, origin, tracked, created_at)",
                new_rows,
                "ON CONFLICT(normalized_name) DO NOTHING",
            )
        )
        discovered = len(new_rows)
        for to_name, normalized, spotify_id, _, _, _ in new_rows:
            link_updates.append((normalized, spotify_id, artist_id, spotify_id))
    if link_updates:
        for params in link_updates:
            if len(params) == 3:
                statements.append(
                    (
                        """
                        UPDATE related_edges SET to_artist_id = ?
                        WHERE from_artist_id = ? AND to_spotify_id = ? AND source = 'spotify'
                        """,
                        params,
                    )
                )
            else:
                # Runs after the insert in this same batch, so the new row is visible.
                statements.append(
                    (
                        """
                        UPDATE related_edges SET to_artist_id = (
                            SELECT id FROM artists
                            WHERE normalized_name = ? OR spotify_id = ?
                            LIMIT 1
                        )
                        WHERE from_artist_id = ? AND to_spotify_id = ? AND source = 'spotify'
                        """,
                        params,
                    )
                )
    return statements, written, discovered


ARTIST_LOOKUP_SQL = "SELECT id, name, normalized_name FROM artists WHERE normalized_name = ?"

SNAPSHOT_SQL = """
SELECT source, metric, value, captured_at
FROM metric_snapshots
WHERE artist_id = ? AND value IS NOT NULL
ORDER BY captured_at DESC, id DESC
LIMIT ?
"""

RELATED_SQL = """
SELECT to_name, to_spotify_id, to_artist_id, rank, first_seen_at, last_seen_at
FROM related_edges
WHERE from_artist_id = ?
ORDER BY rank ASC, last_seen_at DESC
LIMIT ?
"""

SIGHTING_SQL = """
SELECT source, title, kind, published_at, precision, first_seen_at
FROM content_sightings
WHERE artist_id = ?
ORDER BY first_seen_at DESC, id DESC
LIMIT ?
"""


async def find_artist(db, name: str) -> dict | None:
    """Lookup by the same normalized_name the crawler stores. Display case does not matter."""
    normalized = normalize_name(name)
    if not normalized:
        return None
    return await db.query_one(ARTIST_LOOKUP_SQL, (normalized,))


async def load_growth(db, name: str) -> dict:
    """Audience points only. The dashboard uses load_dashboard_history, which also reads edges."""
    history = await load_dashboard_history(db, name)
    if not history.get("growth", {}).get("datasets"):
        history["reason"] = history.get("reason") or "No exact audience count has been stored yet."
    return history


async def load_dashboard_history(db, name: str) -> dict:
    """Indexed reads only: the artist row, recent snapshots, related edges, and sightings.

    The old request path called load_growth, which selected metric_snapshots and left
    related and sightings as empty lists. The template then showed the crawler placeholders
    even when related_edges and content_sightings had rows.
    """
    artist = await find_artist(db, name)
    if not artist:
        return empty_history("This artist has no stored snapshots yet.")
    artist_id = int(artist["id"])
    rows = await db.query(SNAPSHOT_SQL, (artist_id, GROWTH_ROW_LIMIT))
    related = await db.query(RELATED_SQL, (artist_id, RELATED_READ_LIMIT))
    sightings = await db.query(SIGHTING_SQL, (artist_id, SIGHTING_READ_LIMIT))
    history = empty_history("No exact audience count has been stored yet.")
    history["available"] = bool(rows or related or sightings)
    history["artist_id"] = artist_id
    history["growth"] = build_growth(rows)
    history["related"] = present_related(related)
    history["sightings"] = present_sightings(sightings)
    if history["growth"]["datasets"]:
        history["reason"] = None
    return history


async def load_artist_history(db, name: str) -> dict:
    return await load_dashboard_history(db, name)
