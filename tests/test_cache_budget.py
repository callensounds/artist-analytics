"""In-isolate caches stay inside a byte budget, and heavy pages still render."""

import asyncio
import gc
import json
import time

from fastapi.testclient import TestClient

import main
from cache import (
    HISTORY_MAX_BYTES,
    HISTORY_MAX_ENTRY_BYTES,
    HistoryPageCache,
    IsolateCache,
    KVCache,
    MEMORY_MAX_BYTES,
    MEMORY_MAX_ENTRY_BYTES,
    MemoryCache,
    TieredCache,
    _HISTORY_MEMORY,
    _MEMORY,
)
from crawl import _POOL, _POOL_CAP, open_database, reset_pool
from db import SqliteDatabase, migrate
from providers.musicbrainz import _MB_INFLIGHT_CAP, _mb_inflight, _owners


def _stored_bytes(store) -> int:
    return sum(len(item[1]) for item in store.values())


def test_cache_budgets_match_the_isolate_limits():
    assert MEMORY_MAX_BYTES == 6 * 1024 * 1024
    assert MEMORY_MAX_ENTRY_BYTES == 512 * 1024
    assert HISTORY_MAX_BYTES == 4 * 1024 * 1024
    assert HISTORY_MAX_ENTRY_BYTES == 1 * 1024 * 1024


def test_memory_cache_evicts_the_oldest_entry_to_keep_the_byte_budget():
    cache = MemoryCache({}, max_bytes=10, max_entry_bytes=10, entry_cap=50)

    async def run():
        await cache.set("a", "12345", 60)
        await cache.set("b", "67890", 60)
        await cache.set("c", "abcde", 60)
        return await cache.get("a"), await cache.get("b"), await cache.get("c"), _stored_bytes(cache.store)

    missing, kept, newest, total = asyncio.run(run())
    assert missing is None
    assert kept == "67890"
    assert newest == "abcde"
    assert list(cache.store) == ["b", "c"]
    assert total <= 10


def test_refreshing_a_key_makes_it_newest():
    cache = MemoryCache({}, max_bytes=10, max_entry_bytes=10, entry_cap=50)

    async def run():
        await cache.set("a", "11111", 60)
        await cache.set("b", "22222", 60)
        await cache.set("a", "33333", 60)
        await cache.set("c", "44444", 60)
        return await cache.get("a"), await cache.get("b"), await cache.get("c")

    refreshed, oldest, newest = asyncio.run(run())
    assert refreshed == "33333"
    assert oldest is None
    assert newest == "44444"
    assert list(cache.store) == ["a", "c"]


def test_entry_cap_evicts_the_oldest_instead_of_clearing_the_cache():
    cache = IsolateCache({}, cap=2, max_bytes=1000, max_entry_bytes=100)

    async def run():
        await cache.set("a", "a", 30)
        await cache.set("b", "b", 30)
        await cache.set("c", "c", 30)
        return await cache.get("a"), await cache.get("b"), await cache.get("c")

    assert asyncio.run(run()) == (None, "b", "c")
    assert list(cache.store) == ["b", "c"]


def test_oversized_value_skips_memory_and_still_reaches_the_other_layers():
    memory = MemoryCache({}, max_bytes=100, max_entry_bytes=4)
    edge = _Layer()
    kv = _KV()
    tier = TieredCache([memory, edge, KVCache(kv, memory)])
    page_memory = IsolateCache({}, cap=4, max_bytes=100, max_entry_bytes=8)
    page_edge = _Layer()
    pages = HistoryPageCache(page_memory, page_edge)

    async def run():
        await memory.set("kept", "ok", 60)
        await tier.set("big", "hello", 60)
        await tier.set("kept", "ok", 60)
        via_tier = await tier.get("big")
        await pages.set("history", "0123456789", 90)
        via_page = await pages.get("history")
        return (
            await memory.get("big"),
            await memory.get("kept"),
            edge.saved.get("big"),
            kv.values.get("big"),
            via_tier,
            await memory.get("big"),
            await page_memory.get("history"),
            page_edge.saved.get("history"),
            via_page,
        )

    (
        memory_big,
        memory_kept,
        edge_big,
        kv_big,
        via_tier,
        memory_after_backfill,
        page_memory_value,
        page_edge_value,
        via_page,
    ) = asyncio.run(run())
    assert memory_big is None
    assert memory_kept == "ok"
    assert edge_big == "hello"
    assert kv_big == "hello"
    assert via_tier == "hello"
    assert memory_after_backfill is None
    assert page_memory_value is None
    assert page_edge_value == "0123456789"
    assert via_page == "0123456789"


