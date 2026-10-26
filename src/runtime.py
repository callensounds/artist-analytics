"""Worker env access and session cookies signed with a stable secret."""

from __future__ import annotations

import json
import os
from base64 import b64decode, b64encode

from itsdangerous import BadSignature, TimestampSigner
from starlette.datastructures import MutableHeaders
from starlette.middleware.sessions import Session
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Stable across isolates so the demo login keeps working when SECRET_KEY is unset.
# Set the SECRET_KEY Worker secret before treating sessions as unforgeable.
FALLBACK_SESSION_SECRET = "artist-analytics-demo-session-secret"


# A Worker binding sometimes stringifies to one of these. None of them is a secret.
_REJECTED_SECRETS = {
    "",
    "none",
    "undefined",
    "null",
    "[object object]",
    "[object string]",
    "[redacted]",
}


def _iter_plain(env, name: str):
    """Attribute and dict reads only. No js module, no workers.env, no to_py."""
    if isinstance(env, dict):
        yield env.get(name)
    elif env is not None:
        try:
            yield getattr(env, name)
        except Exception:
            pass
        nested = getattr(env, "_env", None)
        if nested is not None and nested is not env:
            try:
                yield getattr(nested, name)
            except Exception:
                pass
    if name in os.environ:
        yield os.environ.get(name)


def _as_text(value) -> str | None:
    if isinstance(value, bytes):
        try:
            value = value.decode()
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    return value.strip()


def env_string(env, name: str) -> str | None:
    """The first plain string on the binding, including a rejected placeholder."""
    for value in _iter_plain(env, name):
        text = _as_text(value)
        if text:
            return text
    return None


def env_get(env, name: str) -> str | None:
    """A usable secret string. Placeholders and non-strings are skipped."""
    for value in _iter_plain(env, name):
        text = _as_text(value)
        if not text or text.casefold() in _REJECTED_SECRETS:
            continue
        if text.startswith("<") and ("JsProxy" in text or "object at 0x" in text):
            continue
        if "\n" in text or "\r" in text:
            continue
        return text
    return None


class WorkerSessionMiddleware:
    """Starlette session cookies, with the signing key read from the request env."""

    def __init__(self, app: ASGIApp, fallback_secret: str = FALLBACK_SESSION_SECRET):
        self.app = app
        self.fallback_secret = fallback_secret
        self.session_cookie = "session"
        self.max_age = 14 * 24 * 60 * 60
        self.path = "/"
        self.security_flags = "httponly; samesite=lax"
        self._signers: dict[str, TimestampSigner] = {}

    def _signer(self, scope: Scope) -> TimestampSigner:
        secret = env_get(scope.get("env"), "SECRET_KEY") or self.fallback_secret
        signer = self._signers.get(secret)
        if signer is None:
            signer = TimestampSigner(secret)
            self._signers[secret] = signer
        return signer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        signer = self._signer(scope)
        connection = HTTPConnection(scope)
        initial_session_was_empty = True
        if self.session_cookie in connection.cookies:
            data = connection.cookies[self.session_cookie].encode("utf-8")
            try:
                unsigned = signer.unsign(data, max_age=self.max_age)
                scope["session"] = Session(json.loads(b64decode(unsigned)))
                initial_session_was_empty = False
            except BadSignature:
                scope["session"] = Session()
        else:
            scope["session"] = Session()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                session: Session = scope["session"]
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                if session.accessed:
                    headers.add_vary_header("Cookie")
                if session.modified and session:
                    payload = b64encode(json.dumps(session).encode("utf-8"))
                    signed = signer.sign(payload)
                    header_value = (
                        f"{self.session_cookie}={signed.decode('utf-8')}; path={self.path}; "
                        f"Max-Age={self.max_age}; {self.security_flags}"
                    )
                    headers.append("Set-Cookie", header_value)
                elif session.modified and not initial_session_was_empty:
                    header_value = (
                        f"{self.session_cookie}=null; path={self.path}; "
                        f"expires=Thu, 01 Jan 1970 00:00:00 GMT; {self.security_flags}"
                    )
                    headers.append("Set-Cookie", header_value)
            await send(message)

        await self.app(scope, receive, send_wrapper)
