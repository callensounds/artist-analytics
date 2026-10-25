"""Process memory cache, with an optional Cloudflare KV binding in front of it."""

from __future__ import annotations

import time

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
            await self.kv.put(key, value, {"expirationTtl": int(ttl)})
        except Exception:
            try:
                await self.kv.put(key, value)
            except Exception:
                return


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


def cache_from_env(env) -> MemoryCache | KVCache:
    kv = binding(env, "CACHE")
    memory = MemoryCache()
    if kv is None:
        return memory
    return KVCache(kv, memory)