def test_replacing_a_small_value_with_an_oversized_one_drops_the_stale_copy():
    cache = MemoryCache({}, max_bytes=100, max_entry_bytes=4)

    async def run():
        await cache.set("k", "ok", 60)
        await cache.set("k", "hello", 60)
        return await cache.get("k"), "k" in cache.store

    assert asyncio.run(run()) == (None, False)


def test_set_purges_expired_entries_before_it_inserts():
    cache = MemoryCache({}, max_bytes=1000, max_entry_bytes=100, entry_cap=10)

    async def run():
        await cache.set("old", "x", 60)
        await cache.set("keep", "y", 60)
        expires, value = cache.store["old"]
        cache.store["old"] = (time.monotonic() - 1, value)
        assert expires > time.monotonic() - 5
        await cache.set("new", "z", 60)
        return "old" in cache.store, await cache.get("keep"), await cache.get("new")

    present, keep, new = asyncio.run(run())
    assert present is False
    assert keep == "y"
    assert new == "z"


def test_get_drops_an_expired_entry():
    cache = IsolateCache({}, cap=4, max_bytes=100, max_entry_bytes=20)

    async def run():
        await cache.set("old", "value", 60)
        _expires, value = cache.store["old"]
        cache.store["old"] = (time.monotonic() - 1, value)
        return await cache.get("old"), "old" in cache.store

    assert asyncio.run(run()) == (None, False)


def test_default_memory_store_stays_shared_and_bounded():
    _MEMORY.clear()
    first = MemoryCache(max_bytes=10, max_entry_bytes=5, entry_cap=10)
    second = MemoryCache(max_bytes=10, max_entry_bytes=5, entry_cap=10)

    async def run():
        await first.set("a", "12345", 30)
        _MEMORY.clear()
        await second.set("b", "67890", 30)
        shared = await first.get("b")
        await second.set("c", "abcde", 30)
        await second.set("d", "fghij", 30)
        return shared, await second.get("a"), await second.get("d"), _stored_bytes(_MEMORY), list(_MEMORY)

    try:
        shared, missing, newest, total, order = asyncio.run(run())
        # The clear happened under the tally. The next insert still fits, and
        # both cache objects see the same module store.
        assert shared == "67890"
        assert missing is None
        assert newest == "fghij"
        assert total <= 10
        assert order == ["c", "d"]
    finally:
        _MEMORY.clear()
        _HISTORY_MEMORY.clear()


def test_musicbrainz_inflight_drops_stale_markers_and_keeps_a_cap():
    _mb_inflight.clear()
    try:
        _mb_inflight["old"] = 0.0
        _mb_inflight["live"] = 10**12
        assert _owners(100.0) == 1
        assert "old" not in _mb_inflight
        assert "live" in _mb_inflight
        for index in range(_MB_INFLIGHT_CAP + 5):
            _mb_inflight[f"q{index}"] = 10**12
        assert _owners(100.0) == _MB_INFLIGHT_CAP
        assert len(_mb_inflight) == _MB_INFLIGHT_CAP
    finally:
        _mb_inflight.clear()


def test_sqlite_pool_closes_the_oldest_path(tmp_path):
    reset_pool()

    async def run():
        paths = []
        for index in range(_POOL_CAP + 1):
            path = str(tmp_path / f"pool-{index}.sqlite")
            paths.append(path)
            await open_database({"ARTIST_ANALYTICS_SQLITE": path})
        return paths

    try:
        paths = asyncio.run(run())
        assert len(_POOL) == _POOL_CAP
        assert paths[0] not in _POOL
        assert paths[-1] in _POOL
    finally:
        reset_pool()


def test_heap_log_is_silent_under_the_threshold_and_never_raises(capsys, monkeypatch):
    monkeypatch.setattr(main, "_pyodide_heap_bytes", lambda: 96 * 1024 * 1024)
    main.note_isolate_heap("/timing")
    assert capsys.readouterr().out == ""

    monkeypatch.setattr(main, "_pyodide_heap_bytes", lambda: 97 * 1024 * 1024)
    main.note_isolate_heap("/timing?scope=all")
    assert capsys.readouterr().out == "mem heap_mb=97 path=/timing?scope=all\n"

    def boom():
        raise RuntimeError("no heap")

    monkeypatch.setattr(main, "_pyodide_heap_bytes", boom)
    main.note_isolate_heap("/digest")


