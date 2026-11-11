"""Playlist card reads stored rows and does not invent a follower count."""

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from crawl import reset_pool
from db import SqliteDatabase, migrate
from history import (
    PLAYLIST_NOT_CRAWLED,
    PLAYLIST_SQL,
    PLAYLIST_SPOTIFY_NOTE,
    load_dashboard_history,
    playlist_counts,
    present_playlists,
)

import main


def _db():
    database = SqliteDatabase(":memory:")

    async def ready():
        await migrate(database)
        return database

    return asyncio.run(ready())


def test_missing_follower_count_stays_missing():
    rows = present_playlists(
        [
            {
                "playlist_id": "pl1",
                "playlist_name": "Indie Mix",
                "owner_name": "Spotify",
                "follower_count": None,
                "position": None,
                "first_seen_at": "2026-11-01T00:00:00Z",
                "last_seen_at": "2026-11-06T00:00:00Z",
            },
            {
                "playlist_id": "pl2",
                "playlist_name": "With Count",
                "owner_name": "",
                "follower_count": 1200,
                "position": 4,
                "first_seen_at": "2026-11-01T00:00:00Z",
                "last_seen_at": "2026-11-05T00:00:00Z",
            },
        ]
    )
    assert rows[0]["follower_count"] is None
    assert rows[0]["follower_text"] is None
    assert rows[0]["position"] is None
    assert rows[1]["follower_text"] == "1,200"
    assert rows[1]["position"] == 4


def test_history_lists_stored_playlists_and_explains_an_empty_card():
    database = _db()

    async def seed():
        await database.execute(
            """
            INSERT INTO artists (name, normalized_name, origin, tracked, created_at)
            VALUES ('Clairo', 'clairo', 'search', 1, '2026-11-01T00:00:00Z')
            """
        )
        await database.execute(
            """
            INSERT INTO artists (name, normalized_name, origin, tracked, created_at)
            VALUES ('Drake', 'drake', 'search', 1, '2026-11-01T00:00:00Z')
            """
        )
        clairo = await database.query_one("SELECT id FROM artists WHERE normalized_name = 'clairo'")
        await database.execute(
            """
            INSERT INTO playlist_appearances (
                artist_id, playlist_id, playlist_name, owner_name, follower_count, position,
                first_seen_at, last_seen_at
            ) VALUES (?, 'abc', 'Indie Mix', 'Spotify', NULL, NULL, '2026-11-01T00:00:00Z', '2026-11-06T00:00:00Z'),
                     (?, 'older', 'Older List', 'Fans', 50, 2, '2026-11-01T00:00:00Z', '2026-11-02T00:00:00Z')
            """,
            (clairo["id"], clairo["id"]),
        )
        filled = await load_dashboard_history(database, "Clairo")
        empty = await load_dashboard_history(database, "Drake")
        return filled, empty

    filled, empty = asyncio.run(seed())
    assert [row["name"] for row in filled["playlists"]] == ["Indie Mix", "Older List"]
    assert filled["playlists"][0]["follower_count"] is None
    assert filled["playlists"][1]["follower_text"] == "50"
    assert filled["playlists_reason"] is None
    assert filled["playlist_counts"]["current"] == 1
    assert filled["playlist_counts"]["dropped"] == 1
    assert empty["playlists"] == []
    assert empty["playlist_counts"] == {"current": 0, "new": 0, "dropped": 0}
    assert empty["playlists_reason"] == PLAYLIST_NOT_CRAWLED
    assert empty["playlists_note"] == PLAYLIST_SPOTIFY_NOTE

    template = main.templates.get_template("history_cards.html")
    shown = template.render(history=filled)
    assert "Indie Mix" in shown
    assert "open.spotify.com/playlist/abc" in shown
    assert "unavailable" in shown
    assert "Older List" in shown
    assert f"{filled['playlist_counts']['current']} current · {filled['playlist_counts']['new']} new in 7 days · {filled['playlist_counts']['dropped']} dropped" in shown
    assert "does not publish which playlists" in shown
    blank = template.render(history=empty)
    assert "not been crawled yet" in blank
    assert "does not publish which playlists" in blank
    assert "Indie Mix" not in blank


