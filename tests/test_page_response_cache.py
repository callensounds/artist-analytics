"""Finished heavy GETs are reused per user. Failures and other users are not."""

import json
from base64 import b64encode

import httpx
from fastapi.testclient import TestClient

import main
import page_cache
from page_cache import (
    PAGE_BROWSER_MAX_AGE,
    PAGE_CACHE_ENTRY_CAP,
    PAGE_CACHE_MAX_BYTES,
    PAGE_CACHE_MAX_ENTRY_BYTES,
    PAGE_CACHE_TTL,
    HeavyResponseCache,
    PageMemory,
    StoredPage,
    cache_key_from_scope,
    client_cache_control,
    edge_cache_control,
    hit_headers,
    is_page_cache_get,
    response_cacheable,
)
from runtime import session_signer


def _seal(payload: dict) -> str:
    signed = session_signer({"type": "http"}).sign(b64encode(json.dumps(payload).encode()))
    return signed.decode("ascii")


def _scope(path="/timing", query=b"", cookie=None):
    headers = []
    if cookie:
        headers.append((b"cookie", f"session={cookie}".encode("ascii")))
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query if isinstance(query, bytes) else query.encode(),
        "headers": headers,
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
    }


def _app(body, *, status=200, extra=None, calls=None):
    async def app(scope, receive, send):
        if calls is not None:
            calls["n"] += 1
            if calls.get("per_key"):
                key = cache_key_from_scope(scope) or "none"
                payload = f"body:{key}".encode()
            else:
                payload = body
        else:
            payload = body
        headers = [(b"content-type", b"text/html; charset=utf-8")]
        if extra:
            headers.extend(extra)
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": payload})

    return app


async def _get(app, path, *, cookie=None, method="GET"):
    transport = httpx.ASGITransport(app=app)
    headers = {}
    if cookie:
        headers["cookie"] = f"session={cookie}"
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=headers)


def test_page_cache_budgets_are_small_and_the_client_header_is_private():
    assert PAGE_CACHE_TTL == 300
    assert PAGE_BROWSER_MAX_AGE == 60
    assert PAGE_BROWSER_MAX_AGE < PAGE_CACHE_TTL
    assert PAGE_CACHE_MAX_BYTES == 2 * 1024 * 1024
    assert PAGE_CACHE_MAX_ENTRY_BYTES == 512 * 1024
    assert PAGE_CACHE_MAX_ENTRY_BYTES <= PAGE_CACHE_MAX_BYTES
    assert PAGE_CACHE_ENTRY_CAP == 8
    assert client_cache_control(cacheable=True) == "private, max-age=60"
    assert "public" not in client_cache_control(cacheable=True)
    assert client_cache_control(cacheable=False) == "no-store"
    assert edge_cache_control().startswith("public, max-age=300")
    served = dict(hit_headers(StoredPage(b"<html></html>", "text/html", 'attachment; filename="timing.csv"')))
    assert served[b"x-aa-cache"] == b"hit"
    assert b"public" not in served[b"cache-control"]
    assert served[b"cache-control"] == b"private, max-age=60"
    assert served[b"content-disposition"] == b'attachment; filename="timing.csv"'


def test_page_memory_evicts_oldest_and_refuses_an_oversized_body():
    memory = PageMemory(max_bytes=10, max_entry_bytes=10, entry_cap=5)
    memory.set("a", StoredPage(b"12345", "text/plain", ""), 60)
    memory.set("b", StoredPage(b"67890", "text/plain", ""), 60)
    memory.set("c", StoredPage(b"abcde", "text/plain", ""), 60)
    assert memory.get("a") is None
    assert memory.get("b").body == b"67890"
    assert memory.get("c").body == b"abcde"
    assert memory.total_bytes <= 10
    memory.set("d", StoredPage(b"x" * 11, "text/plain", ""), 60)
    assert memory.get("d") is None
    assert "b" in memory.store

    capped = PageMemory(max_bytes=100, max_entry_bytes=20, entry_cap=2)
    capped.set("a", StoredPage(b"a", "text/plain", ""), 30)
    capped.set("b", StoredPage(b"b", "text/plain", ""), 30)
    capped.set("c", StoredPage(b"c", "text/plain", ""), 30)
    assert capped.get("a") is None
    assert list(capped.store) == ["b", "c"]


def test_page_memory_drops_an_expired_entry():
    import time

    memory = PageMemory(max_bytes=100, max_entry_bytes=20, entry_cap=4)
    memory.set("old", StoredPage(b"value", "text/plain", ""), 60)
    expires, page = memory.store["old"]
    memory.store["old"] = (time.monotonic() - 1, page)
    assert expires > 0
    assert memory.get("old") is None
    assert "old" not in memory.store


