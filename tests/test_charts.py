"""Stored iTunes charts. The pages do not fetch a feed."""

import asyncio
import csv
import io
from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient

import main
from charts import (
    ARTIST_CHART_POINT_LIMIT,
    ARTIST_CHART_TITLE_LIMIT,
    _ARTIST_CHART_POINTS_SQL,
    _ARTIST_CHART_SUMMARY_SQL,
    _CHART_ON_DATE_SQL,
    _SOURCE_LATEST_SQL,
    load_artist_charts,
    load_chart_board,
    present_chart_history,
)
from crawl import reset_pool
from db import SqliteDatabase, migrate
from export_csv import load_export_rows, render_export_csv


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


def _day_ms(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def _peak_token(position: int, day: str) -> str:
    return f"{position:03d}|{day}"


def _last_token(day: str, position: int) -> str:
    return f"{day}|{1000 - position:03d}"


def test_chart_history_presenter_keeps_peak_days_and_stored_points_only():
    latest = {("itunes_topsongs", "us"): "2026-11-05", ("itunes_topalbums", "us"): "2026-11-05"}
    summaries = [
        {
            "chart_source": "itunes_topsongs",
            "territory": "us",
            "track_or_album_name": "River",
            "peak_position": 2,
            "days_on_chart": 3,
            "last_date": "2026-11-05",
            "peak_token": _peak_token(2, "2026-11-03"),
            "last_token": _last_token("2026-11-05", 11),
        },
        {
            "chart_source": "itunes_topsongs",
            "territory": "us",
            "track_or_album_name": "Gone",
            "peak_position": 4,
            "days_on_chart": 2,
            "last_date": "2026-11-03",
            "peak_token": _peak_token(4, "2026-11-03"),
            "last_token": _last_token("2026-11-03", 4),
        },
        {
            "chart_source": "itunes_topalbums",
            "territory": "us",
            "track_or_album_name": "Night",
            "peak_position": 8,
            "days_on_chart": 2,
            "last_date": "2026-11-05",
            "peak_token": _peak_token(8, "2026-11-04"),
            "last_token": _last_token("2026-11-05", 8),
        },
        {
            "chart_source": "spotify",
            "territory": "us",
            "track_or_album_name": "Hidden",
            "peak_position": 1,
            "days_on_chart": 2,
            "last_date": "2026-11-05",
            "peak_token": _peak_token(1, "2026-11-05"),
            "last_token": _last_token("2026-11-05", 1),
        },
        {
            "chart_source": "itunes_topsongs",
            "territory": "us",
            "track_or_album_name": "Broken",
            "peak_position": 3,
            "days_on_chart": 2,
            "last_date": "2026-11-05",
            "peak_token": _peak_token(9, "2026-11-01"),
            "last_token": _last_token("2026-11-05", 3),
        },
        {
            "chart_source": "itunes_topsongs",
            "territory": "us",
            "track_or_album_name": "   ",
            "peak_position": 1,
            "days_on_chart": 1,
            "last_date": "2026-11-05",
            "peak_token": _peak_token(1, "2026-11-05"),
            "last_token": _last_token("2026-11-05", 1),
        },
        {
            "chart_source": "itunes_topsongs",
            "territory": "us",
            "track_or_album_name": "Fractional",
            "peak_position": 1.5,
            "days_on_chart": 1,
            "last_date": "2026-11-05",
            "peak_token": _peak_token(1, "2026-11-05"),
            "last_token": _last_token("2026-11-05", 1),
        },
    ]
    points = [
        {"chart_source": "itunes_topsongs", "territory": "us", "chart_date": "2026-11-05", "position": 11, "track_or_album_name": "River"},
        {"chart_source": "itunes_topsongs", "territory": "us", "chart_date": "2026-11-03", "position": 2, "track_or_album_name": "River"},
        {"chart_source": "itunes_topsongs", "territory": "us", "chart_date": "2026-11-01", "position": 9, "track_or_album_name": "River"},
        {"chart_source": "itunes_topsongs", "territory": "us", "chart_date": "2026-11-01", "position": 7, "track_or_album_name": "River"},
        {"chart_source": "itunes_topsongs", "territory": "us", "chart_date": "2026-11-03", "position": 4, "track_or_album_name": "Gone"},
        {"chart_source": "itunes_topsongs", "territory": "us", "chart_date": "2026-11-01", "position": 6, "track_or_album_name": "Gone"},
        {"chart_source": "itunes_topalbums", "territory": "us", "chart_date": "2026-11-05", "position": 8, "track_or_album_name": "Night"},
    ]
    rows = present_chart_history(summaries, points, latest)
    assert [row["title"] for row in rows] == ["River", "Gone", "Night"]
    river, gone, night = rows
    assert river["current_position"] == 11
    assert river["peak_position"] == 2
    assert river["peak_date"] == "2026-11-03"
    assert river["days_on_chart"] == 3
    assert river["chart_date"] == "2026-11-05"
    assert river["series"] == [
        {"x": _day_ms("2026-11-01"), "y": 7},
        {"x": _day_ms("2026-11-03"), "y": 2},
        {"x": _day_ms("2026-11-05"), "y": 11},
    ]
    assert _day_ms("2026-11-02") not in {point["x"] for point in river["series"]}
    assert river["series_capped"] is False
    assert gone["current_position"] is None
    assert gone["peak_position"] == 4
    assert gone["peak_date"] == "2026-11-03"
    assert gone["days_on_chart"] == 2
    assert gone["chart_label"] == "iTunes Top Songs"
    assert night["current_position"] == 8
    assert night["chart_source"] == "itunes_topalbums"
    assert night["series_capped"] is True
    assert present_chart_history(summaries, points, {})[0]["current_position"] is None


def test_chart_summary_sql_uses_the_artist_index_and_stays_bounded():
    assert f"LIMIT {ARTIST_CHART_TITLE_LIMIT}" in _ARTIST_CHART_SUMMARY_SQL
    assert f"LIMIT {ARTIST_CHART_POINT_LIMIT}" in _ARTIST_CHART_POINTS_SQL
    assert "JOIN" not in _ARTIST_CHART_SUMMARY_SQL.upper()
    assert "JOIN" not in _ARTIST_CHART_POINTS_SQL.upper()
    database = SqliteDatabase(":memory:")

    async def plans():
        await migrate(database)
        summary = await database.query(f"EXPLAIN QUERY PLAN {_ARTIST_CHART_SUMMARY_SQL}", (1,))
        points = await database.query(f"EXPLAIN QUERY PLAN {_ARTIST_CHART_POINTS_SQL}", (1,))
        latest = await database.query(f"EXPLAIN QUERY PLAN {_SOURCE_LATEST_SQL}", ("itunes_topsongs",))
        on_date = await database.query(
            f"EXPLAIN QUERY PLAN {_CHART_ON_DATE_SQL}",
            ("itunes_topsongs", "2026-11-01"),
        )
        return summary, points, latest, on_date

    summary, points, latest, on_date = asyncio.run(plans())

    def detail(rows):
        return " ".join(str(row.get("detail") or "") for row in rows)

    assert "idx_chart_entries_artist" in detail(summary)
    assert "idx_chart_entries_artist" in detail(points)
    assert "sqlite_autoindex_chart_entries_1" in detail(latest)
    assert "sqlite_autoindex_chart_entries_1" in detail(on_date)


async def _insert_chart(database, source, day, position, title, artist_id, name="Hana"):
    await database.execute(
        """
        INSERT INTO chart_entries (
            chart_source, territory, chart_date, position, track_or_album_name,
            apple_artist_id, artist_name, artist_id, collected_at
        ) VALUES (?, 'us', ?, ?, ?, NULL, ?, ?, ?)
        """,
        (source, day, position, title, name, artist_id, f"{day}T12:00:00Z"),
    )


def test_stored_chart_history_peak_days_and_date_browse(monkeypatch, tmp_path):
    path = tmp_path / "history.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))

    async def seed():
        await migrate(database)
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Hana', 'hana', 'search', 1, '2026-11-01T00:00:00Z')"
        )
        artist_id = int((await database.query_one("SELECT id FROM artists WHERE normalized_name = 'hana'"))["id"])
        songs = "itunes_topsongs"
        albums = "itunes_topalbums"
        for day, position in (
            ("2026-11-01", 9),
            ("2026-11-03", 2),
            ("2026-11-04", 2),
            ("2026-11-05", 11),
        ):
            await _insert_chart(database, songs, day, position, "River", artist_id)
        await _insert_chart(database, songs, "2026-11-01", 6, "Gone", artist_id)
        await _insert_chart(database, songs, "2026-11-03", 4, "Gone", artist_id)
        await _insert_chart(database, songs, "2026-11-05", 1, "Only Now", artist_id)
        await _insert_chart(database, songs, "2026-11-05", 9, "Twice", artist_id)
        await _insert_chart(database, songs, "2026-11-05", 4, "Twice", artist_id)
        await _insert_chart(database, albums, "2026-11-05", 8, "Night", artist_id)
        await _insert_chart(database, albums, "2026-11-01", 3, "Old Album", artist_id)
        history = await load_artist_charts(database, artist_id)
        latest = await load_chart_board(database, None)
        past = await load_chart_board(database, "2026-11-01")
        missing = await load_chart_board(database, "2026-11-02")
        invalid = await load_chart_board(database, "2026-02-31")
        capped = await load_export_rows(database, artist_id, limit=1)
        return artist_id, history, latest, past, missing, invalid, capped

    artist_id, history, latest, past, missing, invalid, capped = asyncio.run(seed())
    by_title = {row["title"]: row for row in history}
    assert [row["title"] for row in history] == ["Only Now", "Twice", "River", "Gone", "Night", "Old Album"]
    assert by_title["River"]["current_position"] == 11
    assert by_title["River"]["peak_position"] == 2
    assert by_title["River"]["peak_date"] == "2026-11-03"
    assert by_title["River"]["days_on_chart"] == 4
    assert [point["y"] for point in by_title["River"]["series"]] == [9, 2, 2, 11]
    assert [point["x"] for point in by_title["River"]["series"]] == [
        _day_ms("2026-11-01"),
        _day_ms("2026-11-03"),
        _day_ms("2026-11-04"),
        _day_ms("2026-11-05"),
    ]
    assert by_title["River"]["series_capped"] is False
    assert by_title["Gone"]["current_position"] is None
    assert by_title["Gone"]["peak_position"] == 4
    assert by_title["Gone"]["peak_date"] == "2026-11-03"
    assert by_title["Gone"]["days_on_chart"] == 2
    assert by_title["Gone"]["chart_date"] == "2026-11-03"
    assert by_title["Twice"]["current_position"] == 4
    assert by_title["Twice"]["days_on_chart"] == 1
    assert by_title["Twice"]["series"] == [{"x": _day_ms("2026-11-05"), "y": 4}]
    assert by_title["Only Now"]["current_position"] == 1
    assert by_title["Only Now"]["days_on_chart"] == 1
    assert by_title["Night"]["current_position"] == 8
    assert by_title["Night"]["chart_label"] == "iTunes Top Albums"
    assert by_title["Old Album"]["current_position"] is None
    assert by_title["Old Album"]["peak_date"] == "2026-11-01"
    assert latest["selected_date"] == ""
    assert latest["songs_date"] == "2026-11-05"
    assert [row["title"] for row in latest["songs"]] == ["Only Now", "Twice", "Twice", "River"]
    assert [row["position"] for row in latest["songs"]] == [1, 4, 9, 11]
    assert [row["title"] for row in latest["albums"]] == ["Night"]
    assert past["selected_date"] == "2026-11-01"
    assert [row["title"] for row in past["songs"]] == ["Gone", "River"]
    assert [row["position"] for row in past["songs"]] == [6, 9]
    assert [row["title"] for row in past["albums"]] == ["Old Album"]
    assert "2026-11-02" not in past["dates"]
    assert missing["selected_date"] == ""
    assert [row["title"] for row in missing["songs"]] == ["Only Now", "Twice", "Twice", "River"]
    assert invalid["selected_date"] == ""
    assert capped["charts"][0]["track_or_album_name"] == "Night"
    assert capped["charts"][0]["position"] == 8
    assert len(capped["charts"]) == 1

    calls = []

    async def fan_out(*args, **kwargs):
        calls.append("collect")
        raise AssertionError("chart history must not call providers")

    monkeypatch.setattr(main, "collect_artist", fan_out)
    client = TestClient(main.app)
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    board = client.get("/charts")
    assert board.status_code == 200
    assert "does not fetch Apple or Spotify" in board.text
    assert "Only Now" in board.text
    assert "Gone" not in board.text
    assert "Night" in board.text
    assert '<option value="">Latest</option>' in board.text
    assert "2026-11-03" in board.text
    past_page = client.get("/charts?date=2026-11-01")
    assert past_page.status_code == 200
    assert '<option value="2026-11-01" selected' in past_page.text
    assert "Gone" in past_page.text
    assert "Old Album" in past_page.text
    assert "Only Now" not in past_page.text
    assert "Night" not in past_page.text
    assert f'href="/artist/{artist_id}"' in past_page.text
    assert client.get("/charts?date=nope").text.count("Only Now") >= 1
    artist = client.get(f"/artist/{artist_id}")
    assert artist.status_code == 200
    river_at = artist.text.find("River")
    river_row = artist.text[river_at : river_at + 500]
    assert ">11<" in artist.text[max(0, river_at - 250) : river_at]
    assert ">2<" in river_row
    assert "2026-11-03" in river_row
    assert ">4<" in river_row
    gone_at = artist.text.find("Gone")
    assert ">—<" in artist.text[max(0, gone_at - 250) : gone_at]
    assert "iTunes Top Albums" in artist.text
    assert 'id="itunes-2"' in artist.text
    assert 'id="itunes-3"' in artist.text
    assert 'id="itunes-0"' not in artist.text
    assert 'id="itunes-1"' not in artist.text
    assert "reverse: true" in artist.text
    assert str(_day_ms("2026-11-01")) in artist.text
    assert str(_day_ms("2026-11-02")) not in artist.text
    assert "2026-11-02" not in artist.text
    downloaded = client.get(f"/artist/{artist_id}/export.csv")
    parsed = list(csv.DictReader(io.StringIO(downloaded.text)))
    charts = [row for row in parsed if row["section"] == "chart_entries"]
    assert len(charts) == 11
    assert charts[0]["title"] == "Night"
    assert charts[0]["value"] == "8"
    assert charts[0]["provider"] == "itunes_topalbums"
    assert charts[0]["metric"] == "position"
    assert charts[0]["captured_at"] == "2026-11-05"
    assert {row["value"] for row in charts} <= {str(number) for number in range(1, 101)}
    assert "1.2M" not in downloaded.text
    assert calls == []
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)


