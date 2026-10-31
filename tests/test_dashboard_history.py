"""Stored Fans also like, first-seen rows, and per-series growth points."""

import asyncio
import time

from fastapi.testclient import TestClient

from crawl import reset_pool
from history import (
    ONE_READING,
    RELATED_SQL,
    SIGHTING_SQL,
    SNAPSHOT_SQL,
    build_growth,
    empty_history,
    find_artist,
    format_delta,
    load_dashboard_history,
    present_related,
    present_sightings,
)
from db import SqliteDatabase, migrate, utc_now
from datetime import datetime, timedelta, timezone

import main


def _db():
    database = SqliteDatabase(":memory:")

    async def ready():
        await migrate(database)
        return database

    return asyncio.run(ready())


def test_growth_series_are_not_aligned_across_providers():
    rows = [
        {"source": "spotify", "metric": "Followers", "value": 2_000_000, "captured_at": "2026-10-31T00:30:32Z"},
        {"source": "instagram", "metric": "Followers", "value": 500_000, "captured_at": "2026-10-31T00:30:33Z"},
        {"source": "tiktok", "metric": "Followers", "value": 80_000, "captured_at": "2026-10-31T00:30:36Z"},
        {"source": "spotify", "metric": "Followers", "value": 2_063_093, "captured_at": "2026-10-31T06:30:32Z"},
        {"source": "spotify", "metric": "Monthly listeners", "value": None, "captured_at": "2026-10-31T00:30:32Z"},
        {"source": "spotify", "metric": "Monthly listeners", "value": 1.5, "captured_at": "2026-10-31T01:30:32Z"},
        {"source": "spotify", "metric": "Monthly listeners", "value": "1.2M", "captured_at": "2026-10-31T02:30:32Z"},
        {"source": "tiktok", "metric": "Likes", "value": 10, "captured_at": "2026-10-31T00:30:32Z"},
    ]
    growth = build_growth(rows)
    by_label = {series["label"]: series for series in growth["datasets"]}
    followers = by_label["Spotify followers"]
    assert [point["y"] for point in followers["data"]] == [2_000_000, 2_063_093]
    assert None not in [point["y"] for point in followers["data"]]
    assert followers["data"][1]["x"] - followers["data"][0]["x"] == 6 * 3600 * 1000
    assert followers["delta"]["change"] == 63_093
    assert followers["delta"]["text"] == "+63,093 over 6h"
    instagram = by_label["Instagram followers"]["data"]
    assert len(instagram) == 1
    assert instagram[0]["x"] != followers["data"][0]["x"]
    assert "Monthly listeners" not in "".join(by_label)
    assert "Likes" not in "".join(series["label"] for series in growth["datasets"])
    for series in growth["datasets"]:
        assert all(isinstance(point["y"], int) for point in series["data"])


def test_single_reading_has_no_invented_trend():
    growth = build_growth(
        [
            {"source": "spotify", "metric": "Monthly listeners", "value": 1000, "captured_at": "2026-10-31T00:30:32Z"},
        ]
    )
    series = growth["datasets"][0]
    assert series["data"] == [{"x": series["data"][0]["x"], "y": 1000}]
    assert series["delta"]["change"] is None
    assert series["delta"]["text"] == ONE_READING
    assert format_delta(-1200, 2 * 86400 * 1000) == "-1,200 over 2d"
    assert format_delta(63093, 6 * 3600 * 1000) == "+63,093 over 6h"


