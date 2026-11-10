"""Deezer parsing, matching, and failure handling. HTTP is mocked."""

import asyncio

import pytest
from fastapi.testclient import TestClient

import main
from analytics import build_release_calendar
from crawl import reset_pool
from db import SqliteDatabase, migrate
from history import build_growth, load_deezer_card, present_deezer_card, remember
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
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)
