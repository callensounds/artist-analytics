"""Stored growth alerts are read, not computed, on the page."""

import asyncio

from fastapi.testclient import TestClient

import main
from crawl import reset_pool
from db import SqliteDatabase, migrate
from history import ALERTS_EMPTY, load_dashboard_history, present_alerts


def test_present_alerts_keeps_a_drop_and_omits_a_missing_percent():
    shown = present_alerts(
        [
            {
                "artist_id": 2,
                "artist_name": "Clairo",
                "provider": "spotify",
                "metric": "Followers",
                "window": "24h",
                "delta": -500,
                "delta_pct": -50,
                "baseline_value": 10000,
                "latest_value": 9500,
                "detected_at": "2026-11-06T12:00:00Z",
            },
            {
                "provider": "tiktok",
                "metric": "Followers",
                "window": "7d",
                "delta": 8000,
                "delta_pct": None,
                "baseline_value": 0,
                "latest_value": 8000,
                "detected_at": "2026-11-06T12:00:00Z",
            },
        ]
    )
    assert shown[0]["delta_text"] == "-500"
    assert shown[0]["percent"] == "-5%"
    assert shown[1]["percent"] is None
    assert shown[1]["baseline_value"] == 0


def test_alerts_page_lists_stored_rows_and_stays_empty_without_them(monkeypatch, tmp_path):
    path = tmp_path / "alerts.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))

    async def seed():
        await migrate(database)
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Clairo', 'clairo', 'search', 1, '2026-11-01T00:00:00Z')"
        )
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Drake', 'drake', 'search', 1, '2026-11-01T00:00:00Z')"
        )
        clairo = await database.query_one("SELECT id FROM artists WHERE normalized_name = 'clairo'")
        await database.execute(
            """
            INSERT INTO metric_alerts (
                artist_id, provider, metric, window, delta, delta_pct,
                baseline_value, latest_value, baseline_at, latest_at, detected_at, dedupe_key
            ) VALUES (?, 'spotify', 'Followers', '24h', 20, 20, 1000, 1020,
                      '2026-11-06T00:00:00Z', '2026-11-06T11:00:00Z', '2026-11-06T12:00:00Z', 'clairo-followers-24h')
            """,
            (clairo["id"],),
        )
        history = await load_dashboard_history(database, "Clairo")
        quiet = await load_dashboard_history(database, "Drake")
        drake = await database.query_one("SELECT id FROM artists WHERE normalized_name = 'drake'")
        return int(clairo["id"]), int(drake["id"]), history, quiet

    artist_id, drake_id, history, quiet = asyncio.run(seed())
    assert history["alerts"][0]["percent"] == "+2%"
    assert history["alerts"][0]["delta"] == 20
    assert quiet["alerts"] == []
    assert quiet["alerts_reason"] == ALERTS_EMPTY

    calls = []

    async def fan_out(*args, **kwargs):
        calls.append("collect")
        raise AssertionError("alerts must not call providers")

    monkeypatch.setattr(main, "collect_artist", fan_out)
    client = TestClient(main.app)
    locked = client.get("/alerts", follow_redirects=False)
    assert locked.status_code == 303
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    page = client.get("/alerts")
    assert page.status_code == 200
    assert "Clairo" in page.text
    assert "+20" in page.text
    assert "+2%" in page.text
    assert f'href="/artist/{artist_id}"' in page.text
    artist = client.get(f"/artist/{artist_id}")
    assert "Growth alerts" in artist.text
    assert "spotify Followers" in artist.text
    drake = client.get(f"/artist/{drake_id}")
    assert "No growth alerts yet" in drake.text
    assert calls == []
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)
