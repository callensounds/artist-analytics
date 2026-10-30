"""Scheduled refresh. One artist per cron tick unless a Queue binding fans the work out."""

from __future__ import annotations

import asyncio
import json
import os
import traceback
from datetime import datetime, timedelta, timezone

from analytics import normalize_name
from cache import binding, cache_from_env
from db import SqliteDatabase, migrate, parse_utc, utc_now
from history import remember
from http_client import HttpClient
from runtime import env_get

# One queue message is one of these. MusicBrainz runs first and stores ids;
# the others then read that row and do a single fetch.
PROVIDERS = ("musicbrainz", "spotify", "instagram", "tiktok", "apple", "youtube")

DEFAULT_STALE_SECONDS = 6 * 60 * 60
SCAN_WINDOW = 50
# Queue send on a dashboard request. Failures and slow bindings are ignored.
ENQUEUE_TIMEOUT_S = 0.15
# Keep a retried message off this isolate until the shared CPU budget can recover.
RETRY_DELAY_SECONDS = 180
# Skip a provider whose audience snapshot is newer than this. A retry after a
# CPU kill then does not parse that source again.
PROVIDER_FRESH_SECONDS = 3 * 60 * 60
# Cloudflare egress often gets HTTP 403 from iTunes. Skip it until this passes.
APPLE_BACKOFF_SECONDS = 30 * 60


def settings(env) -> dict:
    def number(name: str, default: int, upper: int) -> int:
        raw = env_get(env, name)
        try:
            value = int(raw) if raw is not None else default
        except ValueError:
            value = default
        return max(0, min(value, upper))

    return {
        "batch_size": max(1, number("CRAWL_BATCH_SIZE", 1, 5)),
        "enqueue_limit": max(1, number("CRAWL_ENQUEUE_LIMIT", 10, 50)),
        "stale_seconds": max(60, number("CRAWL_STALE_SECONDS", DEFAULT_STALE_SECONDS, 7 * 24 * 3600)),
        "related_cap": number("RELATED_AUTOTRACK_CAP", 25, 500),
    }


def _stale(row: dict, now: datetime, stale_seconds: int) -> bool:
    crawled = parse_utc(row.get("last_crawled_at"))
    if crawled is None:
        return True
    return now - crawled >= timedelta(seconds=stale_seconds)


def known_links(row: dict) -> dict:
    links = {}
    if row.get("spotify_id"):
        links["spotify_id"] = row["spotify_id"]
    if row.get("apple_id"):
        links["apple_id"] = row["apple_id"]
    if row.get("instagram_handle"):
        links["instagram"] = row["instagram_handle"]
    if row.get("tiktok_handle"):
        links["tiktok"] = row["tiktok_handle"]
    if row.get("youtube_channel_id"):
        links["youtube_channel_ids"] = [row["youtube_channel_id"]]
    return links


async def _cursor(db) -> int:
    row = await db.query_one("SELECT next_artist_id FROM crawl_state WHERE id = 1")
    return int(row["next_artist_id"]) if row else 0


async def apple_blocked_until(db) -> datetime | None:
    row = await db.query_one("SELECT apple_blocked_until FROM crawl_state WHERE id = 1")
    if not row:
        return None
    return parse_utc(row.get("apple_blocked_until"))


async def block_apple(db, now: datetime) -> None:
    """Skip iTunes until ``now + APPLE_BACKOFF_SECONDS``. Does not move the artist cursor."""
    until = utc_now(now + timedelta(seconds=APPLE_BACKOFF_SECONDS))
    await db.execute(
        """
        INSERT INTO crawl_state (id, next_artist_id, updated_at, apple_blocked_until)
        VALUES (1, 0, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            apple_blocked_until = excluded.apple_blocked_until,
            updated_at = excluded.updated_at
        """,
        (utc_now(now), until),
    )


def _apple_forbidden(bundle: dict) -> bool:
    for source in bundle.get("sources") or []:
        if source.get("platform") != "apple":
            continue
        if "HTTP 403" in str(source.get("reason") or ""):
            return True
    return False


