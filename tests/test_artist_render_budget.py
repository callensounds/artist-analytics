"""Artist page work stays bounded when the stored history is large."""

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import main
from charts import ARTIST_CHART_POINT_LIMIT
from crawl import reset_pool
from db import SqliteDatabase, migrate, utc_now
from history import (
    ARTIST_CALENDAR_MONTHS,
    GROWTH_ROW_LIMIT,
    SERIES_POINT_CAP,
    _PARAM_BUDGET,
    _value_statements,
    build_growth,
)


def test_multi_row_inserts_count_fixed_parameters_and_stay_under_100():
    tracks = [(index, "deezer", str(index), "Track", None, "Album", index, "t", "t") for index in range(50)]
    track_sql = _value_statements(
        "INSERT INTO artist_tracks (a, b, c, d, e, f, g, h, i)",
        tracks,
        "ON CONFLICT(a) DO UPDATE SET n = ?",
        (7,),
    )
    assert track_sql
    assert all(len(params) <= 100 for _sql, params in track_sql)
    assert all(len(params) <= _PARAM_BUDGET for _sql, params in track_sql)
    assert all(params[-1] == 7 for _sql, params in track_sql)
    assert sum(len(params) - 1 for _sql, params in track_sql) == 50 * 9

    releases = [(1, f"mb-{index}", "Title", "Album", "2020-01-01", "day", "t", "t") for index in range(100)]
    release_sql = _value_statements(
        "INSERT INTO musicbrainz_releases (a, b, c, d, e, f, g, h)",
        releases,
    )
    assert all(len(params) <= 100 for _sql, params in release_sql)
    assert sum(len(params) for _sql, params in release_sql) == 100 * 8


def test_growth_series_keeps_the_real_ends_when_it_samples():
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(500):
        moment = start + timedelta(hours=index)
        rows.append(
            {
                "source": "spotify",
                "metric": "Followers",
                "value": 1000 + index,
                "captured_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )
    series = build_growth(rows)["datasets"][0]
    assert SERIES_POINT_CAP <= 300
    assert series["capped"] is True
    assert 2 <= len(series["data"]) <= SERIES_POINT_CAP
    assert series["data"][0]["y"] == 1000
    assert series["data"][-1]["y"] == 1499
    assert series["delta"]["earliest"] == 1000
    assert series["delta"]["latest"] == 1499
    assert series["delta"]["change"] == 499
    stored = {1000 + index for index in range(500)}
    assert {point["y"] for point in series["data"]} <= stored


def test_artist_page_caps_series_and_calendar_grids(monkeypatch, tmp_path):
    path = tmp_path / "artist.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))
    now = datetime.now(timezone.utc).replace(microsecond=0)

    async def seed():
        await migrate(database)
        await database.execute(
            """
            INSERT INTO artists (
                name, normalized_name, origin, tracked, created_at, deezer_id, deezer_nb_album
            ) VALUES ('Taylor Swift', 'taylorswift', 'search', 1, ?, '12246', 10)
            """,
            (utc_now(now),),
        )
        artist_id = int((await database.query_one("SELECT id FROM artists"))["id"])
        for index in range(360):
            moment = now - timedelta(hours=index)
            await database.execute(
                """
                INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)
                VALUES (?, 'spotify', 'Followers', ?, ?, ?)
                """,
                (artist_id, 1_000_000 + index, str(1_000_000 + index), utc_now(moment)),
            )
        await database.execute(
            """
            INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)
            VALUES (?, 'deezer', 'Fans', 4000, '4,000', ?)
            """,
            (artist_id, utc_now(now)),
        )
        for index in range(4):
            moment = now - timedelta(hours=index + 1)
            await database.execute(
                """
                INSERT INTO content_sightings (
                    artist_id, source, external_id, title, kind, published_at, precision, first_seen_at
                ) VALUES (?, 'instagram', ?, 'Post', 'post', ?, 'time', ?)
                """,
                (artist_id, f"post-{index}", utc_now(moment), utc_now(now)),
            )
        year, month = 2026, 11
        for index in range(16):
            released = f"{year:04d}-{month:02d}-02"
            await database.execute(
                """
                INSERT INTO musicbrainz_releases (
                    artist_id, mbid, title, release_type, released_at, precision, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, 'Album', ?, 'day', ?, ?)
                """,
                (artist_id, f"rg-{index}", f"Album {released[:7]}", released, utc_now(now), utc_now(now)),
            )
            month -= 1
            if month < 1:
                month = 12
                year -= 1
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        for offset in range(300):
            day = (start + timedelta(days=offset)).strftime("%Y-%m-%d")
            await database.execute(
                """
                INSERT INTO chart_entries (
                    chart_source, territory, chart_date, position, track_or_album_name,
                    apple_artist_id, artist_name, artist_id, collected_at
                ) VALUES ('itunes_topsongs', 'us', ?, ?, 'Long Song', NULL, 'Taylor Swift', ?, ?)
                """,
                (day, 40 if offset else 1, artist_id, f"{day}T12:00:00Z"),
            )
        return artist_id

    artist_id = asyncio.run(seed())
    client = TestClient(main.app)
    logged_in = client.post(
        "/login",
        data={"email": "artist@demo.com", "password": "demo123"},
        follow_redirects=False,
    )
    assert logged_in.status_code == 303
    page = client.get(f"/artist/{artist_id}")
    assert page.status_code == 200
    assert "could not be read" not in page.text
    for card in ("Audience growth", "Deezer", "Fans", "4,000", "Momentum", "Releases", "Posting times", "Release calendar"):
        assert card in page.text
    assert "Album 2025-08" in page.text
    assert "calendar-grid" in page.text
    assert page.text.count('<h3 class="text-sm font-medium mb-2">') == ARTIST_CALENDAR_MONTHS
    assert ARTIST_CALENDAR_MONTHS <= 6
    assert "older months" in page.text
    assert len(page.text) < 160_000

    growth = json.loads(re.search(r'<script id="growth-data"[^>]*>(.*?)</script>', page.text, re.S).group(1))
    assert SERIES_POINT_CAP <= 300
    assert GROWTH_ROW_LIMIT <= 300
    assert growth["datasets"]
    for series in growth["datasets"]:
        assert len(series["data"]) <= min(SERIES_POINT_CAP, GROWTH_ROW_LIMIT)
        assert series["data"][0]["y"] == series["delta"]["earliest"]
        assert series["data"][-1]["y"] == series["delta"]["latest"]

    charts = json.loads(re.search(r'<script id="itunes-chart-data"[^>]*>(.*?)</script>', page.text, re.S).group(1))
    assert ARTIST_CHART_POINT_LIMIT <= 300
    long_song = next(row for row in charts if row["title"] == "Long Song")
    assert len(long_song["series"]) == min(300, ARTIST_CHART_POINT_LIMIT)
    assert long_song["series_capped"] is True
    assert long_song["peak_position"] == 1
    assert long_song["days_on_chart"] == 300

    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)
