"""Tracked-artist directory. D1 reads only."""

import asyncio

from fastapi.testclient import TestClient

import main
from crawl import reset_pool
from db import SqliteDatabase, migrate
from history import DIRECTORY_PAGE_SIZE, list_tracked_artists


def _db():
    database = SqliteDatabase(":memory:")

    async def ready():
        await migrate(database)
        return database

    return asyncio.run(ready())


def test_directory_sorts_by_name_or_exact_listeners_and_pages():
    database = _db()

    async def seed():
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Clairo', 'clairo', 'search', 1, '2026-11-01T00:00:00Z')"
        )
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('drake', 'drake', 'search', 1, '2026-11-01T00:00:00Z')"
        )
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Adele', 'adele', 'search', 1, '2026-11-01T00:00:00Z')"
        )
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Hidden', 'hidden', 'search', 0, '2026-11-01T00:00:00Z')"
        )
        clairo = await database.query_one("SELECT id FROM artists WHERE normalized_name = 'clairo'")
        adele = await database.query_one("SELECT id FROM artists WHERE normalized_name = 'adele'")
        await database.execute(
            """
            INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)
            VALUES (?, 'spotify', 'Monthly listeners', 2000000, '2,000,000', '2026-11-01T00:00:00Z'),
                   (?, 'spotify', 'Monthly listeners', 500, '1.2M', '2026-11-06T00:00:00Z'),
                   (?, 'spotify', 'Followers', 10, '10', '2026-11-06T00:00:00Z'),
                   (?, 'spotify', 'Monthly listeners', NULL, '9,000,000', '2026-11-07T00:00:00Z'),
                   (?, 'instagram', 'Followers', 4, '4', '2026-11-06T00:00:00Z')
            """,
            (clairo["id"], adele["id"], clairo["id"], clairo["id"], adele["id"]),
        )
        await database.execute(
            """
            INSERT INTO related_edges
                (from_artist_id, to_spotify_id, to_name, source, rank, first_seen_at, last_seen_at)
            VALUES (?, 'snail', 'Snail Mail', 'spotify', 1, '2026-11-01T00:00:00Z', '2026-11-06T00:00:00Z')
            """,
            (clairo["id"],),
        )
        for index in range(DIRECTORY_PAGE_SIZE):
            await database.execute(
                "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES (?, ?, 'search', 1, '2026-11-01T00:00:00Z')",
                (f"Extra {index:02d}", f"extra{index:02d}"),
            )
        by_name = await list_tracked_artists(database, page=1, sort="name")
        by_listeners = await list_tracked_artists(database, page=1, sort="listeners")
        second = await list_tracked_artists(database, page=2, sort="name")
        return by_name, by_listeners, second

    by_name, by_listeners, second = asyncio.run(seed())
    assert [row["name"] for row in by_name["artists"][:3]] == ["Adele", "Clairo", "drake"]
    assert by_name["total"] == DIRECTORY_PAGE_SIZE + 3
    assert by_name["has_next"] is True
    assert len(by_name["artists"]) == DIRECTORY_PAGE_SIZE
    assert len(second["artists"]) == 3
    assert second["has_prev"] is True
    clairo = next(row for row in by_listeners["artists"] if row["name"] == "Clairo")
    adele = next(row for row in by_listeners["artists"] if row["name"] == "Adele")
    drake = next(row for row in by_listeners["artists"] if row["name"] == "drake")
    assert by_listeners["artists"].index(clairo) < by_listeners["artists"].index(adele)
    assert by_listeners["artists"].index(adele) < by_listeners["artists"].index(drake)
    assert clairo["has_related"] is True
    assert drake["has_related"] is False
    labels = [metric["label"] for metric in clairo["metrics"]]
    assert labels[0] == "Spotify monthly listeners"
    assert clairo["metrics"][0]["text"] == "2,000,000"
    assert "1.2M" not in " ".join(metric["text"] for metric in clairo["metrics"])
    assert "Hidden" not in {row["name"] for row in by_name["artists"]}
    assert "Hidden" not in {row["name"] for row in second["artists"]}


def test_artists_page_requires_login_and_does_not_fetch(monkeypatch, tmp_path):
    path = tmp_path / "artists.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))

    async def seed():
        await migrate(database)
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Clairo', 'clairo', 'search', 1, '2026-11-01T00:00:00Z')"
        )

    asyncio.run(seed())
    calls = []

    async def fan_out(*args, **kwargs):
        calls.append("collect")
        raise AssertionError("directory must not call providers")

    monkeypatch.setattr(main, "collect_artist", fan_out)
    client = TestClient(main.app)
    locked = client.get("/artists", follow_redirects=False)
    assert locked.status_code == 303
    assert locked.headers["location"] == "/login"
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    page = client.get("/artists")
    assert page.status_code == 200
    assert "Clairo" in page.text
    assert 'href="/artist/1"' in page.text
    assert "does not call providers" in page.text
    assert "Average Analytics" in page.text
    listeners = client.get("/artists", params={"sort": "listeners"})
    assert listeners.status_code == 200
    assert "No exact audience count stored yet." in listeners.text
    assert calls == []
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)