async def _save_cursor(db, artist_id: int, now: datetime) -> None:
    await db.execute(
        """
        INSERT INTO crawl_state (id, next_artist_id, updated_at) VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET next_artist_id = excluded.next_artist_id, updated_at = excluded.updated_at
        """,
        (artist_id, utc_now(now)),
    )


async def select_stale(db, *, cursor: int, limit: int, now: datetime, stale_seconds: int) -> tuple[list[dict], int]:
    rows = await db.query(
        "SELECT * FROM artists WHERE tracked = 1 AND id > ? ORDER BY id LIMIT ?",
        (cursor, SCAN_WINDOW),
    )
    if not rows:
        rows = await db.query(
            "SELECT * FROM artists WHERE tracked = 1 ORDER BY id LIMIT ?",
            (SCAN_WINDOW,),
        )
    chosen = []
    last_id = cursor
    for row in rows:
        last_id = int(row["id"])
        if _stale(row, now, stale_seconds):
            chosen.append(row)
        if len(chosen) >= limit:
            break
    return chosen, last_id


def env_binding(env, name: str):
    """A Worker binding, or the same name on a dict env used by tests. Missing stays None."""
    if isinstance(env, dict):
        value = env.get(name)
        if value is None or (isinstance(value, str) and value.strip() in {"", "undefined", "null"}):
            return None
        return value
    return binding(env, name)


def _log(message: str, **fields) -> None:
    print(json.dumps({"message": message, **fields}, default=str), flush=True)


def _bundle_detail(name: str, sources: list | None, exc: BaseException | None = None) -> str:
    bits = [name or ""]
    for source in sources or []:
        reason = " ".join(str(source.get("reason") or "").split())[:140]
        bit = f"{source.get('platform')}={source.get('status')}"
        if reason:
            bit += f" ({reason})"
        bits.append(bit)
    if exc is not None:
        bits.append(f"{type(exc).__name__}: {exc!r}")
        formatted = traceback.format_exc()
        if len(formatted) > 900:
            formatted = formatted[:160] + "\n" + formatted[-720:]
        bits.append(formatted)
    return " | ".join(bit for bit in bits if bit)[:2000]


def _exc_fields(exc: BaseException) -> dict:
    """JS rejections often stringify as '#<Object>'. Keep the traceback and repr."""
    return {
        "error": type(exc).__name__,
        "repr": repr(exc)[:500],
        "trace": traceback.format_exc()[-1800:],
    }


def handler_env(owner, passed):
    """Entrypoint env when it has bindings, otherwise the env argument.

    WorkerEntrypoint stores a wrapped env on ``self.env``. Fetch uses that object.
    The scheduled and queue calls also pass a raw env, which can expose ``DB``
    without the ``prepare`` method this process can call. Prefer ``self.env``.
    """
    own = getattr(owner, "env", None)

    def usable(candidate) -> bool:
        if candidate is None:
            return False
        return env_binding(candidate, "DB") is not None or env_binding(candidate, "CRAWL_QUEUE") is not None

    if usable(own):
        return own
    if usable(passed):
        return passed
    return own if own is not None else passed


async def fresh_sources(db, artist_id: int, now: datetime, seconds: int = PROVIDER_FRESH_SECONDS) -> set[str]:
    rows = await db.query(
        """
        SELECT source, MAX(captured_at) AS captured_at
        FROM metric_snapshots
        WHERE artist_id = ?
        GROUP BY source
        """,
        (artist_id,),
    )
    fresh = set()
    for row in rows:
        captured = parse_utc(row.get("captured_at"))
        if captured is not None and now - captured < timedelta(seconds=seconds):
            fresh.add(str(row.get("source") or ""))
    fresh.discard("")
    return fresh


