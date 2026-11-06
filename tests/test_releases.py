"""MusicBrainz releases on the artist page. Dates stay at the stored precision."""

import asyncio

from db import SqliteDatabase, migrate
from history import RELEASES_EMPTY, load_dashboard_history, present_releases

import main


def test_releases_sort_by_precision_and_do_not_invent_a_day():
    rows = present_releases(
        [
            {
                "mbid": "year",
                "title": "Immunity",
                "release_type": "Single",
                "released_at": "2014",
                "precision": "year",
                "first_seen_at": "2026-11-06T00:00:00Z",
                "last_seen_at": "2026-11-06T00:00:00Z",
            },
            {
                "mbid": "day",
                "title": "Sling",
                "release_type": "Album",
                "released_at": "2021-06-16",
                "precision": "day",
                "first_seen_at": "2026-01-01T00:00:00Z",
                "last_seen_at": "2026-11-06T00:00:00Z",
            },
            {
                "mbid": "blank",
                "title": "",
                "release_type": "Album",
                "released_at": "2020-01-01",
                "precision": "day",
                "first_seen_at": "2026-11-06T00:00:00Z",
                "last_seen_at": "2026-11-06T00:00:00Z",
            },
        ]
    )
    assert [row["title"] for row in rows] == ["Sling", "Immunity"]
    assert rows[0]["precision_label"] == "Exact day"
    assert rows[1]["precision_label"] == "Year only"
    assert rows[1]["released_at"] == "2014"
    assert "-01-01" not in rows[1]["released_at"]


def test_artist_history_shows_stored_releases_and_an_empty_state():
    database = SqliteDatabase(":memory:")

    async def seed():
        await migrate(database)
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Clairo', 'clairo', 'search', 1, '2026-11-01T00:00:00Z')"
        )
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Drake', 'drake', 'search', 1, '2026-11-01T00:00:00Z')"
        )
        clairo = await database.query_one("SELECT id FROM artists WHERE normalized_name = 'clairo'")
        await database.execute(
            """
            INSERT INTO musicbrainz_releases (
                artist_id, mbid, title, release_type, released_at, precision, first_seen_at, last_seen_at
            ) VALUES (?, 'rg-sling', 'Sling', 'Album', '2021-06-16', 'day', '2026-11-01T00:00:00Z', '2026-11-06T00:00:00Z')
            """,
            (clairo["id"],),
        )
        filled = await load_dashboard_history(database, "Clairo")
        empty = await load_dashboard_history(database, "Drake")
        return filled, empty

    filled, empty = asyncio.run(seed())
    assert filled["releases"][0]["title"] == "Sling"
    assert filled["releases"][0]["released_at"] == "2021-06-16"
    assert filled["releases_reason"] is None
    assert empty["releases"] == []
    assert empty["releases_reason"] == RELEASES_EMPTY
    shown = main.templates.get_template("history_cards.html").render(history=filled)
    assert "Sling" in shown
    assert "musicbrainz.org/release-group/rg-sling" in shown
    assert "Exact day" in shown
    blank = main.templates.get_template("history_cards.html").render(history=empty)
    assert "No official MusicBrainz albums" in blank
    assert "Sling" not in blank
