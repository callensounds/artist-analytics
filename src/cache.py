"""Process memory, the Workers Cache API, and optional KV. Any layer can be missing.

Every Cache API and KV call is an awaited JS promise. Nothing here blocks or uses run_sync.
"""

from __future__ import annotations

import asyncio
import time

# A slow cache layer must not eat the dashboard budget.
LAYER_TIMEOUT = 1.5


def _js_options(options: dict):
    """A JS object for KV/Cache options. Plain dicts are fine for fakes outside the Worker."""
    try:
        from js import Object
        from pyodide.ffi import to_js
    except ImportError:
        return options
    return to_js(options, dict_converter=Object.fromEntries)

# In-isolate string caches. Workers Free kills the isolate around 128MB even when
# the request has only used a few dozen milliseconds of CPU, and the Pyodide heap
# does not shrink. Caps are bytes, not just entry counts: one clear-all at 200
# entries still held the raw provider bodies that filled the isolate.
MEMORY_MAX_BYTES = 6 * 1024 * 1024
MEMORY_MAX_ENTRY_BYTES = 512 * 1024
MEMORY_ENTRY_CAP = 200
HISTORY_MAX_BYTES = 4 * 1024 * 1024
HISTORY_MAX_ENTRY_BYTES = 1 * 1024 * 1024
HISTORY_ENTRY_CAP = 32

_MEMORY: dict[str, tuple[float, str]] = {}
# Artist-page history only. Kept off the provider MemoryCache so a full page
# cannot wipe cached provider bodies, and so a page fill does not write KV.
_HISTORY_MEMORY: dict[str, tuple[float, str]] = {}


class _Tally:
    """Byte total for one store. Rescanned when the dict length changes under us."""

    __slots__ = ("bytes", "count")

    def __init__(self) -> None:
        self.bytes = 0
        self.count = 0


_MEMORY_TALLY = _Tally()
_HISTORY_TALLY = _Tally()


def _value_size(value) -> int:
    """len(str) is the byte stand-in. A value without a length counts as empty."""
    try:
        return len(value)
    except Exception:
        return 0


def _item_size(item) -> int:
    try:
        return _value_size(item[1])
    except Exception:
        return 0


def _cache_tally(cache) -> _Tally:
    store = cache.store
    if store is _MEMORY:
        tally = _MEMORY_TALLY
    elif store is _HISTORY_MEMORY:
        tally = _HISTORY_TALLY
    else:
        tally = getattr(cache, "_local_tally", None)
        if tally is None:
            tally = _Tally()
            cache._local_tally = tally
    if tally.count != len(store):
        total = 0
        for item in store.values():
            total += _item_size(item)
        tally.bytes = total
        tally.count = len(store)
    return tally


def _cache_drop(cache, key: str) -> None:
    tally = _cache_tally(cache)
    removed = cache.store.pop(key, None)
    if removed is None:
        return
    tally.bytes -= _item_size(removed)
    tally.count -= 1
    if tally.bytes < 0 or tally.count < 0 or tally.count != len(cache.store):
        tally.bytes = sum(_item_size(item) for item in cache.store.values())
        tally.count = len(cache.store)


def _cache_purge(cache, now: float) -> None:
    stale = []
    for key, item in cache.store.items():
        try:
            expires = item[0]
        except (TypeError, IndexError):
            stale.append(key)
            continue
        if now > expires:
            stale.append(key)
    for key in stale:
        _cache_drop(cache, key)


def _load_value(cache, key: str):
    item = cache.store.get(key)
    if not item:
        return None
    try:
        expires = item[0]
        value = item[1]
    except (TypeError, IndexError):
        cache.store.pop(key, None)
        _cache_tally(cache)
        return None
    if time.monotonic() > expires:
        _cache_drop(cache, key)
        return None
    return value


def _store_value(cache, key: str, value, ttl: int) -> None:
    """Insert in dict order. Expired entries go first, then the oldest live key.

    A value over the per-entry cap is not stored. Callers still write the edge
    and KV layers; this only refuses the isolate copy. Replacing a key drops the
    previous copy so a later get cannot return the smaller body.
    """
    size = _value_size(value)
    if size > cache.max_entry_bytes or size > cache.max_bytes:
        if key in cache.store:
            _cache_drop(cache, key)
        return
    now = time.monotonic()
    _cache_purge(cache, now)
    if key in cache.store:
        _cache_drop(cache, key)
    tally = _cache_tally(cache)
    while cache.store and (tally.bytes + size > cache.max_bytes or len(cache.store) >= cache.entry_cap):
        _cache_drop(cache, next(iter(cache.store)))
    if not cache.store:
        tally.bytes = 0
        tally.count = 0
    if tally.bytes + size > cache.max_bytes:
        return
    cache.store[key] = (now + max(int(ttl), 1), value)
    tally.bytes += size
    tally.count += 1


class MemoryCache:
    def __init__(
        self,
        store: dict[str, tuple[float, str]] | None = None,
        *,
        max_bytes: int = MEMORY_MAX_BYTES,
        max_entry_bytes: int = MEMORY_MAX_ENTRY_BYTES,
        entry_cap: int = MEMORY_ENTRY_CAP,
    ):
        self.store = _MEMORY if store is None else store
        self.max_bytes = max_bytes
        self.max_entry_bytes = max_entry_bytes
        self.entry_cap = entry_cap

    async def get(self, key: str) -> str | None:
        return _load_value(self, key)

    async def set(self, key: str, value: str, ttl: int) -> None:
        _store_value(self, key, value, ttl)