async def refresh_artist(db, env, row: dict, *, collect, http, cache, now: datetime, related_cap: int) -> dict:
    # A fresh client per artist so one crawl cannot spend another's byte budget.
    client = http if http is not None else HttpClient()
    try:
        client.crawl_light = True
    except Exception:
        pass
    skip: set[str] = set()
    if row.get("id") is not None:
        try:
            skip = await fresh_sources(db, int(row["id"]), now)
        except Exception:
            skip = set()
    apple_blocked = False
    try:
        until = await apple_blocked_until(db)
        apple_blocked = until is not None and until > now
    except Exception:
        apple_blocked = False
    if apple_blocked:
        skip.add("apple")
    if collect is None:
        from providers import collect_artist as collect
    bundle = await collect(
        row["name"],
        env=env,
        http=client,
        cache=cache,
        known_links=known_links(row),
        include_related=True,
        skip_platforms=skip,
    )
    if not apple_blocked and _apple_forbidden(bundle):
        try:
            await block_apple(db, now)
        except Exception:
            pass
    saved = await remember(db, row["name"], bundle, origin=row.get("origin") or "search", now=now, related_cap=related_cap)
    sources = bundle.get("sources") or []
    statuses = [source.get("status") for source in sources]
    saved["ok"] = "error" not in statuses
    saved["name"] = row["name"]
    saved["source_notes"] = [
        {"platform": source.get("platform"), "status": source.get("status"), "reason": source.get("reason")}
        for source in sources
    ]
    return saved


async def run_batch(db, env, *, collect=None, http=None, cache=None, now: datetime | None = None, limit: int | None = None) -> dict:
    moment = now or datetime.now(timezone.utc)
    config = settings(env)
    batch_limit = limit if limit is not None else config["batch_size"]
    cursor = await _cursor(db)
    run_id = await db.execute(
        "INSERT INTO crawl_runs (started_at, status, cursor) VALUES (?, 'running', ?)",
        (utc_now(moment), str(cursor)),
    )
    if not run_id:
        latest = await db.query_one("SELECT id FROM crawl_runs ORDER BY id DESC LIMIT 1")
        run_id = int(latest["id"]) if latest else None
    chosen, last_id = await select_stale(
        db,
        cursor=cursor,
        limit=batch_limit,
        now=moment,
        stale_seconds=config["stale_seconds"],
    )
    store = cache if cache is not None else cache_from_env(env)
    ok = 0
    failed = 0
    snapshots = 0
    edges = 0
    discovered = 0
    errors = []
    for row in chosen:
        try:
            saved = await refresh_artist(
                db,
                env,
                row,
                collect=collect,
                http=http,
                cache=store,
                now=moment,
                related_cap=config["related_cap"],
            )
        except Exception as exc:
            failed += 1
            errors.append(f"{row.get('name')}: {exc.__class__.__name__}")
            continue
        snapshots += saved["snapshots"]
        edges += saved["edges"]
        discovered += saved["discovered"]
        if saved["ok"]:
            ok += 1
        else:
            failed += 1
            errors.append(f"{saved['name']}: a source returned an error")
    await _save_cursor(db, last_id if chosen or last_id != cursor else 0, moment)
    status = "ok" if not failed else ("partial" if ok else "error")
    if not chosen:
        status = "ok"
    detail = json.dumps({"errors": errors[:20], "mode": "cursor"})[:2000]
    await db.execute(
        """
        UPDATE crawl_runs SET
            finished_at = ?, status = ?, artists_attempted = ?, artists_ok = ?,
            artists_error = ?, snapshots_written = ?, edges_written = ?, discovered = ?,
            cursor = ?, detail = ?
        WHERE id = ?
        """,
        (
            utc_now(moment),
            status,
            len(chosen),
            ok,
            failed,
            snapshots,
            edges,
            discovered,
            str(last_id),
            detail,
            run_id,
        ),
    )
    return {
        "status": status,
        "attempted": len(chosen),
        "ok": ok,
        "error": failed,
        "snapshots": snapshots,
        "edges": edges,
        "discovered": discovered,
        "cursor": last_id,
    }


