"""Spotify top tracks: parse, store, and show. A missing popularity stays missing."""

import asyncio
import csv
import io
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import main
from cache import MemoryCache
from crawl import reset_pool
from db import SqliteDatabase, migrate
from export_csv import render_export_csv
from history import TRACK_SQL, TRACKS_EMPTY, load_dashboard_history, popularity_score, present_tracks, remember
from http_client import HttpResult
from providers.spotify import fetch_spotify, parse_api_tracks, parse_popular_tracks


def test_public_page_ids_are_kept_and_play_counts_are_not_popularity():
    page = """
    <span>Popular</span>
    <div aria-labelledby="listrow-title-track-spotify:track:0dpyzcT3RMNNSd2xKBf35I-0" aria-label="Be Sweet">
      <span>Be Sweet</span><span>113,493,783</span>
    </div>
    <div aria-labelledby="listrow-title-track-spotify:track:0O4sIQ728ugLlwBVaxF8UM-1" aria-label="No Score">
      <span>No Score</span><span>44,959,155</span>
    </div>
    <h2>Popular releases</h2>
    """
    rows = parse_popular_tracks(page)
    assert [row["name"] for row in rows] == ["Be Sweet", "No Score"]
    assert rows[0]["external_id"] == "0dpyzcT3RMNNSd2xKBf35I"
    assert rows[0]["rank"] == 1
    assert rows[0]["popularity"] is None
    assert rows[0]["album_name"] is None
    assert all(row["popularity"] is None for row in rows)
    assert "113493783" not in json.dumps(rows)

    plain = """
    Popular
    Example Song
    9,876,543
    Popular releases
    """
    named = parse_popular_tracks(plain)
    assert named == [
        {
            "name": "Example Song",
            "external_id": "name:examplesong",
            "rank": 1,
            "popularity": None,
            "album_name": None,
        }
    ]


def test_api_tracks_keep_zero_and_leave_a_missing_score_missing():
    rows = parse_api_tracks(
        {
            "tracks": [
                {"id": "0dpyzcT3RMNNSd2xKBf35I", "name": "Be Sweet", "popularity": 72, "album": {"name": "Jubilee"}},
                {"id": "0O4sIQ728ugLlwBVaxF8UM", "name": "No Score", "popularity": None, "album": {}},
                {"id": "aaaaaaaaaaaaaaaaaaaaaa", "name": "Zero", "popularity": 0, "album": {"name": "EP"}},
                {"id": "bbbbbbbbbbbbbbbbbbbbbb", "name": "Plays", "popularity": 9876543, "album": {"name": "Nope"}},
            ]
        }
    )
    assert [row["popularity"] for row in rows] == [72, None, 0, None]
    assert rows[0]["album_name"] == "Jubilee"
    assert rows[1]["album_name"] is None
    assert popularity_score(None) is None
    assert popularity_score(0) == 0
    assert popularity_score(9876543) is None
    presented = present_tracks(
        [
            {"track_name": "No Score", "external_id": "name:noscore", "popularity": None, "rank": 2, "album_name": None},
            {"track_name": "Zero", "external_id": "aaaaaaaaaaaaaaaaaaaaaa", "popularity": 0, "rank": 3, "album_name": "EP"},
        ]
    )
    assert presented[0]["popularity"] is None
    assert presented[0]["url"] is None
    assert presented[1]["popularity"] == 0
    assert presented[1]["url"] == "https://open.spotify.com/track/aaaaaaaaaaaaaaaaaaaaaa"


