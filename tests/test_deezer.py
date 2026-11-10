"""Deezer parsing, matching, and failure handling. HTTP is mocked."""

import asyncio

import pytest
from fastapi.testclient import TestClient

import main
from analytics import build_release_calendar
from crawl import reset_pool
from db import SqliteDatabase, migrate
from history import (
    DEEZER_FANS_SQL,
    DEEZER_IDENTITY_SQL,
    DEEZER_READ_ERROR,
    DEEZER_RELEASES_SQL,
    DEEZER_TRACKS_SQL,
    build_growth,
    find_artist_by_id,
    load_deezer_card,
    load_history_for_artist,
    present_deezer_card,
    remember,
)
from http_client import HttpResult, TransportError
from providers.common import deezer_artist_id
from providers.deezer import (
    DeezerBusy,
    build_deezer_result,
    choose_deezer_artist,
    date_only,
    fetch_deezer,
    parse_album_rows,
    parse_search_artists,
    parse_top_tracks,
    whole_count,
)
from providers.musicbrainz import extract_links
from scores import score_from_counts


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def get_json(self, url, **kwargs):
        self.calls.append(url)
        for needle, response in self.routes:
            if needle in url:
                status, payload = response
                if isinstance(payload, Exception):
                    raise payload
                body = "{}" if payload is None else "ok"
                return HttpResult(status, body, url, {}), payload
        return HttpResult(404, "", url, {}), None


def _artist(artist_id, name, fans, albums=4):
    return {
        "id": artist_id,
        "name": name,
        "link": f"https://www.deezer.com/artist/{artist_id}",
        "nb_fan": fans,
        "nb_album": albums,
        "picture_medium": "https://cdn-images.dzcdn.net/images/artist/example/250x250.jpg",
    }


def test_musicbrainz_deezer_url_is_an_artist_id():
    assert deezer_artist_id("https://www.deezer.com/artist/27") == "27"
    assert deezer_artist_id("https://www.deezer.com/en/artist/27?app=1") == "27"
    assert deezer_artist_id("https://www.deezer.com/album/302127") is None
    links = extract_links(
        [
            {"url": {"resource": "https://open.spotify.com/artist/4tZwfgrHOc3mvqYlEYSvVi"}},
            {"url": {"resource": "https://www.deezer.com/artist/27"}},
        ]
    )
    assert links["deezer_id"] == "27"
    assert links["spotify_id"] == "4tZwfgrHOc3mvqYlEYSvVi"


def test_exact_name_prefers_a_stored_id_then_the_highest_fan_count():
    rows = parse_search_artists(
        {
            "data": [
                _artist(8, "Daft Punk", 100),
                _artist(27, "Daft Punk", 50),
                _artist(99, "Daft Punks", 9000),
                _artist(3, "daft-punk", 10),
                _artist(4, "Daft", 999999),
            ]
        }
    )
    assert [row["id"] for row in rows] == ["8", "27", "99", "3", "4"]
    assert choose_deezer_artist(rows, "Daft Punk")["id"] == "8"
    assert choose_deezer_artist(rows, "Daft Punk", "27")["id"] == "27"
    assert choose_deezer_artist(rows, "Daft Punks")["id"] == "99"
    assert choose_deezer_artist(rows, "Daft Punk Live") is None
    assert choose_deezer_artist([], "Daft Punk") is None
    assert parse_search_artists({"error": {"message": "no data"}}) == []
    assert whole_count("1.2M") is None
    assert whole_count(True) is None
    assert whole_count(12) == 12


