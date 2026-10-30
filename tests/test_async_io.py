"""The request path stays async: no run_sync, no time.sleep, no sync HTTP, no shared asyncio primitives."""

from __future__ import annotations

import ast
import asyncio
import json
import sys
import time
import types
from pathlib import Path

import httpx
import pytest

import http_client
from http_client import HttpClient, HttpResult, TransportError, WorkersFetchTransport

SRC = Path(__file__).resolve().parents[1] / "src"
SOURCES = sorted(SRC.rglob("*.py"))
SYNC_HTTP = {"requests", "urllib3", "httpx", "aiohttp", "pycurl"}
PRIMITIVES = {"Lock", "Semaphore", "BoundedSemaphore", "Event", "Condition", "Queue", "Barrier"}


def _dotted(node) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(SRC)))
def test_source_has_no_blocking_calls_or_sync_http(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            assert not name.endswith("run_sync"), f"{path.name}:{node.lineno} calls {name}"
            assert name != "time.sleep", f"{path.name}:{node.lineno} calls time.sleep"
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in SYNC_HTTP, f"{path.name}:{node.lineno}"
            names = {alias.name for alias in node.names}
            assert not ({"run_sync", "sleep"} & names and node.module in {"pyodide.ffi", "time"}), path.name
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in SYNC_HTTP, f"{path.name}:{node.lineno} imports {alias.name}"


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(SRC)))
def test_no_asyncio_primitives_at_import_time(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        targets = []
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = [node.value]
        for value in targets:
            for inner in ast.walk(value):
                if isinstance(inner, ast.Call):
                    name = _dotted(inner.func)
                    assert name.split(".")[-1] not in PRIMITIVES, f"{path.name}:{node.lineno} builds {name} at import"


def _awaits_while_unwinding(nodes) -> list[int]:
    """Await / async-with nested in an except or finally, ignoring nested function bodies."""
    lines = []
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(node, (ast.Await, ast.AsyncWith)):
            lines.append(node.lineno)
            continue
        lines.extend(_awaits_while_unwinding(list(ast.iter_child_nodes(node))))
    return lines


def test_no_await_while_an_exception_is_propagating():
    """Awaiting a JS promise from except, finally, or __aexit__ raises the promising-task SystemError."""
    offenders = []
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncWith):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno} async with")
            blocks = []
            if isinstance(node, ast.ExceptHandler):
                blocks.append(node.body)
            elif isinstance(node, ast.Try) and node.finalbody:
                blocks.append(node.finalbody)
            for block in blocks:
                for line in _awaits_while_unwinding(block):
                    offenders.append(f"{path.relative_to(SRC)}:{line}")
    assert offenders == []


def test_requests_is_not_a_runtime_dependency():
    pyproject = (SRC.parent / "pyproject.toml").read_text(encoding="utf-8")
    runtime = pyproject.split("[dependency-groups]", 1)[0]
    for name in ("requests", "urllib3", "httpx"):
        assert f'"{name}' not in runtime


# A fake of the JS fetch API, shaped like the Pyodide proxies the Worker sees.


class _Bytes:
    def __init__(self, data: bytes):
        self.data = data

    def to_bytes(self) -> bytes:
        return self.data


class _Part:
    def __init__(self, data: bytes | None):
        self.done = data is None
        self.value = None if data is None else _Bytes(data)


class _Reader:
    def __init__(self, chunks, delay=0.0):
        self.chunks = list(chunks)
        self.delay = delay
        self.cancelled = False

    async def read(self):
        await asyncio.sleep(self.delay)
        if not self.chunks:
            return _Part(None)
        return _Part(self.chunks.pop(0))

    async def cancel(self):
        self.cancelled = True


class _Body:
    def __init__(self, reader):
        self.reader = reader

    def getReader(self):
        return self.reader


class _Headers:
    def __init__(self, pairs):
        self.pairs = pairs

    def entries(self):
        return iter([list(pair) for pair in self.pairs])


class _Response:
    def __init__(self, status, url, chunks, headers=(), delay=0.0):
        self.status = status
        self.url = url
        self.headers = _Headers(headers)
        self.reader = _Reader(chunks, delay)
        self.body = _Body(self.reader)


class _Controller:
    made: list["_Controller"] = []

    def __init__(self):
        self.signal = object()
        self.aborted = False

    @classmethod
    def new(cls):
        controller = cls()
        cls.made.append(controller)
        return controller

    def abort(self):
        self.aborted = True