def test_parse_stores_and_presents_without_inventing_a_score():
    database = SqliteDatabase(":memory:")
    start = datetime(2026, 11, 6, tzinfo=timezone.utc)
    page = """
    <span>Popular</span>
    <div aria-labelledby="listrow-title-track-spotify:track:0dpyzcT3RMNNSd2xKBf35I-0" aria-label="Be Sweet"></div>
    <div aria-labelledby="listrow-title-track-spotify:track:0O4sIQ728ugLlwBVaxF8UM-1" aria-label="No Score"></div>
    <h2>Popular releases</h2>
    """

    async def run():
        await migrate(database)
        applied = await database.query_one(
            "SELECT version FROM schema_migrations WHERE version = '0010_artist_tracks.sql'"
        )
        tracks = parse_popular_tracks(page)
        await remember(
            database,
            "Japanese Breakfast",
            {"sources": [{"platform": "spotify", "status": "ok", "tracks": tracks, "metrics": []}]},
            now=start,
            related_cap=0,
        )
        stored = await database.query("SELECT external_id, popularity, album_name FROM artist_tracks ORDER BY rank")
        snaps = await database.query_one("SELECT COUNT(*) AS n FROM track_snapshots")
        await remember(
            database,
            "Japanese Breakfast",
            {
                "sources": [
                    {
                        "platform": "spotify",
                        "status": "ok",
                        "metrics": [],
                        "tracks": [
                            {
                                "name": "Be Sweet",
                                "external_id": "0dpyzcT3RMNNSd2xKBf35I",
                                "rank": 1,
                                "popularity": 72,
                                "album_name": "Jubilee",
                            },
                            {
                                "name": "No Score",
                                "external_id": "0O4sIQ728ugLlwBVaxF8UM",
                                "rank": 2,
                                "popularity": None,
                                "album_name": None,
                            },
                        ],
                    }
                ]
            },
            now=start + timedelta(hours=2),
            related_cap=0,
        )
        scored = await database.query(
            """
            SELECT 'spotify' AS source, external_id, track_name, popularity, album_name, rank,
                   first_seen_at, last_seen_at
            FROM artist_tracks ORDER BY rank
            """
        )
        after = await database.query_one("SELECT COUNT(*) AS n FROM track_snapshots")
        await remember(
            database,
            "Japanese Breakfast",
            {
                "sources": [
                    {
                        "platform": "spotify",
                        "status": "ok",
                        "metrics": [],
                        "tracks": [
                            {
                                "name": "Be Sweet",
                                "external_id": "0dpyzcT3RMNNSd2xKBf35I",
                                "rank": 1,
                                "popularity": 72,
                                "album_name": "Jubilee",
                            }
                        ],
                    }
                ]
            },
            now=start + timedelta(hours=2, minutes=10),
            related_cap=0,
        )
        deduped = await database.query_one("SELECT COUNT(*) AS n FROM track_snapshots")
        await remember(
            database,
            "Japanese Breakfast",
            {
                "sources": [
                    {
                        "platform": "spotify",
                        "status": "ok",
                        "metrics": [],
                        "tracks": [
                            {
                                "name": "Be Sweet",
                                "external_id": "0dpyzcT3RMNNSd2xKBf35I",
                                "rank": 1,
                                "popularity": None,
                                "album_name": None,
                            }
                        ],
                    }
                ]
            },
            now=start + timedelta(hours=3),
            related_cap=0,
        )
        kept = await database.query_one(
            "SELECT popularity, album_name FROM artist_tracks WHERE external_id = '0dpyzcT3RMNNSd2xKBf35I'"
        )
        history = await load_dashboard_history(database, "Japanese Breakfast")
        empty = await remember(
            database,
            "Quiet",
            {"sources": [{"platform": "spotify", "status": "ok", "metrics": [], "tracks": []}]},
            now=start,
            related_cap=0,
        )
        blank = await load_dashboard_history(database, "Quiet")
        return applied, stored, snaps, scored, after, deduped, kept, history, empty, blank

    applied, stored, snaps, scored, after, deduped, kept, history, empty, blank = asyncio.run(run())
    assert applied["version"] == "0010_artist_tracks.sql"
    assert stored[0]["popularity"] is None
    assert snaps["n"] == 0
    assert scored[0]["external_id"] == "0dpyzcT3RMNNSd2xKBf35I"
    assert scored[0]["popularity"] == 72
    assert scored[0]["album_name"] == "Jubilee"
    assert scored[0]["track_name"] == "Be Sweet"
    assert scored[1]["popularity"] is None
    assert after["n"] == 1
    assert deduped["n"] == 1
    assert kept == {"popularity": 72, "album_name": "Jubilee"}
    assert history["tracks"][0]["name"] == "Be Sweet"
    assert history["tracks"][0]["popularity"] == 72
    assert history["tracks"][0]["url"].endswith("/track/0dpyzcT3RMNNSd2xKBf35I")
    assert history["tracks_reason"] is None
    assert empty["artist_id"]
    assert blank["tracks"] == []
    assert blank["tracks_reason"] == TRACKS_EMPTY
    shown = main.templates.get_template("history_cards.html").render(history=history)
    assert "Be Sweet" in shown
    assert "Jubilee" in shown
    assert "width: 72%" in shown
    assert "open.spotify.com/track/0dpyzcT3RMNNSd2xKBf35I" in shown
    blank_html = main.templates.get_template("history_cards.html").render(history=blank)
    assert "No Spotify top tracks have been stored yet" in blank_html
    assert "Be Sweet" not in blank_html
    text = render_export_csv([], [], [], [], scored)
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert parsed[0]["section"] == "track"
    assert parsed[0]["value"] == "72"
    assert parsed[0]["external_id"] == "0dpyzcT3RMNNSd2xKBf35I"
    assert parsed[0]["album_name"] == "Jubilee"
    assert parsed[1]["value"] == ""
    assert "9876543" not in text


