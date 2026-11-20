"""Short-lived cache of finished heavy GET responses.

A repeat load of /timing, /digest, /compare, /dashboard, or an artist page
should not read D1 or render a template again. The key is the path, the sorted
query string, and the user id from the validated session. The raw session
cookie is never part of the key.

Two tiers:

1. An in-isolate LRU with a hard byte cap. This is the hit path on
   ``*.workers.dev``. Cloudflare does not apply Cache API operations there
   (a custom domain or a zone route is required; ``workers.dev``, the
   dashboard editor, and playground previews no-op).
2. ``caches.default`` under a synthetic URL that includes the same key. That
   copy survives an isolate restart when the Worker is on a custom domain.
   The stored response uses ``Cache-Control: public`` only so the Cache API
   keeps it. The client is always sent ``private`` or ``no-store``.

Every cache call is fail-open. A miss, a throw, or an oversized body renders
the page as usual. No KV binding: the free plan's write quota is reserved
for crawl data.
"""

from __future__ import annotations

import time
from urllib.parse import parse_qsl, quote, urlencode

from cache import _js_options
from runtime import read_signed_session

# Five minutes. Repeat bursts of the same heavy URL stay inside this window.
PAGE_CACHE_TTL = 300
# Browser cache only, and shorter than the server copy. Never ``public``.
PAGE_BROWSER_MAX_AGE = 60
# Same per-entry ceiling as the provider memory cache. A larger page is
# rendered and not stored, so one response cannot fill the isolate.
PAGE_CACHE_MAX_BYTES = 2 * 1024 * 1024
PAGE_CACHE_MAX_ENTRY_BYTES = 512 * 1024
PAGE_CACHE_ENTRY_CAP = 8
PAGE_CACHE_VERSION = "v1"

_NEVER_CACHE_PATHS = frozenset({"/login", "/logout", "/signup", "/register"})
_BLOCKED_BODIES = (
    b"Data temporarily unavailable",
    b"csrf",
    b"CSRF",
    b"Csrf",
    b"flash-message",
    b"flash_message",
)
_MISSING = object()
_JS_CACHES = _MISSING


class StoredPage:
    """A finished 200 body plus the headers needed to replay it."""

    __slots__ = ("body", "content_type", "disposition")

    def __init__(self, body: bytes, content_type: str, disposition: str):
        self.body = body
        self.content_type = content_type or "text/html; charset=utf-8"
        self.disposition = disposition or ""


class PageMemory:
    """Insertion-ordered LRU. ``total_bytes`` is the sum of stored bodies."""

    def __init__(
        self,
        max_bytes: int = PAGE_CACHE_MAX_BYTES,
        max_entry_bytes: int = PAGE_CACHE_MAX_ENTRY_BYTES,
        entry_cap: int = PAGE_CACHE_ENTRY_CAP,
    ):
        self.max_bytes = max_bytes
        self.max_entry_bytes = max_entry_bytes
        self.entry_cap = entry_cap
        self.store: dict[str, tuple[float, StoredPage]] = {}
        self.total_bytes = 0

    def clear(self) -> None:
        self.store.clear()
        self.total_bytes = 0

    def get(self, key: str) -> StoredPage | None:
        item = self.store.get(key)
        if item is None:
            return None
        expires, page = item
        if time.monotonic() > expires:
            self._drop(key)
            return None
        # Touch so a hot page is the last eviction candidate. Expiry stays put.
        self.store.pop(key)
        self.store[key] = item
        return page

    def set(self, key: str, page: StoredPage, ttl: int) -> None:
        size = len(page.body)
        if size <= 0 or size > self.max_entry_bytes or size > self.max_bytes:
            self._drop(key)
            return
        if key in self.store:
            self._drop(key)
        now = time.monotonic()
        self._purge(now)
        while self.store and (
            self.total_bytes + size > self.max_bytes or len(self.store) >= self.entry_cap
        ):
            self._drop(next(iter(self.store)))
        if self.total_bytes + size > self.max_bytes:
            return
        self.store[key] = (now + max(int(ttl), 1), page)
        self.total_bytes += size

    def _drop(self, key: str) -> None:
        item = self.store.pop(key, None)
        if item is None:
            return
        self.total_bytes -= len(item[1].body)
        if self.total_bytes < 0 or self.total_bytes != sum(len(entry[1].body) for entry in self.store.values()):
            self.total_bytes = sum(len(entry[1].body) for entry in self.store.values())

    def _purge(self, now: float) -> None:
        stale = [key for key, (expires, _page) in self.store.items() if now > expires]
        for key in stale:
            self._drop(key)


PAGE_MEMORY = PageMemory()


def clear_page_cache() -> None:
    PAGE_MEMORY.clear()


