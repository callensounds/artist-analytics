"""Logged-in diagnostics. Fingerprints of secrets and upstream status. Never a full secret."""

from __future__ import annotations

from providers.youtube import redact_key, youtube_url
from runtime import env_get

PROBE_ARTIST = "159260351"
YOUTUBE_PARAMS = {"part": "id", "forHandle": "@TaylorSwift"}


def _snippet(payload) -> str | None:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])[:180]
    return None


async def _probe(http, url: str, headers: dict | None = None, secret: str | None = None) -> dict:
    try:
        result, payload = await http.get_json(url, timeout=8, headers=headers)
    except Exception as exc:
        return {
            "url": redact_key(url, secret),
            "status": None,
            "error": type(exc).__name__,
            "message": None,
        }
    text = result.text or ""
    final = redact_key(getattr(result, "url", None) or url, secret)
    return {
        "url": final,
        "status": result.status,
        "error": None,
        "message": _snippet(payload) if isinstance(payload, dict) else None,
        "has_server_data": "serialized-server-data" in text,
        "has_release_meta": 'property="music:release_date"' in text or "music:release_date" in text,
    }


async def diagnose(env, http) -> dict:
    # JS secret introspection is imported only when this logged-in page runs.
    from diag_secrets import describe_env

    key = env_get(env, "YOUTUBE_API_KEY")
    plain = youtube_url("channels", YOUTUBE_PARAMS)
    built = youtube_url("channels", {**YOUTUBE_PARAMS, "key": key}) if key else None
    header_probe = None
    query_probe = None
    if key:
        header_probe = await _probe(http, plain, {"X-Goog-Api-Key": key}, key)
        query_probe = await _probe(http, built, None, key)
    apple = {
        "itunes_lookup": await _probe(
            http,
            f"https://itunes.apple.com/lookup?id={PROBE_ARTIST}&entity=album&limit=1&country=us",
        ),
        "music_apple_artist": await _probe(http, f"https://music.apple.com/us/artist/{PROBE_ARTIST}"),
        "rss_most_played": await _probe(
            http,
            "https://rss.marketingtools.apple.com/api/v2/us/music/most-played/5/albums.json",
        ),
    }
    return {
        "secrets": describe_env(env)["secrets"],
        "youtube": {
            "key_present": bool(key),
            "built_query_url": redact_key(built, key) if built else None,
            "header_request": header_probe,
            "query_request": query_probe,
            "dashboard_sends": "X-Goog-Api-Key header, no key query parameter",
        },
        "apple": apple,
    }