def test_cache_keys_separate_users_queries_and_anonymous():
    demo = {"user_id": 1, "auth": "d1", "email": "artist@demo.com", "artist_name": "Demo Artist"}
    other = {"user_id": 2, "auth": "d1", "email": "other@example.com", "artist_name": "Other"}
    memory_user = {"user_id": 1, "auth": "memory", "email": "artist@demo.com", "artist_name": "Demo Artist"}
    demo_cookie = _seal(demo)
    other_cookie = _seal(other)

    anon = cache_key_from_scope(_scope())
    signed = cache_key_from_scope(_scope(cookie=demo_cookie))
    second = cache_key_from_scope(_scope(cookie=other_cookie))
    memory_key = cache_key_from_scope(_scope(cookie=_seal(memory_user)))
    ordered = cache_key_from_scope(_scope(query=b"scope=all&days=all&kind=all", cookie=demo_cookie))
    flipped = cache_key_from_scope(_scope(query=b"kind=all&days=all&scope=all", cookie=demo_cookie))
    narrower = cache_key_from_scope(_scope(query=b"scope=watchlist&days=all&kind=all", cookie=demo_cookie))
    other_path = cache_key_from_scope(_scope("/digest", cookie=demo_cookie))

    assert anon != signed
    assert signed != second
    assert signed != memory_key
    assert "anon" in anon
    assert "d1:1" in signed
    assert "d1:2" in second
    assert "memory:1" in memory_key
    assert ordered == flipped
    assert ordered != narrower
    assert signed != other_path
    for key in (anon, signed, second, memory_key, ordered):
        assert demo_cookie not in key
        assert other_cookie not in key
        assert "artist@demo.com" not in key
        assert "other@example.com" not in key
        assert "Demo Artist" not in key
    assert cache_key_from_scope(_scope(query=b"a=1&b=2", cookie=demo_cookie)) == cache_key_from_scope(
        _scope(query=b"b=2&a=1", cookie=demo_cookie)
    )
    assert is_page_cache_get("GET", "/timing", lambda path: True)
    assert not is_page_cache_get("POST", "/timing", lambda path: True)
    assert not is_page_cache_get("GET", "/login", lambda path: True)
    assert not is_page_cache_get("GET", "/logout", lambda path: True)
    assert not is_page_cache_get("GET", "/signup", lambda path: True)
    assert not is_page_cache_get("GET", "/register", lambda path: True)
    assert not is_page_cache_get("GET", "/favicon.ico", main.heavy_request_path)


def test_response_cacheable_rejects_errors_cookies_csrf_and_oversized_bodies():
    headers = [(b"content-type", b"text/html; charset=utf-8")]
    assert response_cacheable(200, headers, b"<html>ok</html>")
    assert not response_cacheable(503, headers, b"<html>ok</html>")
    assert not response_cacheable(303, headers, b"")
    assert not response_cacheable(404, headers, b"missing")
    assert not response_cacheable(200, headers, b"")
    assert not response_cacheable(200, headers + [(b"set-cookie", b"session=abc")], b"<html>ok</html>")
    assert not response_cacheable(200, [(b"Set-Cookie", b"session=abc")], b"<html>ok</html>")
    assert not response_cacheable(200, headers, b"<html>Data temporarily unavailable.</html>")
    assert not response_cacheable(200, headers, b'<form><input name="csrf_token" value="x"></form>')
    assert not response_cacheable(200, headers, b'<div class="flash-message">Saved</div>')
    assert not response_cacheable(200, headers, b"x" * (PAGE_CACHE_MAX_ENTRY_BYTES + 1))
    assert not response_cacheable(
        200,
        headers + [(b"cache-control", b"no-store")],
        b"<html>ok</html>",
    )


def test_middleware_reuses_one_users_page_and_not_anothers():
    calls = {"n": 0, "per_key": True}
    app = HeavyResponseCache(_app(b"", calls=calls), is_heavy=lambda path: path == "/timing")
    demo = _seal({"user_id": 1, "auth": "d1", "email": "artist@demo.com"})
    other = _seal({"user_id": 2, "auth": "d1", "email": "other@example.com"})

    import asyncio

    async def run():
        anonymous = await _get(app, "/timing")
        first = await _get(app, "/timing", cookie=demo)
        second = await _get(app, "/timing", cookie=demo)
        flipped = await _get(app, "/timing?days=all&scope=all", cookie=demo)
        same = await _get(app, "/timing?scope=all&days=all", cookie=demo)
        someone = await _get(app, "/timing", cookie=other)
        return anonymous, first, second, flipped, same, someone

    anonymous, first, second, flipped, same, someone = asyncio.run(run())
    assert anonymous.status_code == 200
    assert anonymous.headers["x-aa-cache"] == "miss"
    assert first.headers["x-aa-cache"] == "miss"
    assert second.headers["x-aa-cache"] == "hit"
    assert second.content == first.content
    assert first.content != anonymous.content
    assert "d1:1" in first.text
    assert "anon" in anonymous.text
    assert flipped.headers["x-aa-cache"] == "miss"
    assert same.headers["x-aa-cache"] == "hit"
    assert same.content == flipped.content
    assert someone.headers["x-aa-cache"] == "miss"
    assert someone.content != first.content
    assert "d1:2" in someone.text
    assert calls["n"] == 4
    assert second.headers["cache-control"] == "private, max-age=60"
    assert "public" not in second.headers["cache-control"]


