"""Audience score uses only exact stored counts."""

import asyncio

from fastapi.testclient import TestClient

import main
from crawl import reset_pool
from db import SqliteDatabase, migrate
from scores import SCORE_FORMULA, format_points, log10_hundredths, rank_scores, score_from_counts


def test_score_is_the_sum_of_rounded_log10_and_omits_missing_or_inexact_counts():
    assert log10_hundredths(1) == 0
    assert log10_hundredths(10) == 100
    assert log10_hundredths(100) == 200
    assert log10_hundredths(1000) == 300
    assert format_points(300) == "3.00"
    scored = score_from_counts(
        [
            {"source": "spotify", "metric": "Followers", "value": 1000},
            {"source": "spotify", "metric": "Monthly listeners", "value": 100},
            {"source": "instagram", "metric": "Followers", "value": 0},
            {"source": "tiktok", "metric": "Likes", "value": "1.2M"},
            {"source": "youtube", "metric": "Subscribers", "value": 1.5},
            {"source": "spotify", "metric": "Popularity", "value": 80},
        ]
    )
    assert scored["score_text"] == "5.00"
    assert scored["score_hundredths"] == 500
    assert [(part["label"], part["points_text"]) for part in scored["components"]] == [
        ("Spotify monthly listeners", "2.00"),
        ("Spotify followers", "3.00"),
    ]
    assert scored["formula"] == SCORE_FORMULA
    assert score_from_counts([])["score_text"] is None


def test_rank_uses_the_latest_exact_count_only():
    database = SqliteDatabase(":memory:")

    async def seed():
        await migrate(database)
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Clairo', 'clairo', 'search', 1, '2026-11-01T00:00:00Z')"
        )
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Drake', 'drake', 'search', 1, '2026-11-01T00:00:00Z')"
        )
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Hidden', 'hidden', 'search', 0, '2026-11-01T00:00:00Z')"
        )
        clairo = int((await database.query_one("SELECT id FROM artists WHERE normalized_name = 'clairo'"))["id"])
        drake = int((await database.query_one("SELECT id FROM artists WHERE normalized_name = 'drake'"))["id"])
        hidden = int((await database.query_one("SELECT id FROM artists WHERE normalized_name = 'hidden'"))["id"])
        await database.execute(
            """
            INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)
            VALUES (?, 'spotify', 'Followers', 100, '100', '2026-11-01T00:00:00Z'),
                   (?, 'spotify', 'Followers', 1000, '1,000', '2026-11-06T00:00:00Z'),
                   (?, 'spotify', 'Monthly listeners', NULL, '9,000,000', '2026-11-07T00:00:00Z'),
                   (?, 'instagram', 'Followers', 10, '10', '2026-11-06T00:00:00Z'),
                   (?, 'youtube', 'Subscribers', 1000000, '1,000,000', '2026-11-06T00:00:00Z')
            """,
            (clairo, clairo, clairo, drake, hidden),
        )
        return await rank_scores(database)

    ranked = asyncio.run(seed())
    assert [row["name"] for row in ranked] == ["Clairo", "Drake"]
    assert ranked[0]["score_text"] == "3.00"
    assert ranked[0]["components"][0]["count"] == 1000
    assert ranked[1]["score_text"] == "1.00"
    assert all(part["source"] != "youtube" for part in ranked[0]["components"])


def test_scores_page_requires_login_and_does_not_fetch(monkeypatch, tmp_path):
    path = tmp_path / "scores.sqlite"
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
            INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)
            VALUES (?, 'spotify', 'Followers', 1000, '1,000', '2026-11-06T00:00:00Z')
            """,
            (artist["id"],),
        )
        return int(artist["id"])

    artist_id = asyncio.run(seed())
    calls = []

    async def fan_out(*args, **kwargs):
        calls.append("collect")
        raise AssertionError("scores must not call providers")

    monkeypatch.setattr(main, "collect_artist", fan_out)
    client = TestClient(main.app)
    locked = client.get("/scores", follow_redirects=False)
    assert locked.status_code == 303
    assert locked.headers["location"] == "/login"
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    page = client.get("/scores")
    assert page.status_code == 200
    assert "Average Analytics" in page.text
    assert f'href="/artist/{artist_id}"' in page.text
    assert "3.00" in page.text
    assert "log10" in page.text
    assert "does not call providers" in page.text
    assert calls == []
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)