class _Http:
    def __init__(self, responder):
        self.responder = responder

    async def get_text(self, url, **kwargs):
        return self.responder("GET", url, kwargs.get("params"), kwargs.get("data"))

    async def get_json(self, url, **kwargs):
        result = await self.get_text(url, **kwargs)
        try:
            payload = result.json()
        except json.JSONDecodeError:
            payload = None
        return result, payload

    async def post_form(self, url, data, **kwargs):
        result = self.responder("POST", url, kwargs.get("params"), data)
        try:
            payload = result.json()
        except json.JSONDecodeError:
            payload = None
        return result, payload


def test_web_api_tracks_are_structured_and_the_public_path_does_not_call_it():
    import providers.spotify as spotify_mod

    spotify_mod._TOKEN["value"] = ""
    spotify_mod._TOKEN["expires"] = 0.0
    seen = []

    def respond(method, url, params, data):
        seen.append(url)
        if "accounts.spotify.com" in url:
            return HttpResult(200, json.dumps({"access_token": "token", "expires_in": 3600}), url, {})
        if "top-tracks" in url:
            body = {
                "tracks": [
                    {"id": "0dpyzcT3RMNNSd2xKBf35I", "name": "Be Sweet", "popularity": 72, "album": {"name": "Jubilee"}},
                    {"id": "0O4sIQ728ugLlwBVaxF8UM", "name": "No Score"},
                ]
            }
            return HttpResult(200, json.dumps(body), url, {})
        if "albums" in url:
            return HttpResult(200, json.dumps({"items": []}), url, {})
        if "api.spotify.com" in url:
            return HttpResult(
                200,
                json.dumps({"name": "Japanese Breakfast", "followers": {"total": 10}, "external_urls": {}}),
                url,
                {},
            )
        return HttpResult(404, "", url, {})

    result = asyncio.run(
        fetch_spotify(
            "Japanese Breakfast",
            {"spotify_id": "7MoIc5s9KXolCBH1fy9kkw"},
            env={"SPOTIFY_CLIENT_ID": "cid-value", "SPOTIFY_CLIENT_SECRET": "secret-value"},
            http=_Http(respond),
            cache=MemoryCache({}),
        )
    )
    assert result.status == "ok"
    assert [track["popularity"] for track in result.tracks] == [72, None]
    assert result.tracks[0]["album_name"] == "Jubilee"
    assert all(metric.label != "Top tracks" for metric in result.metrics)
    dumped = json.dumps(result.to_dict())
    assert "secret-value" not in dumped
    assert "cid-value" not in dumped

    seen.clear()

    def public(method, url, params, data):
        seen.append(url)
        assert "api.spotify.com" not in url
        assert "accounts.spotify.com" not in url
        if "oembed" in url:
            return HttpResult(200, json.dumps({"title": "Japanese Breakfast"}), url, {})
        if "/embed/" in url:
            return HttpResult(200, "<html></html>", url, {})
        body = """
        <p>1,234,567 monthly listeners</p>
        <span>Popular</span>
        <div aria-labelledby="listrow-title-track-spotify:track:0dpyzcT3RMNNSd2xKBf35I-0" aria-label="Be Sweet">
          <span>113,493,783</span>
        </div>
        <h2>Popular releases</h2>
        """
        return HttpResult(200, body, url, {})

    public_result = asyncio.run(
        fetch_spotify(
            "Japanese Breakfast",
            {"spotify_id": "7MoIc5s9KXolCBH1fy9kkw"},
            env={},
            http=_Http(public),
            cache=MemoryCache({}),
        )
    )
    assert public_result.tracks[0]["external_id"] == "0dpyzcT3RMNNSd2xKBf35I"
    assert public_result.tracks[0]["popularity"] is None
    assert all("api.spotify.com" not in url for url in seen)