def test_middleware_does_not_cache_set_cookie_post_login_or_errors():
    import asyncio

    async def run():
        cookie_calls = {"n": 0}
        cookie_app = HeavyResponseCache(
            _app(b"<html>secret</html>", extra=[(b"set-cookie", b"session=fresh")], calls=cookie_calls),
            is_heavy=lambda path: True,
        )
        posted_calls = {"n": 0}
        posted_app = HeavyResponseCache(_app(b"<html>posted</html>", calls=posted_calls), is_heavy=lambda path: True)
        error_calls = {"n": 0}
        error_app = HeavyResponseCache(
            _app(b"Data temporarily unavailable.\n", status=503, calls=error_calls),
            is_heavy=lambda path: True,
        )
        csrf_calls = {"n": 0}
        csrf_app = HeavyResponseCache(
            _app(b'<html><input name="csrf_token" value="abc"></html>', calls=csrf_calls),
            is_heavy=lambda path: True,
        )
        login_calls = {"n": 0}
        login_app = HeavyResponseCache(_app(b"<html>login</html>", calls=login_calls), is_heavy=lambda path: True)

        cookie_first = await _get(cookie_app, "/timing")
        cookie_second = await _get(cookie_app, "/timing")
        post_first = await _get(posted_app, "/timing", method="POST")
        post_second = await _get(posted_app, "/timing", method="POST")
        login_first = await _get(login_app, "/login")
        login_second = await _get(login_app, "/logout")
        error_first = await _get(error_app, "/digest.csv")
        error_second = await _get(error_app, "/digest.csv")
        csrf_first = await _get(csrf_app, "/dashboard")
        csrf_second = await _get(csrf_app, "/dashboard")
        return {
            "cookie": (cookie_calls["n"], cookie_first, cookie_second),
            "post": (posted_calls["n"], post_first, post_second),
            "login": login_calls["n"],
            "login_headers": (login_first.headers, login_second.headers),
            "error": (error_calls["n"], error_first, error_second),
            "csrf": (csrf_calls["n"], csrf_first, csrf_second),
        }

    result = asyncio.run(run())
    assert result["cookie"][0] == 2
    assert result["cookie"][1].headers["x-aa-cache"] == "miss"
    assert result["cookie"][2].headers["x-aa-cache"] == "miss"
    assert result["cookie"][1].headers["cache-control"] == "no-store"
    assert result["post"][0] == 2
    assert "x-aa-cache" not in result["post"][1].headers
    assert "x-aa-cache" not in result["post"][2].headers
    assert result["login"] == 2
    assert "x-aa-cache" not in result["login_headers"][0]
    assert "x-aa-cache" not in result["login_headers"][1]
    assert result["error"][0] == 2
    assert result["error"][1].status_code == 503
    assert result["error"][1].headers["x-aa-cache"] == "miss"
    assert result["error"][2].headers["x-aa-cache"] == "miss"
    assert result["error"][1].headers["cache-control"] == "no-store"
    assert result["csrf"][0] == 2
    assert result["csrf"][1].headers["x-aa-cache"] == "miss"
    assert result["csrf"][2].headers["x-aa-cache"] == "miss"


