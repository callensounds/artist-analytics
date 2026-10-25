"""Small HTTP client with an honest User-Agent, timeouts, and per-host pacing."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests

USER_AGENT = "ArtistAnalytics/0.2 (+https://github.com/callensounds/artist-analytics)"
DEFAULT_INTERVALS = {
    "musicbrainz.org": 1.0,
    "www.instagram.com": 0.4,
    "instagram.com": 0.4,
    "www.tiktok.com": 0.35,
    "tiktok.com": 0.35,
    "open.spotify.com": 0.4,
}


class TransportError(Exception):
    pass


@dataclass
class HttpResult:
    status: int
    text: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self):
        return json.loads(self.text)

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")


class RequestsClient:
    def __init__(self, intervals: dict[str, float] | None = None):
        self.intervals = dict(DEFAULT_INTERVALS if intervals is None else intervals)
        self._last: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def _wait(self, url: str) -> None:
        host = urlparse(url).netloc
        interval = self.intervals.get(host, 0.0)
        if interval <= 0:
            return
        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            delay = interval - (now - self._last.get(host, 0.0))
            if delay > 0:
                await asyncio.sleep(delay)
            self._last[host] = loop.time()

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        data: dict | None = None,
        timeout: float = 12.0,
        allow_redirects: bool = True,
    ) -> HttpResult:
        await self._wait(url)
        merged = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.9"}
        if headers:
            merged.update(headers)

        def send():
            try:
                response = requests.request(
                    method,
                    url,
                    params=params,
                    headers=merged,
                    data=data,
                    timeout=timeout,
                    allow_redirects=allow_redirects,
                )
            except requests.Timeout as exc:
                raise TransportError("The request timed out.") from exc
            except requests.RequestException as exc:
                raise TransportError("The service could not be reached.") from exc
            header_map = {key.lower(): value for key, value in response.headers.items()}
            return HttpResult(
                status=response.status_code,
                text=response.text or "",
                url=str(response.url),
                headers=header_map,
            )

        return await asyncio.to_thread(send)

    async def get_text(self, url: str, **kwargs) -> HttpResult:
        return await self.request("GET", url, **kwargs)

    async def get_json(self, url: str, **kwargs):
        result = await self.get_text(url, **kwargs)
        try:
            payload = result.json()
        except json.JSONDecodeError:
            payload = None
        return result, payload

    async def post_form(self, url: str, data: dict, **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        result = await self.request("POST", url, data=data, headers=headers, **kwargs)
        try:
            payload = result.json()
        except json.JSONDecodeError:
            payload = None
        return result, payload