async def enqueue_stale(db, env, queue, *, now: datetime | None = None) -> dict:
    moment = now or datetime.now(timezone.utc)
    config = settings(env)
    cursor = await _cursor(db)
    chosen, last_id = await select_stale(
        db,
        cursor=cursor,
        limit=config["enqueue_limit"],
        now=moment,
        stale_seconds=config["stale_seconds"],
    )
    sent = 0
    for row in chosen:
        result = queue.send({"artist_id": int(row["id"]), "provider": "musicbrainz"})
        if hasattr(result, "__await__"):
            await result
        sent += 1
    await _save_cursor(db, last_id if chosen else cursor, moment)
    await db.execute(
        """
        INSERT INTO crawl_runs
            (started_at, finished_at, status, artists_attempted, cursor, detail)
        VALUES (?, ?, 'queued', ?, ?, ?)
        """,
        (
            utc_now(moment),
            utc_now(moment),
            sent,
            str(last_id),
            json.dumps({"mode": "queue", "sent": sent}),
        ),
    )
    return {"status": "queued", "attempted": sent, "cursor": last_id}


def _message_fields(message) -> dict:
    body = getattr(message, "body", message)
    fields = {"artist_id": None, "name": None, "origin": "search", "provider": "musicbrainz", "mbid": ""}
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            return fields
    if not isinstance(body, dict):
        try:
            fields["artist_id"] = int(body["artist_id"])
        except Exception:
            pass
        return fields
    if body.get("origin"):
        fields["origin"] = str(body.get("origin"))
    if body.get("name"):
        fields["name"] = str(body.get("name"))
    provider = str(body.get("provider") or "musicbrainz")
    fields["provider"] = provider if provider in PROVIDERS else "musicbrainz"
    fields["mbid"] = str(body.get("mbid") or "")
    try:
        if body.get("artist_id") is not None:
            fields["artist_id"] = int(body["artist_id"])
    except (TypeError, ValueError):
        fields["artist_id"] = None
    return fields


async def enqueue_search(name: str, env) -> None:
    """One queue send. The consumer does the crawl. A miss or a slow send is ignored."""
    cleaned = (name or "").strip()[:120]
    if not cleaned:
        return
    queue = env_binding(env, "CRAWL_QUEUE")
    if queue is None or not hasattr(queue, "send"):
        return
    try:
        sent = queue.send({"name": cleaned, "origin": "search", "provider": "musicbrainz"})
        if hasattr(sent, "__await__"):
            await asyncio.wait_for(sent, ENQUEUE_TIMEOUT_S)
    except Exception:
        return


async def _ensure_named_artist(db, name: str, origin: str, now: datetime) -> dict | None:
    cleaned = (name or "").strip()[:120]
    normalized = normalize_name(cleaned)
    if not cleaned or not normalized:
        return None
    if origin not in {"search", "related"}:
        origin = "search"
    await db.execute(
        """
        INSERT INTO artists (name, normalized_name, origin, tracked, created_at)
        VALUES (?, ?, ?, 1, ?)
        ON CONFLICT(normalized_name) DO UPDATE SET
            name = excluded.name,
            tracked = 1,
            origin = CASE WHEN excluded.origin = 'search' THEN 'search' ELSE artists.origin END
        """,
        (cleaned, normalized, origin, utc_now(now)),
    )
    return await db.query_one("SELECT * FROM artists WHERE normalized_name = ?", (normalized,))


async def _finish_message(message, method: str, *, delay_seconds: int | None = None) -> None:
    fn = getattr(message, method, None)
    if fn is None:
        return
    try:
        if method == "retry" and delay_seconds:
            try:
                result = fn({"delaySeconds": int(delay_seconds)})
            except TypeError:
                result = fn()
        else:
            result = fn()
    except Exception as exc:
        _log("queue settle failed", method=method, **_exc_fields(exc))
        return
    if hasattr(result, "__await__"):
        try:
            await result
        except Exception as exc:
            _log("queue settle failed", method=method, **_exc_fields(exc))


