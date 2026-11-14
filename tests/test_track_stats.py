"""Track stat series: stored numbers only, one card, and the CSV section."""

import asyncio
import csv
import io
import re
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import main
from crawl import reset_pool
from db import SqliteDatabase, migrate, utc_now
from export_csv import load_export_rows, render_export_csv
from history import TRACK_STATS_SQL, _latest_track_stat_map, present_track_stats, remember


def _plan(database: SqliteDatabase, sql: str, params: tuple) -> str:
    async def run():
        rows = await database.query("EXPLAIN QUERY PLAN " + sql, params)
        return " ".join(str(row.get("detail") or "") for row in rows)

    return asyncio.run(run())


def test_present_track_stats_uses_the_real_earlier_date():
    cutoff = "2026-11-06T12:00:00Z"
    rows = present_track_stats(
        [
            {
                "source": "deezer",
                "external_id": "11",
                "metric": "deezer_rank",
                "value": 150,
                "captured_at": "2026-11-13T12:00:00Z",
                "track_name": "One More Time",
                "list_rank": 1,
                "rn_latest": 1,
                "rn_first": 0,
                "rn_ago": 0,
            },
            {
                "source": "deezer",
                "external_id": "11",
                "metric": "deezer_rank",
                "value": 130,
                "captured_at": "2026-11-06T00:00:00Z",
                "track_name": "One More Time",
                "list_rank": 1,
                "rn_latest": 0,
                "rn_first": 0,
                "rn_ago": 1,
            },
            {
                "source": "deezer",
                "external_id": "11",
                "metric": "deezer_rank",
                "value": 100,
                "captured_at": "2026-10-01T00:00:00Z",
                "track_name": "One More Time",
                "list_rank": 1,
                "rn_latest": 0,
                "rn_first": 1,
                "rn_ago": 0,
            },
            {
                "source": "spotify",
                "external_id": "0dpyzcT3RMNNSd2xKBf35I",
                "metric": "spotify_playcount",
                "value": 50,
                "captured_at": "2026-11-13T12:00:00Z",
                "track_name": "Be Sweet",
                "list_rank": 1,
                "rn_latest": 1,
                "rn_first": 1,
                "rn_ago": 0,
            },
        ],
        cutoff,
    )
    assert rows[0]["name"] == "One More Time"
    assert rows[0]["source_label"] == "Deezer"
    assert rows[0]["metric_label"] == "Rank score"
    assert rows[0]["value_text"] == "150"
    assert rows[0]["change_text"] == "+20 since 2026-11-06"
    assert rows[0]["captured_at"] == "2026-11-13T12:00:00Z"
    assert "first stored value" not in rows[0]["change_text"]
    assert rows[1]["metric"] == "spotify_playcount"
    assert rows[1]["value_text"] == "50"
    assert rows[1]["change_text"] == "No earlier value"
    assert present_track_stats([], cutoff) == []


