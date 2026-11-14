"""Assembled artist history can be reused without inventing a newer timestamp."""

import asyncio
import time
from datetime import datetime, timedelta, timezone

from cache import IsolateCache, history_page_cache
from db import D1Database, SqliteDatabase, migrate, parse_utc, utc_now
from history import HISTORY_CACHE_TTL, _history_cache_key, load_history_for_artist


def test_parse_utc_accepts_the_stored_stamp_and_rejects_a_bad_day():
    parsed = parse_utc("2026-11-13T18:04:05Z")
    assert parsed == datetime(2026, 11, 13, 18, 4, 5, tzinfo=timezone.utc)
    assert parse_utc("2024-02-29T00:00:00Z") is not None
    assert parse_utc("2023-02-29T00:00:00Z") is None
    assert parse_utc("2026-11-13 18:04:05Z") is None
    assert parse_utc("") is None
    assert parse_utc(None) is None


def test_history_page_cache_stays_off_sqlite():
    database = SqliteDatabase(":memory:")
    assert history_page_cache(None, database) is None
    assert history_page_cache(None, D1Database(object())) is not None


def test_cached_history_keeps_the_stored_timestamp_and_misses_after_a_new_crawl():
    database = SqliteDatabase(":memory:")
    cache = IsolateCache({})
    moment = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)

    async def run():
        await migrate(database)
        await database.execute(
            """
            INSERT INTO artists (name, normalized_name, origin, tracked, created_at, last_crawled_at)
            VALUES ('Drake', 'drake', 'search', 1, ?, ?)
            """,
            (utc_now(moment), utc_now(moment)),
        )
        artist = await database.query_one("SELECT * FROM artists")
        artist_id = int(artist["id"])
        captured = utc_now(moment - timedelta(days=2))
        await database.execute(
            """
            INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)
            VALUES (?, 'spotify', 'Followers', 1000, '1,000', ?)
            """,
            (artist_id, captured),
        )
        seen = {}
        first = await load_history_for_artist(database, artist, on_growth=seen.update, cache=cache)
        await database.execute("DELETE FROM metric_snapshots")
        second = await load_history_for_artist(database, artist, cache=cache)
        later = dict(artist)
        later["last_crawled_at"] = utc_now(moment + timedelta(hours=1))
        await database.execute(
            """
            INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)
            VALUES (?, 'spotify', 'Followers', 2500, '2,500', ?)
            """,
            (artist_id, utc_now(moment + timedelta(hours=1))),
        )
        third = await load_history_for_artist(database, later, cache=cache)
        key = _history_cache_key(artist)
        expires, payload = cache.store[key]
        cache.store[key] = (time.monotonic() - 1, payload)
        await database.execute("DELETE FROM metric_snapshots")
        fourth = await load_history_for_artist(database, artist, cache=cache)
        return seen, first, second, third, fourth, expires

    seen, first, second, third, fourth, expires = asyncio.run(run())
    point = first["growth"]["datasets"][0]["data"][0]
    assert point["y"] == 1000
    assert second["growth"]["datasets"][0]["data"][0]["y"] == 1000
    assert second["growth"]["datasets"][0]["delta"]["latest_text"] == "1,000"
    assert seen["growth"]["datasets"][0]["data"][0]["y"] == 1000
    assert third["growth"]["datasets"][0]["data"][0]["y"] == 2500
    assert fourth["growth"]["datasets"] == []
    assert expires > time.monotonic()
    assert HISTORY_CACHE_TTL == 90


def test_cached_history_rejects_another_artists_payload():
    database = SqliteDatabase(":memory:")
    cache = IsolateCache({})

    async def run():
        await migrate(database)
        await database.execute(
            """
            INSERT INTO artists (name, normalized_name, origin, tracked, created_at, last_crawled_at)
            VALUES ('Drake', 'drake', 'search', 1, '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')
            """
        )
        artist = await database.query_one("SELECT * FROM artists")
        await cache.set(
            _history_cache_key(artist),
            '{"artist_id": 999, "name": "Someone Else", "growth": {"datasets": []}}',
            90,
        )
        history = await load_history_for_artist(database, artist, cache=cache)
        return int(artist["id"]), history

    artist_id, history = asyncio.run(run())
    assert history["artist_id"] == artist_id
    assert history["name"] == "Drake"