async def record_consumer_run(
    db,
    *,
    now: datetime,
    status: str,
    ok: int,
    error: int,
    snapshots: int,
    edges: int,
    discovered: int,
    detail: str,
) -> None:
    await db.execute(
        """
        INSERT INTO crawl_runs (
            started_at, finished_at, status, artists_attempted, artists_ok, artists_error,
            snapshots_written, edges_written, discovered, detail
        ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now(now),
            utc_now(now),
            status,
            ok,
            error,
            snapshots,
            edges,
            discovered,
            (detail or "")[:2000],
        ),
    )


def _crawl_run_statement(now, status, ok, error, snapshots, edges, discovered, detail):
    return (
        """
        INSERT INTO crawl_runs (
            started_at, finished_at, status, artists_attempted, artists_ok, artists_error,
            snapshots_written, edges_written, discovered, detail
        ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now(now),
            utc_now(now),
            status,
            ok,
            error,
            snapshots,
            edges,
            discovered,
            (detail or "")[:2000],
        ),
    )


async def _send_json(queue, body) -> None:
    if queue is None or not hasattr(queue, "send"):
        return
    result = queue.send(body)
    if hasattr(result, "__await__"):
        await result


def _followup_providers(links: dict, fresh: set[str], *, youtube: bool, apple_blocked: bool) -> list[str]:
    chosen = []
    if links.get("spotify_id") and "spotify" not in fresh:
        chosen.append("spotify")
    if links.get("instagram") and "instagram" not in fresh:
        chosen.append("instagram")
    if links.get("tiktok") and "tiktok" not in fresh:
        chosen.append("tiktok")
    if not apple_blocked and "apple" not in fresh:
        chosen.append("apple")
    if youtube and "youtube" not in fresh:
        chosen.append("youtube")
    return chosen


async def _fetch_one(provider: str, name: str, links: dict, env, http, cache):
    if provider == "spotify":
        from providers.spotify import fetch_spotify

        return await fetch_spotify(name, links, env, http, cache, include_related=True)
    if provider == "instagram":
        from providers.instagram import fetch_instagram

        return await fetch_instagram(name, links, http, cache)
    if provider == "tiktok":
        from providers.tiktok import fetch_tiktok

        return await fetch_tiktok(name, links, http, cache)
    if provider == "apple":
        from providers.apple import fetch_apple

        return await fetch_apple(name, links, http, cache)
    if provider == "youtube":
        from providers.youtube import fetch_youtube

        return await fetch_youtube(name, links, env, http, cache)
    from models import unavailable

    return unavailable(provider, provider, "This crawler message has no provider.")


def _notes(source: dict) -> list[dict]:
    return [{"platform": source.get("platform"), "status": source.get("status"), "reason": source.get("reason")}]


async def _save_one(db, row, source: dict, *, now, related_cap: int):
    notes = _notes(source)
    detail = _bundle_detail(str(row.get("name") or ""), notes)
    ok = source.get("status") != "error"

    def extra(snapshots, edges, discovered):
        wrote = snapshots or edges
        if ok:
            status = "ok"
        elif wrote:
            status = "partial"
        else:
            status = "error"
        return [
            _crawl_run_statement(
                now,
                status,
                1 if status == "ok" else 0,
                0 if status == "ok" else 1,
                snapshots,
                edges,
                discovered,
                detail,
            )
        ]

    saved = await remember(
        db,
        row["name"],
        {"sources": [source]},
        origin=row.get("origin") or "search",
        now=now,
        related_cap=related_cap,
        artist_row=row,
        extra=extra,
    )
    saved["logged"] = True
    saved["ok"] = ok
    saved["name"] = row["name"]
    saved["source_notes"] = notes
    return saved