class FakeJsFetch:
    def __init__(self, router, latency=0.02):
        self.router = router
        self.latency = latency
        self.calls = []
        self.in_flight = 0
        self.peak = 0

    async def __call__(self, url, init):
        self.calls.append((url, init))
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.latency)
            outcome = self.router(url, init)
            if isinstance(outcome, _Response):
                return outcome
            status, body = outcome
            data = body if isinstance(body, bytes) else body.encode()
            return _Response(status, url, [data[i : i + 4096] for i in range(0, len(data), 4096)])
        finally:
            self.in_flight -= 1


def _transport(fetch):
    return WorkersFetchTransport(
        fetch=fetch,
        to_js=lambda value, **_: value,
        abort_controller=_Controller,
        object_from_entries=dict,
    )


@pytest.fixture
def forbid_blocking(monkeypatch):
    """Any run_sync, time.sleep, or to_thread on the request path fails the test."""
    calls = []

    def run_sync(*args, **kwargs):
        calls.append("run_sync")
        raise AssertionError("pyodide.ffi.run_sync was called on the request path")

    def blocking_sleep(*args, **kwargs):
        calls.append("time.sleep")
        raise AssertionError("time.sleep was called on the request path")

    async def to_thread(*args, **kwargs):
        calls.append("to_thread")
        raise AssertionError("asyncio.to_thread was called on the Workers request path")

    pyodide = types.ModuleType("pyodide")
    ffi = types.ModuleType("pyodide.ffi")
    ffi.run_sync = run_sync
    ffi.to_js = lambda value, **_: value
    pyodide.ffi = ffi
    monkeypatch.setitem(sys.modules, "pyodide", pyodide)
    monkeypatch.setitem(sys.modules, "pyodide.ffi", ffi)
    monkeypatch.setattr(time, "sleep", blocking_sleep)
    monkeypatch.setattr(asyncio, "to_thread", to_thread)
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.setattr("providers.musicbrainz.MB_INTERVAL", 0.01)
    monkeypatch.setattr("providers.musicbrainz._mb_next", 0.0)
    yield calls
    assert calls == []


def _router(url, init):
    if "musicbrainz.org/ws/2/artist/?" in url:
        return 200, json.dumps({"artists": [{"id": "mb-1", "name": "Clairo", "score": 100}]})
    if "musicbrainz.org/ws/2/artist/mb-1" in url:
        return 200, json.dumps({"id": "mb-1", "name": "Clairo", "relations": []})
    if "musicbrainz.org/ws/2/release-group" in url:
        return 200, json.dumps({"release-groups": []})
    if "musicbrainz.org/ws/2/release" in url:
        return 200, json.dumps({"releases": []})
    return 404, "not found"


def test_workers_fetch_path_never_blocks(forbid_blocking, monkeypatch):
    from cache import MemoryCache, TieredCache
    from providers import collect_artist

    fetch = FakeJsFetch(_router)
    monkeypatch.setattr(http_client, "default_transport", lambda: _transport(fetch))

    async def run():
        return await asyncio.gather(
            *(collect_artist("Clairo", cache=TieredCache([MemoryCache({})])) for _ in range(8))
        )

    bundles = asyncio.run(run())
    assert len(bundles) == 8
    for bundle in bundles:
        assert [source["platform"] for source in bundle["sources"]] == [
            "musicbrainz",
            "apple",
            "spotify",
            "youtube",
            "instagram",
            "tiktok",
        ]
    assert fetch.calls, "the Workers fetch transport was not used"
    # Eight dashboards overlapped on one loop instead of running one fetch at a time.
    assert fetch.peak > 1
    url, init = fetch.calls[0]
    assert init["method"] == "GET"
    assert init["redirect"] in {"follow", "manual"}
    assert init["signal"] is not None
    assert init["headers"]["User-Agent"].split("/")[0] in {"ArtistAnalytics", "AverageAnalytics"}


