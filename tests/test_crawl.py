import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import main
from cache import MemoryCache
from crawl import reset_pool, run_batch, run_queue_batch
from db import SqliteDatabase, migrate, utc_now
from history import audience_exact, build_growth, remember
from providers.spotify import parse_related_artists, parse_spotify_followers, visible_lines


def _db():
    database = SqliteDatabase(":memory:")

    async def ready():
        await migrate(database)
        return database

    return asyncio.run(ready())


def _bundle(listeners=1000, related=None, rounded=False):
    if related is None:
        related = [
            {"name": "Olivia Rodrigo", "spotify_id": "olivia", "rank": 1},
            {"name": "Rank Six", "spotify_id": "six", "rank": 6},
        ]
    metric = {
        "label": "Monthly listeners",
        "available": True,
        "value": "1.2M" if rounded else f"{listeners:,}",
        "exact": None if rounded else listeners,
    }
    return {
        "sources": [
            {
                "platform": "musicbrainz",
                "label": "MusicBrainz",
                "status": "ok",
                "profile_url": "https://musicbrainz.org/artist/20244d07-534f-4eff-b4d4-930878889970",
                "links": {"spotify_id": "sp-self", "apple_id": "5"},
                "metrics": [],
                "items": [],
            },
            {
                "platform": "spotify",
                "label": "Spotify",
                "status": "ok",
                "metrics": [metric],
                "items": [
                    {
                        "title": "Debut",
                        "kind": "release",
                        "url": "https://open.spotify.com/album/debut",
                        "sort_key": "2020",
                        "display": "2020",
                        "precision": "year",
                    }
                ],
                "related": related,
            },
        ]
    }


def test_rounded_listener_text_is_not_an_exact_point():
    assert audience_exact({"value": "1.2M", "exact": None}) is None
    assert audience_exact({"value": "1,000", "exact": None}) == 1000
    assert audience_exact({"value": "1.2M", "exact": 1200000}) == 1200000


def test_growth_series_keeps_only_audience_counts():
    growth = build_growth(
        [
            {"source": "spotify", "metric": "Monthly listeners", "value": 10, "captured_at": "2026-10-25T00:00:00Z"},
            {"source": "spotify", "metric": "Monthly listeners", "value": 12, "captured_at": "2026-10-25T06:00:00Z"},
            {"source": "apple", "metric": "Albums returned", "value": 4, "captured_at": "2026-10-25T06:00:00Z"},
        ]
    )
    assert growth["labels"] == ["2026-10-25T00:00:00Z", "2026-10-25T06:00:00Z"]
    assert growth["datasets"][0]["data"] == [10, 12]
    assert len(growth["datasets"]) == 1


def test_snapshots_repeat_only_after_an_hour_and_edges_keep_first_seen():
    database = _db()
    start = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    first = asyncio.run(remember(database, "Example", _bundle(), now=start, related_cap=10))
    assert first["snapshots"] == 1
    assert first["discovered"] == 1
    again = asyncio.run(remember(database, "Example", _bundle(), now=start + timedelta(minutes=10), related_cap=10))
    assert again["snapshots"] == 0
    later = asyncio.run(remember(database, "Example", _bundle(listeners=1001), now=start + timedelta(hours=2), related_cap=10))
    assert later["snapshots"] == 1
    rows = asyncio.run(
        database.query("SELECT value, captured_at FROM metric_snapshots ORDER BY captured_at")
    )
    assert [row["value"] for row in rows] == [1000, 1001]
    sightings = asyncio.run(database.query("SELECT first_seen_at FROM content_sightings"))
    assert len(sightings) == 1
    edge = asyncio.run(
        database.query_one("SELECT first_seen_at, last_seen_at, rank FROM related_edges WHERE to_spotify_id = 'olivia'")
    )
    assert edge["first_seen_at"] == utc_now(start)
    assert edge["last_seen_at"] == utc_now(start + timedelta(hours=2))
    related_people = asyncio.run(
        database.query("SELECT name FROM artists WHERE origin = 'related' ORDER BY name")
    )
    assert [row["name"] for row in related_people] == ["Olivia Rodrigo"]
    rounded = asyncio.run(
        remember(database, "Rounded", _bundle(rounded=True, related=[]), now=start, related_cap=0)
    )
    assert rounded["snapshots"] == 0


