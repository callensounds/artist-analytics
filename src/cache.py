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

_MEMORY: dict[str, tuple[float, str]] = {}


class MemoryCache:
    def __init__(self, store: dict[str, tuple[float, str]] | None = None):
        self.store = _MEMORY if store is None else store

    async def get(self, key: str) -> str | None:
        item = self.store.get(key)
        if not item:
            return None
        expires, value = item
        if time.monotonic() > expires:
            self.store.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: str, ttl: int) -> None:
        if len(self.store) > 200:
            self.store.clear()
        self.store[key] = (time.monotonic() + max(int(ttl), 1), value)


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
        try:
            await self.kv.put(key, value, _js_options({"expirationTtl": max(int(ttl), 60)}))
        except Exception:
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