class _FakeD1:
    """Records the SQL the dashboard asks for. Rows are preset, not scraped."""

    def __init__(self):
        self.calls = []

    async def query_one(self, sql, params=()):
        self.calls.append((sql, params))
        if params == ("clairo",):
            return {"id": 2, "name": "Clairo", "normalized_name": "clairo"}
        if params == ("taylorswift",):
            return {"id": 3, "name": "Taylor Swift", "normalized_name": "taylorswift"}
        if params == ("drake",):
            return {"id": 4, "name": "Drake", "normalized_name": "drake"}
        return None

    async def query(self, sql, params=()):
        self.calls.append((sql, params))
        if "related_edges" in sql:
            return [
                {
                    "to_name": "Snail Mail",
                    "to_spotify_id": "snail",
                    "to_artist_id": 9,
                    "rank": 2,
                    "first_seen_at": "2026-10-30T00:00:00Z",
                    "last_seen_at": "2026-10-31T00:00:00Z",
                },
                {
                    "to_name": "Untracked",
                    "to_spotify_id": "none",
                    "to_artist_id": None,
                    "rank": 1,
                    "first_seen_at": "2026-10-30T00:00:00Z",
                    "last_seen_at": "2026-10-31T00:00:00Z",
                },
            ]
        if "content_sightings" in sql:
            return [
                {
                    "source": "spotify",
                    "title": "Charm",
                    "kind": "release",
                    "published_at": "2024",
                    "precision": "year",
                    "first_seen_at": "2026-10-31T00:30:32Z",
                }
            ]
        if "metric_snapshots" in sql:
            return [
                {"source": "spotify", "metric": "Followers", "value": 100, "captured_at": "2026-10-31T00:30:32Z"},
                {"source": "spotify", "metric": "Followers", "value": 163, "captured_at": "2026-10-31T06:30:32Z"},
            ]
        return []


def test_related_and_sightings_queries_use_the_stored_columns():
    assert "from_artist_id" in RELATED_SQL
    assert "source_artist_id" not in RELATED_SQL
    assert "artist_id" in SIGHTING_SQL
    assert "first_seen_at" in SIGHTING_SQL
    assert "source_artist_id" not in SNAPSHOT_SQL
    database = _FakeD1()
    history = asyncio.run(load_dashboard_history(database, "Clairo"))
    assert history["artist_id"] == 2
    assert history["related"][0]["to_name"] == "Snail Mail"
    assert history["related"][0]["to_artist_id"] == 9
    assert history["related"][1]["to_artist_id"] is None
    assert history["sightings"][0]["title"] == "Charm"
    assert history["sightings"][0]["precision"] == "year"
    assert history["growth"]["datasets"][0]["delta"]["text"] == "+63 over 6h"
    asked = " ".join(sql for sql, _params in database.calls)
    assert "FROM related_edges" in asked
    assert "FROM content_sightings" in asked
    assert "source_artist_id" not in asked
    rendered = present_related(history["related"])
    assert rendered[1]["to_name"] == "Untracked"
    assert present_sightings([{"title": "", "source": "spotify"}]) == []


def test_artist_lookup_matches_clairo_taylor_swift_and_drake():
    database = _db()
    stamp = utc_now(datetime(2026, 10, 30, tzinfo=timezone.utc))

    async def seed():
        for name, normalized in (("Clairo", "clairo"), ("Taylor Swift", "taylorswift"), ("Drake", "drake")):
            await database.execute(
                """
                INSERT INTO artists (name, normalized_name, origin, tracked, created_at)
                VALUES (?, ?, 'search', 1, ?)
                """,
                (name, normalized, stamp),
            )
        clairo = await find_artist(database, "Clairo")
        taylor = await find_artist(database, "taylor swift")
        drake = await find_artist(database, "Drake")
        missing = await find_artist(database, "Nobody")
        return clairo, taylor, drake, missing

    clairo, taylor, drake, missing = asyncio.run(seed())
    assert clairo["normalized_name"] == "clairo"
    assert taylor["name"] == "Taylor Swift"
    assert drake["normalized_name"] == "drake"
    assert missing is None