def test_top_tracks_use_list_order_and_dates_drop_a_clock():
    tracks = parse_top_tracks(
        {
            "data": [
                {"id": 3135556, "title": "One More Time", "rank": 900000, "album": {"title": "Discovery"}},
                {"id": 3135557, "title": "Digital Love", "rank": 800000, "album": {"title": "Discovery"}},
                {"id": "nope", "title": "Skipped", "rank": 1},
            ]
        }
    )
    assert [(track["rank"], track["name"], track["popularity"]) for track in tracks] == [
        (1, "One More Time", None),
        (2, "Digital Love", None),
    ]
    assert date_only("2001-03-12T00:00:00") == "2001-03-12"
    assert date_only("2001-03-12 00:00:00") == "2001-03-12"
    dated, missing = parse_album_rows(
        {
            "data": [
                {"id": 302127, "title": "Discovery", "release_date": "2001-03-12T00:00:00", "record_type": "album"},
                {"id": 9, "title": "Homework", "release_date": "1997", "record_type": "album"},
                {"id": 10, "title": "Undated", "record_type": "album"},
            ]
        }
    )
    assert missing == ["10"]
    result = build_deezer_result(_artist(27, "Daft Punk", 4000, 2) | {"id": "27"}, tracks=tracks, albums=dated)
    assert result.metrics[0].exact == 4000
    assert result.metrics[1].exact == 2
    assert [item.precision for item in result.items] == ["day", "year"]
    assert all(item.hour_utc is None for item in result.items)
    assert all(":" not in item.display for item in result.items)
    assert result.items[0].display == "2001-03-12"
    assert result.links["deezer_id"] == "27"
    detail = parse_album_rows({"id": 10, "title": "Undated", "release_date": "2019-11-07", "record_type": "single"})
    assert detail[0][0]["release_date"] == "2019-11-07"


def test_fetch_uses_a_stored_id_and_does_not_search():
    http = FakeHttp(
        [
            ("/artist/27/top", (200, {"data": [{"id": 1, "title": "One More Time", "rank": 5, "album": {"title": "Discovery"}}]})),
            ("/artist/27/albums", (200, {"data": [{"id": 302127, "title": "Discovery", "release_date": "2001-03-12", "record_type": "album"}]})),
            ("/artist/27", (200, _artist(27, "Daft Punk", 4000))),
            ("/search/artist", (200, {"data": [_artist(99, "Daft Punk", 999999)]})),
        ]
    )
    result = asyncio.run(fetch_deezer("Daft Punk", {"deezer_id": "27"}, http))
    assert result.status == "ok"
    assert result.links["deezer_id"] == "27"
    assert result.metrics[0].exact == 4000
    assert result.tracks[0]["rank"] == 1
    assert result.items[0].precision == "day"
    assert result.items[0].hour_utc is None
    assert not any("/search/" in url for url in http.calls)
    assert "stored" in " ".join(result.notices).lower() or "already stored" in " ".join(result.notices)


def test_fetch_picks_the_highest_fan_count_when_no_id_is_stored():
    http = FakeHttp(
        [
            ("/search/artist", (200, {"data": [_artist(8, "Daft Punk", 10), _artist(27, "Daft Punk", 4000), _artist(4, "Daft", 9)]})),
            ("/artist/27/top", (200, {"data": []})),
            ("/artist/27/albums", (200, {"data": []})),
            ("/artist/27", (200, _artist(27, "Daft Punk", 4000, 0))),
        ]
    )
    result = asyncio.run(fetch_deezer("Daft Punk", {}, http))
    assert result.status == "ok"
    assert result.links["deezer_id"] == "27"
    assert result.metrics[1].exact == 0
    assert any("/search/artist" in url for url in http.calls)


def test_fetch_reports_unavailable_on_a_miss_and_error_on_http_failure():
    missed = FakeHttp([("/search/artist", (200, {"data": [_artist(4, "Daft", 9)]}))])
    result = asyncio.run(fetch_deezer("Daft Punk", {}, missed))
    assert result.status == "unavailable"
    assert result.metrics == []
    assert "exact name" in result.reason

    failed = FakeHttp([("/search/artist", (500, {"error": {"message": "boom"}}))])
    result = asyncio.run(fetch_deezer("Daft Punk", {}, failed))
    assert result.status == "error"
    assert result.metrics == []

    busy = FakeHttp([("/search/artist", (429, None))])
    with pytest.raises(DeezerBusy):
        asyncio.run(fetch_deezer("Daft Punk", {}, busy))

    down = FakeHttp([("/search/artist", (200, TransportError("The request timed out.")))])
    result = asyncio.run(fetch_deezer("Daft Punk", {}, down))
    assert result.status == "error"
    assert "timed out" in result.reason


