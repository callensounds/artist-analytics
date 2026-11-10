"""Dashboard serves stored snapshots and live-fetches only stale providers."""

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import main
from crawl import reset_pool
from dashboard_refresh import (
    DASHBOARD_FRESH_SECONDS,
    DASHBOARD_LIVE_CAP,
    choose_live_providers,
    is_fresh,
)
from db import SqliteDatabase, migrate, utc_now


def test_fresh_providers_are_not_selected_and_the_cap_holds():
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    fresh = now - timedelta(hours=1)
    stale = now - timedelta(seconds=DASHBOARD_FRESH_SECONDS)
    clocks = {
        "spotify": fresh,
        "instagram": fresh,
        "tiktok": fresh,
        "apple": fresh,
        "youtube": fresh,
        "musicbrainz": fresh,
    }
    assert choose_live_providers(clocks, now) == []
    assert is_fresh(fresh, now) is True
    assert is_fresh(stale, now) is False
    assert is_fresh(None, now) is False
    clocks["instagram"] = stale
    clocks["apple"] = None
    clocks["tiktok"] = stale
    assert choose_live_providers(clocks, now) == ["instagram", "tiktok"]
    assert len(choose_live_providers(clocks, now, cap=1)) == 1
    missing = {name: None for name in ("spotify", "instagram", "tiktok", "apple", "youtube", "musicbrainz")}
    assert choose_live_providers(missing, now) == ["spotify", "instagram"]
    assert DASHBOARD_LIVE_CAP == 2


def _seed(database, *, spotify_at, instagram_at, apple_at, crawled_at):
    async def run():
        await migrate(database)
        await database.execute(
            """
            INSERT INTO artists (
                name, normalized_name, origin, tracked, created_at, last_crawled_at,
                spotify_id, instagram_handle, tiktok_handle, apple_id
            ) VALUES ('Clairo', 'clairo', 'search', 1, ?, ?, 'sp', 'clairo', 'clairo', '99')
            """,
            (crawled_at, crawled_at),
        )
        artist = await database.query_one("SELECT id FROM artists WHERE normalized_name = 'clairo'")
        artist_id = int(artist["id"])
        await database.execute(
            """
            INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)
            VALUES (?, 'spotify', 'Followers', 2000000, '2,000,000', ?),
                   (?, 'instagram', 'Followers', 10, '10', ?),
                   (?, 'tiktok', 'Followers', 80, '80', ?)
            """,
            (artist_id, spotify_at, artist_id, instagram_at, artist_id, crawled_at),
        )
        if apple_at:
            await database.execute(
                """
                INSERT INTO content_sightings (
                    artist_id, source, external_id, title, kind, published_at, precision, first_seen_at
                ) VALUES (?, 'apple', 'album', 'Sling', 'release', '2021', 'year', ?)
                """,
                (artist_id, apple_at),
            )
        return artist_id

    return asyncio.run(run())


def test_dashboard_skips_live_fetch_when_snapshots_are_fresh(monkeypatch, tmp_path):
    path = tmp_path / "fresh.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    now = datetime.now(timezone.utc) - timedelta(minutes=30)
    stamp = utc_now(now)
    _seed(SqliteDatabase(str(path)), spotify_at=stamp, instagram_at=stamp, apple_at=stamp, crawled_at=stamp)
    calls = []

    async def fake_collect(*args, **kwargs):
        calls.append(kwargs.get("skip_platforms"))
        raise AssertionError("fresh snapshots must not fetch providers")

    monkeypatch.setattr(main, "collect_artist", fake_collect)
    client = TestClient(main.app)
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    page = client.get("/dashboard", params={"artist": "Clairo"})
    assert page.status_code == 200
    assert calls == []
    assert "2,000,000" in page.text
    assert "Stored" in page.text
    assert "No provider was fetched" in page.text
    assert "1.2M" not in page.text
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)


def test_dashboard_shows_an_honest_empty_state_when_nothing_is_stored(monkeypatch, tmp_path):
    path = tmp_path / "empty.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))

    async def seed():
        await migrate(database)
        await database.execute(
            """
            INSERT INTO artists (name, normalized_name, origin, tracked, created_at)
            VALUES ('Clairo', 'clairo', 'search', 1, '2026-11-01T00:00:00Z')
            """
        )

    asyncio.run(seed())
    client = TestClient(main.app)
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    page = client.get("/dashboard", params={"artist": "Clairo"})
    assert page.status_code == 200
    assert "No exact audience count has been stored yet." in page.text
    assert "Stored growth could not be read" not in page.text
    assert "1.2M" not in page.text
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)


