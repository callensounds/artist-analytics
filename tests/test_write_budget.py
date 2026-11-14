"""Unchanged crawls must not rewrite playlist, track, or related rows."""

import asyncio
import csv
import io
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import main
from crawl import reset_pool
from db import SqliteDatabase, migrate, utc_now
from export_csv import load_export_rows, render_export_csv
from history import (
    load_dashboard_history,
    present_playlists,
    remember,
    save_deezer_playlists,
)


def _db():
    database = SqliteDatabase(":memory:")

    async def ready():
        await migrate(database)
        return database

    return asyncio.run(ready())


class _Changes:
    """Runs each write on its own so SQLite changes() names that statement."""

    def __init__(self, database):
        self.database = database
        self.changes = []

    async def execute(self, sql, params=()):
        result = await self.database.execute(sql, params)
        row = await self.database.query_one("SELECT changes() AS n")
        self.changes.append((sql, int(row["n"])))
        return result

    async def batch(self, statements):
        for sql, params in statements:
            await self.execute(sql, params)

    async def query(self, sql, params=()):
        return await self.database.query(sql, params)

    async def query_one(self, sql, params=()):
        return await self.database.query_one(sql, params)


def _touched(log: _Changes, table: str) -> int:
    return sum(count for sql, count in log.changes if table in sql.split("(", 1)[0])


def _bundle(tracks, related):
    return {
        "sources": [
            {
                "platform": "spotify",
                "status": "ok",
                "metrics": [],
                "tracks": tracks,
                "tracks_confirmed": True,
                "related": related,
                "related_confirmed": True,
            }
        ]
    }


def test_unchanged_recrawl_writes_nothing_and_a_change_or_absence_does():
    database = _db()
    log = _Changes(database)
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    tracks = [
        {"external_id": "kept", "name": "Kept Song", "rank": 1, "popularity": 40, "album_name": "Album"},
        {"external_id": "gone", "name": "Gone Song", "rank": 2, "popularity": 10, "album_name": "Album"},
    ]
    related = [
        {"name": "Kept Pal", "spotify_id": "kept-pal", "rank": 1},
        {"name": "Gone Pal", "spotify_id": "gone-pal", "rank": 2},
    ]
    playlists = [
        {"id": "10", "title": "Pop Hits", "owner_name": "Deezer Charts", "follower_count": 80, "fans_known": True, "editorial": True},
        {"id": "12", "title": "Old Mix", "owner_name": "Sam", "follower_count": 40, "fans_known": True, "editorial": False},
    ]

    async def run():
        await remember(log, "Clairo", _bundle(tracks, related), now=start, related_cap=10)
        await save_deezer_playlists(log, 1, playlists, start)
        log.changes.clear()
        later = start + timedelta(days=2)
        await remember(log, "Clairo", _bundle(tracks, related), now=later, related_cap=10)
        await save_deezer_playlists(log, 1, playlists, later)
        quiet = (
            _touched(log, "artist_tracks"),
            _touched(log, "related_edges"),
            _touched(log, "playlist_appearances"),
        )
        stored_seen = await database.query_one("SELECT last_seen_at FROM playlist_appearances WHERE playlist_id = '10'")
        log.changes.clear()
        changed_at = start + timedelta(days=2, hours=1)
        await remember(
            log,
            "Clairo",
            _bundle([tracks[0]], [related[0]]),
            now=changed_at,
            related_cap=10,
        )
        await save_deezer_playlists(
            log,
            1,
            [{**playlists[0], "follower_count": 90}],
            changed_at,
        )
        writes = (
            _touched(log, "artist_tracks"),
            _touched(log, "related_edges"),
            _touched(log, "playlist_appearances"),
        )
        history = await load_dashboard_history(database, "Clairo")
        exported = await load_export_rows(database, 1)
        return quiet, stored_seen, writes, history, exported, utc_now(changed_at)

    quiet, stored_seen, writes, history, exported, confirmed = asyncio.run(run())
    assert quiet == (0, 0, 0)
    assert stored_seen["last_seen_at"] == utc_now(start)
    assert writes == (1, 1, 2)
    assert [row["name"] for row in history["tracks"]] == ["Kept Song"]
    assert history["tracks"][0]["last_seen_at"] == confirmed
    assert [row["to_name"] for row in history["related"]] == ["Kept Pal"]
    assert history["related"][0]["last_seen_at"] == confirmed
    current = [row for row in history["playlists"] if row["current"]]
    dropped = [row for row in history["playlists"] if row["dropped"]]
    assert [row["name"] for row in current] == ["Pop Hits"]
    assert current[0]["last_seen_at"] == confirmed
    assert current[0]["follower_count"] == 90
    assert [row["name"] for row in dropped] == ["Old Mix"]
    assert history["playlist_counts"] == {"current": 1, "new": 0, "dropped": 1}

    text = render_export_csv(
        [],
        [],
        exported["edges"],
        [],
        exported["tracks"],
        [],
        exported["playlists"],
    )
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert [row["title"] for row in parsed if row["section"] == "track"] == ["Kept Song"]
    assert [row["to_name"] for row in parsed if row["section"] == "related"] == ["Kept Pal"]
    pop = next(row for row in parsed if row["section"] == "playlist" and row["title"] == "Pop Hits")
    assert pop["last_seen_at"] == confirmed
    assert next(row["title"] for row in parsed if row["section"] == "playlist" and row["title"] == "Old Mix")