def test_stored_fans_feed_growth_score_calendar_and_the_artist_card():
    database = SqliteDatabase(":memory:")

    async def seed():
        await migrate(database)
        source = build_deezer_result(
            _artist(27, "Daft Punk", 4000, 12) | {"id": "27"},
            tracks=[{"external_id": "3135556", "name": "One More Time", "rank": 1, "album_name": "Discovery", "popularity": 900000}],
            albums=[{"id": "302127", "title": "Discovery", "kind": "album", "release_date": "2001-03-12T07:00:00", "url": "https://www.deezer.com/album/302127"}],
        ).to_dict()
        await remember(database, "Daft Punk", {"sources": [source]})
        artist = await database.query_one("SELECT id, deezer_id, deezer_nb_album FROM artists")
        snaps = await database.query("SELECT source, metric, value FROM metric_snapshots")
        sighting = await database.query_one("SELECT published_at, precision FROM content_sightings")
        card = await load_deezer_card(database, int(artist["id"]), "Daft Punk")
        rows = await database.query(
            "SELECT source, title, kind, published_at, precision FROM content_sightings WHERE artist_id = ?",
            (artist["id"],),
        )
        return artist, snaps, sighting, card, build_release_calendar(rows)

    artist, snaps, sighting, card, calendar = asyncio.run(seed())
    assert artist["deezer_id"] == "27"
    assert artist["deezer_nb_album"] == 12
    assert snaps == [{"source": "deezer", "metric": "Fans", "value": 4000}]
    assert sighting["published_at"] == "2001-03-12"
    assert sighting["precision"] == "day"
    assert ":" not in sighting["published_at"]
    growth = build_growth([{"source": "deezer", "metric": "Fans", "value": 4000, "captured_at": "2026-11-01T00:00:00Z"}])
    assert growth["datasets"][0]["label"] == "Deezer fans"
    scored = score_from_counts([{"source": "deezer", "metric": "Fans", "value": 4000}])
    assert scored["components"][0]["label"] == "Deezer fans"
    assert scored["score_text"] == "3.60"
    assert card["status"] == "ok"
    assert card["metrics"][0]["value"] == "4,000"
    assert card["metrics"][1]["value"] == "12"
    assert card["tracks"][0]["rank"] == 1
    assert card["tracks"][0]["url"] == "https://www.deezer.com/track/3135556"
    assert card["items"][0]["display"] == "2001-03-12"
    assert card["items"][0]["hour_utc"] is None
    assert calendar["day_count"] == 1
    assert calendar["months"][0]["cells"]
    placed = [item for cell in calendar["months"][0]["cells"] for item in cell["items"]]
    assert placed[0]["source"] == "deezer"
    assert placed[0]["clock"] == ""
    blank = present_deezer_card("Daft Punk", None, None, [], [])
    assert blank["status"] == "unavailable"


def test_artist_page_renders_the_stored_deezer_card_without_a_live_call(monkeypatch, tmp_path):
    path = tmp_path / "deezer.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))

    async def seed():
        await migrate(database)
        await database.execute(
            """
            INSERT INTO artists (name, normalized_name, origin, tracked, created_at, deezer_id, deezer_nb_album)
            VALUES ('Daft Punk', 'daftpunk', 'search', 1, '2026-11-01T00:00:00Z', '27', 12)
            """
        )
        artist = await database.query_one("SELECT id FROM artists")
        artist_id = int(artist["id"])
        await database.execute(
            """
            INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)
            VALUES (?, 'deezer', 'Fans', 4000, '4,000', '2026-11-01T00:00:00Z')
            """,
            (artist_id,),
        )
        await database.execute(
            """
            INSERT INTO artist_tracks (
                artist_id, source, external_id, track_name, popularity, album_name, rank, first_seen_at, last_seen_at
            ) VALUES (?, 'deezer', '3135556', 'One More Time', NULL, 'Discovery', 1, '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')
            """,
            (artist_id,),
        )
        await database.execute(
            """
            INSERT INTO content_sightings (
                artist_id, source, external_id, title, kind, published_at, precision, first_seen_at
            ) VALUES (?, 'deezer', 'https://www.deezer.com/album/302127', 'Discovery', 'album', '2001-03-12', 'day', '2026-11-01T00:00:00Z')
            """,
            (artist_id,),
        )
        return artist_id

    artist_id = asyncio.run(seed())
    calls = []

    async def fan_out(*args, **kwargs):
        calls.append("collect")
        raise AssertionError("artist page must not call Deezer")

    monkeypatch.setattr(main, "collect_artist", fan_out)
    client = TestClient(main.app)
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    page = client.get(f"/artist/{artist_id}")
    assert page.status_code == 200
    assert "Deezer" in page.text
    assert "4,000" in page.text
    assert "One More Time" in page.text
    assert "2001-03-12" in page.text
    assert "did not call Deezer" in page.text
    assert "2001-03-12 00:00" not in page.text
    assert "2001-03-12T" not in page.text
    assert calls == []

    async def clear():
        await database.execute("DELETE FROM metric_snapshots")
        await database.execute("DELETE FROM metric_latest")
        await database.execute("DELETE FROM artist_tracks")
        await database.execute("DELETE FROM content_sightings")
        await database.execute("UPDATE artists SET deezer_id = NULL, deezer_nb_album = NULL")

    asyncio.run(clear())
    empty = client.get(f"/artist/{artist_id}")
    assert empty.status_code == 200
    assert "Deezer" in empty.text
    assert "unavailable" in empty.text.lower()
    assert calls == []
    assert "Stored history could not be read" not in empty.text
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)