def lookup_memory(key: str) -> StoredPage | None:
    return PAGE_MEMORY.get(key)


def remember_memory(key: str, page: StoredPage) -> None:
    PAGE_MEMORY.set(key, page, PAGE_CACHE_TTL)


def _caches():
    """``caches`` from the Workers JS runtime, or None outside it."""
    global _JS_CACHES
    if _JS_CACHES is _MISSING:
        try:
            from js import caches

            _JS_CACHES = caches
        except Exception:
            _JS_CACHES = None
    return _JS_CACHES


def _edge_request(key: str):
    from js import Request

    # Not a public URL. The client never requests this host.
    url = "https://cache.artist-analytics.internal/page/v1/" + quote(key, safe="")
    return Request.new(url)


def edge_cache_control(ttl: int = PAGE_CACHE_TTL) -> str:
    """Cache-Control on the synthetic Cache API entry only.

    The client response uses ``client_cache_control`` instead. ``public`` here
    is what ``cache.put`` needs in order to retain the entry. It is not a
    permission for a shared CDN to store the user's page.
    """
    return f"public, max-age={max(int(ttl), 1)}"


def client_cache_control(*, cacheable: bool) -> str:
    if cacheable:
        return f"private, max-age={PAGE_BROWSER_MAX_AGE}"
    return "no-store"


def user_cache_token(session: dict | None) -> str:
    """``anon`` or ``auth:id``. Never the cookie, the email, or the display name."""
    if not isinstance(session, dict):
        return "anon"
    user_id = session.get("user_id")
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        return "anon"
    auth = session.get("auth")
    if auth not in ("d1", "memory"):
        auth = "user"
    return f"{auth}:{user_id}"