def test_heavy_paths_are_the_large_pages_only():
    assert main.heavy_request_path("/artist/4")
    assert main.heavy_request_path("/artist/4/calendar")
    assert main.heavy_request_path("/timing")
    assert main.heavy_request_path("/timing.csv")
    assert main.heavy_request_path("/timing.json")
    assert main.heavy_request_path("/digest")
    assert main.heavy_request_path("/digest.csv")
    assert main.heavy_request_path("/digest.json")
    assert main.heavy_request_path("/compare")
    assert main.heavy_request_path("/compare.csv")
    assert main.heavy_request_path("/dashboard")
    assert not main.heavy_request_path("/artist/4/related")
    assert not main.heavy_request_path("/artist/4/export.csv")
    assert not main.heavy_request_path("/login")
    assert not main.heavy_request_path("/favicon.ico")
    assert not main.heavy_request_path("/search")


def test_heavy_routes_do_not_collect_and_keep_their_content(monkeypatch, tmp_path, capsys):
    path = tmp_path / "memory.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))

    async def seed():
        await migrate(database)
        for name in ("Ada", "Bea"):
            await database.execute(
                """
                INSERT INTO artists (name, normalized_name, origin, tracked, created_at)
                VALUES (?, ?, 'search', 1, '2026-11-01T00:00:00Z')
                """,
                (name, name.casefold()),
            )

    asyncio.run(seed())
    calls = []

    def collect():
        calls.append(1)

    async def quiet(*_args, **_kwargs):
        return None

    monkeypatch.setattr(gc, "collect", collect)
    monkeypatch.setattr(main, "enqueue_search", quiet)
    monkeypatch.setattr(main, "collect_artist", quiet)
    monkeypatch.setattr(main, "_pyodide_heap_bytes", lambda: 100 * 1024 * 1024)

    client = TestClient(main.app)
    icon = client.get("/favicon.ico")
    login_page = client.get("/login")
    assert icon.status_code == 200
    assert login_page.status_code == 200
    assert calls == []

    logged = client.post(
        "/login",
        data={"email": "artist@demo.com", "password": "demo123"},
        follow_redirects=False,
    )
    assert logged.status_code == 303
    assert calls == []

    heavy = [
        ("/dashboard", 200, "Demo Artist"),
        ("/timing", 200, "Timing"),
        ("/timing.csv", 200, "artist_id"),
        ("/timing.json", 200, '"timezone"'),
        ("/digest", 200, "Daily digest"),
        ("/digest.csv", 200, "artist_set"),
        ("/digest.json", 200, '"window_days"'),
        ("/compare", 200, "Compare"),
        ("/compare.csv?a=1&b=2", 200, "artist"),
        ("/artist/1", 200, "Ada"),
        ("/artist/1/calendar", 200, "Release calendar"),
    ]
    first = {}
    for url, status, marker in heavy:
        response = client.get(url)
        assert response.status_code == status, url
        assert marker in response.text, url
        if response.headers["content-type"].startswith("text/html"):
            assert response.text.rstrip().endswith("</html>"), url
        first[url] = _stable_body(url, response.text)
    assert calls == []
    logged_heap = capsys.readouterr().out
    heap_lines = [line for line in logged_heap.splitlines() if line.startswith("mem heap_mb=")]
    assert heap_lines == [f"mem heap_mb=100 path={url.split('?', 1)[0]}" for url, _status, _marker in heavy]
    assert "path=/favicon.ico" not in logged_heap
    assert "path=/login" not in logged_heap

    for url, status, marker in heavy:
        again = client.get(url)
        assert again.status_code == status, url
        assert _stable_body(url, again.text) == first[url], url
        assert marker in again.text
    assert calls == []
    reset_pool()


def _stable_body(url: str, text: str):
    """JSON stamps the request clock. Everything else is compared verbatim."""
    path = url.split("?", 1)[0]
    if not path.endswith(".json"):
        return text
    payload = json.loads(text)
    payload.pop("generated_at", None)
    return payload


class _Layer:
    def __init__(self):
        self.saved = {}

    async def get(self, key):
        return self.saved.get(key)

    async def set(self, key, value, ttl):
        self.saved[key] = value


class _KV:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def put(self, key, value, options=None):
        self.values[key] = value