HISTORY_UNAVAILABLE = "Stored history could not be read on this request."


def _norm_sql(sql: str) -> str:
    return " ".join(sql.split())


def _card_sql() -> set[str]:
    return {_norm_sql(sql) for sql in (DEEZER_FANS_SQL, DEEZER_TRACKS_SQL, DEEZER_RELEASES_SQL, DEEZER_IDENTITY_SQL)}


def _plan(database: SqliteDatabase, sql: str, params: tuple = ()) -> str:
    async def run():
        rows = await database.query("EXPLAIN QUERY PLAN " + sql, params)
        return " ".join(str(row.get("detail") or "") for row in rows)

    return asyncio.run(run())


async def _seed_spotify_history(database: SqliteDatabase, *, deezer_id: str | None = None) -> int:
    await migrate(database)
    await database.execute(
        """
        INSERT INTO artists (name, normalized_name, origin, tracked, created_at, deezer_id, deezer_nb_album)
        VALUES ('Daft Punk', 'daftpunk', 'search', 1, '2026-11-01T00:00:00Z', ?, NULL)
        """,
        (deezer_id,),
    )
    artist_id = int((await database.query_one("SELECT id FROM artists"))["id"])
    await database.execute(
        """
        INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)
        VALUES (?, 'spotify', 'Followers', 2000000, '2,000,000', '2026-10-31T00:30:32Z'),
               (?, 'spotify', 'Followers', 2063093, '2,063,093', '2026-10-31T06:30:32Z')
        """,
        (artist_id, artist_id),
    )
    return artist_id


def _open_page(monkeypatch, path):
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()

    async def fan_out(*args, **kwargs):
        raise AssertionError("artist page must not call Deezer")

    monkeypatch.setattr(main, "collect_artist", fan_out)
    client = TestClient(main.app)
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    return client


def test_deezer_card_queries_hit_indexes_and_history_does_not_run_them():
    database = SqliteDatabase(":memory:")

    async def run():
        artist_id = await _seed_spotify_history(database, deezer_id="27")
        artist = await find_artist_by_id(database, artist_id)
        seen = []
        original = database.query

        async def spy(sql, params=()):
            seen.append(_norm_sql(sql))
            return await original(sql, params)

        database.query = spy
        history = await load_history_for_artist(database, artist)
        return artist_id, history, seen

    artist_id, history, seen = asyncio.run(run())
    assert artist_id
    assert history["deezer"] is None
    assert history["growth"]["datasets"]
    assert _card_sql().isdisjoint(seen)

    fans = _plan(database, DEEZER_FANS_SQL, (artist_id,))
    tracks = _plan(database, DEEZER_TRACKS_SQL, (artist_id, artist_id, 10))
    releases = _plan(database, DEEZER_RELEASES_SQL, (artist_id, 20))
    assert "metric_latest" in fans and "SCAN metric_latest" not in fans
    assert "idx_artist_tracks_seen" in tracks and "SCAN artist_tracks" not in tracks
    assert "SCAN content_sightings" not in releases


