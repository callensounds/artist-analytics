"""Trending ranks stored alerts. No provider calls."""

import asyncio

from fastapi.testclient import TestClient

import main
from crawl import reset_pool
from db import SqliteDatabase, migrate
from history import TRENDING_CAP, list_trending


def test_trending_ranks_by_percent_then_absolute_change_and_filters():
    database = SqliteDatabase(":memory:")

    async def seed():
        await migrate(database)
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Clairo', 'clairo', 'search', 1, '2026-11-01T00:00:00Z')"
        )
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Drake', 'drake', 'search', 1, '2026-11-01T00:00:00Z')"
        )
        clairo = int((await database.query_one("SELECT id FROM artists WHERE normalized_name = 'clairo'"))["id"])
        drake = int((await database.query_one("SELECT id FROM artists WHERE normalized_name = 'drake'"))["id"])
        rows = [
            (clairo, "spotify", "Followers", "24h", 20, 20, 1000, 1020, "a"),
            (drake, "instagram", "Followers", "7d", 500, 500, 1000, 1500, "b"),
            (clairo, "tiktok", "Likes", "24h", 90000, None, 0, 90000, "c"),
            (drake, "spotify", "Monthly listeners", "7d", -1000, -100, 10000, 9000, "d"),
        ]
        for artist_id, provider, metric, window, delta, pct, baseline, latest, key in rows:
            await database.execute(
                """
                INSERT INTO metric_alerts (
                    artist_id, provider, metric, window, delta, delta_pct,
                    baseline_value, latest_value, baseline_at, latest_at, detected_at, dedupe_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '2026-11-01T00:00:00Z', '2026-11-06T00:00:00Z', '2026-11-06T12:00:00Z', ?)
                """,
                (artist_id, provider, metric, window, delta, pct, baseline, latest, key),
            )
        ranked = await list_trending(database)
        week = await list_trending(database, window="7d")
        instagram = await list_trending(database, provider="instagram")
        return ranked, week, instagram

    ranked, week, instagram = asyncio.run(seed())
    assert [row["dedupe"] if False else (row["provider"], row["window"], row["percent"]) for row in ranked] == [
        ("instagram", "7d", "+50%"),
        ("spotify", "7d", "-10%"),
        ("spotify", "24h", "+2%"),
        ("tiktok", "24h", None),
    ]
    assert [row["provider"] for row in week] == ["instagram", "spotify"]
    assert [row["artist_name"] for row in instagram] == ["Drake"]
    assert TRENDING_CAP == 50


def test_trending_page_links_artists_and_does_not_fetch(monkeypatch, tmp_path):
    path = tmp_path / "trending.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))

    async def seed():
        await migrate(database)
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Clairo', 'clairo', 'search', 1, '2026-11-01T00:00:00Z')"
        )
        artist = await database.query_one("SELECT id FROM artists WHERE normalized_name = 'clairo'")
        await database.execute(
            """
            INSERT INTO metric_alerts (
                artist_id, provider, metric, window, delta, delta_pct,
                baseline_value, latest_value, baseline_at, latest_at, detected_at, dedupe_key
            ) VALUES (?, 'spotify', 'Followers', '24h', 20, 20, 1000, 1020,
                      '2026-11-06T00:00:00Z', '2026-11-06T11:00:00Z', '2026-11-06T12:00:00Z', 'one')
            """,
            (artist["id"],),
        )
        return int(artist["id"])

    artist_id = asyncio.run(seed())
    calls = []

    async def fan_out(*args, **kwargs):
        calls.append("collect")
        raise AssertionError("trending must not call providers")

    monkeypatch.setattr(main, "collect_artist", fan_out)
    client = TestClient(main.app)
    locked = client.get("/trending", follow_redirects=False)
    assert locked.status_code == 303
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    page = client.get("/trending")
    assert page.status_code == 200
    assert "Average Analytics" in page.text
    assert f'href="/artist/{artist_id}"' in page.text
    assert "+2%" in page.text
    assert "does not call providers" in page.text
    filtered = client.get("/trending", params={"window": "7d"})
    assert "No stored alerts match" in filtered.text
    assert calls == []
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)