def test_middleware_fails_open_and_can_serve_an_edge_hit_as_private(monkeypatch):
    import asyncio

    calls = {"n": 0}
    app = HeavyResponseCache(_app(b"<html>origin</html>", calls=calls), is_heavy=lambda path: path == "/timing")

    def boom(*_args, **_kwargs):
        raise RuntimeError("cache down")

    async def aboom(*_args, **_kwargs):
        raise RuntimeError("cache down")

    monkeypatch.setattr(page_cache, "lookup_memory", boom)
    monkeypatch.setattr(page_cache, "lookup_edge", aboom)
    monkeypatch.setattr(page_cache, "remember_memory", boom)
    monkeypatch.setattr(page_cache, "remember_edge", aboom)

    async def broken():
        return await _get(app, "/timing")

    missed = asyncio.run(broken())
    assert missed.status_code == 200
    assert missed.text == "<html>origin</html>"
    assert missed.headers["x-aa-cache"] == "miss"
    assert calls["n"] == 1

    page = StoredPage(b"<html>from-edge</html>", "text/html; charset=utf-8", "")

    def memory_miss(_key):
        return None

    async def edge_hit(_key):
        return page

    monkeypatch.setattr(page_cache, "lookup_memory", memory_miss)
    monkeypatch.setattr(page_cache, "lookup_edge", edge_hit)
    monkeypatch.setattr(page_cache, "remember_memory", lambda *_args, **_kwargs: None)

    async def edged():
        return await _get(app, "/timing")

    hit = asyncio.run(edged())
    assert hit.status_code == 200
    assert hit.text == "<html>from-edge</html>"
    assert hit.headers["x-aa-cache"] == "hit"
    assert hit.headers["cache-control"] == "private, max-age=60"
    assert "public" not in hit.headers["cache-control"]
    assert calls["n"] == 1


def test_signed_in_heavy_page_hits_on_repeat_and_is_not_shared(monkeypatch):
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)
    client = TestClient(main.app)
    anonymous = client.get("/timing", follow_redirects=False)
    assert anonymous.status_code == 303
    assert anonymous.headers["location"] == "/login"
    assert anonymous.headers["x-aa-cache"] == "miss"
    assert anonymous.headers["cache-control"] == "no-store"
    assert "Demo Artist" not in anonymous.text

    logged = client.post(
        "/login",
        data={"email": "artist@demo.com", "password": "demo123"},
        follow_redirects=False,
    )
    assert logged.status_code == 303
    assert "x-aa-cache" not in logged.headers

    login_form = client.get("/login")
    assert login_form.status_code == 200
    assert "x-aa-cache" not in login_form.headers

    first = client.get("/timing")
    assert first.status_code == 200
    assert first.headers["x-aa-cache"] == "miss"
    assert first.headers["cache-control"] == "private, max-age=60"
    assert "public" not in first.headers["cache-control"]
    assert "set-cookie" not in first.headers
    assert "Demo Artist" in first.text
    second = client.get("/timing")
    assert second.headers["x-aa-cache"] == "hit"
    assert second.text == first.text
    assert second.headers["cache-control"] == "private, max-age=60"

    varied = client.get("/timing", params={"scope": "all", "days": "all", "kind": "all"})
    assert varied.headers["x-aa-cache"] == "miss"
    assert varied.text != first.text
    reordered = client.get("/timing?kind=all&days=all&scope=all")
    assert reordered.headers["x-aa-cache"] == "hit"
    assert reordered.text == varied.text

    failed = client.get("/digest.csv")
    assert failed.status_code == 503
    assert failed.headers["x-aa-cache"] == "miss"
    assert failed.headers["cache-control"] == "no-store"
    assert client.get("/digest.csv").headers["x-aa-cache"] == "miss"

    other = TestClient(main.app)
    other.cookies.set(
        "session",
        _seal({"user_id": 2, "auth": "d1", "email": "other@example.com", "artist_name": "Other Name"}),
    )
    stolen = other.get("/timing", follow_redirects=False)
    assert stolen.status_code == 200
    assert stolen.headers["x-aa-cache"] == "miss"
    assert "Other Name" in stolen.text
    assert "Demo Artist" not in stolen.text
    assert stolen.text != first.text

    stranger = TestClient(main.app)
    anonymous_again = stranger.get("/timing", follow_redirects=False)
    assert anonymous_again.status_code == 303
    assert anonymous_again.headers["x-aa-cache"] == "miss"
    assert "Demo Artist" not in anonymous_again.text
    assert "Other Name" not in anonymous_again.text

    css = stranger.get("/static/app.css")
    assert css.status_code == 200
    assert css.headers["cache-control"] == "public, max-age=86400"
    assert "x-aa-cache" not in css.headers


def test_real_app_cache_failure_still_renders(monkeypatch):
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)
    client = TestClient(main.app)
    logged = client.post(
        "/login",
        data={"email": "artist@demo.com", "password": "demo123"},
        follow_redirects=False,
    )
    assert logged.status_code == 303

    def boom(*_args, **_kwargs):
        raise RuntimeError("cache down")

    async def aboom(*_args, **_kwargs):
        raise RuntimeError("cache down")

    monkeypatch.setattr(page_cache, "lookup_memory", boom)
    monkeypatch.setattr(page_cache, "lookup_edge", aboom)
    monkeypatch.setattr(page_cache, "remember_memory", boom)
    monkeypatch.setattr(page_cache, "remember_edge", aboom)
    page = client.get("/timing")
    assert page.status_code == 200
    assert "Demo Artist" in page.text
    assert page.headers["x-aa-cache"] == "miss"
