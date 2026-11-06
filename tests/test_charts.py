"""Stored iTunes charts. The pages do not fetch a feed."""

import asyncio

from fastapi.testclient import TestClient

import main
from crawl import reset_pool
from db import SqliteDatabase, migrate


UPSERT = """
INSERT INTO chart_entries (
    chart_source, territory, chart_date, position, track_or_album_name,
    apple_artist_id, artist_name, artist_id, collected_at
) VALUES (?, 'us', '2026-11-06', ?, ?, ?, ?, ?, ?)
ON CONFLICT(chart_source, territory, chart_date, position) DO UPDATE SET
    track_or_album_name = excluded.track_or_album_name,
    apple_artist_id = excluded.apple_artist_id,
    artist_name = excluded.artist_name,
    artist_id = excluded.artist_id,
    collected_at = excluded.collected_at
"""


def test_chart_migration_upserts_one_row_per_position():
    database = SqliteDatabase(":memory:")

    async def seed():
        await migrate(database)
        applied = await database.query_one(
            "SELECT version FROM schema_migrations WHERE version = '0009_itunes_charts.sql'"
        )
        indexes = await database.query(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'index' AND name IN ('idx_chart_entries_artist', 'idx_chart_entries_source_date')
            """
        )
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Ella Langley', 'ellalangley', 'search', 1, '2026-11-01T00:00:00Z')"
        )
        artist = int((await database.query_one("SELECT id FROM artists WHERE normalized_name = 'ellalangley'"))["id"])
        await database.execute(
            UPSERT,
            ("itunes_topsongs", 1, "First", "1", "Ella Langley", artist, "2026-11-06T12:00:00Z"),
        )
        await database.execute(
            UPSERT,
            ("itunes_topsongs", 1, "Choosin' Texas", "1384373733", "Ella Langley", artist, "2026-11-06T18:00:00Z"),
        )
        rows = await database.query("SELECT track_or_album_name, apple_artist_id, artist_id FROM chart_entries")
        return applied, indexes, rows

    applied, indexes, rows = asyncio.run(seed())
    assert applied["version"] == "0009_itunes_charts.sql"
    assert {row["name"] for row in indexes} == {"idx_chart_entries_artist", "idx_chart_entries_source_date"}
    assert rows == [{"track_or_album_name": "Choosin' Texas", "apple_artist_id": "1384373733", "artist_id": 1}]


def test_charts_and_artist_page_read_stored_rows_and_do_not_fetch(monkeypatch, tmp_path):
    path = tmp_path / "charts.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))

    async def seed():
        await migrate(database)
        await database.execute(
            "INSERT INTO artists (name, normalized_name, apple_id, origin, tracked, created_at) VALUES ('Ella Langley', 'ellalangley', '1384373733', 'search', 1, '2026-11-01T00:00:00Z')"
        )
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Quiet', 'quiet', 'search', 1, '2026-11-01T00:00:00Z')"
        )
        ella = int((await database.query_one("SELECT id FROM artists WHERE normalized_name = 'ellalangley'"))["id"])
        quiet = int((await database.query_one("SELECT id FROM artists WHERE normalized_name = 'quiet'"))["id"])
        await database.execute(
            UPSERT,
            ("itunes_topsongs", 4, "Choosin' Texas", "1384373733", "Ella Langley", ella, "2026-11-06T18:00:00Z"),
        )
        await database.execute(
            UPSERT,
            ("itunes_topalbums", 2, "Some Album", None, "Not In Catalog", None, "2026-11-06T18:00:00Z"),
        )
        await database.execute(
            """
            INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)
            VALUES (?, 'spotify', 'Followers', 1000, '1,000', '2026-11-06T00:00:00Z')
            """,
            (ella,),
        )
        return ella, quiet

    ella, quiet = asyncio.run(seed())
    calls = []

    async def fan_out(*args, **kwargs):
        calls.append("collect")
        raise AssertionError("chart pages must not call providers")

    monkeypatch.setattr(main, "collect_artist", fan_out)
    client = TestClient(main.app)
    locked = client.get("/charts", follow_redirects=False)
    assert locked.status_code == 303
    assert locked.headers["location"] == "/login"
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    board = client.get("/charts")
    assert board.status_code == 200
    assert "iTunes Top Songs" in board.text
    assert "iTunes Top Albums" in board.text
    assert "Choosin&#39; Texas" in board.text
    assert f'href="/artist/{ella}"' in board.text
    assert "Not In Catalog" in board.text
    assert f'href="/artist/{quiet}"' not in board.text
    assert "does not fetch Apple or Spotify" in board.text
    assert 'href="/charts"' in board.text
    artist = client.get(f"/artist/{ella}")
    assert artist.status_code == 200
    assert "Choosin&#39; Texas" in artist.text
    assert "iTunes Top Songs" in artist.text
    assert ">4<" in artist.text
    assert "2026-11-06" in artist.text
    assert "Audience score" in artist.text
    assert "log10 3.00" in artist.text
    assert "1,000" in artist.text
    empty = client.get(f"/artist/{quiet}")
    assert "No Apple Music or iTunes chart position is stored for this artist." in empty.text
    assert "No exact audience count is stored yet, so there is no score." in empty.text
    assert calls == []
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)