def test_artist_page_renders_full_history_with_zero_deezer_data(monkeypatch, tmp_path):
    path = tmp_path / "zero-deezer.sqlite"
    database = SqliteDatabase(str(path))
    artist_id = asyncio.run(_seed_spotify_history(database))
    seen = []
    original = SqliteDatabase.query

    async def spy(self, sql, params=()):
        seen.append(_norm_sql(sql))
        return await original(self, sql, params)

    monkeypatch.setattr(SqliteDatabase, "query", spy)
    client = _open_page(monkeypatch, path)
    page = client.get(f"/artist/{artist_id}")
    assert page.status_code == 200
    assert "+63,093 over 6h" in page.text
    assert "2,063,093" in page.text
    assert "Deezer" in page.text
    assert "No Deezer artist is stored for this name" in page.text
    assert "unavailable" in page.text.lower()
    assert "pending" not in page.text.lower()
    assert HISTORY_UNAVAILABLE not in page.text
    assert _card_sql().isdisjoint(seen)
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)


def test_stored_deezer_id_without_fans_is_pending_and_keeps_history(monkeypatch, tmp_path):
    path = tmp_path / "pending-deezer.sqlite"
    database = SqliteDatabase(str(path))
    artist_id = asyncio.run(_seed_spotify_history(database, deezer_id="27"))
    seen = []
    original = SqliteDatabase.query

    async def spy(self, sql, params=()):
        seen.append(_norm_sql(sql))
        return await original(self, sql, params)

    monkeypatch.setattr(SqliteDatabase, "query", spy)
    client = _open_page(monkeypatch, path)
    page = client.get(f"/artist/{artist_id}")
    assert page.status_code == 200
    assert "+63,093 over 6h" in page.text
    assert "pending" in page.text.lower()
    assert "No fan count has been stored yet." in page.text
    assert HISTORY_UNAVAILABLE not in page.text
    assert _norm_sql(DEEZER_IDENTITY_SQL) not in seen
    assert {_norm_sql(DEEZER_FANS_SQL), _norm_sql(DEEZER_TRACKS_SQL), _norm_sql(DEEZER_RELEASES_SQL)} <= set(seen)
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)


def test_deezer_read_failure_keeps_history_and_logs_without_secrets(monkeypatch, tmp_path, capsys):
    path = tmp_path / "failed-deezer.sqlite"
    database = SqliteDatabase(str(path))
    artist_id = asyncio.run(_seed_spotify_history(database, deezer_id="27"))
    original = SqliteDatabase.query

    async def explode(self, sql, params=()):
        if sql == DEEZER_TRACKS_SQL:
            raise RuntimeError("D1_ERROR: token=super-secret-value deezer tracks")
        return await original(self, sql, params)

    monkeypatch.setattr(SqliteDatabase, "query", explode)
    client = _open_page(monkeypatch, path)
    page = client.get(f"/artist/{artist_id}")
    assert page.status_code == 200
    assert "+63,093 over 6h" in page.text
    assert "2,063,093" in page.text
    assert DEEZER_READ_ERROR in page.text
    assert HISTORY_UNAVAILABLE not in page.text
    assert "super-secret-value" not in page.text
    logged = capsys.readouterr().out
    assert "stored read failed" in logged
    assert "deezer card" in logged
    assert "super-secret-value" not in logged
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)


def test_deezer_card_timeout_keeps_full_history(monkeypatch, tmp_path, capsys):
    path = tmp_path / "slow-deezer.sqlite"
    database = SqliteDatabase(str(path))
    artist_id = asyncio.run(_seed_spotify_history(database, deezer_id="27"))
    monkeypatch.setattr(main, "GROWTH_READ_TIMEOUT_S", 0.25)
    monkeypatch.setattr(main, "DEEZER_CARD_TIMEOUT_S", 0.05)

    async def slow_card(*args, **kwargs):
        await asyncio.sleep(0.4)
        raise AssertionError("the Deezer card read should have timed out")

    monkeypatch.setattr(main, "load_deezer_card", slow_card)
    client = _open_page(monkeypatch, path)
    page = client.get(f"/artist/{artist_id}")
    assert page.status_code == 200
    assert "+63,093 over 6h" in page.text
    assert "2,063,093" in page.text
    assert DEEZER_READ_ERROR in page.text
    assert HISTORY_UNAVAILABLE not in page.text
    logged = capsys.readouterr().out
    assert "stored read failed" in logged
    assert "deezer card" in logged
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)


