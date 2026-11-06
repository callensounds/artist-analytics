"""D1 artist search and the stored /artist/<id> page. No provider fan-out."""

import asyncio

from fastapi.testclient import TestClient

import main
from crawl import reset_pool
from db import SqliteDatabase, migrate, utc_now
from history import search_artists


def _db():
    database = SqliteDatabase(":memory:")

    async def ready():
        await migrate(database)
        return database

    return asyncio.run(ready())


async def _insert(database, name, normalized, tracked=1):
    await database.execute(
        """
        INSERT INTO artists (name, normalized_name, origin, tracked, created_at)
        VALUES (?, ?, 'search', ?, '2026-10-31T00:00:00Z')
        """,
        (name, normalized, tracked),
    )


def test_search_ranks_prefix_before_substring_and_skips_a_blank_query():
    database = _db()

    async def seed():
        await _insert(database, "Clairo", "clairo")
        await _insert(database, "Claire", "claire")
        await _insert(database, "Mac", "mac")
        await _insert(database, "Snail Mail", "snailmail")
        await _insert(database, "Hidden", "hidden", tracked=0)
        clairo = await database.query_one("SELECT id FROM artists WHERE normalized_name = 'clairo'")
        await database.execute(
            """
            INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)
            VALUES (?, 'spotify', 'Monthly listeners', 5000, '5,000', '2026-10-31T00:30:32Z'),
                   (?, 'spotify', 'Followers', 100, '100', '2026-10-31T00:30:33Z'),
                   (?, 'spotify', 'Followers', 180, '180', '2026-10-31T06:30:33Z'),
                   (?, 'instagram', 'Followers', 9, '9', '2026-10-31T00:30:36Z'),
                   (?, 'spotify', 'Monthly listeners', NULL, '1.2M', '2026-10-31T07:30:32Z'),
                   (?, 'tiktok', 'Likes', 40, '40', '2026-10-31T00:30:32Z')
            """,
            (clairo["id"], clairo["id"], clairo["id"], clairo["id"], clairo["id"], clairo["id"]),
        )
        blank = await search_artists(database, "   ")
        symbols = await search_artists(database, "!!!")
        one = await search_artists(database, "c")
        prefix = await search_artists(database, "clai")
        exact = await search_artists(database, "Clairo")
        inside = await search_artists(database, "mail")
        return blank, symbols, one, prefix, exact, inside

    blank, symbols, one, prefix, exact, inside = asyncio.run(seed())
    assert blank == []
    assert symbols == []
    assert [row["name"] for row in one] == ["Claire", "Clairo"]
    assert [row["name"] for row in prefix] == ["Claire", "Clairo"]
    assert [row["name"] for row in exact] == ["Clairo"]
    assert exact[0]["metrics"] == [
        {"label": "Spotify monthly listeners", "value": 5000, "text": "5,000", "captured_at": "2026-10-31T00:30:32Z"},
        {"label": "Spotify followers", "value": 180, "text": "180", "captured_at": "2026-10-31T06:30:33Z"},
    ]
    assert [row["name"] for row in inside] == ["Snail Mail"]
    assert inside[0]["metrics"] == []


def test_artist_page_reads_d1_and_unknown_names_are_not_fetched(monkeypatch, tmp_path):
    path = tmp_path / "artists.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))

    async def seed():
        await migrate(database)
        await _insert(database, "Clairo", "clairo")
        await _insert(database, "Snail Mail", "snailmail")
        clairo = await database.query_one("SELECT id FROM artists WHERE normalized_name = 'clairo'")
        snail = await database.query_one("SELECT id FROM artists WHERE normalized_name = 'snailmail'")
        await database.execute(
            """
            INSERT INTO related_edges
                (from_artist_id, to_spotify_id, to_name, to_artist_id, source, rank, first_seen_at, last_seen_at)
            VALUES (?, 'snail', 'Snail Mail', ?, 'spotify', 1, '2026-10-31T00:00:00Z', '2026-10-31T06:00:00Z')
            """,
            (clairo["id"], snail["id"]),
        )
        await database.execute(
            """
            INSERT INTO content_sightings
                (artist_id, source, external_id, title, kind, published_at, precision, first_seen_at)
            VALUES (?, 'spotify', 'charm', 'Charm', 'release', '2024', 'year', '2026-10-31T00:30:32Z')
            """,
            (clairo["id"],),
        )
        await database.execute(
            """
            INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)
            VALUES (?, 'spotify', 'Followers', 2000000, '2,000,000', '2026-10-31T00:30:32Z'),
                   (?, 'spotify', 'Followers', 2063093, '2,063,093', '2026-10-31T06:30:32Z')
            """,
            (clairo["id"], clairo["id"]),
        )
        return int(clairo["id"]), int(snail["id"])

    clairo_id, snail_id = asyncio.run(seed())
    calls = []

    async def fan_out(*args, **kwargs):
        calls.append("collect")
        raise AssertionError("artist page must not fan out providers")

    async def enqueue(*args, **kwargs):
        calls.append("enqueue")

    monkeypatch.setattr(main, "collect_artist", fan_out)
    monkeypatch.setattr(main, "enqueue_search", enqueue)
    client = TestClient(main.app)
    anon = client.get(f"/artist/{clairo_id}", follow_redirects=False)
    assert anon.status_code == 303
    assert anon.headers["location"] == "/login"
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)

    page = client.get(f"/artist/{clairo_id}")
    assert page.status_code == 200
    assert "Clairo" in page.text
    assert "Charm" in page.text
    assert "+63,093 over 6h" in page.text
    assert f'href="/artist/{snail_id}"' in page.text
    assert "does not fetch providers" in page.text
    assert calls == []

    slug = client.get("/artist/clairo", follow_redirects=False)
    assert slug.status_code == 303
    assert slug.headers["location"] == f"/artist/{clairo_id}"

    missing = client.get("/artist/99999")
    assert missing.status_code == 404
    assert "not tracked" in missing.text
    assert calls == []

    found = client.get("/search", params={"q": "clai"})
    assert found.status_code == 200
    assert f'href="/artist/{clairo_id}"' in found.text
    assert "2,063,093" in found.text
    assert "1.2M" not in found.text
    cheap = client.get("/search", params={"q": ""})
    assert cheap.status_code == 200
    assert "Type a name" in cheap.text
    assert calls == []
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)


def test_search_requires_login():
    client = TestClient(main.app)
    locked = client.get("/search", follow_redirects=False)
    assert locked.status_code == 303
    assert locked.headers["location"] == "/login"