def test_sqlite_history_orders_edges_and_caps_sightings():
    database = _db()
    start = datetime(2026, 10, 31, 0, 30, tzinfo=timezone.utc)

    async def seed():
        await database.execute(
            """
            INSERT INTO artists (name, normalized_name, origin, tracked, created_at)
            VALUES ('Clairo', 'clairo', 'search', 1, ?)
            """,
            (utc_now(start),),
        )
        artist = await find_artist(database, "Clairo")
        artist_id = int(artist["id"])
        await database.execute(
            """
            INSERT INTO artists (name, normalized_name, origin, tracked, created_at)
            VALUES ('Snail Mail', 'snailmail', 'related', 1, ?)
            """,
            (utc_now(start),),
        )
        linked = await find_artist(database, "Snail Mail")
        await database.execute(
            """
            INSERT INTO related_edges
                (from_artist_id, to_spotify_id, to_name, to_artist_id, source, rank, first_seen_at, last_seen_at)
            VALUES (?, 'snail', 'Snail Mail', ?, 'spotify', 2, ?, ?)
            """,
            (artist_id, int(linked["id"]), utc_now(start), utc_now(start + timedelta(hours=6))),
        )
        await database.execute(
            """
            INSERT INTO related_edges
                (from_artist_id, to_spotify_id, to_name, source, rank, first_seen_at, last_seen_at)
            VALUES (?, 'earlier', 'Earlier Name', 'spotify', 1, ?, ?)
            """,
            (artist_id, utc_now(start), utc_now(start)),
        )
        await database.execute(
            """
            INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)
            VALUES (?, 'spotify', 'Followers', 1000, '1,000', ?)
            """,
            (artist_id, utc_now(start)),
        )
        await database.execute(
            """
            INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)
            VALUES (?, 'spotify', 'Followers', 64093, '64,093', ?)
            """,
            (artist_id, utc_now(start + timedelta(hours=6))),
        )
        for index in range(25):
            seen = start + timedelta(minutes=index)
            await database.execute(
                """
                INSERT INTO content_sightings
                    (artist_id, source, external_id, title, kind, published_at, precision, first_seen_at)
                VALUES (?, 'spotify', ?, ?, 'release', '2024', 'year', ?)
                """,
                (artist_id, f"id-{index}", f"Title {index}", utc_now(seen)),
            )
        started = time.perf_counter()
        history = await load_dashboard_history(database, "Clairo")
        elapsed = time.perf_counter() - started
        indexes = await database.query(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name IN ('idx_related_from_artist', 'idx_sightings_artist_seen', 'idx_snapshots_lookup')"
        )
        return history, elapsed, indexes

    history, elapsed, indexes = asyncio.run(seed())
    assert [row["to_name"] for row in history["related"]] == ["Earlier Name", "Snail Mail"]
    assert history["related"][1]["to_artist_id"]
    assert history["related"][0]["to_artist_id"] is None
    assert len(history["sightings"]) == 20
    assert history["sightings"][0]["title"] == "Title 24"
    assert history["sightings"][-1]["title"] == "Title 5"
    assert history["growth"]["datasets"][0]["delta"]["text"] == "+63,093 over 6h"
    assert elapsed < 0.2
    assert {row["name"] for row in indexes} == {
        "idx_related_from_artist",
        "idx_sightings_artist_seen",
        "idx_snapshots_lookup",
    }


def _render(history):
    template = main.templates.get_template("dashboard.html")
    return template.render(
        query="Clairo",
        user={"artist_name": "Demo Artist"},
        sources=[],
        source_cards="",
        patterns={
            "release_hours": [],
            "social_hours": [],
            "weekdays": [],
            "hour_labels": [],
            "weekday_labels": [],
        },
        insights=["No day-level dates or exact timestamps were returned for this artist yet."],
        history=history,
    )