def test_dashboard_keeps_stored_growth_when_a_later_read_fails(monkeypatch, tmp_path, capsys):
    path = tmp_path / "clocks.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    stamp = utc_now(datetime.now(timezone.utc) - timedelta(minutes=30))
    _seed(SqliteDatabase(str(path)), spotify_at=stamp, instagram_at=stamp, apple_at=stamp, crawled_at=stamp)
    original = SqliteDatabase.query

    async def explode_clocks(self, sql, params=()):
        if "source_freshness" in sql:
            raise RuntimeError("D1_ERROR: token=super-secret-value no such column: captured_at")
        return await original(self, sql, params)

    async def no_live(*args, **kwargs):
        return {"sources": [], "patterns": {}, "insights": []}

    monkeypatch.setattr(SqliteDatabase, "query", explode_clocks)
    monkeypatch.setattr(main, "collect_artist", no_live)
    client = TestClient(main.app)
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    page = client.get("/dashboard", params={"artist": "Clairo"})
    logged = capsys.readouterr().out
    assert page.status_code == 200
    assert "2,000,000" in page.text
    assert "Stored growth could not be read" not in page.text
    assert "dashboard clocks" in logged
    assert "no such column: captured_at" in logged
    assert "super-secret-value" not in logged
    assert "super-secret-value" not in page.text
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)


def test_dashboard_keeps_stored_growth_when_the_read_budget_expires(monkeypatch, tmp_path, capsys):
    path = tmp_path / "budget.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    stamp = utc_now(datetime.now(timezone.utc) - timedelta(minutes=30))
    _seed(SqliteDatabase(str(path)), spotify_at=stamp, instagram_at=stamp, apple_at=stamp, crawled_at=stamp)
    original = SqliteDatabase.query

    async def slow_related(self, sql, params=()):
        if "related_edges" in sql:
            await asyncio.sleep(1)
        return await original(self, sql, params)

    monkeypatch.setattr(SqliteDatabase, "query", slow_related)
    monkeypatch.setattr(main, "GROWTH_READ_TIMEOUT_S", 0.2)
    client = TestClient(main.app)
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    page = client.get("/dashboard", params={"artist": "Clairo"})
    logged = capsys.readouterr().out
    assert page.status_code == 200
    assert "2,000,000" in page.text
    assert "Stored growth could not be read" not in page.text
    assert "TimeoutError" in logged
    assert "dashboard plan" in logged
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)


def test_dashboard_logs_a_failed_growth_read_without_the_secret(monkeypatch, tmp_path, capsys):
    path = tmp_path / "growth-fail.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    stamp = utc_now(datetime.now(timezone.utc) - timedelta(minutes=30))
    _seed(SqliteDatabase(str(path)), spotify_at=stamp, instagram_at=stamp, apple_at=stamp, crawled_at=stamp)
    original = SqliteDatabase.query

    async def explode_snapshots(self, sql, params=()):
        if "FROM metric_snapshots" in sql and "LIMIT" in sql:
            raise RuntimeError("D1_ERROR: token=super-secret-value snapshot read failed")
        return await original(self, sql, params)

    monkeypatch.setattr(SqliteDatabase, "query", explode_snapshots)
    client = TestClient(main.app)
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    page = client.get("/dashboard", params={"artist": "Clairo"})
    logged = capsys.readouterr().out
    assert page.status_code == 200
    assert "Stored growth could not be read on this request." in page.text
    assert '{"datasets": []}' in page.text
    assert page.text.count("Stored growth could not be read on this request.") == 1
    assert "growth snapshots" in logged
    assert "RuntimeError" in logged
    assert "super-secret-value" not in logged
    assert "super-secret-value" not in page.text
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)


def test_dashboard_fetches_only_the_stale_provider(monkeypatch, tmp_path):
    path = tmp_path / "stale.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    now = datetime.now(timezone.utc)
    fresh = utc_now(now - timedelta(hours=1))
    stale = utc_now(now - timedelta(hours=8))
    _seed(SqliteDatabase(str(path)), spotify_at=fresh, instagram_at=stale, apple_at=fresh, crawled_at=fresh)
    calls = []

    async def fake_collect(name, env=None, http=None, cache=None, known_links=None, include_related=False, skip_platforms=None):
        calls.append({"name": name, "skip": set(skip_platforms or []), "links": known_links, "related": include_related})
        return {
            "sources": [
                {
                    "platform": "instagram",
                    "label": "Instagram",
                    "status": "ok",
                    "reason": None,
                    "metrics": [{"label": "Followers", "available": True, "value": "12", "exact": 12}],
                    "items": [
                        {
                            "title": "Post",
                            "kind": "post",
                            "precision": "time",
                            "precision_label": "Exact time",
                            "display": "2026-11-06 01:00 UTC",
                            "weekday": "Friday",
                            "hour_utc": 1,
                            "sort_key": "2026-11-06T01:00:00Z",
                        }
                    ],
                    "notices": [],
                }
            ],
            "patterns": {
                "hour_labels": [],
                "weekday_labels": [],
                "release_hours": [],
                "social_hours": [],
                "weekdays": [],
            },
            "insights": [],
        }

    monkeypatch.setattr(main, "collect_artist", fake_collect)
    client = TestClient(main.app)
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    page = client.get("/dashboard", params={"artist": "Clairo"})
    assert page.status_code == 200
    assert len(calls) == 1
    assert calls[0]["name"] == "Clairo"
    assert calls[0]["related"] is False
    assert "instagram" not in calls[0]["skip"]
    assert "spotify" in calls[0]["skip"]
    assert "musicbrainz" in calls[0]["skip"]
    assert "apple" in calls[0]["skip"]
    assert "tiktok" in calls[0]["skip"]
    assert calls[0]["links"]["spotify_id"] == "sp"
    assert "2026-11-06 01:00 UTC" in page.text
    assert "2,000,000" in page.text
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)
