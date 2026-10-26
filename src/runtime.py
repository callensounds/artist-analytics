"""Worker env access and session cookies signed with a stable secret."""

from __future__ import annotations

import asyncio
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


# str() of a Worker secret is often "[object Object]" or "undefined".
# Those are not the secret. Google then answers "API key not valid".
_UNUSABLE_SECRET = {
    "",
    "none",
    "undefined",
    "null",
    "[object object]",
    "[object string]",
}

SECRET_NAMES = (
    "YOUTUBE_API_KEY",
    "SECRET_KEY",
    "SPOTIFY_CLIENT_ID",
    "SPOTIFY_CLIENT_SECRET",
)


def _usable_secret(text: str | None) -> str | None:
    if not isinstance(text, str):
        return None
    text = text.strip()
    if text.casefold() in _UNUSABLE_SECRET:
        return None
    if text.startswith("<") and ("JsProxy" in text or "object at 0x" in text):
        return None
    if "\n" in text or "\r" in text:
        return None
    return text


def fingerprint(text: str | None) -> dict:
    """Length and a 4-character prefix. Never the rest of the value."""
    usable = _usable_secret(text) if isinstance(text, str) else None
    raw = text.strip() if isinstance(text, str) else ""
    folded = raw.casefold()
    return {
        "length": len(usable) if usable is not None else (len(raw) if isinstance(text, str) else None),
        "prefix4": (usable or raw)[:4] if isinstance(text, str) and (usable or raw) else None,
        "is_object_object": folded == "[object object]",
        "is_undefined": folded in {"undefined", "null", "none"},
        "usable": usable is not None,
    }


def _js_typeof(value) -> tuple[str | None, str | None]:
    """Constructor name only. Object toString via a JS call re-enters a promising task."""
    if isinstance(value, str):
        return "string", None
    if value is None:
        return "undefined", None
    if isinstance(value, bool):
        return "boolean", None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "number", None
    ctor = _js_constructor(value)
    return {
        "String": "string",
        "Number": "number",
        "Boolean": "boolean",
        "Array": "object",
        "Undefined": "undefined",
    }.get(ctor or "", "object" if ctor else None), None


def _json_stringify(value):
    try:
        import js
    except Exception:
        return None
    try:
        return js.JSON.stringify(value)
    except Exception:
        return None


def _js_constructor(value) -> str | None:
    try:
        ctor = getattr(value, "constructor", None)
        name = getattr(ctor, "name", None)
    except Exception:
        return None
    if not isinstance(name, str) or not name or name in {"undefined", "None"}:
        return None
    return name


def _json_string(value) -> str | None:
    """JSON.stringify round-trips a real string, including a boxed String. No eval."""
    encoded = _json_stringify(value)
    if encoded is None:
        return None
    if not isinstance(encoded, str):
        try:
            encoded = str(encoded)
        except Exception:
            return None
    if encoded in {"", "undefined", "null"}:
        return None
    if len(encoded) >= 2 and encoded[0] == '"' and encoded[-1] == '"':
        try:
            text = json.loads(encoded)
        except json.JSONDecodeError:
            return None
        if isinstance(text, str):
            return text
    return None


def _indexed_string(value) -> str | None:
    """A boxed JS String has numeric indexes and a length. Join those characters."""
    try:
        length = int(value.length)
    except Exception:
        return None
    if length <= 0 or length > 256:
        return None
    chars = []
    for index in range(length):
        try:
            piece = value[index]
        except Exception:
            return None
        if not isinstance(piece, str):
            piece = _json_string(piece)
        if not isinstance(piece, str) or len(piece) != 1:
            return None
        chars.append(piece)
    return "".join(chars)


def _is_boxed_string(value) -> bool:
    """A JS String object, not a Python str and not a general object."""
    if value is None or isinstance(value, (str, bytes, bytearray, bool, int, float)):
        return False
    ctor = _js_constructor(value)
    if ctor not in {None, "String"}:
        return False
    try:
        length = int(getattr(value, "length"))
    except Exception:
        return False
    return 0 < length <= 256


def coercions(value) -> list[tuple[str, str | None]]:
    """Every way to read one binding, in the order we trust them.

    JSON.stringify and per-character reads run only for a boxed string.
    Both walk into JS. Doing that for every binding on every request kept
    the isolate inside its one promising task long enough for the next
    request's initPyInstance to fail.
    """
    found: list[tuple[str, str | None]] = []
    if isinstance(value, bytes):
        try:
            found.append(("bytes", value.decode()))
        except UnicodeDecodeError:
            found.append(("bytes", None))
    if isinstance(value, str):
        found.append(("python-str", value))
    boxed = _is_boxed_string(value)
    if boxed:
        found.append(("json-stringify", _json_string(value)))
    to_py = getattr(value, "to_py", None)
    if callable(to_py):
        try:
            converted = to_py()
        except Exception:
            converted = None
        if isinstance(converted, bytes):
            try:
                converted = converted.decode()
            except UnicodeDecodeError:
                converted = None
        found.append(("to_py", converted if isinstance(converted, str) else None))
    value_of = getattr(value, "valueOf", None)
    if callable(value_of):
        try:
            converted = value_of()
        except Exception:
            converted = None
        if not isinstance(converted, str):
            converted = _json_string(converted) if _is_boxed_string(converted) else None
        found.append(("valueOf", converted if isinstance(converted, str) else None))
    if boxed:
        found.append(("indexed-chars", _indexed_string(value)))
    if not isinstance(value, str):
        try:
            found.append(("str", str(value)))
        except Exception:
            found.append(("str", None))
    return found