def test_artist_page_lists_stored_tracks_and_does_not_fetch(monkeypatch, tmp_path):
    path = tmp_path / "tracks.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))

    async def seed():
        await migrate(database)
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Clairo', 'clairo', 'search', 1, '2026-11-01T00:00:00Z')"
        )
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Quiet', 'quiet', 'search', 1, '2026-11-01T00:00:00Z')"
        )
        clairo = int((await database.query_one("SELECT id FROM artists WHERE normalized_name = 'clairo'"))["id"])
        quiet = int((await database.query_one("SELECT id FROM artists WHERE normalized_name = 'quiet'"))["id"])
        await database.execute(
            """
            INSERT INTO artist_tracks (
                artist_id, source, external_id, track_name, popularity, album_name, rank, first_seen_at, last_seen_at
            ) VALUES (?, 'spotify', '0dpyzcT3RMNNSd2xKBf35I', 'Be Sweet', NULL, NULL, 1, '2026-11-01T00:00:00Z', '2026-11-06T00:00:00Z')
            """,
            (clairo,),
        )
        return clairo, quiet

    clairo, quiet = asyncio.run(seed())
    calls = []

    async def fan_out(*args, **kwargs):
        calls.append("collect")
        raise AssertionError("artist pages must not call providers")

    monkeypatch.setattr(main, "collect_artist", fan_out)
    client = TestClient(main.app)
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    page = client.get(f"/artist/{clairo}")
    assert page.status_code == 200
    assert "Be Sweet" in page.text
    assert "open.spotify.com/track/0dpyzcT3RMNNSd2xKBf35I" in page.text
    assert "unavailable" in page.text
    assert "113,493,783" not in page.text
    assert ">0<" not in page.text
    empty = client.get(f"/artist/{quiet}")
    assert "No Spotify top tracks have been stored yet" in empty.text
    assert "Be Sweet" not in empty.text
    downloaded = client.get(f"/artist/{clairo}/export.csv")
    parsed = list(csv.DictReader(io.StringIO(downloaded.text)))
    track = next(row for row in parsed if row["section"] == "track")
    assert track["title"] == "Be Sweet"
    assert track["value"] == ""
    assert track["external_id"] == "0dpyzcT3RMNNSd2xKBf35I"
    assert calls == []
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)


def test_a_track_refreshed_within_36_hours_stays_and_an_older_one_drops():
    database = SqliteDatabase(":memory:")

    async def run():
        await migrate(database)
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('A', 'a', 'search', 1, '2026-11-01T00:00:00Z')"
        )
        artist_id = int((await database.query_one("SELECT id FROM artists"))["id"])
        await database.execute(
            """
            INSERT INTO artist_tracks (
                artist_id, source, external_id, track_name, popularity, album_name, rank, first_seen_at, last_seen_at
            ) VALUES
                (?, 'spotify', 'current', 'Current', 10, NULL, 1, '2026-11-01T00:00:00Z', '2026-11-11T12:00:00Z'),
                (?, 'spotify', 'daily', 'Daily', 20, NULL, 2, '2026-11-01T00:00:00Z', '2026-11-10T00:00:00Z'),
                (?, 'spotify', 'gone', 'Gone', 30, NULL, 3, '2026-11-01T00:00:00Z', '2026-11-09T12:00:00Z')
            """,
            (artist_id, artist_id, artist_id),
        )
        rows = await database.query(TRACK_SQL, (artist_id, artist_id, 10))
        plan = await database.query("EXPLAIN QUERY PLAN " + TRACK_SQL, (artist_id, artist_id, 10))
        return rows, " ".join(str(row.get("detail") or "") for row in plan)

    rows, plan = asyncio.run(run())
    assert [row["external_id"] for row in rows] == ["current", "daily"]
    assert "idx_artist_tracks_seen" in plan
    assert "SCAN artist_tracks" not in plan
