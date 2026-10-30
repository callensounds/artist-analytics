"""Async HTTP with an honest User-Agent, per-call timeouts, a request deadline, and byte caps.

On Cloudflare Python Workers every call is ``await fetch(...)`` on the JS global.
Nothing here calls ``pyodide.ffi.run_sync``, ``time.sleep``, or a sync HTTP library.

Why: ``requests`` -> urllib3's emscripten backend runs every fetch, and every body
read, through ``pyodide.ffi.run_sync`` (JSPI). ``asyncio.to_thread`` does not help,
because Pyodide's WebLoop.run_in_executor calls the function inline. The sync call
then holds the isolate's single promising task while another request arrives, and
that request fails with "SystemError: Cannot enter a promising task from inside
another running promising task" (Cloudflare 1101). It also serialises every
provider in a request, so asyncio.wait_for cannot cut a slow source off.

Plain CPython (pytest, uvicorn) has no ``js`` module. There, a stdlib urllib call
runs in a real worker thread. That path is never selected on the Workers runtime.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlparse

USER_AGENT = "ArtistAnalytics/0.2 (+https://github.com/callensounds/artist-analytics)"
# Spacing between calls to the same host from one dashboard request.
# MusicBrainz has its own isolate-wide spacing in providers/musicbrainz.py.
DEFAULT_INTERVALS = {
    "www.instagram.com": 0.4,
    "instagram.com": 0.4,
    "www.tiktok.com": 0.35,
    "tiktok.com": 0.35,
    "open.spotify.com": 0.4,
}
DEFAULT_TIMEOUT = 8.0
# One response body. Callers can ask for less, never for more.
MAX_RESPONSE_BYTES = 1_500_000
# Everything one dashboard request may download.
MAX_TOTAL_BYTES = 8_000_000

IS_WORKERS = sys.platform == "emscripten"


class TransportError(Exception):
    pass


@dataclass
class HttpResult:
    status: int
    text: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self):
        return json.loads(self.text)

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")


def _with_params(url: str, params: dict | None) -> str:
    if not params:
        return url
    joiner = "&" if "?" in url else "?"
    return url + joiner + urlencode(params)


def _decode(chunks: list[bytes]) -> str:
    return b"".join(chunks).decode("utf-8", errors="replace")


class WorkersFetchTransport:
    """``await fetch(url, init)`` on the Workers runtime. Bodies are read chunk by chunk up to the cap."""

    def __init__(self, fetch=None, to_js=None, abort_controller=None, object_from_entries=None):
        if fetch is None or to_js is None or abort_controller is None:
            from js import AbortController, Object, fetch as js_fetch
            from pyodide.ffi import to_js as js_to_js

            fetch = fetch or js_fetch
            to_js = to_js or js_to_js
            abort_controller = abort_controller or AbortController
            object_from_entries = object_from_entries or Object.fromEntries
        self._fetch = fetch
        self._to_js = to_js
        self._abort_controller = abort_controller
        self._from_entries = object_from_entries

    def _js(self, value: dict):
        if self._from_entries is None:
            return self._to_js(value)
        return self._to_js(value, dict_converter=self._from_entries)

    async def send(self, method, url, headers, body, allow_redirects, max_bytes, timeout) -> HttpResult:
        controller = self._abort_controller.new()
        init = {
            "method": method,
            "headers": dict(headers),
            "redirect": "follow" if allow_redirects else "manual",
            "signal": controller.signal,
        }
        if body is not None:
            init["body"] = body
        done = False
        try:
            result = await asyncio.wait_for(self._exchange(url, init, max_bytes), timeout)
            done = True
            return result
        except asyncio.TimeoutError as exc:
            raise TransportError("The request timed out.") from exc
        except TransportError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise TransportError("The service could not be reached.") from exc
        finally:
            # abort() is synchronous. Awaiting it here would run while the
            # exception above is still propagating.
            if not done:
                try:
                    controller.abort()
                except Exception:
                    pass

    async def _exchange(self, url, init, max_bytes) -> HttpResult:
        response = await self._fetch(url, self._js(init))
        header_map: dict[str, str] = {}
        try:
            for pair in response.headers.entries():
                header_map[str(pair[0]).lower()] = str(pair[1])
        except Exception:
            pass
        chunks: list[bytes] = []
        size = 0
        truncated = False
        stream = getattr(response, "body", None)
        get_reader = getattr(stream, "getReader", None) if stream else None
        if get_reader is None:
            # No stream (HEAD, 204, or a null body). text() is still an awaited promise.
            text = await response.text() if hasattr(response, "text") else ""
            data = str(text or "").encode("utf-8", errors="replace")
            truncated = len(data) > max_bytes
            chunks.append(data[:max_bytes])
        else:
            reader = get_reader()
            while True:
                part = await reader.read()
                if part.done:
                    break
                data = part.value.to_bytes()
                if size + len(data) > max_bytes:
                    chunks.append(data[: max_bytes - size])
                    size = max_bytes
                    truncated = True
                    try:
                        await reader.cancel()
                    except Exception:
                        pass
                    break
                chunks.append(data)
                size += len(data)
        return HttpResult(
            status=int(response.status),
            text=_decode(chunks),
            url=str(getattr(response, "url", "") or url),
            headers=header_map,
            truncated=truncated,
        )


class CPythonTransport:
    """Local tests and uvicorn only. A stdlib urllib call in a real thread. Refused under Pyodide."""

    async def send(self, method, url, headers, body, allow_redirects, max_bytes, timeout) -> HttpResult:
        if IS_WORKERS:
            raise RuntimeError("CPythonTransport must not run on the Workers runtime.")
        return await asyncio.wait_for(
            asyncio.to_thread(self._send, method, url, headers, body, allow_redirects, max_bytes, timeout),
            timeout + 1,
        )

    @staticmethod
    def _send(method, url, headers, body, allow_redirects, max_bytes, timeout) -> HttpResult:
        import socket
        import urllib.error
        import urllib.request

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        handlers = [] if allow_redirects else [_NoRedirect()]
        opener = urllib.request.build_opener(*handlers)
        data = body.encode() if isinstance(body, str) else body
        request = urllib.request.Request(url, data=data, headers=dict(headers), method=method)
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            if isinstance(getattr(exc, "reason", exc), (socket.timeout, TimeoutError)):
                raise TransportError("The request timed out.") from exc
            raise TransportError("The service could not be reached.") from exc
        try:
            raw = response.read(max_bytes + 1) or b""
        except Exception as exc:
            raise TransportError("The service could not be reached.") from exc
        finally:
            try:
                response.close()
            except Exception:
                pass
        truncated = len(raw) > max_bytes
        header_map = {key.lower(): value for key, value in (response.headers or {}).items()}
        return HttpResult(
            status=int(getattr(response, "status", None) or response.code),
            text=raw[:max_bytes].decode("utf-8", errors="replace"),
            url=str(response.geturl() or url),
            headers=header_map,
            truncated=truncated,
        )


def default_transport():
    return WorkersFetchTransport() if IS_WORKERS else CPythonTransport()


class HttpClient:
    """One instance per dashboard request. Holds no asyncio primitives."""

    def __init__(
        self,
        intervals: dict[str, float] | None = None,
        *,
        transport=None,
        deadline: float | None = None,
        max_total_bytes: int = MAX_TOTAL_BYTES,
    ):
        self.intervals = dict(DEFAULT_INTERVALS if intervals is None else intervals)
        self.transport = transport
        # An absolute event-loop time. Every call's timeout is clamped to it.
        self.deadline = deadline
        self.max_total_bytes = max_total_bytes
        self.bytes_read = 0
        self._next_slot: dict[str, float] = {}

    def remaining(self) -> float | None:
        if self.deadline is None:
            return None
        return self.deadline - asyncio.get_running_loop().time()

    async def _wait(self, url: str) -> None:
        """Reserve the next slot for this host, then sleep until it. No lock: nothing awaits in between."""
        host = urlparse(url).netloc
        interval = self.intervals.get(host, 0.0)
        if interval <= 0:
            return
        now = asyncio.get_running_loop().time()
        slot = max(now, self._next_slot.get(host, 0.0))
        self._next_slot[host] = slot + interval
        if slot > now:
            await asyncio.sleep(slot - now)

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        data: dict | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        allow_redirects: bool = True,
        max_bytes: int | None = None,
    ) -> HttpResult:
        await self._wait(url)
        left = self.remaining()
        if left is not None:
            if left <= 0.05:
                raise TransportError("The dashboard time budget ran out before this request.")
            timeout = min(float(timeout), left)
        cap = min(int(max_bytes or MAX_RESPONSE_BYTES), MAX_RESPONSE_BYTES)
        budget = self.max_total_bytes - self.bytes_read
        if budget <= 0:
            raise TransportError("The dashboard download budget ran out before this request.")
        cap = min(cap, budget)
        merged = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.9"}
        if headers:
            merged.update(headers)
        body = urlencode(data) if data is not None else None
        transport = self.transport or default_transport()
        self.transport = transport
        result = await transport.send(
            method,
            _with_params(url, params),
            merged,
            body,
            allow_redirects,
            cap,
            timeout,
        )
        self.bytes_read += len(result.text.encode("utf-8", errors="ignore"))
        return result

    async def get_text(self, url: str, **kwargs) -> HttpResult:
        return await self.request("GET", url, **kwargs)

    async def get_json(self, url: str, **kwargs):
        result = await self.get_text(url, **kwargs)
        if result.truncated:
            return result, None
        try:
            payload = result.json()
        except (json.JSONDecodeError, ValueError):
            payload = None
        return result, payload

    async def post_form(self, url: str, data: dict, **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        result = await self.request("POST", url, data=data, headers=headers, **kwargs)
        if result.truncated:
            return result, None
        try:
            payload = result.json()
        except (json.JSONDecodeError, ValueError):
            payload = None
        return result, payload