def test_concurrent_app_burst_on_workers_path(forbid_blocking, monkeypatch):
    import main

    fetch = FakeJsFetch(_router)
    monkeypatch.setattr(http_client, "default_transport", lambda: _transport(fetch))

    async def run():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            login = await client.post("/login", data={"email": "artist@demo.com", "password": "demo123"})
            assert login.status_code == 303
            artists = ["Clairo", "Billie Eilish", "Bad Bunny", "Taylor Swift", "Drake"]
            requests = [client.get("/dashboard", params={"artist": name}) for name in artists * 10]
            return await asyncio.gather(*requests)

    responses = asyncio.run(run())
    assert len(responses) == 50
    assert [r.status_code for r in responses] == [200] * len(responses)


def test_concurrent_dashboards_with_async_d1_do_not_block(forbid_blocking, monkeypatch):
    import sqlite3

    import main
    from db import D1Database

    class Statement:
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

    class Database:
        def __init__(self):
            self.conn = sqlite3.connect(":memory:")
            self.conn.row_factory = sqlite3.Row

        def prepare(self, sql):
            return Statement(self.conn, sql)

    binding = Database()
    from db import migrate

    asyncio.run(migrate(D1Database(binding)))

    class Env:
        DB = binding

    fetch = FakeJsFetch(_router)
    monkeypatch.setattr(http_client, "default_transport", lambda: _transport(fetch))
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)

    async def application(scope, receive, send):
        if scope.get("type") == "http":
            scope = dict(scope)
            scope["env"] = Env()
        await main.app(scope, receive, send)

    async def run():
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            login = await client.post("/login", data={"email": "artist@demo.com", "password": "demo123"})
            assert login.status_code == 303
            artists = ["Clairo", "Billie Eilish", "Bad Bunny", "Taylor Swift", "Drake"]
            return await asyncio.gather(
                *(client.get("/dashboard", params={"artist": name}) for name in artists * 10)
            )

    responses = asyncio.run(run())
    assert len(responses) == 50
    assert [r.status_code for r in responses] == [200] * len(responses)
    assert all("growth-data" in response.text for response in responses)
    stored = asyncio.run(D1Database(binding).query("SELECT COUNT(*) AS n FROM artists"))
    assert stored[0]["n"] == 0


def test_body_is_capped_and_the_stream_cancelled(forbid_blocking):
    response = _Response(200, "https://example.test/big", [b"a" * 1000] * 50)
    fetch = FakeJsFetch(lambda url, init: response, latency=0)
    client = HttpClient(intervals={}, transport=_transport(fetch))

    result = asyncio.run(client.get_text("https://example.test/big", max_bytes=2500))
    assert len(result.text) == 2500
    assert result.truncated
    assert response.reader.cancelled


def test_truncated_json_is_not_parsed(forbid_blocking):
    body = json.dumps({"rows": ["x" * 100] * 100})
    fetch = FakeJsFetch(lambda url, init: (200, body), latency=0)
    client = HttpClient(intervals={}, transport=_transport(fetch))
    result, payload = asyncio.run(client.get_json("https://example.test/json", max_bytes=500))
    assert result.truncated and payload is None


def test_hung_fetch_times_out_and_aborts(forbid_blocking):
    _Controller.made.clear()

    async def never(url, init):
        await asyncio.sleep(60)

    client = HttpClient(intervals={}, transport=_transport(never))

    async def run():
        started = asyncio.get_running_loop().time()
        with pytest.raises(TransportError):
            await client.get_text("https://example.test/slow", timeout=0.2)
        return asyncio.get_running_loop().time() - started

    assert asyncio.run(run()) < 1.0
    assert _Controller.made and _Controller.made[-1].aborted


def test_deadline_clamps_every_call(forbid_blocking):
    async def run():
        loop = asyncio.get_running_loop()
        client = HttpClient(intervals={}, transport=_transport(FakeJsFetch(_router)), deadline=loop.time() - 1)
        with pytest.raises(TransportError):
            await client.get_text("https://example.test/")

    asyncio.run(run())


def test_one_hung_provider_cannot_hold_the_dashboard(forbid_blocking, monkeypatch):
    from cache import MemoryCache
    from providers import collect_artist

    async def hang_or_route(url, init):
        if "itunes.apple.com" in url or "music.apple.com" in url:
            await asyncio.sleep(60)
        return await FakeJsFetch(_router, latency=0)(url, init)

    monkeypatch.setattr(http_client, "default_transport", lambda: _transport(hang_or_route))

    async def run():
        loop = asyncio.get_running_loop()
        started = loop.time()
        bundle = await collect_artist("Clairo", cache=MemoryCache({}), deadline_s=1.5)
        return bundle, loop.time() - started

    bundle, elapsed = asyncio.run(run())
    assert elapsed < 2.5
    apple = next(source for source in bundle["sources"] if source["platform"] == "apple")
    assert apple["status"] in {"error", "unavailable"}