def test_new_and_dropped_use_the_crawl_time_and_do_not_invent_followers():
    moment = datetime(2026, 11, 11, tzinfo=timezone.utc)
    rows = [
        {
            "playlist_id": "10",
            "playlist_name": "Pop Hits",
            "owner_name": "Deezer Charts",
            "source": "deezer",
            "editorial": 1,
            "follower_count": None,
            "position": 0,
            "first_seen_at": "2026-11-04T00:00:00Z",
            "last_seen_at": "2026-11-11T00:00:00Z",
            "crawled_at": "2026-11-11T00:00:00Z",
        },
        {
            "playlist_id": "11",
            "playlist_name": "Road Trip",
            "owner_name": "Sam",
            "source": "deezer",
            "editorial": 0,
            "follower_count": 0,
            "position": None,
            "first_seen_at": "2026-11-03T23:59:59Z",
            "last_seen_at": "2026-11-11T00:00:00Z",
            "crawled_at": "2026-11-11T00:00:00Z",
        },
        {
            "playlist_id": "12",
            "playlist_name": "Old Mix",
            "owner_name": "Sam",
            "source": "deezer",
            "editorial": False,
            "follower_count": 40,
            "position": 3,
            "first_seen_at": "2026-10-01T00:00:00Z",
            "last_seen_at": "2026-11-01T00:00:00Z",
            "crawled_at": "2026-11-11T00:00:00Z",
        },
    ]
    presented = present_playlists(rows, moment)
    counts = playlist_counts(rows, moment)
    assert presented[0]["url"] == "https://www.deezer.com/playlist/10"
    assert presented[0]["editorial"] is True
    assert presented[0]["follower_count"] is None
    assert presented[0]["follower_text"] is None
    assert presented[0]["position"] is None
    assert presented[0]["current"] is True
    assert presented[0]["first_seen_at"] == "2026-11-04T00:00:00Z"
    assert presented[1]["follower_count"] == 0
    assert presented[1]["follower_text"] == "0"
    assert presented[1]["editorial"] is False
    assert presented[1]["current"] is True
    assert presented[2]["dropped"] is True
    assert presented[2]["position"] == 3
    assert counts == {"current": 2, "new": 1, "dropped": 1}


def test_playlist_read_uses_the_artist_index_and_the_artist_page_shows_deezer_rows(monkeypatch, tmp_path):
    from db import utc_now
    database = _db()

    async def plan():
        rows = await database.query("EXPLAIN QUERY PLAN " + PLAYLIST_SQL, (1, 1, 50))
        return " ".join(str(row.get("detail") or "") for row in rows)

    detail = asyncio.run(plan())
    assert "idx_playlist_artist_seen" in detail
    assert "SCAN playlist_appearances" not in detail

    path = tmp_path / "playlists.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    stored = SqliteDatabase(str(path))

    async def seed():
        await migrate(stored)
        await stored.execute(
            """
            INSERT INTO artists (name, normalized_name, origin, tracked, created_at, deezer_id)
            VALUES ('Clairo', 'clairo', 'search', 1, '2026-11-01T00:00:00Z', '27')
            """
        )
        artist_id = int((await stored.query_one("SELECT id FROM artists WHERE normalized_name = 'clairo'"))["id"])
        moment = datetime.now(timezone.utc).replace(microsecond=0)
        crawled = utc_now(moment)
        first_seen = utc_now(moment - timedelta(days=1))
        await stored.execute(
            """
            INSERT INTO playlist_crawls (artist_id, source, crawled_at)
            VALUES (?, 'deezer', ?)
            """,
            (artist_id, crawled),
        )
        await stored.execute(
            """
            INSERT INTO playlist_appearances (
                artist_id, playlist_id, playlist_name, owner_name, follower_count, position,
                first_seen_at, last_seen_at, source, editorial
            ) VALUES
                (?, '10', 'Pop Hits', 'Deezer Charts', NULL, NULL, ?, ?, 'deezer', 1),
                (?, '12', 'Old Mix', 'Sam', 40, NULL, '2026-10-01T00:00:00Z', '2026-11-01T00:00:00Z', 'deezer', 0)
            """,
            (artist_id, first_seen, crawled, artist_id),
        )
        return artist_id, first_seen, crawled

    artist_id, first_seen, crawled = asyncio.run(seed())
    client = TestClient(main.app)
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    page = client.get(f"/artist/{artist_id}")
    assert page.status_code == 200
    assert "Pop Hits" in page.text
    assert "https://www.deezer.com/playlist/10" in page.text
    assert "Editorial" in page.text
    assert first_seen in page.text
    assert crawled in page.text
    assert "unavailable" in page.text
    assert "1 current · 1 new in 7 days · 1 dropped" in page.text
    assert "Dropped" in page.text
    assert "Old Mix" in page.text
    assert "not been crawled yet" not in page.text
    assert "does not publish which playlists" in page.text
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)
