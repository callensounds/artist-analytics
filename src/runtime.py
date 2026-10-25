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


def env_get(env, name: str) -> str | None:
    value = None
    if isinstance(env, dict):
        value = env.get(name)
    elif env is not None:
        try:
            value = getattr(env, name)
        except Exception:
            value = None
        if value is None:
            try:
                value = env[name]
            except Exception:
                value = None
    if _blank(value):
        value = os.getenv(name)
    if _blank(value):
        return None
    return str(value).strip()


def _blank(value) -> bool:
    if value is None:
        return True
    if not isinstance(value, (str, int, float)):
        return False
    text = str(value).strip()
    return text == "" or text in {"None", "undefined", "null"}


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