def test_a_crashing_provider_becomes_an_error_card(forbid_blocking, monkeypatch):
    import providers
    from cache import MemoryCache

    async def boom(*args, **kwargs):
        raise RuntimeError("provider bug")

    monkeypatch.setattr(providers, "fetch_tiktok", boom)
    monkeypatch.setattr(http_client, "default_transport", lambda: _transport(FakeJsFetch(_router, latency=0)))
    bundle = asyncio.run(providers.collect_artist("Clairo", cache=MemoryCache({})))
    tiktok = next(source for source in bundle["sources"] if source["platform"] == "tiktok")
    assert tiktok["status"] == "error"


def test_musicbrainz_queue_fails_fast_instead_of_piling_up(forbid_blocking, monkeypatch):
    from providers import musicbrainz

    async def run():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(musicbrainz, "_mb_next", loop.time() + 30)
        started = loop.time()
        with pytest.raises(musicbrainz.MusicBrainzBusy):
            await musicbrainz._mb_slot(None)
        return loop.time() - started

    assert asyncio.run(run()) < 0.1


def test_cpython_transport_is_refused_on_workers(monkeypatch):
    monkeypatch.setattr(http_client, "IS_WORKERS", True)
    with pytest.raises(RuntimeError):
        asyncio.run(http_client.CPythonTransport().send("GET", "https://example.test/", {}, None, True, 10, 1))


def test_kv_and_edge_cache_calls_are_awaited():
    from cache import EdgeCache, KVCache

    class FakeKV:
        def __init__(self):
            self.store = {}

        async def get(self, key):
            return self.store.get(key)

        async def put(self, key, value, options=None):
            self.store[key] = value

    kv = KVCache(FakeKV())
    asyncio.run(kv.set("k", "v", 60))
    assert asyncio.run(KVCache(kv.kv).get("k")) == "v"

    class FakeDefault:
        async def match(self, request):
            return None

    class FakeCaches:
        default = FakeDefault()

    edge = EdgeCache(api=FakeCaches())
    edge._request = staticmethod(lambda key: key)
    assert asyncio.run(edge.get("k")) is None


def test_concurrent_lookups_for_one_artist_share_one_musicbrainz_search(forbid_blocking, monkeypatch):
    from cache import MemoryCache, TieredCache
    from providers import collect_artist

    fetch = FakeJsFetch(_router, latency=0.05)
    monkeypatch.setattr(http_client, "default_transport", lambda: _transport(fetch))
    monkeypatch.setattr("providers.musicbrainz._mb_inflight", {})
    store = TieredCache([MemoryCache({})])

    async def run():
        return await asyncio.gather(*(collect_artist("Clairo", cache=store) for _ in range(6)))

    bundles = asyncio.run(run())
    searches = [url for url, _ in fetch.calls if "musicbrainz.org/ws/2/artist/?" in url]
    assert len(searches) == 1
    for bundle in bundles:
        assert bundle["sources"][0]["status"] == "ok"


def test_musicbrainz_owner_cap_keeps_lookups_finishable(forbid_blocking, monkeypatch):
    import providers.musicbrainz as mb

    monkeypatch.setattr(mb, "_mb_inflight", {})
    calls = []

    async def fake_match(name, http):
        calls.append(name)
        await asyncio.sleep(0.3)
        return mb.SourceResult(platform="musicbrainz", label="MusicBrainz", status="ok", profile_name=name)

    monkeypatch.setattr(mb, "_match", fake_match)
    monkeypatch.setattr(mb, "MB_OPEN_TIMEOUT", 1.0)

    async def run():
        names = ["A", "B", "C", "D"]
        return await asyncio.gather(
            *(mb._match_or_wait(n, None, None, n.lower()) for n in names), return_exceptions=True
        )

    started = time.monotonic()
    results = asyncio.run(run())
    assert time.monotonic() - started < 1.5
    # Never more than MB_MAX_OWNERS lookups at once; the rest wait and then take a turn or fail fast.
    assert calls[:2] == ["A", "B"]
    assert all(isinstance(r, (tuple, mb.MusicBrainzBusy)) for r in results)