async def _crawl_identity(db, env, row, fields, http, now, related_cap) -> dict:
    from providers.musicbrainz import fetch_identity

    result = await fetch_identity(http, row["name"], fields.get("mbid") or "")
    links = dict(result.links or {})
    if links.get("_needs_lookup") and links.get("_mbid"):
        await _send_json(
            env_binding(env, "CRAWL_QUEUE"),
            {"artist_id": int(row["id"]), "provider": "musicbrainz", "mbid": links["_mbid"]},
        )
        return {
            "ok": True,
            "snapshots": 0,
            "edges": 0,
            "discovered": 0,
            "name": row["name"],
            "source_notes": [{"platform": "musicbrainz", "status": "ok", "reason": "lookup queued"}],
        }
    saved = await _save_one(db, row, result.to_dict(), now=now, related_cap=0)
    if result.status in {"ok", "unavailable"}:
        fresh: set[str] = set()
        try:
            fresh = await fresh_sources(db, int(row["id"]), now)
        except Exception:
            fresh = set()
        blocked = False
        try:
            until = await apple_blocked_until(db)
            blocked = until is not None and until > now
        except Exception:
            blocked = False
        # A miss still lets Apple match by name. Spotify, Instagram, and TikTok
        # need an id stored from a successful identity.
        stored = links if result.status == "ok" else {}
        await _fan_out(env, int(row["id"]), stored, fresh, apple_blocked=blocked)
    return saved


async def _fan_out(env, artist_id: int, links: dict, fresh: set[str], *, apple_blocked: bool) -> None:
    queue = env_binding(env, "CRAWL_QUEUE")
    youtube = bool(env_get(env, "YOUTUBE_API_KEY"))
    for provider in _followup_providers(links, fresh, youtube=youtube, apple_blocked=apple_blocked):
        await _send_json(queue, {"artist_id": artist_id, "provider": provider})


async def _crawl_followup(db, env, row, provider, http, cache, now, related_cap) -> dict:
    if provider == "apple":
        try:
            until = await apple_blocked_until(db)
            if until is not None and until > now:
                return {
                    "ok": True,
                    "snapshots": 0,
                    "edges": 0,
                    "discovered": 0,
                    "name": row["name"],
                    "source_notes": [{"platform": "apple", "status": "ok", "reason": "skipped during Apple backoff"}],
                }
        except Exception:
            pass
    if provider == "youtube" and not env_get(env, "YOUTUBE_API_KEY"):
        return {
            "ok": True,
            "snapshots": 0,
            "edges": 0,
            "discovered": 0,
            "name": row["name"],
            "source_notes": [{"platform": "youtube", "status": "missing_credentials", "reason": "no key"}],
        }
    fresh: set[str] = set()
    try:
        fresh = await fresh_sources(db, int(row["id"]), now)
    except Exception:
        fresh = set()
    if provider in fresh:
        return {
            "ok": True,
            "snapshots": 0,
            "edges": 0,
            "discovered": 0,
            "name": row["name"],
            "source_notes": [{"platform": provider, "status": "ok", "reason": "snapshot is fresh"}],
        }
    links = known_links(row)
    if provider == "spotify" and not links.get("spotify_id"):
        return _missing_id(row, "spotify")
    if provider == "instagram" and not links.get("instagram"):
        return _missing_id(row, "instagram")
    if provider == "tiktok" and not links.get("tiktok"):
        return _missing_id(row, "tiktok")
    result = await _fetch_one(provider, row["name"], links, env, http, cache)
    source = result.to_dict()
    if provider == "apple" and "HTTP 403" in str(source.get("reason") or ""):
        try:
            await block_apple(db, now)
        except Exception:
            pass
    cap = related_cap if provider == "spotify" else 0
    return await _save_one(db, row, source, now=now, related_cap=cap)


def _missing_id(row, provider: str) -> dict:
    return {
        "ok": True,
        "snapshots": 0,
        "edges": 0,
        "discovered": 0,
        "name": row["name"],
        "source_notes": [
            {"platform": provider, "status": "unavailable", "reason": "MusicBrainz has not stored an id for this provider."}
        ],
    }


