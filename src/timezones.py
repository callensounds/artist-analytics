"""Viewer timezone. Conversion is render-time only and DST-correct.

``zoneinfo`` is imported on the first non-UTC conversion, not at startup.
ZoneInfo objects are cached and capped at the allowlist. UTC never loads
tzdata. Pyodide has no system zone database, so the ``tzdata`` package
supplies the IANA files that ``zoneinfo`` reads when the platform database
is missing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qsl, unquote

UTC = "UTC"
TZ_COOKIE = "aa_tz"
TZ_QUERY = "tz"
TZ_COOKIE_MAX_AGE = 60 * 60 * 24 * 365

# Music-market zones, selector order. The value is the IANA name.
ZONES: tuple[tuple[str, str], ...] = (
    ("UTC", "UTC"),
    ("America/New_York", "New York"),
    ("America/Chicago", "Chicago"),
    ("America/Denver", "Denver"),
    ("America/Los_Angeles", "Los Angeles"),
    ("America/Toronto", "Toronto"),
    ("America/Mexico_City", "Mexico City"),
    ("America/Sao_Paulo", "Sao Paulo"),
    ("America/Buenos_Aires", "Buenos Aires"),
    ("Europe/London", "London"),
    ("Europe/Paris", "Paris"),
    ("Europe/Berlin", "Berlin"),
    ("Europe/Madrid", "Madrid"),
    ("Europe/Amsterdam", "Amsterdam"),
    ("Europe/Stockholm", "Stockholm"),
    ("Europe/Moscow", "Moscow"),
    ("Africa/Lagos", "Lagos"),
    ("Africa/Johannesburg", "Johannesburg"),
    ("Africa/Cairo", "Cairo"),
    ("Asia/Dubai", "Dubai"),
    ("Asia/Kolkata", "Mumbai"),
    ("Asia/Bangkok", "Bangkok"),
    ("Asia/Singapore", "Singapore"),
    ("Asia/Hong_Kong", "Hong Kong"),
    ("Asia/Seoul", "Seoul"),
    ("Asia/Tokyo", "Tokyo"),
    ("Asia/Shanghai", "Shanghai"),
    ("Australia/Sydney", "Sydney"),
    ("Pacific/Auckland", "Auckland"),
)

ALLOWED = frozenset(name for name, _label in ZONES)
TZ_CHOICES = tuple({"name": name, "label": label} for name, label in ZONES)

# One ZoneInfo per allowlisted name. Never grows past the list.
_ZONE_CACHE: dict[str, object] = {}
_ZONE_CACHE_CAP = len(ALLOWED)


def zone_label(tz: str) -> str:
    """Chart heading suffix. UTC stays ``UTC``. Other zones name the IANA id."""
    name = tz if tz in ALLOWED else UTC
    if name == UTC:
        return UTC
    return f"{name}, DST-aware"


def hour_heading(tz: str) -> str:
    return f"Hour of day ({zone_label(tz)})"


def weekday_heading(tz: str) -> str:
    return f"Weekday ({zone_label(tz)})"


def normalize_tz(value: str | None) -> str:
    """Allowlisted IANA name, or UTC. Unknown text does not raise."""
    text = (value or "").strip()
    if text not in ALLOWED:
        return UTC
    if text == UTC:
        return UTC
    if _zone(text) is None:
        return UTC
    return text


def _zone(name: str):
    cached = _ZONE_CACHE.get(name)
    if cached is not None:
        return cached
    if len(_ZONE_CACHE) >= _ZONE_CACHE_CAP:
        _ZONE_CACHE.clear()
    try:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(name)
    except Exception:
        return None
    _ZONE_CACHE[name] = zone
    return zone


def shift(moment: datetime, tz: str) -> datetime:
    """UTC instant in the viewer zone. Naive values are UTC. DST comes from IANA."""
    if moment.tzinfo is None:
        utc = moment.replace(tzinfo=timezone.utc)
    else:
        utc = moment.astimezone(timezone.utc)
    name = tz if tz in ALLOWED else UTC
    if name == UTC:
        return utc
    zone = _zone(name)
    if zone is None:
        return utc
    return utc.astimezone(zone)


def format_zoned(moment: datetime, tz: str) -> str:
    """Local clock beside the UTC clock. Callers pass exact timestamps only."""
    utc = shift(moment, UTC)
    utc_text = utc.strftime("%Y-%m-%d %H:%M UTC")
    name = normalize_tz(tz)
    if name == UTC:
        return utc_text
    local = shift(utc, name)
    abbr = local.tzname() or ""
    suffix = f" {abbr}" if abbr else ""
    local_text = local.strftime("%Y-%m-%d %H:%M") + suffix
    return f"{local_text} · {utc_text}"


def format_clocks(moment: datetime, tz: str) -> tuple[str, str, str]:
    """Short clock, full timestamp, and the UTC timestamp used for ``datetime``."""
    utc = shift(moment, UTC)
    utc_full = utc.strftime("%Y-%m-%d %H:%M UTC")
    utc_clock = utc.strftime("%H:%M UTC")
    name = normalize_tz(tz)
    if name == UTC:
        return utc_clock, utc_full, utc_full
    local = shift(utc, name)
    abbr = local.tzname() or ""
    suffix = f" {abbr}" if abbr else ""
    local_clock = local.strftime("%H:%M") + suffix
    local_full = local.strftime("%Y-%m-%d %H:%M") + suffix
    return f"{local_clock} · {utc_clock}", f"{local_full} · {utc_full}", utc_full


def _query_bytes(scope) -> bytes:
    raw = scope.get("query_string") or b""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    return raw


def query_value(scope, name: str) -> str | None:
    """First query value, or None when the parameter is absent."""
    if not isinstance(scope, dict):
        return None
    raw = _query_bytes(scope)
    if len(raw) > 4096:
        raw = raw[:4096]
    try:
        pairs = parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=50)
    except Exception:
        return None
    for key, value in pairs:
        if key == name:
            return value
    return None


def cookie_value(scope, name: str) -> str | None:
    if not isinstance(scope, dict):
        return None
    prefix = name + "="
    for key, value in scope.get("headers") or []:
        if isinstance(key, str):
            header = key.lower()
        else:
            header = bytes(key).lower()
        if header != b"cookie" and header != "cookie":
            continue
        text = value.decode("latin-1", "replace") if isinstance(value, (bytes, bytearray)) else str(value)
        for part in text.split(";"):
            piece = part.strip()
            if piece.startswith(prefix):
                return unquote(piece[len(prefix) :])
    return None


def resolved_tz_from_scope(scope) -> str:
    """Query ``tz`` wins. A missing query uses the cookie. Unknown values are UTC."""
    if not isinstance(scope, dict):
        return UTC
    query = query_value(scope, TZ_QUERY)
    if query is not None:
        return normalize_tz(query)
    return normalize_tz(cookie_value(scope, TZ_COOKIE))


def tz_cookie_header(scope) -> bytes | None:
    """Set-Cookie when a valid ``tz`` query differs from the cookie. Unknown does not overwrite."""
    if not isinstance(scope, dict):
        return None
    method = scope.get("method") or "GET"
    if method not in {"GET", "HEAD"}:
        return None
    path = scope.get("path") or ""
    if not isinstance(path, str) or path.startswith("/static") or path == "/favicon.ico":
        return None
    query = query_value(scope, TZ_QUERY)
    if query is None:
        return None
    text = query.strip()
    if text not in ALLOWED:
        return None
    if cookie_value(scope, TZ_COOKIE) == text:
        return None
    return f"{TZ_COOKIE}={text}; Path=/; Max-Age={TZ_COOKIE_MAX_AGE}; SameSite=Lax; HttpOnly".encode("ascii")
