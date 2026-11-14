"""Decide which dashboard providers are still fresh in D1.

A request renders stored counts first. It live-fetches at most DASHBOARD_LIVE_CAP
providers, and only those whose newest snapshot or sighting is missing or older
than DASHBOARD_FRESH_SECONDS. Search and /artist pages do not use this module.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from analytics import WEEKDAYS, build_insights, build_patterns, hour_labels, normalize_name
from db import log_read_failure, missing_table, parse_utc

# Six hours: long enough that a crawled artist is not fetched again on every
# page view, short enough that a missed crawl still refreshes the same day.
DASHBOARD_FRESH_SECONDS = 6 * 60 * 60
# Two live providers per request. The other stale sources stay on stored rows
# and the queued crawl.
DASHBOARD_LIVE_CAP = 2

# Missing sources are considered in this order. MusicBrainz is last because a
# recent artists row already holds the ids the others need.
LIVE_PRIORITY = ("spotify", "instagram", "tiktok", "apple", "youtube", "musicbrainz")
DISPLAY_ORDER = ("musicbrainz", "apple", "spotify", "youtube", "instagram", "tiktok")
LABELS = {
    "musicbrainz": "MusicBrainz",
    "apple": "Apple Music",
    "spotify": "Spotify",
    "youtube": "YouTube",
    "instagram": "Instagram",
    "tiktok": "TikTok",
}

_SNAPSHOT_CLOCK_SQL = """
SELECT source, captured_at
FROM source_freshness
WHERE artist_id = ?
"""

_SNAPSHOT_CLOCK_FALLBACK_SQL = """
SELECT source, MAX(captured_at) AS captured_at
FROM metric_snapshots
WHERE artist_id = ?
GROUP BY source
"""

_SIGHTING_CLOCK_SQL = """
SELECT source, MAX(first_seen_at) AS captured_at
FROM content_sightings
WHERE artist_id = ?
GROUP BY source
"""

_ARTIST_SQL = """
SELECT id, name, normalized_name, tracked, last_crawled_at,
       spotify_id, apple_id, youtube_channel_id, instagram_handle, tiktok_handle