async def crawl_provider_message(db, env, row, fields, *, http, cache, now, related_cap) -> dict:
    client = http if http is not None else HttpClient()
    try:
        client.crawl_light = True
    except Exception:
        pass
    if fields["provider"] == "musicbrainz":
        return await _crawl_identity(db, env, row, fields, client, now, related_cap)
    return await _crawl_followup(db, env, row, fields["provider"], client, cache, now, related_cap)


async def run_queue_batch(batch, env, *, collect=None, http=None, cache=None) -> dict:
    # max_batch_size is 1. If a batch is larger anyway, only the first artist runs
    # in this invocation. The rest go back on the queue instead of stacking subrequests.
    incoming = list(getattr(batch, "messages", None) or [])
    for extra in incoming[1:]:
        await _finish_message(extra, "retry", delay_seconds=RETRY_DELAY_SECONDS)
    messages = incoming[:1]
    extra_retries = max(0, len(incoming) - 1)
    db = await open_database(env)
    if db is None:
        for message in messages:
            await _finish_message(message, "retry", delay_seconds=RETRY_DELAY_SECONDS)
        return {
            "messages": len(incoming),
            "acked": 0,
            "retried": len(messages) + extra_retries,
            "snapshots": 0,
        }
    await migrate(db)
    config = settings(env)
    store = cache if cache is not None else cache_from_env(env)
    moment = datetime.now(timezone.utc)
    acked = 0
    retried = 0
    snapshots = 0
    for message in messages:
        fields = _message_fields(message)
        artist_id, name, origin = fields["artist_id"], fields["name"], fields["origin"]
        row = None
        if artist_id is not None:
            row = await db.query_one("SELECT * FROM artists WHERE id = ? AND tracked = 1", (artist_id,))
        elif name:
            row = await _ensure_named_artist(db, name, origin, moment)
        if row is None:
            await _finish_message(message, "ack")
            acked += 1
            continue
        # A full-artist test passes collect=. Production messages are one provider.
        # Only the MusicBrainz identity message is deduped by last_crawled_at.
        # Follow-up provider messages must still run after that timestamp moves.
        identity = fields["provider"] == "musicbrainz" and not fields["mbid"]
        if (collect is not None or identity) and not _stale(row, moment, config["stale_seconds"]):
            await _finish_message(message, "ack")
            acked += 1
            continue
        failure = None
        try:
            if collect is not None:
                saved = await refresh_artist(
                    db,
                    env,
                    row,
                    collect=collect,
                    http=http,
                    cache=store,
                    now=moment,
                    related_cap=config["related_cap"],
                )
            else:
                saved = await crawl_provider_message(
                    db,
                    env,
                    row,
                    fields,
                    http=http,
                    cache=store,
                    now=moment,
                    related_cap=config["related_cap"],
                )
        except Exception as exc:
            # format_exc only works while the exception is active. The D1 write and
            # message.retry below are JS promises and must run after this block.
            failure = {
                "detail": _bundle_detail(str(row.get("name") or ""), None, exc),
                "fields": _exc_fields(exc),
            }
        if failure is not None:
            _log("queue message failed", name=row.get("name"), detail=failure["detail"], **failure["fields"])
            write_error = None
            try:
                await record_consumer_run(
                    db,
                    now=moment,
                    status="error",
                    ok=0,
                    error=1,
                    snapshots=0,
                    edges=0,
                    discovered=0,
                    detail=failure["detail"],
                )
            except Exception as write_exc:
                write_error = _exc_fields(write_exc)
            if write_error is not None:
                _log("crawl run write failed", **write_error)
            await _finish_message(message, "retry", delay_seconds=RETRY_DELAY_SECONDS)
            retried += 1
            continue
        snapshots += int(saved.get("snapshots") or 0)
        edges = int(saved.get("edges") or 0)
        wrote = int(saved.get("snapshots") or 0) or int(saved.get("edges") or 0)
        if saved.get("ok"):
            run_status = "ok"
        elif wrote:
            run_status = "partial"
        else:
            run_status = "error"
        detail = _bundle_detail(str(saved.get("name") or ""), saved.get("source_notes"))
        _log(
            "queue artist",
            name=saved.get("name"),
            status=run_status,
            snapshots=saved.get("snapshots"),
            edges=saved.get("edges"),
            detail=detail,
        )
        if not saved.get("logged"):
            try:
                await record_consumer_run(
                    db,
                    now=moment,
                    status=run_status,
                    ok=1 if run_status == "ok" else 0,
                    error=0 if run_status == "ok" else 1,
                    snapshots=int(saved.get("snapshots") or 0),
                    edges=edges,
                    discovered=int(saved.get("discovered") or 0),
                    detail=detail,
                )
            except Exception as write_exc:
                write_fields = _exc_fields(write_exc)
            else:
                write_fields = None
            if write_fields is not None:
                _log("crawl run write failed", **write_fields)
        await _finish_message(message, "ack")
        acked += 1
    return {
        "messages": len(incoming),
        "acked": acked,
        "retried": retried + extra_retries,
        "snapshots": snapshots,
    }