class KVCache:
    """Read-through KV. Failures fall back to memory so a missing namespace never breaks a request."""

    def __init__(self, kv, memory: MemoryCache | None = None):
        self.kv = kv
        self.memory = memory or MemoryCache()

    async def get(self, key: str) -> str | None:
        local = await self.memory.get(key)
        if local is not None:
            return local
        try:
            value = await self.kv.get(key)
        except Exception:
            return None
        if value is None:
            return None
        text = value.decode() if isinstance(value, bytes) else str(value)
        if not text:
            return None
        await self.memory.set(key, text, 120)
        return text

    async def set(self, key: str, value: str, ttl: int) -> None:
        await self.memory.set(key, value, ttl)
        # A rejected KV put must not be followed by another awaited put inside except.
        # That second promise is what raises the promising-task SystemError.
        fallback = False
        try:
            await self.kv.put(key, value, _js_options({"expirationTtl": max(int(ttl), 60)}))
        except Exception:
            fallback = True
        if not fallback:
            return
        try:
            await self.kv.put(key, value)
        except Exception:
            return


class EdgeCache:
    """Workers Cache API (caches.default). Absent outside the Worker runtime."""

    def __init__(self, api=None):
        self.api = api

    def _api(self):
        if self.api is not None:
            return self.api
        from js import caches

        return caches

    @staticmethod
    def _request(key: str):
        from urllib.parse import quote

        from js import Request

        url = "https://cache.artist-analytics.internal/v1/" + quote(key, safe="")
        return Request.new(url)

    async def get(self, key: str) -> str | None:
        try:
            api = self._api()
            response = await api.default.match(self._request(key))
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
        text = str(text)
        return text or None

    async def set(self, key: str, value: str, ttl: int) -> None:
        try:
            from js import Headers, Response

            api = self._api()
            headers = Headers.new()
            headers.set("Cache-Control", f"public, max-age={max(int(ttl), 60)}")
            response = Response.new(value, _js_options({"headers": headers}))
            await api.default.put(self._request(key), response)
        except Exception:
            return


class TieredCache:
    """Read the first layer that has the key. Writes go to every layer."""

    def __init__(self, layers):
        self.layers = [layer for layer in layers if layer is not None]

    async def get(self, key: str) -> str | None:
        for index, layer in enumerate(self.layers):
            try:
                value = await asyncio.wait_for(layer.get(key), LAYER_TIMEOUT)
            except Exception:
                value = None
            if not value:
                continue
            for earlier in self.layers[:index]:
                try:
                    await asyncio.wait_for(earlier.set(key, value, 120), LAYER_TIMEOUT)
                except Exception:
                    pass
            return value
        return None

    async def set(self, key: str, value: str, ttl: int) -> None:
        for layer in self.layers:
            try:
                await asyncio.wait_for(layer.set(key, value, ttl), LAYER_TIMEOUT)
            except Exception:
                continue


def binding(env, name: str):
    if env is None:
        return None
    try:
        value = getattr(env, name)
    except Exception:
        value = None
    if value is None:
        return None
    if isinstance(value, str) and value.strip() in {"", "undefined", "null"}:
        return None
    return value


def cache_from_env(env) -> TieredCache:
    memory = MemoryCache()
    layers: list = [memory, EdgeCache()]
    kv = binding(env, "CACHE")
    if kv is not None:
        layers.append(KVCache(kv))
    return TieredCache(layers)


class IsolateCache:
    """In-isolate string cache. No network and no daily write cap."""

    def __init__(
        self,
        store: dict[str, tuple[float, str]] | None = None,
        cap: int = HISTORY_ENTRY_CAP,
        *,
        max_bytes: int = HISTORY_MAX_BYTES,
        max_entry_bytes: int = HISTORY_MAX_ENTRY_BYTES,
    ):
        self.store = _HISTORY_MEMORY if store is None else store
        self.cap = cap
        self.entry_cap = cap
        self.max_bytes = max_bytes
        self.max_entry_bytes = max_entry_bytes

    async def get(self, key: str) -> str | None:
        return _load_value(self, key)

    async def set(self, key: str, value: str, ttl: int) -> None:
        _store_value(self, key, value, ttl)


class HistoryPageCache:
    """Isolate memory, then the Cache API. Reads and writes never touch KV or D1."""

    def __init__(self, memory: IsolateCache | None = None, edge: EdgeCache | None = None):
        self.memory = memory or IsolateCache()
        self.edge = edge or EdgeCache()

    async def get(self, key: str) -> str | None:
        local = await self.memory.get(key)
        if local:
            return local
        try:
            value = await self.edge.get(key)
        except Exception:
            return None
        if not value:
            return None
        # Same TTL the history loader stores with. Imported here to avoid a cycle.
        from history import HISTORY_CACHE_TTL

        await self.memory.set(key, value, HISTORY_CACHE_TTL)
        return value

    async def set(self, key: str, value: str, ttl: int) -> None:
        await self.memory.set(key, value, ttl)
        try:
            await self.edge.set(key, value, ttl)
        except Exception:
            return


def history_page_cache(env, db):
    """Cache assembled artist history on D1. Sqlite tests always recompute."""
    from db import D1Database

    if not isinstance(db, D1Database):
        return None
    return HistoryPageCache()