FROM artists
WHERE normalized_name = ?
"""


def empty_patterns() -> dict:
    return {
        "hour_labels": hour_labels(),
        "weekday_labels": list(WEEKDAYS),
        "release_hours": [],
        "social_hours": [],
        "weekdays": [],
    }


def is_fresh(captured: datetime | None, now: datetime, ttl: int = DASHBOARD_FRESH_SECONDS) -> bool:
    if captured is None:
        return False
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now - captured < timedelta(seconds=ttl)


def choose_live_providers(
    clocks: dict[str, datetime | None],
    now: datetime,
    *,
    ttl: int = DASHBOARD_FRESH_SECONDS,
    cap: int = DASHBOARD_LIVE_CAP,
) -> list[str]:
    """Stale or missing providers, in LIVE_PRIORITY order, capped."""
    stale = [platform for platform in LIVE_PRIORITY if not is_fresh(clocks.get(platform), now, ttl)]
    return stale[: max(0, cap)]


def _stamp(captured: datetime | None) -> str:
    if captured is None:
        return ""
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    return captured.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stored_notice(captured: datetime | None, fresh: bool) -> str:
    when = _stamp(captured)
    hours = DASHBOARD_FRESH_SECONDS // 3600
    if fresh:
        return f"Stored {when}. Not fetched on this request."
    if when:
        return f"Stored {when}. Older than {hours} hours. Not fetched on this request."
    return "Not fetched on this request."


def cards_from_history(
    history: dict,
    clocks: dict[str, datetime | None],
    now: datetime,
    live: list[str],
    *,
    youtube_key: bool,
) -> list[dict]:
    """Source cards for providers this request will not fetch. Counts are stored integers."""
    live_set = set(live)
    grouped: dict[str, list[dict]] = {}
    for series in (history.get("growth") or {}).get("datasets") or []:
        grouped.setdefault(series["platform"], []).append(series)
    cards = []
    for platform in DISPLAY_ORDER:
        if platform in live_set:
            continue
        captured = clocks.get(platform)
        fresh = is_fresh(captured, now)
        metrics = []
        for series in grouped.get(platform) or []:
            latest = (series.get("delta") or {}).get("latest")
            if not isinstance(latest, int) or isinstance(latest, bool) or latest < 0:
                continue
            label = series.get("metric") or series.get("label")
            metrics.append(
                {
                    "label": label,
                    "available": True,
                    "value": f"{latest:,}",
                    "exact": latest,
                    "reason": None,
                }
            )
        if platform == "youtube" and not youtube_key and not metrics:
            cards.append(
                {
                    "platform": "youtube",
                    "label": "YouTube",
                    "status": "missing_credentials",
                    "reason": "Set the Worker secret YOUTUBE_API_KEY. Public upload times use the Data API; no user OAuth.",
                    "metrics": [],
                    "items": [],
                    "notices": ["The key was not sent. Stored history has no subscriber count."],
                }
            )
            continue
        if platform == "musicbrainz" and fresh and not metrics:
            cards.append(
                {
                    "platform": "musicbrainz",
                    "label": "MusicBrainz",
                    "status": "stored",
                    "reason": None,
                    "metrics": [],
                    "items": [],
                    "notices": ["Identity is stored. MusicBrainz was not called on this request."],
                }
            )
            continue
        if metrics or fresh:
            cards.append(
                {
                    "platform": platform,
                    "label": LABELS[platform],
                    "status": "stored",
                    "reason": None,
                    "metrics": metrics,
                    "items": [],
                    "notices": [_stored_notice(captured, fresh)],
                }
            )
            continue
        if platform in set(LIVE_PRIORITY) - live_set and not fresh:
            cards.append(
                {
                    "platform": platform,
                    "label": LABELS[platform],
                    "status": "unavailable",
                    "reason": "No stored count yet. This request did not fetch the live page.",
                    "metrics": [],
                    "items": [],
                    "notices": [],
                }
            )
    return cards


def assemble_bundle(cards: list[dict], live_bundle: dict | None, live: list[str], *, stored_only: bool) -> dict:
    live_by = {}
    if live_bundle and live:
        live_by = {
            source["platform"]: source
            for source in live_bundle.get("sources") or []
            if source.get("platform") in set(live)
        }
    sources = []
    used = set()
    for card in cards:
        replacement = live_by.get(card["platform"])
        if replacement is not None:
            sources.append(replacement)
            used.add(card["platform"])
        else:
            sources.append(card)
    for platform, source in live_by.items():
        if platform not in used:
            sources.append(source)
    hours = DASHBOARD_FRESH_SECONDS // 3600
    if stored_only:
        note = f"Stored readings from the last {hours} hours. No provider was fetched on this request."
    elif live:
        note = "Live fetch this request: " + ", ".join(live) + "."
    else:
        note = "No provider was fetched on this request."
    insights = [note]
    insights.extend(build_insights(sources))
    return {
        "sources": sources,
        "patterns": build_patterns(sources) if sources else empty_patterns(),
        "insights": insights[:8],
    }


def queued_bundle() -> dict:
    note = "This artist is not stored yet. A crawl was queued. This page did not call providers."
    return {
        "sources": [],
        "patterns": empty_patterns(),
        "insights": [note],
        "reason": note,
    }


async def provider_clocks(db, artist: dict) -> dict[str, datetime | None]:
    """Newest snapshot or sighting time per source, plus last_crawled_at for MusicBrainz."""
    clocks: dict[str, datetime | None] = {}
    artist_id = int(artist["id"])
    legacy_clock = False
    try:
        snapshot_rows = await db.query(_SNAPSHOT_CLOCK_SQL, (artist_id,))
    except Exception as exc:
        if not missing_table(exc):
            raise
        legacy_clock = True
    if legacy_clock:
        snapshot_rows = await db.query(_SNAPSHOT_CLOCK_FALLBACK_SQL, (artist_id,))
    for sql_rows in (snapshot_rows, await db.query(_SIGHTING_CLOCK_SQL, (artist_id,))):
        for row in sql_rows:
            source = str(row.get("source") or "")
            moment = parse_utc(row.get("captured_at"))
            if not source or moment is None:
                continue
            current = clocks.get(source)
            if current is None or moment > current:
                clocks[source] = moment
    crawled = parse_utc(artist.get("last_crawled_at"))
    if crawled is not None:
        current = clocks.get("musicbrainz")
        if current is None or crawled > current:
            clocks["musicbrainz"] = crawled
    return clocks


async def load_dashboard_plan(db, name: str, now: datetime, *, youtube_key: bool, on_growth=None, cache=None) -> dict:
    """Artist row, history, and the live-provider list. No outbound fetches.

    ``on_growth`` is invoked as soon as the audience series is built, before the
    other stored reads. A clock-table error does not discard that series.
    """
    from history import empty_history, load_history_for_artist

    normalized = normalize_name(name)
    artist = None
    if normalized:
        artist = await db.query_one(_ARTIST_SQL, (normalized,))
    if not artist:
        queued = queued_bundle()
        history = empty_history(queued["reason"])
        return {"artist": None, "history": history, "live": [], "cards": [], "clocks": {}}
    history = await load_history_for_artist(db, artist, on_growth=on_growth, cache=cache)
    try:
        clocks = await provider_clocks(db, artist)
    except Exception as exc:
        log_read_failure("dashboard clocks", exc)
        clocks = {}
    if not youtube_key:
        clocks["youtube"] = now
    live = choose_live_providers(clocks, now)
    cards = cards_from_history(history, clocks, now, live, youtube_key=youtube_key)
    return {"artist": artist, "history": history, "live": live, "cards": cards, "clocks": clocks}