def sorted_query_string(raw: bytes | str) -> str:
    """Every parameter, sorted, so ``a=1&b=2`` and ``b=2&a=1`` share an entry."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    text = raw.decode("utf-8", "replace")
    pairs = parse_qsl(
        text,
        keep_blank_values=True,
        encoding="utf-8",
        errors="replace",
        max_num_fields=100,
    )
    pairs.sort()
    return urlencode(pairs, encoding="utf-8", errors="replace")


def page_cache_key(path: str, query: str, user_token: str) -> str:
    return f"{PAGE_CACHE_VERSION}\x1f{user_token}\x1f{path}\x1f{query}"


def cache_key_from_scope(scope) -> str | None:
    """Path + sorted query + session user. None means do not cache this request."""
    raw = scope.get("query_string") or b""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    if len(raw) > 2048:
        return None
    path = scope.get("path") or "/"
    if not isinstance(path, str) or len(path) > 512:
        return None
    token = user_cache_token(read_signed_session(scope))
    return page_cache_key(path, sorted_query_string(raw), token)


def is_page_cache_get(method: str, path: str, is_heavy) -> bool:
    if method != "GET":
        return False
    if path in _NEVER_CACHE_PATHS or path.startswith(
        ("/login", "/logout", "/signup", "/register")
    ):
        return False
    return bool(is_heavy(path))


def _header_name(name) -> bytes:
    if isinstance(name, str):
        return name.lower().encode("latin-1", "replace")
    return bytes(name).lower()


def _header_text(headers, wanted: bytes) -> str:
    for name, value in headers:
        if _header_name(name) != wanted:
            continue
        if isinstance(value, str):
            return value
        return bytes(value).decode("latin-1", "replace")
    return ""


def body_blocks_cache(body: bytes) -> bool:
    """Error pages, CSRF tokens, and flash banners are not reusable per user."""
    return any(marker in body for marker in _BLOCKED_BODIES)


def response_cacheable(status: int, headers, body: bytes) -> bool:
    if status != 200 or not body or len(body) > PAGE_CACHE_MAX_ENTRY_BYTES:
        return False
    for name, value in headers:
        low = _header_name(name)
        if low == b"set-cookie":
            return False
        if low == b"cache-control":
            text = value if isinstance(value, str) else bytes(value)
            if b"no-store" in text.lower():
                return False
    return not body_blocks_cache(body)


def hit_headers(page: StoredPage) -> list[tuple[bytes, bytes]]:
    headers = [
        (b"content-type", page.content_type.encode("latin-1", "replace")),
        (b"cache-control", client_cache_control(cacheable=True).encode("ascii")),
        (b"vary", b"Cookie"),
        (b"x-aa-cache", b"hit"),
    ]
    if page.disposition:
        headers.append((b"content-disposition", page.disposition.encode("latin-1", "replace")))
    return headers


def apply_client_cache_headers(headers, *, cacheable: bool) -> list:
    """Drop any public cache directive and mark the response hit-or-miss."""
    kept = []
    for name, value in headers:
        if _header_name(name) in {b"cache-control", b"x-aa-cache"}:
            continue
        raw_name = name if isinstance(name, bytes) else _header_name(name)
        raw_value = value if isinstance(value, bytes) else str(value).encode("latin-1", "replace")
        kept.append((raw_name, raw_value))
    control = client_cache_control(cacheable=cacheable).encode("ascii")
    kept.append((b"cache-control", control))
    if cacheable and not any(_header_name(name) == b"vary" for name, _value in kept):
        kept.append((b"vary", b"Cookie"))
    kept.append((b"x-aa-cache", b"miss"))
    return kept


async def lookup_edge(key: str) -> StoredPage | None:
    api = _caches()
    if api is None:
        return None
    try:
        response = await api.default.match(_edge_request(key))
    except Exception:
        return None
    if not response:
        return None
    try:
        text = await response.text()
    except Exception:
        return None
    if text is None:
        return None
    body = str(text).encode("utf-8")
    if not body or len(body) > PAGE_CACHE_MAX_ENTRY_BYTES:
        return None
    content_type = "text/html; charset=utf-8"
    disposition = ""
    try:
        content_type = str(response.headers.get("Content-Type") or content_type)
        disposition = str(response.headers.get("X-AA-Disposition") or "")
    except Exception:
        pass
    return StoredPage(body, content_type, disposition)


async def remember_edge(key: str, page: StoredPage) -> None:
    api = _caches()
    if api is None or len(page.body) > PAGE_CACHE_MAX_ENTRY_BYTES:
        return
    try:
        from js import Headers, Response

        headers = Headers.new()
        headers.set("Cache-Control", edge_cache_control(PAGE_CACHE_TTL))
        headers.set("Content-Type", page.content_type)
        if page.disposition:
            headers.set("X-AA-Disposition", page.disposition)
        response = Response.new(page.body.decode("utf-8"), _js_options({"headers": headers}))
        await api.default.put(_edge_request(key), response)
    except Exception:
        return


class HeavyResponseCache:
    """ASGI middleware. A hit returns before the route, the session, or D1."""

    def __init__(self, app, is_heavy):
        self.app = app
        self.is_heavy = is_heavy

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        try:
            candidate = is_page_cache_get(
                scope.get("method") or "",
                scope.get("path") or "",
                self.is_heavy,
            )
        except Exception:
            candidate = False
        if not candidate:
            await self.app(scope, receive, send)
            return

        key = None
        cached = None
        try:
            key = cache_key_from_scope(scope)
        except Exception:
            key = None
        if key is not None:
            try:
                cached = lookup_memory(key)
            except Exception:
                cached = None
            if cached is None:
                try:
                    cached = await lookup_edge(key)
                except Exception:
                    cached = None
                if cached is not None:
                    try:
                        remember_memory(key, cached)
                    except Exception:
                        pass
        if cached is not None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": hit_headers(cached),
                }
            )
            await send({"type": "http.response.body", "body": cached.body, "more_body": False})
            return

        status = 0
        raw_headers: list = []
        chunks: list[bytes] = []
        started = False
        emitted = False

        async def emit() -> None:
            nonlocal emitted
            if emitted:
                return
            emitted = True
            body = b"".join(chunks)
            cacheable = False
            page = None
            if key is not None:
                try:
                    cacheable = response_cacheable(status, raw_headers, body)
                except Exception:
                    cacheable = False
            if cacheable:
                page = StoredPage(
                    body,
                    _header_text(raw_headers, b"content-type") or "text/html; charset=utf-8",
                    _header_text(raw_headers, b"content-disposition"),
                )
                try:
                    remember_memory(key, page)
                except Exception:
                    page = None
            try:
                headers = apply_client_cache_headers(raw_headers, cacheable=cacheable)
            except Exception:
                headers = list(raw_headers)
                headers.append((b"x-aa-cache", b"miss"))
            await send({"type": "http.response.start", "status": status or 500, "headers": headers})
            await send({"type": "http.response.body", "body": body, "more_body": False})
            if page is not None and key is not None:
                try:
                    await remember_edge(key, page)
                except Exception:
                    return

        async def send_wrapper(message):
            nonlocal status, raw_headers, started
            kind = message.get("type")
            if kind == "http.response.start":
                if emitted:
                    return
                status = int(message.get("status") or 0)
                raw_headers = list(message.get("headers") or [])
                started = True
                return
            if kind != "http.response.body":
                await send(message)
                return
            if emitted:
                return
            chunk = message.get("body") or b""
            if chunk:
                chunks.append(chunk)
            if message.get("more_body"):
                return
            await emit()

        await self.app(scope, receive, send_wrapper)
        if started and not emitted:
            await emit()