_POOL: dict[str, SqliteDatabase] = {}


def sqlite_path(env) -> str | None:
    import os

    configured = env_get(env, "ARTIST_ANALYTICS_SQLITE")
    if configured == "off":
        return None
    if configured:
        return configured
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    if env is not None and not isinstance(env, dict):
        return None
    return "data/artist-analytics.sqlite"


async def open_database(env, *, migrate_schema: bool = True):
    database = env_binding(env, "DB")
    if database is not None and hasattr(database, "prepare"):
        from db import D1Database

        return D1Database(database)
    path = sqlite_path(env)
    if path is None:
        return None
    # A dashboard read must not create the sqlite file or apply migrations.
    if not migrate_schema and path != ":memory:" and not os.path.isfile(path):
        return None
    pooled = _POOL.get(path)
    if pooled is None:
        pooled = SqliteDatabase(path)
        _POOL[path] = pooled
    if migrate_schema:
        await migrate(pooled)
    return pooled


def reset_pool() -> None:
    _POOL.clear()


async def run_scheduled(env) -> dict:
    db = await open_database(env)
    if db is None:
        _log("crawl skipped", reason="no database")
        return {"status": "skipped", "reason": "no database"}
    await migrate(db)
    queue = env_binding(env, "CRAWL_QUEUE")
    if queue is not None and hasattr(queue, "send"):
        try:
            return await enqueue_stale(db, env, queue)
        except Exception as exc:
            _log("crawl queue send failed", error=exc.__class__.__name__)
    return await run_batch(db, env)


async def status_report(env) -> dict:
    db = await open_database(env)
    report = {
        "database": "unavailable",
        "reason": "D1 is not bound on this request, so nothing was stored. Local uvicorn uses data/artist-analytics.sqlite.",
        "mode": "cursor",
        "schedule": "*/15 * * * *",
        "artists": 0,
        "related_artists": 0,
        "snapshots": 0,
        "edges": 0,
        "cursor": 0,
        "runs": [],
        "settings": settings(env),
    }
    if env_binding(env, "CRAWL_QUEUE") is not None:
        report["mode"] = "queue"
    if db is None:
        return report
    await migrate(db)
    report["database"] = "d1" if env_binding(env, "DB") is not None else "sqlite"
    report["reason"] = None
    counts = await db.query_one(
        """
        SELECT
            (SELECT COUNT(*) FROM artists WHERE tracked = 1) AS artists,
            (SELECT COUNT(*) FROM artists WHERE origin = 'related' AND tracked = 1) AS related_artists,
            (SELECT COUNT(*) FROM metric_snapshots) AS snapshots,
            (SELECT COUNT(*) FROM related_edges) AS edges
        """
    )
    report.update(counts or {})
    report["cursor"] = await _cursor(db)
    report["runs"] = await db.query(
        """
        SELECT started_at, finished_at, status, artists_attempted, artists_ok, artists_error,
               snapshots_written, edges_written, discovered, detail
        FROM crawl_runs
        ORDER BY id DESC
        LIMIT 20
        """
    )
    return report