def test_batch_skips_a_fresh_artist_and_queue_acks(monkeypatch, tmp_path):
    database = _db()
    now = datetime(2026, 10, 25, 13, 0, tzinfo=timezone.utc)
    asyncio.run(
        database.execute(
            """
            INSERT INTO artists (name, normalized_name, origin, tracked, created_at, last_crawled_at)
            VALUES ('Fresh Act', 'freshact', 'search', 1, ?, ?)
            """,
            (utc_now(now), utc_now(now - timedelta(hours=1))),
        )
    )
    asyncio.run(
        database.execute(
            """
            INSERT INTO artists (name, normalized_name, spotify_id, origin, tracked, created_at)
            VALUES ('Stale Act', 'staleact', 'sp-stale', 'search', 1, ?)
            """,
            (utc_now(now),),
        )
    )
    seen = []

    async def fake_collect(name, env=None, http=None, cache=None, known_links=None):
        seen.append((name, known_links))
        return _bundle(listeners=50, related=[])

    result = asyncio.run(
        run_batch(database, {}, collect=fake_collect, cache=MemoryCache({}), now=now, limit=1)
    )
    assert result["attempted"] == 1
    assert seen == [("Stale Act", {"spotify_id": "sp-stale"})]
    second = asyncio.run(
        run_batch(database, {}, collect=fake_collect, cache=MemoryCache({}), now=now, limit=1)
    )
    assert second["attempted"] == 0
    assert seen == [("Stale Act", {"spotify_id": "sp-stale"})]

    path = tmp_path / "queue.sqlite"
    os.environ["ARTIST_ANALYTICS_SQLITE"] = str(path)
    reset_pool()
    queued = SqliteDatabase(str(path))
    asyncio.run(migrate(queued))
    asyncio.run(
        queued.execute(
            """
            INSERT INTO artists (name, normalized_name, origin, tracked, created_at)
            VALUES ('Queued Act', 'queuedact', 'search', 1, ?)
            """,
            (utc_now(now),),
        )
    )

    class Message:
        def __init__(self, body):
            self.body = body
            self.acked = False
            self.retried = False

        def ack(self):
            self.acked = True

        def retry(self):
            self.retried = True

    message = Message({"artist_id": 1})
    missing = Message({"artist_id": 99})

    class Batch:
        def __init__(self, messages):
            self.messages = messages

    asyncio.run(run_queue_batch(Batch([message]), env={}, collect=fake_collect, cache=MemoryCache({})))
    asyncio.run(run_queue_batch(Batch([missing]), env={}, collect=fake_collect, cache=MemoryCache({})))
    assert message.acked
    assert missing.acked
    stored = asyncio.run(queued.query("SELECT value FROM metric_snapshots"))
    assert stored[0]["value"] == 50
    os.environ.pop("ARTIST_ANALYTICS_SQLITE", None)
    reset_pool()


def test_related_section_stops_before_appears_on():
    page = """
    <div>Fans also like</div>
    <a href="/artist/1McMsnEElThX1knmY4oliG"><span class="name">Olivia Rodrigo</span></a>
    <a href="/artist/74KM79TiuVKeVCqs8QtB0B"><img alt=""><span>Sabrina Carpenter</span></a>
    <div>Appears On</div>
    <a href="/artist/NOTRELATED"><span>Someone Else</span></a>
    <div>165,848,234</div>
    <div>Followers</div>
    """
    related = parse_related_artists(page)
    assert [(item["name"], item["spotify_id"], item["rank"]) for item in related] == [
        ("Olivia Rodrigo", "1McMsnEElThX1knmY4oliG", 1),
        ("Sabrina Carpenter", "74KM79TiuVKeVCqs8QtB0B", 2),
    ]
    assert parse_spotify_followers(visible_lines(page)) == 165848234


class _Message:
    def __init__(self, body):
        self.body = body
        self.acked = False
        self.retried = False

    def ack(self):
        self.acked = True

    def retry(self):
        self.retried = True