def _looks_like_api_key(text: str) -> bool:
    if not text.startswith("AIza"):
        return False
    if not 30 <= len(text) <= 128:
        return False
    return all(char.isalnum() or char in "-_" for char in text)


def _best_text(name: str, options: list[tuple[str, str]]) -> tuple[str | None, str | None]:
    usable = [(path, text) for path, text in options if _usable_secret(text)]
    if name == "YOUTUBE_API_KEY":
        for path, text in usable:
            if _looks_like_api_key(text):
                return path, text
    if usable:
        return usable[0]
    return None, None


def _read_attr(source, name: str):
    try:
        return True, getattr(source, name)
    except Exception as exc:
        return False, exc


def _read_item(source, name: str):
    try:
        return True, source[name]
    except Exception as exc:
        return False, exc


def iter_bindings(env, name: str, *, include_global: bool = True):
    """Yield (path, ok, value_or_error) for each place a secret might live."""
    if isinstance(env, dict):
        yield "dict", True, env.get(name)
    elif env is not None:
        ok, value = _read_attr(env, name)
        yield "env-attr", ok, value
        ok, value = _read_item(env, name)
        yield "env-item", ok, value
        raw = getattr(env, "_env", None)
        if raw is not None and raw is not env:
            ok, value = _read_attr(raw, name)
            yield "wrapper-env-attr", ok, value
            ok, value = _read_item(raw, name)
            yield "wrapper-env-item", ok, value
    else:
        yield "env", False, None
    if name in os.environ:
        yield "os.environ", True, os.environ.get(name)
    else:
        yield "os.environ", False, None
    if not include_global:
        return
    try:
        from workers import env as workers_env
    except Exception as exc:
        yield "workers.env", False, exc
    else:
        ok, value = _read_attr(workers_env, name)
        yield "workers.env-attr", ok, value


def choose_secret(env, name: str) -> dict:
    """Pick a secret string and record how each path looked. No full values leave this dict's text field."""
    reads = []
    options: list[tuple[str, str]] = []
    for path, ok, value in iter_bindings(env, name):
        if not ok:
            error = value
            reads.append(
                {
                    "path": path,
                    "present": False,
                    "python_type": type(error).__name__ if isinstance(error, BaseException) else None,
                    "js_typeof": None,
                    "js_error": None,
                    "js_constructor": None,
                    "coercions": [],
                    "error": type(error).__name__ if isinstance(error, BaseException) else "missing",
                }
            )
            continue
        row_options = []
        for method, text in coercions(value):
            if text is None:
                continue
            row_options.append((f"{path}:{method}", text))
            options.append((f"{path}:{method}", text))
        js_typeof, js_error = _js_typeof(value)
        reads.append(
            {
                "path": path,
                "present": value is not None,
                "python_type": type(value).__name__,
                "js_typeof": js_typeof,
                "js_error": js_error,
                "js_constructor": _js_constructor(value),
                "coercions": [
                    {"method": method.split(":", 1)[-1], **fingerprint(text)} for method, text in row_options
                ],
                "error": None,
            }
        )
    chosen_path, chosen_text = _best_text(name, options)
    return {"name": name, "path": chosen_path, "text": chosen_text, "reads": reads}


def describe_secret(env, name: str) -> dict:
    chosen = choose_secret(env, name)
    return {
        "name": name,
        "chosen_path": chosen["path"],
        "chosen": fingerprint(chosen["text"]),
        "reads": chosen["reads"],
    }


def describe_env(env) -> dict:
    return {"secrets": [describe_secret(env, name) for name in SECRET_NAMES]}


def _good_enough(name: str, options: list[tuple[str, str]]) -> bool:
    _path, text = _best_text(name, options)
    if not text:
        return False
    if name == "YOUTUBE_API_KEY":
        return _looks_like_api_key(text)
    return True


def _scan_secret(env, name: str, *, include_global: bool) -> str | None:
    options: list[tuple[str, str]] = []
    for path, ok, value in iter_bindings(env, name, include_global=include_global):
        if not ok:
            continue
        for method, text in coercions(value):
            if text:
                options.append((f"{path}:{method}", text))
        if _good_enough(name, options):
            break
    return _best_text(name, options)[1]


def env_get(env, name: str) -> str | None:
    """Read one secret. The session cookie calls this on every request.

    The global workers.env binding is a JS import. Skip it when the request
    env already has the value, and skip it entirely for SECRET_KEY so a
    missing session secret falls back without entering JavaScript.
    """
    text = _scan_secret(env, name, include_global=False)
    if text or name == "SECRET_KEY":
        return text
    return _scan_secret(env, name, include_global=True)


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

        # Pyodide runs one promising task at a time. initPyInstance of the next
        # request raises SystemError if this task is still inside synchronous
        # Python. Yield before the secret read so a concurrent favicon, CSS, or
        # login request can enter.
        await asyncio.sleep(0)
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