def test_artist_and_related_pages_keep_the_confirmed_rows(monkeypatch, tmp_path):
    path = tmp_path / "budget.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    confirmed_at = datetime(2026, 11, 1, tzinfo=timezone.utc)

    async def seed():
        await migrate(database)
        await remember(
            database,
            "Clairo",
            _bundle(
                [
                    {"external_id": "kept", "name": "Kept Song", "rank": 1, "popularity": 40, "album_name": "Album"},
                    {"external_id": "gone", "name": "Gone Song", "rank": 2, "popularity": 10, "album_name": "Album"},
                ],
                [
                    {"name": "Kept Pal", "spotify_id": "kept-pal", "rank": 1},
                    {"name": "Gone Pal", "spotify_id": "gone-pal", "rank": 2},
                ],
            ),
            now=start,
            related_cap=10,
        )
        artist_id = int((await database.query_one("SELECT id FROM artists WHERE normalized_name = 'clairo'"))["id"])
        await save_deezer_playlists(
            database,
            artist_id,
            [
                {"id": "10", "title": "Pop Hits", "owner_name": "Deezer Charts", "follower_count": 80, "fans_known": True, "editorial": True},
                {"id": "12", "title": "Old Mix", "owner_name": "Sam", "follower_count": 40, "fans_known": True, "editorial": False},
            ],
            start,
        )
        await remember(
            database,
            "Clairo",
            _bundle(
                [{"external_id": "kept", "name": "Kept Song", "rank": 1, "popularity": 40, "album_name": "Album"}],
                [{"name": "Kept Pal", "spotify_id": "kept-pal", "rank": 1}],
            ),
            now=confirmed_at,
            related_cap=10,
        )
        await save_deezer_playlists(
            database,
            artist_id,
            [{"id": "10", "title": "Pop Hits", "owner_name": "Deezer Charts", "follower_count": 80, "fans_known": True, "editorial": True}],
            confirmed_at,
        )
        return artist_id

    artist_id = asyncio.run(seed())
    client = TestClient(main.app)
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    page = client.get(f"/artist/{artist_id}")
    assert page.status_code == 200
    assert "Kept Song" in page.text
    assert "Gone Song" not in page.text
    assert "Kept Pal" in page.text
    assert "Gone Pal" not in page.text
    assert "Pop Hits" in page.text
    assert "Old Mix" in page.text
    assert "1 current · 0 new in 7 days · 1 dropped" in page.text
    assert utc_now(confirmed_at) in page.text
    assert "Dropped" in page.text

    related = client.get(f"/artist/{artist_id}/related")
    assert related.status_code == 200
    assert "Kept Pal" in related.text
    assert "Gone Pal" not in related.text
    assert f"last {utc_now(confirmed_at)}" in related.text

    downloaded = client.get(f"/artist/{artist_id}/export.csv")
    parsed = list(csv.DictReader(io.StringIO(downloaded.text)))
    assert [row["title"] for row in parsed if row["section"] == "track"] == ["Kept Song"]
    assert [row["to_name"] for row in parsed if row["section"] == "related"] == ["Kept Pal"]
    assert next(row for row in parsed if row["title"] == "Pop Hits")["last_seen_at"] == utc_now(confirmed_at)
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)


def test_a_playlist_without_an_absence_mark_still_uses_the_36_hour_window():
    moment = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
    rows = [
        {
            "playlist_id": "10",
            "playlist_name": "Still Here",
            "last_seen_at": "2026-11-10T00:00:00Z",
            "first_seen_at": "2026-10-01T00:00:00Z",
            "crawled_at": "2026-11-11T12:00:00Z",
            "source": "deezer",
        },
        {
            "playlist_id": "12",
            "playlist_name": "Gone",
            "last_seen_at": "2026-11-09T23:00:00Z",
            "first_seen_at": "2026-10-01T00:00:00Z",
            "crawled_at": "2026-11-11T12:00:00Z",
            "source": "deezer",
        },
    ]
    presented = {item["playlist_id"]: item for item in present_playlists(rows, moment)}
    assert presented["10"]["current"] is True
    assert presented["12"]["dropped"] is True
    assert presented["10"]["last_seen_at"] == "2026-11-10T00:00:00Z"