def test_track_stats_card_empty_state_and_csv(monkeypatch, tmp_path):
    path = tmp_path / "stats.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    latest = utc_now(now)
    ago = utc_now(now - timedelta(days=7, hours=1))
    first = utc_now(now - timedelta(days=40))
    only_first = utc_now(now - timedelta(days=3))

    async def seed():
        await migrate(database)
        await database.execute(
            """
            INSERT INTO artists (name, normalized_name, origin, tracked, created_at)
            VALUES ('Daft Punk', 'daftpunk', 'search', 1, ?)
            """,
            (latest,),
        )
        artist_id = int((await database.query_one("SELECT id FROM artists"))["id"])
        await database.execute(
            """
            INSERT INTO artist_tracks (
                artist_id, source, external_id, track_name, popularity, album_name, rank, first_seen_at, last_seen_at
            ) VALUES
                (?, 'deezer', '11', 'One More Time', NULL, 'Discovery', 1, ?, ?),
                (?, 'deezer', '12', 'Digital Love', NULL, 'Discovery', 2, ?, ?),
                (?, 'spotify', '0dpyzcT3RMNNSd2xKBf35I', 'Be Sweet', NULL, NULL, 1, ?, ?)
            """,
            (artist_id, latest, latest, artist_id, latest, latest, artist_id, latest, latest),
        )
        await database.execute(
            """
            INSERT INTO track_stats (artist_id, source, external_id, metric, value, captured_at)
            VALUES
                (?, 'deezer', '11', 'deezer_rank', 918273, ?),
                (?, 'deezer', '11', 'deezer_rank', 500000, ?),
                (?, 'deezer', '11', 'deezer_rank', 604020, ?),
                (?, 'deezer', '12', 'deezer_rank', 80, ?),
                (?, 'deezer', '12', 'deezer_rank', 95, ?),
                (?, 'spotify', '0dpyzcT3RMNNSd2xKBf35I', 'spotify_playcount', 113493783, ?)
            """,
            (artist_id, first, artist_id, ago, artist_id, latest, artist_id, only_first, artist_id, latest, artist_id, latest),
        )
        await database.execute(
            """
            INSERT INTO artists (name, normalized_name, origin, tracked, created_at)
            VALUES ('No Stats', 'nostats', 'search', 1, ?)
            """,
            (latest,),
        )
        empty_id = int((await database.query_one("SELECT id FROM artists WHERE normalized_name = 'nostats'"))["id"])
        return artist_id, empty_id

    artist_id, empty_id = asyncio.run(seed())
    plan = _plan(
        database,
        TRACK_STATS_SQL,
        (artist_id, artist_id, artist_id, artist_id, ago, artist_id, ago),
    )
    assert "idx_track_stats_lookup" in plan
    assert "SCAN track_stats" not in plan

    client = TestClient(main.app)
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    page = client.get(f"/artist/{artist_id}")
    assert page.status_code == 200
    assert "Track stats" in page.text
    assert "604,020" in page.text
    assert "918,273" not in page.text
    assert f"+104,020 since {ago[:10]}" in page.text
    assert latest in page.text
    assert "Rank score" in page.text
    assert "113,493,783" in page.text
    assert "No earlier value" in page.text
    assert f"since {only_first[:10]} (first stored value)" in page.text
    assert "not yet collected" not in page.text
    assert "could not be read" not in page.text
    assert "unavailable" in page.text
    assert len(page.text) < 160_000

    empty = client.get(f"/artist/{empty_id}")
    assert empty.status_code == 200
    assert "not yet collected" in empty.text
    assert "918,273" not in empty.text
    assert "Nothing here is estimated." in empty.text

    downloaded = client.get(f"/artist/{artist_id}/export.csv")
    parsed = list(csv.DictReader(io.StringIO(downloaded.text)))
    stats = [row for row in parsed if row["section"] == "track_stat"]
    assert len(stats) == 6
    latest_row = next(row for row in stats if row["captured_at"] == latest and row["external_id"] == "11")
    assert latest_row["metric"] == "deezer_rank"
    assert latest_row["value"] == "604020"
    assert latest_row["provider"] == "deezer"
    assert latest_row["title"] == "One More Time"
    plays = next(row for row in stats if row["metric"] == "spotify_playcount")
    assert plays["value"] == "113493783"
    assert plays["title"] == "Be Sweet"

    text = render_export_csv([], [], [], track_stats=[{"metric": "made_up", "value": 1, "captured_at": latest, "external_id": "x", "source": "deezer"}])
    assert "made_up" not in text
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)


def test_remember_writes_one_deezer_rank_per_utc_day():
    database = SqliteDatabase(":memory:")
    start = datetime(2026, 11, 13, 0, 0, tzinfo=timezone.utc)
    track = {
        "external_id": "3135553",
        "name": "One More Time",
        "rank": 1,
        "deezer_rank": 896666,
        "album_name": "Discovery",
        "popularity": None,
    }

    async def run():
        await migrate(database)
        bundle = {"sources": [{"platform": "deezer", "status": "ok", "tracks": [track], "metrics": []}]}
        await remember(database, "Daft Punk", bundle, now=start, related_cap=0)
        await remember(
            database,
            "Daft Punk",
            {"sources": [{"platform": "deezer", "status": "ok", "tracks": [{**track, "deezer_rank": 900000}], "metrics": []}]},
            now=start + timedelta(hours=6),
            related_cap=0,
        )
        same_day = await database.query("SELECT value, captured_at FROM track_stats")
        await remember(
            database,
            "Daft Punk",
            bundle,
            now=start + timedelta(days=1),
            related_cap=0,
        )
        unchanged = await database.query("SELECT value FROM track_stats ORDER BY captured_at")
        await remember(
            database,
            "Daft Punk",
            {"sources": [{"platform": "deezer", "status": "ok", "tracks": [{**track, "deezer_rank": 896700}], "metrics": []}]},
            now=start + timedelta(days=1, hours=2),
            related_cap=0,
        )
        changed = await database.query("SELECT value, captured_at FROM track_stats ORDER BY captured_at")
        stored = await database.query_one("SELECT rank, popularity FROM artist_tracks")
        snaps = await database.query_one("SELECT COUNT(*) AS n FROM track_snapshots")
        return same_day, unchanged, changed, stored, snaps

    same_day, unchanged, changed, stored, snaps = asyncio.run(run())
    assert same_day == [{"value": 896666, "captured_at": "2026-11-13T00:00:00Z"}]
    assert unchanged == [{"value": 896666}]
    assert [row["value"] for row in changed] == [896666, 896700]
    assert stored == {"rank": 1, "popularity": None}
    assert snaps["n"] == 0