def test_template_shows_stored_rows_and_placeholders_when_empty():
    database = _db()
    start = datetime(2026, 10, 31, tzinfo=timezone.utc)

    async def seed():
        await database.execute(
            """
            INSERT INTO artists (name, normalized_name, origin, tracked, created_at)
            VALUES ('Clairo', 'clairo', 'search', 1, ?)
            """,
            (utc_now(start),),
        )
        artist = await find_artist(database, "Clairo")
        artist_id = int(artist["id"])
        await database.execute(
            """
            INSERT INTO artists (name, normalized_name, origin, tracked, created_at)
            VALUES ('Snail Mail', 'snailmail', 'related', 1, ?)
            """,
            (utc_now(start),),
        )
        linked = await find_artist(database, "Snail Mail")
        await database.execute(
            """
            INSERT INTO related_edges
                (from_artist_id, to_spotify_id, to_name, to_artist_id, source, rank, first_seen_at, last_seen_at)
            VALUES (?, 'snail', 'Snail Mail', ?, 'spotify', 1, ?, ?)
            """,
            (artist_id, int(linked["id"]), utc_now(start), utc_now(start + timedelta(hours=6))),
        )
        await database.execute(
            """
            INSERT INTO content_sightings
                (artist_id, source, external_id, title, kind, published_at, precision, first_seen_at)
            VALUES (?, 'spotify', 'charm', 'Charm', 'release', '2024', 'year', ?)
            """,
            (artist_id, utc_now(start)),
        )
        await database.execute(
            """
            INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)
            VALUES (?, 'instagram', 'Followers', 10, '10', ?),
                   (?, 'instagram', 'Followers', 12, '12', ?)
            """,
            (artist_id, "2026-10-31T00:30:33Z", artist_id, "2026-10-31T06:30:33Z"),
        )
        return await load_dashboard_history(database, "Clairo")

    filled = _render(asyncio.run(seed()))
    assert "Snail Mail" in filled
    assert "/dashboard?artist=Snail%20Mail" in filled
    assert "first 2026-10-31T00:00:00Z" in filled
    assert "last 2026-10-31T06:00:00Z" in filled
    assert ">Charm<" in filled
    assert ">year<" in filled
    assert "First seen" in filled
    assert "+2 over 6h" in filled
    assert "growth-0" in filled
    assert "does not parse that list" not in filled
    assert "No releases or posts have been stored yet." not in filled

    blank = _render(empty_history("No exact audience count has been stored yet."))
    assert "No related artists have been stored yet." in blank
    assert "No releases or posts have been stored yet." in blank
    assert "No exact audience count has been stored yet." in blank
    assert "Snail Mail" not in blank
    assert "growth-0" not in blank


def test_dashboard_request_reads_d1_when_the_live_parse_is_empty(monkeypatch, tmp_path):
    """The page used to ignore related_edges because the live Spotify parse is off."""
    path = tmp_path / "dash.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))
    stamp = "2026-10-31T00:30:32Z"

    async def seed():
        await migrate(database)
        await database.execute(
            """
            INSERT INTO artists (name, normalized_name, origin, tracked, created_at)
            VALUES ('Clairo', 'clairo', 'search', 1, ?)
            """,
            (stamp,),
        )
        await database.execute(
            """
            INSERT INTO artists (name, normalized_name, origin, tracked, created_at)
            VALUES ('Snail Mail', 'snailmail', 'related', 1, ?)
            """,
            (stamp,),
        )
        clairo = await find_artist(database, "Clairo")
        linked = await find_artist(database, "Snail Mail")
        await database.execute(
            """
            INSERT INTO related_edges
                (from_artist_id, to_spotify_id, to_name, to_artist_id, source, rank, first_seen_at, last_seen_at)
            VALUES (?, 'snail', 'Snail Mail', ?, 'spotify', 1, ?, ?)
            """,
            (int(clairo["id"]), int(linked["id"]), stamp, "2026-10-31T06:30:32Z"),
        )
        await database.execute(
            """
            INSERT INTO content_sightings
                (artist_id, source, external_id, title, kind, published_at, precision, first_seen_at)
            VALUES (?, 'spotify', 'charm', 'Charm', 'release', '2024', 'year', ?)
            """,
            (int(clairo["id"]), stamp),
        )
        await database.execute(
            """
            INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)
            VALUES (?, 'spotify', 'Followers', 2000000, '2,000,000', ?),
                   (?, 'spotify', 'Followers', 2063093, '2,063,093', ?)
            """,
            (int(clairo["id"]), stamp, int(clairo["id"]), "2026-10-31T06:30:32Z"),
        )

    asyncio.run(seed())

    async def fake_collect(name, env=None, http=None, cache=None, known_links=None, include_related=False, skip_platforms=None):
        assert include_related is False
        return {
            "sources": [],
            "patterns": {"release_hours": [], "social_hours": [], "weekdays": [], "hour_labels": [], "weekday_labels": []},
            "insights": [],
        }

    monkeypatch.setattr(main, "collect_artist", fake_collect)
    client = TestClient(main.app)
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    started = time.perf_counter()
    page = client.get("/dashboard", params={"artist": "Clairo"})
    elapsed = time.perf_counter() - started
    assert page.status_code == 200
    assert "Snail Mail" in page.text
    assert "/dashboard?artist=Snail%20Mail" in page.text
    assert "Charm" in page.text
    assert "+63,093 over 6h" in page.text
    assert "does not parse that list" not in page.text
    assert "No releases or posts have been stored yet." not in page.text
    assert elapsed < 1.0
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)