def test_chart_history_keeps_peak_when_the_line_is_capped():
    database = SqliteDatabase(":memory:")

    async def seed():
        await migrate(database)
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Hana', 'hana', 'search', 1, '2024-01-01T00:00:00Z')"
        )
        artist_id = int((await database.query_one("SELECT id FROM artists"))["id"])
        start = date(2024, 1, 1)
        for offset in range(ARTIST_CHART_POINT_LIMIT + 1):
            day = (start + timedelta(days=offset)).isoformat()
            await _insert_chart(database, "itunes_topsongs", day, 1 if offset == 0 else 50, "Long", artist_id)
        return await load_artist_charts(database, artist_id)

    rows = asyncio.run(seed())
    assert len(rows) == 1
    long_row = rows[0]
    assert long_row["days_on_chart"] == ARTIST_CHART_POINT_LIMIT + 1
    assert long_row["peak_position"] == 1
    assert long_row["peak_date"] == "2024-01-01"
    assert long_row["current_position"] == 50
    assert long_row["series_capped"] is True
    assert len(long_row["series"]) == ARTIST_CHART_POINT_LIMIT
    assert long_row["series"][0]["y"] == 50
    assert long_row["series"][-1]["y"] == 50
    assert _day_ms("2024-01-01") not in {point["x"] for point in long_row["series"]}


