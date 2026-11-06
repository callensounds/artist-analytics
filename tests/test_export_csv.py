"""Stored CSV export and the artist timeline. No provider calls."""

import asyncio
import csv
import io
from datetime import datetime, timezone

from fastapi.testclient import TestClient

import main
from crawl import reset_pool
from db import SqliteDatabase, migrate
from export_csv import EXPORT_ROW_CAP, load_export_rows, render_export_csv
from history import present_sightings, timeline_instant


def test_timeline_orders_by_published_time_and_keeps_precision():
    assert timeline_instant("2024", "year", "2026-11-06T00:00:00Z") == datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert timeline_instant("not-a-time", "time", "2026-11-01T00:00:00Z") == datetime(2026, 11, 1, tzinfo=timezone.utc)
    rows = present_sightings(
        [
            {
                "source": "spotify",
                "title": "Year only release",
                "kind": "release",
                "published_at": "2024",
                "precision": "year",
                "first_seen_at": "2026-11-06T00:00:00Z",
            },
            {
                "source": "apple",
                "title": "Exact drop",
                "kind": "release",
                "published_at": "2026-08-23T07:00:00Z",
                "precision": "time",
                "first_seen_at": "2026-01-01T00:00:00Z",
            },
            {
                "source": "instagram",
                "title": "No source time",
                "kind": "post",
                "published_at": "",
                "precision": "",
                "first_seen_at": "2026-11-02T00:00:00Z",
            },
        ]
    )
    assert [row["title"] for row in rows] == ["No source time", "Exact drop", "Year only release"]
    assert rows[1]["precision_label"] == "Exact time"
    assert rows[2]["precision_label"] == "Year only"
    assert "07:00:00" not in rows[2]["published_at"]


def test_csv_keeps_exact_values_and_caps_each_section():
    text = render_export_csv(
        [
            {"captured_at": "2026-11-06T00:00:00Z", "source": "spotify", "metric": "Followers", "value": 2000000},
            {"captured_at": "2026-11-06T00:00:00Z", "source": "spotify", "metric": "Followers", "value": None},
            {"captured_at": "2026-11-06T00:00:00Z", "source": "spotify", "metric": "Followers", "value": "1.2M"},
        ],
        [
            {
                "source": "apple",
                "title": "Exact drop",
                "kind": "release",
                "published_at": "2026-08-23T07:00:00Z",
                "precision": "time",
                "first_seen_at": "2026-11-01T00:00:00Z",
            }
        ],
        [
            {
                "to_name": "Snail Mail",
                "to_spotify_id": "snail",
                "to_artist_id": 2,
                "rank": 1,
                "first_seen_at": "2026-10-01T00:00:00Z",
                "last_seen_at": "2026-11-01T00:00:00Z",
            },
            {
                "to_name": "Untracked",
                "to_spotify_id": "none",
                "to_artist_id": None,
                "rank": 2,
                "first_seen_at": "2026-10-01T00:00:00Z",
                "last_seen_at": "2026-11-01T00:00:00Z",
            },
        ],
        [
            {
                "mbid": "rg-sling",
                "title": "Sling",
                "release_type": "Album",
                "released_at": "2021-06-16",
                "precision": "day",
                "first_seen_at": "2026-11-01T00:00:00Z",
                "last_seen_at": "2026-11-06T00:00:00Z",
            }
        ],
    )
    parsed = list(csv.DictReader(io.StringIO(text)))
    metrics = [row for row in parsed if row["section"] == "metric"]
    assert metrics == [
        {
            "section": "metric",
            "captured_at": "2026-11-06T00:00:00Z",
            "provider": "spotify",
            "metric": "Followers",
            "value": "2000000",
            "title": "",
            "kind": "",
            "published_at": "",
            "precision": "",
            "first_seen_at": "",
            "last_seen_at": "",
            "rank": "",
            "to_name": "",
            "to_spotify_id": "",
            "to_artist_id": "",
            "mbid": "",
            "release_type": "",
        }
    ]
    assert "1.2M" not in text
    sighting = next(row for row in parsed if row["section"] == "sighting")
    assert sighting["published_at"] == "2026-08-23T07:00:00Z"
    assert sighting["precision"] == "time"
    related = [row for row in parsed if row["section"] == "related"]
    assert related[1]["to_artist_id"] == ""
    release = next(row for row in parsed if row["section"] == "release")
    assert release["mbid"] == "rg-sling"
    assert release["published_at"] == "2021-06-16"
    assert release["precision"] == "day"
    assert release["release_type"] == "Album"
    assert "-01-01" not in release["published_at"]
    assert EXPORT_ROW_CAP == 5000


def test_export_route_reads_d1_only(monkeypatch, tmp_path):
    path = tmp_path / "export.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))

    async def seed():
        await migrate(database)
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Clairo', 'clairo', 'search', 1, '2026-11-01T00:00:00Z')"
        )
        artist = await database.query_one("SELECT id FROM artists WHERE normalized_name = 'clairo'")
        artist_id = int(artist["id"])
        await database.execute(
            """
            INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)
            VALUES (?, 'spotify', 'Followers', 10, '10', '2026-11-01T00:00:00Z'),
                   (?, 'spotify', 'Followers', 12, '1.2M', '2026-11-06T00:00:00Z')
            """,
            (artist_id, artist_id),
        )
        await database.execute(
            """
            INSERT INTO content_sightings
                (artist_id, source, external_id, title, kind, published_at, precision, first_seen_at)
            VALUES (?, 'apple', 'drop', 'Exact drop', 'release', '2026-08-23T07:00:00Z', 'time', '2026-11-01T00:00:00Z')
            """,
            (artist_id,),
        )
        rows = await load_export_rows(database, artist_id, limit=1)
        return artist_id, rows

    artist_id, capped = asyncio.run(seed())
    assert len(capped["metrics"]) == 1
    assert capped["metrics"][0]["value"] == 12
    calls = []

    async def fan_out(*args, **kwargs):
        calls.append("collect")
        raise AssertionError("export must not call providers")

    monkeypatch.setattr(main, "collect_artist", fan_out)
    client = TestClient(main.app)
    locked = client.get(f"/artist/{artist_id}/export.csv", follow_redirects=False)
    assert locked.status_code == 303
    assert locked.headers["location"] == "/login"
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    page = client.get(f"/artist/{artist_id}")
    assert f'href="/artist/{artist_id}/export.csv"' in page.text
    assert "Exact time" in page.text
    assert "2026-08-23T07:00:00Z" in page.text
    downloaded = client.get(f"/artist/{artist_id}/export.csv")
    assert downloaded.status_code == 200
    assert "text/csv" in downloaded.headers["content-type"]
    assert f'filename="artist-{artist_id}.csv"' in downloaded.headers["content-disposition"]
    parsed = list(csv.DictReader(io.StringIO(downloaded.text)))
    assert parsed[0]["section"] == "metric"
    assert parsed[0]["value"] == "12"
    assert any(row["section"] == "sighting" and row["precision"] == "time" for row in parsed)
    assert "1.2M" not in downloaded.text
    slug = client.get("/artist/clairo/export.csv", follow_redirects=False)
    assert slug.status_code == 303
    assert slug.headers["location"] == f"/artist/{artist_id}/export.csv"
    missing = client.get("/artist/99999/export.csv")
    assert missing.status_code == 404
    assert "not tracked" in missing.text
    assert calls == []
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)
