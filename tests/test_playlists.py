"""Playlist card reads stored rows and does not invent a follower count."""

import asyncio

from db import SqliteDatabase, migrate
from history import PLAYLIST_UNAVAILABLE, load_dashboard_history, present_playlists

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
    assert empty["playlists"] == []
    assert empty["playlists_reason"] == PLAYLIST_UNAVAILABLE

    template = main.templates.get_template("history_cards.html")
    shown = template.render(history=filled)
    assert "Indie Mix" in shown
    assert "open.spotify.com/playlist/abc" in shown
    assert "unavailable" in shown
    assert "Older List" in shown
    blank = template.render(history=empty)
    assert "does not publish which playlists" in blank
    assert "Indie Mix" not in blank