class _Queue:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    async def send(self, body):
        if self.fail:
            raise RuntimeError("queue unavailable")
        self.sent.append(body)


class _D1:
    """Async stand-in for the Workers D1 binding. Statements run inline, never in a thread."""

    def __init__(self):
        import sqlite3

        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    def prepare(self, sql):
        return _D1Statement(self.conn, sql)


class _D1Statement:
    def __init__(self, conn, sql):
        self.conn = conn
        self.sql = sql
        self.params = ()

    def bind(self, *params):
        self.params = params
        return self

    async def run(self):
        cursor = self.conn.execute(self.sql, self.params)
        self.conn.commit()
        return {"meta": {"last_row_id": cursor.lastrowid}, "results": []}

    async def all(self):
        cursor = self.conn.execute(self.sql, self.params)
        return {"results": [{key: row[key] for key in row.keys()} for row in cursor.fetchall()]}


def _insert_stale(database, name, normalized, now):
    asyncio.run(
        database.execute(
            """
            INSERT INTO artists (name, normalized_name, origin, tracked, created_at)
            VALUES (?, ?, 'search', 1, ?)
            """,
            (name, normalized, utc_now(now)),
        )
    )


def test_default_batch_is_one_and_a_missing_database_skips(monkeypatch):
    from crawl import run_scheduled, settings

    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)
    assert settings(None)["batch_size"] == 1
    skipped = asyncio.run(run_scheduled(None))
    assert skipped == {"status": "skipped", "reason": "no database"}


def test_queue_send_failure_crawls_one_artist_in_process(monkeypatch, tmp_path):
    import crawl
    from crawl import run_scheduled

    now = datetime(2026, 10, 25, 13, 0, tzinfo=timezone.utc)
    path = tmp_path / "fallback.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))
    asyncio.run(migrate(database))
    _insert_stale(database, "First Act", "firstact", now)
    _insert_stale(database, "Second Act", "secondact", now)
    seen = []

    async def fake_collect(name, env=None, http=None, cache=None, known_links=None):
        seen.append(name)
        return _bundle(listeners=7, related=[])

    real_batch = crawl.run_batch

    async def wrapped(db, env, **kwargs):
        kwargs.setdefault("collect", fake_collect)
        kwargs.setdefault("cache", MemoryCache({}))
        return await real_batch(db, env, **kwargs)

    monkeypatch.setattr(crawl, "run_batch", wrapped)
    failed = _Queue(fail=True)
    result = asyncio.run(run_scheduled({"CRAWL_QUEUE": failed}))
    assert result["attempted"] == 1
    assert seen == ["First Act"]
    queued = _Queue()
    seen.clear()
    queued_result = asyncio.run(run_scheduled({"CRAWL_QUEUE": queued}))
    assert queued_result["status"] == "queued"
    assert queued_result["attempted"] == 1
    assert [body["artist_id"] for body in queued.sent] == [2]
    assert seen == []
    reset_pool()


def test_queue_consumer_refreshes_one_message_and_retries_the_rest(monkeypatch, tmp_path):
    now = datetime(2026, 10, 25, 13, 0, tzinfo=timezone.utc)
    path = tmp_path / "queue.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))
    asyncio.run(migrate(database))
    _insert_stale(database, "Queued Act", "queuedact", now)
    _insert_stale(database, "Later Act", "lateract", now)
    seen = []

    async def fake_collect(name, env=None, http=None, cache=None, known_links=None):
        seen.append(name)
        return _bundle(listeners=50, related=[])

    first = _Message({"artist_id": 1})
    second = _Message({"artist_id": 2})
    missing = _Message({"artist_id": 99})

    class Batch:
        messages = [first, second, missing]

    asyncio.run(run_queue_batch(Batch(), env={}, collect=fake_collect, cache=MemoryCache({})))
    assert first.acked and not first.retried
    assert second.retried and not second.acked
    assert missing.retried and not missing.acked
    assert seen == ["Queued Act"]
    stored = asyncio.run(database.query("SELECT value FROM metric_snapshots"))
    assert stored[0]["value"] == 50
    reset_pool()