def test_export_rows_include_track_stats():
    database = SqliteDatabase(":memory:")

    async def run():
        await migrate(database)
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('A', 'a', 'search', 1, '2026-11-01T00:00:00Z')"
        )
        artist_id = int((await database.query_one("SELECT id FROM artists"))["id"])
        await database.execute(
            """
            INSERT INTO track_stats (artist_id, source, external_id, metric, value, captured_at)
            VALUES (?, 'deezer', '11', 'deezer_rank', 10, '2026-11-13T00:00:00Z')
            """,
            (artist_id,),
        )
        return await load_export_rows(database, artist_id)

    rows = asyncio.run(run())
    assert rows["track_stats"][0]["value"] == 10


def test_latest_track_stat_read_has_no_compound_select():
    database = SqliteDatabase(":memory:")
    start = datetime(2026, 11, 13, tzinfo=timezone.utc)
    tracks = [
        {
            "external_id": str(index + 1),
            "name": f"Track {index + 1}",
            "rank": index + 1,
            "deezer_rank": 1000 + index,
            "album_name": "Album",
            "popularity": None,
        }
        for index in range(12)
    ]

    async def run():
        await migrate(database)
        await database.execute(
            """
            INSERT INTO artists (name, normalized_name, origin, tracked, created_at)
            VALUES ('Kendrick Lamar', 'kendricklamar', 'search', 1, '2026-11-01T00:00:00Z')
            """
        )
        artist = await database.query_one("SELECT * FROM artists")
        artist_id = int(artist["id"])
        for track in tracks:
            external = track["external_id"]
            value = track["deezer_rank"]
            # Latest row is inserted first, and an older row has the larger value.
            await database.execute(
                """
                INSERT INTO track_stats (artist_id, source, external_id, metric, value, captured_at)
                VALUES (?, 'deezer', ?, 'deezer_rank', ?, '2026-11-12T00:00:00Z')
                """,
                (artist_id, external, value),
            )
            await database.execute(
                """
                INSERT INTO track_stats (artist_id, source, external_id, metric, value, captured_at)
                VALUES (?, 'deezer', ?, 'deezer_rank', ?, '2026-11-10T00:00:00Z')
                """,
                (artist_id, external, value + 500),
            )
            await database.execute(
                """
                INSERT INTO track_stats (artist_id, source, external_id, metric, value, captured_at)
                VALUES (?, 'deezer', ?, 'deezer_rank', ?, '2026-11-01T00:00:00Z')
                """,
                (artist_id, external, value - 50),
            )
        queries = []
        original = database.query

        async def spy(sql, params=()):
            if "FROM track_stats" in sql and "MAX(" in sql:
                queries.append((sql, params))
            return await original(sql, params)

        database.query = spy
        pairs = [("deezer", track["external_id"], "deezer_rank") for track in tracks]
        found = await _latest_track_stat_map(database, artist_id, pairs)
        bundle = {"sources": [{"platform": "deezer", "status": "ok", "tracks": tracks, "metrics": []}]}
        await remember(database, "Kendrick Lamar", bundle, now=start, related_cap=0, artist_row=artist)
        unchanged = await database.query_one("SELECT COUNT(*) AS n FROM track_stats")
        changed_tracks = [{**track, "deezer_rank": 1100} if track["external_id"] == "1" else track for track in tracks]
        await remember(
            database,
            "Kendrick Lamar",
            {"sources": [{"platform": "deezer", "status": "ok", "tracks": changed_tracks, "metrics": []}]},
            now=start + timedelta(days=1),
            related_cap=0,
            artist_row=artist,
        )
        written = await database.query(
            "SELECT value, captured_at FROM track_stats WHERE external_id = '1' ORDER BY captured_at"
        )
        others = await database.query_one("SELECT COUNT(*) AS n FROM track_stats WHERE external_id = '2'")
        database.query = original
        return queries, found, unchanged, written, others

    queries, found, unchanged, written, others = asyncio.run(run())
    assert len(queries) == 3
    for sql, params in queries:
        assert re.search(r"\b(UNION|INTERSECT|EXCEPT)\b", sql, re.I) is None
        assert len(params) < 100
    first_ids = [value for value in queries[0][1] if isinstance(value, str) and value.isdigit()]
    assert first_ids == [str(index + 1) for index in range(12)]
    assert len(found) == 12
    for track in tracks:
        row = found[("deezer", track["external_id"], "deezer_rank")]
        assert row["value"] == track["deezer_rank"]
        assert row["captured_at"] == "2026-11-12T00:00:00Z"
    plan = _plan(database, queries[0][0], queries[0][1])
    assert "idx_track_stats_lookup" in plan
    assert "SCAN track_stats" not in plan
    assert unchanged["n"] == 36
    assert written == [
        {"value": 950, "captured_at": "2026-11-01T00:00:00Z"},
        {"value": 1500, "captured_at": "2026-11-10T00:00:00Z"},
        {"value": 1000, "captured_at": "2026-11-12T00:00:00Z"},
        {"value": 1100, "captured_at": "2026-11-14T00:00:00Z"},
    ]
    assert others["n"] == 3