async def _seed_page(database: SqliteDatabase) -> int:
    artist_id = await _seed_spotify_history(database, deezer_id="27")
    await database.execute("UPDATE artists SET deezer_nb_album = 2 WHERE id = ?", (artist_id,))
    await database.execute(
        """
        INSERT INTO related_edges
            (from_artist_id, to_spotify_id, to_name, source, rank, first_seen_at, last_seen_at)
        VALUES (?, 'snail', 'Snail Mail', 'spotify', 1, '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')
        """,
        (artist_id,),
    )
    await database.execute(
        """
        INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)
        VALUES (?, 'deezer', 'Fans', 4000, '4000', '2026-11-01T00:00:00Z')
        """,
        (artist_id,),
    )
    return artist_id


def test_artist_page_batches_the_tail_and_deezer_card(monkeypatch, tmp_path):
    """Each stored read used to be its own D1 round trip. Fourteen of them miss a 1.5s budget.

    Growth stays one indexed read so a cancelled tail can keep it. Everything else
    on the artist page, including the Deezer card, is one batch and still renders.
    """
    path = tmp_path / "batched.sqlite"
    artist_id = asyncio.run(_seed_page(SqliteDatabase(str(path))))
    original = SqliteDatabase.query
    batches = []

    async def slow_query(self, sql, params=()):
        await asyncio.sleep(0.25)
        return await original(self, sql, params)

    async def one_batch(self, statements):
        batches.append(len(statements))
        await asyncio.sleep(0.25)
        return [await original(self, sql, params) for sql, params in statements]

    monkeypatch.setattr(SqliteDatabase, "query", slow_query)
    monkeypatch.setattr(SqliteDatabase, "query_batch", one_batch)
    monkeypatch.setattr(main, "GROWTH_READ_TIMEOUT_S", 1.0)
    monkeypatch.setattr(main, "DEEZER_CARD_TIMEOUT_S", 0.5)
    client = _open_page(monkeypatch, path)
    page = client.get(f"/artist/{artist_id}")
    assert page.status_code == 200
    assert "+63,093 over 6h" in page.text
    assert "Snail Mail" in page.text
    assert "4,000" in page.text
    assert HISTORY_UNAVAILABLE not in page.text
    assert "Stored Deezer data could not be read" not in page.text
    assert batches == [14, 3]
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)


def test_stalled_history_tail_keeps_growth_and_still_reads_deezer(monkeypatch, tmp_path, capsys):
    path = tmp_path / "stalled-tail.sqlite"
    artist_id = asyncio.run(_seed_page(SqliteDatabase(str(path))))
    original = SqliteDatabase.query

    async def stall_batch(self, statements):
        if any("related_edges" in sql for sql, _params in statements):
            await asyncio.sleep(1)
            raise AssertionError("the history tail should have been cancelled")
        return [await original(self, sql, params) for sql, params in statements]

    monkeypatch.setattr(SqliteDatabase, "query_batch", stall_batch)
    monkeypatch.setattr(main, "GROWTH_READ_TIMEOUT_S", 0.2)
    client = _open_page(monkeypatch, path)
    page = client.get(f"/artist/{artist_id}")
    assert page.status_code == 200
    assert "+63,093 over 6h" in page.text
    assert "2,063,093" in page.text
    assert "Snail Mail" not in page.text
    assert "No related artists have been stored yet." not in page.text
    assert "Stored related artists could not be read on this request." in page.text
    assert "4,000" in page.text
    assert HISTORY_UNAVAILABLE not in page.text
    logged = capsys.readouterr().out
    assert "artist history" in logged
    assert "TimeoutError" in logged
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)


def test_failed_deezer_card_does_not_claim_the_catalog_is_empty():
    failed = present_deezer_card("Daft Punk", {"deezer_id": "27", "deezer_nb_album": 12}, None, [], [], failed=True)
    assert failed["status"] == "unavailable"
    assert failed["reason"] == DEEZER_READ_ERROR
    assert failed["metrics"] == []
    assert failed["profile_url"] == "https://www.deezer.com/artist/27"
    pending = present_deezer_card("Daft Punk", {"deezer_id": "27", "deezer_nb_album": None}, None, [], [])
    assert pending["status"] == "pending"
    assert pending["metrics"][0]["reason"] == "No fan count has been stored yet."