def test_d1_binding_stores_exact_integers_asynchronously(monkeypatch):
    import db
    from history import build_growth

    binding = _D1()
    now = datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc)
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)

    async def fail_thread(*args, **kwargs):
        raise AssertionError("asyncio.to_thread was used for D1")

    monkeypatch.setattr(asyncio, "to_thread", fail_thread)

    async def fake_collect(name, env=None, http=None, cache=None, known_links=None):
        assert known_links == {"spotify_id": "sp-stale"}
        return _bundle(listeners=50, related=[])

    async def run():
        wrapped = db.D1Database(binding)
        await migrate(wrapped)
        await wrapped.execute(
            """
            INSERT INTO artists (name, normalized_name, spotify_id, origin, tracked, created_at)
            VALUES ('Stale Act', 'staleact', 'sp-stale', 'search', 1, ?)
            """,
            (utc_now(now),),
        )
        result = await run_batch(
            wrapped,
            {"DB": binding},
            collect=fake_collect,
            cache=MemoryCache({}),
            now=now,
        )
        rows = await wrapped.query("SELECT value, captured_at FROM metric_snapshots")
        return result, rows

    result, rows = asyncio.run(run())
    assert result["attempted"] == 1
    assert result["snapshots"] == 1
    assert rows[0]["value"] == 50
    growth = build_growth(
        [
            {
                "source": "spotify",
                "metric": "Monthly listeners",
                "value": rows[0]["value"],
                "captured_at": rows[0]["captured_at"],
            }
        ]
    )
    assert growth["datasets"][0]["data"] == [50]
    assert type(growth["datasets"][0]["data"][0]) is int


def test_known_links_survive_a_musicbrainz_error(monkeypatch):
    import providers
    from models import errored, unavailable

    seen = {}

    async def fail_open(*args, **kwargs):
        return errored("musicbrainz", "MusicBrainz", "down"), None

    async def grab_spotify(name, links, env, http, cache):
        seen["spotify"] = dict(links)
        return unavailable("spotify", "Spotify", "stopped")

    async def stopped(*args, **kwargs):
        return unavailable("x", "X", "stopped")

    monkeypatch.setattr(providers, "open_musicbrainz", fail_open)
    monkeypatch.setattr(providers, "fetch_spotify", grab_spotify)
    monkeypatch.setattr(providers, "fetch_apple", stopped)
    monkeypatch.setattr(providers, "fetch_youtube", stopped)
    monkeypatch.setattr(providers, "fetch_instagram", stopped)
    monkeypatch.setattr(providers, "fetch_tiktok", stopped)
    bundle = asyncio.run(
        providers.collect_artist(
            "Example",
            known_links={"spotify_id": "abc123"},
            cache=MemoryCache({}),
            http=object(),
        )
    )
    assert seen["spotify"]["spotify_id"] == "abc123"
    assert "_incomplete" not in seen["spotify"]
    assert bundle["sources"][0]["status"] == "error"


def test_admin_requires_login_and_dashboard_shows_stored_growth(monkeypatch, tmp_path):
    os.environ["ARTIST_ANALYTICS_SQLITE"] = str(tmp_path / "app.sqlite")
    reset_pool()

    async def fake_collect(name, env=None, http=None, cache=None, known_links=None):
        return _bundle()

    monkeypatch.setattr(main, "collect_artist", fake_collect)
    client = TestClient(main.app)
    locked = client.get("/admin", follow_redirects=False)
    assert locked.status_code == 303
    assert locked.headers["location"] == "/login"
    client.post(
        "/login",
        data={"email": "artist@demo.com", "password": "demo123"},
        follow_redirects=False,
    )
    page = client.get("/dashboard", params={"artist": "Example"})
    assert page.status_code == 200
    assert "Olivia Rodrigo" in page.text
    assert "growthChart" in page.text
    payload = json.loads(page.text.split('id="growth-data" type="application/json">', 1)[1].split("</script>", 1)[0])
    assert payload["datasets"][0]["data"] == [1000]
    assert "1.2M" not in page.text
    admin = client.get("/admin")
    assert admin.status_code == 200
    assert "Tracked artists" in admin.text
    assert "*/15 * * * *" in admin.text
    os.environ.pop("ARTIST_ANALYTICS_SQLITE", None)
    reset_pool()