def test_chart_history_caps_the_title_list():
    database = SqliteDatabase(":memory:")

    async def seed():
        await migrate(database)
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Hana', 'hana', 'search', 1, '2026-11-01T00:00:00Z')"
        )
        artist_id = int((await database.query_one("SELECT id FROM artists"))["id"])
        for position in range(1, ARTIST_CHART_TITLE_LIMIT + 2):
            await _insert_chart(
                database,
                "itunes_topsongs",
                "2026-11-06",
                position,
                f"Title {position:02d}",
                artist_id,
            )
        return await load_artist_charts(database, artist_id)

    rows = asyncio.run(seed())
    titles = {row["title"] for row in rows}
    assert len(rows) == ARTIST_CHART_TITLE_LIMIT
    assert "Title 01" in titles
    assert f"Title {ARTIST_CHART_TITLE_LIMIT:02d}" in titles
    assert f"Title {ARTIST_CHART_TITLE_LIMIT + 1:02d}" not in titles


def test_csv_chart_section_keeps_exact_positions_only():
    text = render_export_csv(
        [],
        [],
        [],
        [],
        [],
        [
            {
                "chart_source": "itunes_topsongs",
                "territory": "us",
                "chart_date": "2026-11-06",
                "position": 4,
                "track_or_album_name": "River",
            },
            {
                "chart_source": "itunes_topsongs",
                "territory": "us",
                "chart_date": "2026-11-06",
                "position": None,
                "track_or_album_name": "Blank",
            },
            {
                "chart_source": "itunes_topsongs",
                "territory": "us",
                "chart_date": "2026-11-05",
                "position": 0,
                "track_or_album_name": "Zero",
            },
            {
                "chart_source": "itunes_topsongs",
                "territory": "us",
                "chart_date": "2026-11-04",
                "position": True,
                "track_or_album_name": "Bool",
            },
        ],
    )
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert len(parsed) == 1
    assert parsed[0]["section"] == "chart_entries"
    assert parsed[0]["value"] == "4"
    assert parsed[0]["rank"] == "4"
    assert parsed[0]["title"] == "River"
    assert parsed[0]["captured_at"] == "2026-11-06"
    assert parsed[0]["provider"] == "itunes_topsongs"
    assert parsed[0]["kind"] == "us"
    assert "Blank" not in text
    assert "Zero" not in text
    assert "Bool" not in text
