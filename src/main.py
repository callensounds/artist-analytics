import asyncio
import gc
import hmac
import html
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from cache import history_page_cache
from charts import CHARTS_PAGE_EMPTY, load_chart_board
from accounts import (
    DEMO_EMAIL,
    DEMO_PASSWORD,
    authenticate,
    list_watched,
    register_user,
    unwatch_artist,
    watch_artist,
)
from crawl import (
    enqueue_search,
    known_links,
    open_database,
    run_scheduled,
    settings,
    status_report,
)
from diag import diagnose
from export_csv import load_export_rows, render_export_csv
from graph import load_related_graph
from analytics import (
    empty_posting_patterns,
    empty_release_calendar,
    ensure_month_nav,
    focus_release_month,
    normalize_name,
    parse_calendar_month,
    parse_calendar_period,
)
from dashboard_refresh import assemble_bundle, cards_from_history, load_dashboard_plan, queued_bundle
from db import log_read_failure
from history import (
    ALERTS_EMPTY,
    CALENDAR_READ_ERROR,
    GROWTH_READ_ERROR,
    POSTING_READ_ERROR,
    TRACKS_READ_ERROR,
    empty_history,
    find_artist,
    find_artist_by_id,
    list_tracked_artists,
    list_trending,
    load_alert_feed,
    load_calendar_page,
    load_dashboard_history,
    load_deezer_card,
    load_history_for_artist,
    present_deezer_card,
    search_artists,
)
from http_client import HttpClient
from passwords import hash_password, verify_password
from providers import collect_artist
from runtime import FALLBACK_SESSION_SECRET, WorkerSessionMiddleware, env_get
from compare import build_compare_view, empty_compare_view, render_compare_csv
from digest import (
    DIGEST_DB_NOTE,
    DIGEST_READ_ERROR,
    build_digest,
    empty_digest,
    parse_threshold,
    render_digest_csv,
    render_digest_json,
)
from timing import (
    TIMING_DB_NOTE,
    TIMING_READ_ERROR,
    build_timing,
    empty_timing,
    parse_days as parse_timing_days,
    parse_kind as parse_timing_kind,
    parse_scope as parse_timing_scope,
    release_render_fields,
    render_timing_csv,
    render_timing_json,
)
from scores import SCORE_FORMULA, SCORE_RANK_EMPTY, rank_scores

try:
    from workers import asgi
except ImportError:  # The workers runtime exists on Cloudflare, not in local tests.
    asgi = None

app = FastAPI(title="Average Analytics")
app.add_middleware(WorkerSessionMiddleware, fallback_secret=FALLBACK_SESSION_SECRET)


@app.middleware("http")
async def d1_outage_page(request: Request, call_next):
    """A D1 limit error must not become a bare Internal Server Error."""
    try:
        return await call_next(request)
    except Exception as exc:
        if not _d1_outage(exc):
            raise
        if request.url.path.endswith(".csv"):
            return Response(
                f"{DATA_UNAVAILABLE}\n",
                status_code=503,
                media_type="text/plain; charset=utf-8",
            )
        page = (
            "<!DOCTYPE html><html><body><main>"
            f"<h1>{DATA_UNAVAILABLE}</h1>"
            "<p>Stored rows could not be read. Nothing on this page is estimated.</p>"
            "</main></body></html>"
        )
        return HTMLResponse(page, status_code=503)


# Pages that assemble a large row set or a full artist history. Trivial routes
# (login, CSS, search) are left alone: gc.collect() walks the whole heap.
_HEAVY_PATHS = frozenset(
    {
        "/timing",
        "/timing.csv",
        "/timing.json",
        "/digest",
        "/digest.csv",
        "/digest.json",
        "/compare",
        "/compare.csv",
        "/dashboard",
    }
)
# Log once the WASM heap is this close to the 128MB isolate limit.
_HEAP_LOG_BYTES = 96 * 1024 * 1024


def heavy_request_path(path: str) -> bool:
    """Artist page, calendar, timing, digest, compare, and dashboard."""
    if path in _HEAVY_PATHS:
        return True
    if not path.startswith("/artist/"):
        return False
    rest = path[len("/artist/") :]
    if not rest or "/" not in rest:
        return bool(rest)
    key, tail = rest.split("/", 1)
    return bool(key) and tail == "calendar"


def _pyodide_heap_bytes() -> int | None:
    try:
        import pyodide_js

        return int(pyodide_js._module.HEAPU8.length)
    except Exception:
        return None


def note_isolate_heap(path: str) -> None:
    """One console line when the Pyodide heap is near the isolate limit. Never raises."""
    try:
        heap = _pyodide_heap_bytes()
        if heap is None or heap <= _HEAP_LOG_BYTES:
            return
        safe = " ".join(str(path or "").split())[:160] or "/"
        print(f"mem heap_mb={heap // (1024 * 1024)} path={safe}", flush=True)
    except Exception:
        return


def _collect_request() -> None:
    try:
        gc.collect()
    except Exception:
        return


class ReleaseHeavyMemory:
    """Free the previous request's pages after the body has been sent.

    This is raw ASGI middleware. Starlette's HTTP middleware reads the whole
    body into a buffer, which is the peak we are trying to avoid.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        try:
            await self.app(scope, receive, send)
        finally:
            if scope.get("type") == "http":
                path = scope.get("path") or ""
                if heavy_request_path(path):
                    _collect_request()
                note_isolate_heap(path)


app.add_middleware(ReleaseHeavyMemory)

# Keep templates next to this module. The Worker bundle includes src/, not the repo root,
# so a ../templates path exists locally and 500s on every HTML route after deploy.
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_CSS_PATH = Path(__file__).resolve().parent / "static" / "app.css"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

# Browsers request /favicon.ico even when a page also links an icon.
_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="6" fill="#0f172a"/>'
    '<text x="16" y="23" text-anchor="middle" font-size="18" fill="#34d399">&#9834;</text>'
    "</svg>"
)

# Used only when D1 is not bound. password_hash stays empty until the first
# memory login so Worker startup never calls secrets.token_bytes.
USERS = {
    DEMO_EMAIL: {
        "id": 1,
        "email": DEMO_EMAIL,
        "password_hash": "",
        "artist_name": "Demo Artist",
    }
}
NEXT_ID = 2


def _memory_user(email: str) -> dict | None:
    """Fill the demo hash on first use. Register already hashes during the request."""
    user = USERS.get(email)
    if user is None:
        return None
    if email == DEMO_EMAIL and not user.get("password_hash"):
        user["password_hash"] = hash_password(DEMO_PASSWORD)
    return user

# Overlaps the provider fan-out. A stuck D1 read cannot stretch the page.
GROWTH_READ_TIMEOUT_S = 1.5
# One stored Deezer id, then its fans, tracks, and releases. Not part of the history budget.
DEEZER_CARD_TIMEOUT_S = 0.5
DATA_UNAVAILABLE = "Data temporarily unavailable."


def _unavailable(detail: str) -> str:
    return f"{DATA_UNAVAILABLE} {detail}"


def _demo_password(password: str) -> bool:
    return hmac.compare_digest(password, DEMO_PASSWORD)


def _issue_session(request: Request, user: dict, auth: str) -> None:
    request.session["user_id"] = int(user["id"])
    request.session["auth"] = auth
    request.session["email"] = str(user.get("email") or "")
    request.session["artist_name"] = str(user.get("artist_name") or user.get("email") or "")


def _user_from_session(request: Request) -> dict | None:
    """A logged-in shell when D1 cannot be read. It does not grant a new login."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    email = str(request.session.get("email") or "")
    artist_name = request.session.get("artist_name")
    if not artist_name:
        artist_name = "Demo Artist" if email == DEMO_EMAIL else (email or "Artist")
    return {"id": int(user_id), "email": email, "artist_name": str(artist_name)}


def _d1_outage(exc: BaseException) -> bool:
    text = f"{exc.__class__.__name__}: {exc}"
    return "D1_ERROR" in text or "exceeded D1" in text or "row read limit" in text


def _history_from_saved(saved: dict | None) -> dict | None:
    """Audience points already read, when a later read is cancelled or fails."""
    if not isinstance(saved, dict):
        return None
    growth = saved.get("growth")
    if not isinstance(growth, dict):
        return None
    reason = saved.get("reason") or "No exact audience count has been stored yet."
    history = empty_history(reason)
    history["growth"] = growth
    history["artist_id"] = saved.get("artist_id")
    history["name"] = saved.get("name")
    if growth.get("datasets"):
        history["available"] = True
        history["reason"] = None
    else:
        history["reason"] = reason
    # These reads did not finish. Empty-catalog copy would claim the rows are not stored.
    unread = "Data temporarily unavailable. Stored {0} could not be read on this request."
    history["related_reason"] = unread.format("related artists")
    history["sightings_reason"] = unread.format("timeline rows")
    history["playlists_reason"] = unread.format("playlists")
    history["tracks_reason"] = TRACKS_READ_ERROR
    history["track_stats"] = []
    history["track_stats_reason"] = "Data temporarily unavailable. Stored track stats could not be read on this request."
    history["momentum_reason"] = unread.format("momentum")
    history["alerts_reason"] = unread.format("alerts")
    history["releases_reason"] = unread.format("MusicBrainz releases")
    history["posting"] = empty_posting_patterns(POSTING_READ_ERROR)
    history["calendar"] = empty_release_calendar(CALENDAR_READ_ERROR)
    history["charts_reason"] = unread.format("chart positions")
    history["score_reason"] = unread.format("audience counts")
    return history


def _plan_from_growth(saved: dict, now: datetime, *, youtube_key: bool) -> dict | None:
    """A plan that keeps audience points already read when a later read is cancelled."""
    history = _history_from_saved(saved)
    if history is None:
        return None
    clocks: dict = {}
    if not youtube_key:
        clocks["youtube"] = now
    cards = cards_from_history(history, clocks, now, [], youtube_key=youtube_key)
    return {
        "artist": {"id": saved.get("artist_id"), "name": saved.get("name") or ""},
        "history": history,
        "live": [],
        "cards": cards,
        "clocks": clocks,
    }


_QUIET_SOURCE_STATUSES = {"unavailable", "error", "missing_credentials", "pending"}


def _metric_number(metric: dict) -> str | None:
    """A published number only. Unavailable metrics stay blank."""
    if not isinstance(metric, dict):
        return None
    if not metric.get("available") or metric.get("value") is None:
        return None
    return str(metric.get("value"))


def render_source_cards(sources: list[dict] | None) -> str:
    """Source cards are plain strings. Jinja compiling that loop was the slow template step."""
    parts = ['<div class="source-grid">']
    for source in sources or []:
        if not isinstance(source, dict):
            continue
        label = html.escape(str(source.get("label") or ""))
        status_raw = str(source.get("status") or "").strip().lower()
        status = html.escape(status_raw.replace("_", " ") or "unavailable")
        metrics = [metric for metric in (source.get("metrics") or []) if isinstance(metric, dict)]
        numbered = [(metric, _metric_number(metric)) for metric in metrics]
        items = source.get("items") or []
        tracks = source.get("tracks") or []
        quiet = status_raw in _QUIET_SOURCE_STATUSES and not any(value for _, value in numbered) and not items and not tracks
        section_class = "source-card source-card-quiet" if quiet else "source-card"
        parts.append(f'<section class="{section_class}">')
        parts.append('<div class="source-card-head"><div class="min-w-0">')
        parts.append('<h2 class="source-card-title">')
        image = source.get("image_url")
        if image and not quiet:
            parts.append(
                f'<img src="{html.escape(str(image), quote=True)}" alt="" class="w-8 h-8 rounded-md object-cover">'
            )
        parts.append(f"{label}</h2>")
        profile = source.get("profile_name")
        if profile:
            safe_name = html.escape(str(profile))
            url = source.get("profile_url")
            if url:
                parts.append(
                    '<p class="source-card-profile">'
                    f'<a href="{html.escape(str(url), quote=True)}">{safe_name}</a></p>'
                )
            else:
                parts.append(f'<p class="source-card-profile">{safe_name}</p>')
        parts.append("</div>")
        parts.append(f'<span class="source-status">{status}</span></div>')
        reason = source.get("reason")
        if reason:
            parts.append(f'<p class="source-card-reason">{html.escape(str(reason))}</p>')
        if metrics:
            parts.append('<dl class="source-metrics">')
            for metric, value in numbered:
                metric_label = html.escape(str(metric.get("label") or ""))
                if value is not None:
                    parts.append('<div class="source-metric">')
                    parts.append(f'<dt class="source-metric-label">{metric_label}</dt>')
                    parts.append(f'<dd class="stat-value">{html.escape(value)}</dd>')
                else:
                    extra = f" — {html.escape(str(metric.get('reason')))}" if metric.get("reason") else ""
                    parts.append('<div class="source-metric source-metric-quiet">')
                    parts.append(f'<dt class="source-metric-label">{metric_label}</dt>')
                    parts.append(f'<dd class="source-unavailable">Unavailable{extra}</dd>')
                parts.append("</div>")
            parts.append("</dl>")
        if items and not quiet:
            parts.append('<div class="source-items">')
            for item in items[:20]:
                title = html.escape(str(item.get("title") or ""))
                url = item.get("url")
                if url:
                    title = f'<a href="{html.escape(str(url), quote=True)}">{title}</a>'
                detail = html.escape(str(item.get("detail") or item.get("kind") or ""))
                display = html.escape(str(item.get("display") or ""))
                precision = html.escape(str(item.get("precision_label") or ""))
                weekday = html.escape(str(item.get("weekday"))) if item.get("weekday") else ""
                clock = f"{precision} · {weekday}" if weekday else precision
                parts.append(
                    '<div class="source-item"><div class="min-w-0">'
                    f'<div class="source-item-title">{title}</div>'
                    f'<div class="source-item-detail">{detail}</div></div>'
                    '<div class="source-item-time">'
                    f'<div class="calendar-time">{display}</div>'
                    f'<div class="source-item-detail">{clock}</div></div></div>'
                )
            parts.append("</div>")
        if tracks and not quiet:
            parts.append('<ol class="source-tracks">')
            for track in tracks[:10]:
                rank = track.get("rank")
                prefix = f"{int(rank)}. " if isinstance(rank, int) and not isinstance(rank, bool) else ""
                title = html.escape(str(track.get("name") or ""))
                url = track.get("url")
                if url:
                    title = f'<a href="{html.escape(str(url), quote=True)}">{title}</a>'
                album = html.escape(str(track.get("album_name") or ""))
                extra = f' <span class="source-item-detail">{album}</span>' if album else ""
                parts.append(f'<li><span class="source-track-rank">{prefix}</span>{title}{extra}</li>')
            parts.append("</ol>")
        elif source.get("status") == "ok" and not items and not quiet:
            parts.append('<p class="source-card-reason">This source returned profile data, but no dated rows.</p>')
        for note in source.get("notices") or []:
            parts.append(f'<p class="source-note">{html.escape(str(note))}</p>')
        parts.append("</section>")
    parts.append("</div>")
    return "".join(parts)


def _live_related(bundle: dict) -> list[dict]:
    for source in bundle.get("sources") or []:
        if source.get("platform") == "spotify":
            return [
                {
                    "to_name": item.get("name"),
                    "to_spotify_id": item.get("spotify_id"),
                    "rank": item.get("rank"),
                    "first_seen_at": None,
                    "last_seen_at": None,
                }
                for item in source.get("related") or []
                if item.get("name") and item.get("spotify_id")
            ]
    return []


async def _open_read_db(env):
    try:
        return await open_database(env, migrate_schema=False)
    except Exception:
        return None


async def read_dashboard_history(name: str, env) -> dict:
    """A few indexed reads. Migrations and writes stay on the crawl."""
    db = await _open_read_db(env)
    if db is None:
        return empty_history(
            "Growth is stored by the crawler when D1 is bound. This page does not write history."
        )
    try:
        return await asyncio.wait_for(
            load_dashboard_history(db, name, cache=history_page_cache(env, db)),
            GROWTH_READ_TIMEOUT_S,
        )
    except Exception as exc:
        log_read_failure("dashboard growth", exc)
        return empty_history(GROWTH_READ_ERROR)


async def get_current_user(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    if request.session.get("auth") == "d1":
        db = await _open_read_db(request.scope.get("env"))
        if db is None:
            return _user_from_session(request)
        row = None
        try:
            row = await db.query_one("SELECT id, email FROM users WHERE id = ?", (int(user_id),))
        except Exception:
            return _user_from_session(request)
        if not row:
            return None
        email = str(row["email"])
        artist_name = "Demo Artist" if email == DEMO_EMAIL else email
        request.session["email"] = email
        request.session["artist_name"] = artist_name
        return {"id": int(row["id"]), "email": email, "artist_name": artist_name}
    for user in USERS.values():
        if user["id"] == user_id:
            return user
    return None


@app.get("/favicon.ico")
async def favicon():
    return Response(content=_FAVICON_SVG, media_type="image/svg+xml")


@app.get("/static/app.css")
async def app_css():
    if not _CSS_PATH.is_file():
        return Response(status_code=404)
    return Response(
        content=_CSS_PATH.read_text(encoding="utf-8"),
        media_type="text/css",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"user": await get_current_user(request)},
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    # The public demo password is checked here so a D1 outage can still sign
    # that account in. A wrong demo password never touches D1. Other accounts
    # still require the stored hash.
    demo = email == DEMO_EMAIL
    if demo and not _demo_password(password):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid email or password"},
            status_code=400,
        )
    db = await _open_read_db(request.scope.get("env"))
    if db is not None:
        try:
            user = await authenticate(db, email, password)
        except Exception:
            if demo:
                _issue_session(
                    request,
                    {"id": USERS[DEMO_EMAIL]["id"], "email": DEMO_EMAIL, "artist_name": "Demo Artist"},
                    "memory",
                )
                return RedirectResponse("/dashboard", status_code=303)
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": DATA_UNAVAILABLE},
                status_code=503,
            )
        if user:
            _issue_session(request, user, "d1")
            return RedirectResponse("/dashboard", status_code=303)
    else:
        user = _memory_user(email)
        if user and verify_password(password, str(user.get("password_hash") or "")):
            _issue_session(request, user, "memory")
            return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": "Invalid email or password"},
        status_code=400,
    )


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse(request, "register.html", {"error": None})


@app.post("/register")
async def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    artist_name: str = Form(...),
):
    global NEXT_ID
    email = email.strip().lower()
    artist_name = artist_name.strip()
    if len(password) < 6:
        return templates.TemplateResponse(
            request,
            "register.html",
            {"error": "Password must be at least 6 characters"},
            status_code=400,
        )
    if not artist_name or len(artist_name) > 120:
        return templates.TemplateResponse(
            request,
            "register.html",
            {"error": "Enter an artist name up to 120 characters"},
            status_code=400,
        )
    db = await _open_read_db(request.scope.get("env"))
    if db is not None:
        try:
            created = await register_user(db, email, password)
        except Exception:
            return templates.TemplateResponse(
                request,
                "register.html",
                {"error": DATA_UNAVAILABLE},
                status_code=503,
            )
        if created is None:
            return templates.TemplateResponse(
                request,
                "register.html",
                {"error": "Email already registered"},
                status_code=400,
            )
        _issue_session(request, created, "d1")
        return RedirectResponse("/dashboard", status_code=303)
    if any(user.get("email") == email for user in USERS.values()):
        return templates.TemplateResponse(
            request,
            "register.html",
            {"error": "Email already registered"},
            status_code=400,
        )
    USERS[email] = {
        "id": NEXT_ID,
        "email": email,
        "password_hash": hash_password(password),
        "artist_name": artist_name,
    }
    _issue_session(request, USERS[email], "memory")
    NEXT_ID += 1
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/watch", response_class=HTMLResponse)
async def watch_page(request: Request):
    user = await _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    artists = []
    note = None
    if request.session.get("auth") != "d1":
        note = "A watchlist is stored when D1 is bound. This page does not call providers."
    else:
        db = await _open_read_db(request.scope.get("env"))
        failed = False
        if db is not None:
            try:
                artists = await asyncio.wait_for(list_watched(db, int(user["id"])), GROWTH_READ_TIMEOUT_S)
            except Exception:
                failed = True
        if db is None or failed:
            note = "Data temporarily unavailable. The watchlist could not be read on this request."
        elif not artists:
            note = "You are not watching any artists yet."
    return templates.TemplateResponse(
        request,
        "watch.html",
        {"user": user, "artists": artists, "note": note},
    )


@app.post("/watch/{artist_id}")
async def watch_post(request: Request, artist_id: int):
    user = await _require_user(request)
    if not user or request.session.get("auth") != "d1":
        return RedirectResponse("/login", status_code=303)
    db = await _open_read_db(request.scope.get("env"))
    if db is not None and artist_id > 0:
        try:
            await watch_artist(db, int(user["id"]), artist_id)
        except Exception:
            pass
    return RedirectResponse("/watch", status_code=303)


@app.post("/unwatch/{artist_id}")
async def unwatch_post(request: Request, artist_id: int):
    user = await _require_user(request)
    if not user or request.session.get("auth") != "d1":
        return RedirectResponse("/login", status_code=303)
    db = await _open_read_db(request.scope.get("env"))
    if db is not None and artist_id > 0:
        try:
            await unwatch_artist(db, int(user["id"]), artist_id)
        except Exception:
            pass
    return RedirectResponse("/watch", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


async def _require_user(request: Request):
    user = await get_current_user(request)
    if not user:
        return None
    return user


@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request, q: str | None = None):
    user = await _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    query = (q or "").strip()[:120]
    matches: list[dict] = []
    note = None
    if normalize_name(query):
        db = await _open_read_db(request.scope.get("env"))
        if db is None:
            note = "Search needs the crawl database. This page does not call providers."
        else:
            try:
                matches = await asyncio.wait_for(search_artists(db, query), GROWTH_READ_TIMEOUT_S)
            except Exception:
                matches = []
                note = "Data temporarily unavailable. Search could not be read on this request."
    return templates.TemplateResponse(
        request,
        "search.html",
        {"user": user, "query": query, "matches": matches, "note": note},
    )


@app.get("/artists", response_class=HTMLResponse)
async def artists_page(request: Request, page: int = 1, sort: str | None = None):
    """Tracked artists already in D1. No provider fan-out."""
    user = await _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    db = await _open_read_db(request.scope.get("env"))
    if db is None:
        return templates.TemplateResponse(
            request,
            "artists.html",
            {
                "user": user,
                "artists": [],
                "page": 1,
                "pages": 1,
                "total": 0,
                "sort": "listeners" if sort == "listeners" else "name",
                "has_prev": False,
                "has_next": False,
                "note": "The artist directory is stored by the crawler when D1 is bound. This page does not call providers.",
            },
        )
    listing = None
    failed = False
    try:
        listing = await asyncio.wait_for(
            list_tracked_artists(db, page=page, sort=sort or "name"),
            GROWTH_READ_TIMEOUT_S,
        )
    except Exception:
        failed = True
    if failed or listing is None:
        listing = {
            "artists": [],
            "page": 1,
            "pages": 1,
            "total": 0,
            "sort": "listeners" if sort == "listeners" else "name",
            "has_prev": False,
            "has_next": False,
        }
        note = "Data temporarily unavailable. The artist directory could not be read on this request."
    else:
        note = None
    return templates.TemplateResponse(
        request,
        "artists.html",
        {"user": user, "note": note, **listing},
    )


@app.get("/trending", response_class=HTMLResponse)
async def trending_page(request: Request, window: str | None = None, provider: str | None = None):
    """Stored growth alerts ranked by percent, then absolute change. No provider fan-out."""
    user = await _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    chosen_window = window if window in {"24h", "7d"} else ""
    chosen_provider = provider if provider in {"spotify", "instagram", "tiktok"} else ""
    db = await _open_read_db(request.scope.get("env"))
    alerts: list[dict] = []
    failed = False
    if db is not None:
        try:
            alerts = await asyncio.wait_for(
                list_trending(db, window=chosen_window, provider=chosen_provider),
                GROWTH_READ_TIMEOUT_S,
            )
        except Exception:
            failed = True
    if db is None:
        reason = "Trending is stored by the crawler when D1 is bound. This page does not call providers."
    elif failed:
        reason = "Data temporarily unavailable. Stored alerts could not be read on this request."
    elif alerts:
        reason = ""
    elif chosen_window or chosen_provider:
        reason = "No stored alerts match that window or provider."
    else:
        reason = ALERTS_EMPTY
    return templates.TemplateResponse(
        request,
        "trending.html",
        {
            "user": user,
            "alerts": alerts,
            "reason": reason,
            "window": chosen_window,
            "provider": chosen_provider,
        },
    )


@app.get("/alerts", response_class=HTMLResponse)
async def alerts_page(request: Request):
    """Recent stored growth alerts. No provider fan-out."""
    user = await _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    db = await _open_read_db(request.scope.get("env"))
    alerts: list[dict] = []
    failed = False
    if db is not None:
        try:
            alerts = await asyncio.wait_for(load_alert_feed(db), GROWTH_READ_TIMEOUT_S)
        except Exception:
            failed = True
    if db is None:
        reason = "Alerts are stored by the crawler when D1 is bound. This page does not call providers."
    elif failed:
        reason = "Data temporarily unavailable. Stored alerts could not be read on this request."
    elif alerts:
        reason = ""
    else:
        reason = ALERTS_EMPTY
    return templates.TemplateResponse(
        request,
        "alerts.html",
        {"user": user, "alerts": alerts, "reason": reason},
    )


def _digest_user_id(request: Request, user: dict) -> int | None:
    """A watchlist exists only for a D1 session. Memory demo login has none."""
    if request.session.get("auth") != "d1":
        return None
    try:
        user_id = int(user["id"])
    except (TypeError, ValueError):
        return None
    if user_id < 1:
        return None
    return user_id


async def _digest_view(request: Request, user: dict, threshold: str) -> dict:
    chosen = parse_threshold(threshold)
    db = await _open_read_db(request.scope.get("env"))
    if db is None:
        return empty_digest(DIGEST_DB_NOTE, failed=True, threshold=chosen)
    try:
        return await asyncio.wait_for(
            build_digest(
                db,
                user_id=_digest_user_id(request, user),
                threshold=threshold,
                now=datetime.now(timezone.utc),
            ),
            GROWTH_READ_TIMEOUT_S,
        )
    except Exception as exc:
        log_read_failure("digest", exc)
        return empty_digest(DIGEST_READ_ERROR, failed=True, threshold=chosen)


@app.get("/digest.csv")
async def digest_csv(request: Request, threshold: str = ""):
    """The digest as CSV. Stored rows only. No provider calls and no writes."""
    user = await _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    view = await _digest_view(request, user, threshold)
    if view.get("failed"):
        return Response(
            f"{view.get('read_error') or DIGEST_READ_ERROR}\n",
            status_code=503,
            media_type="text/plain; charset=utf-8",
        )
    return Response(
        content=render_digest_csv(view),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="digest.csv"'},
    )


@app.get("/digest.json")
async def digest_json(request: Request, threshold: str = ""):
    """The digest as JSON. Stored rows only. No provider calls and no writes."""
    user = await _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    view = await _digest_view(request, user, threshold)
    status = 503 if view.get("failed") else 200
    return Response(render_digest_json(view), status_code=status, media_type="application/json")


@app.get("/digest", response_class=HTMLResponse)
async def digest_page(request: Request, threshold: str = ""):
    """Watchlist digest for the last 7 days. Stored rows only. No writes."""
    user = await _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    view = await _digest_view(request, user, threshold)
    return templates.TemplateResponse(request, "digest.html", {"user": user, **view})


async def _timing_view(request: Request, user: dict, scope: str, days: str, kind: str) -> dict:
    chosen_scope = parse_timing_scope(scope)
    chosen_days = parse_timing_days(days)
    chosen_kind = parse_timing_kind(kind)
    db = await _open_read_db(request.scope.get("env"))
    if db is None:
        return empty_timing(
            TIMING_DB_NOTE,
            failed=True,
            scope=chosen_scope,
            days=chosen_days,
            kind=chosen_kind,
        )
    try:
        view = await asyncio.wait_for(
            build_timing(
                db,
                user_id=_digest_user_id(request, user),
                scope=chosen_scope,
                days=chosen_days,
                kind=chosen_kind,
                now=datetime.now(timezone.utc),
            ),
            GROWTH_READ_TIMEOUT_S,
        )
    except Exception as exc:
        log_read_failure("timing", exc)
        view = empty_timing(
            TIMING_READ_ERROR,
            failed=True,
            scope=chosen_scope,
            days=chosen_days,
            kind=chosen_kind,
        )
    # The read is finished. Drop the binding before the template or export encodes the view.
    db = None
    return view


@app.get("/timing.csv")
async def timing_csv(request: Request, scope: str = "", days: str = "", kind: str = ""):
    """Per-artist timing as CSV. Stored rows only. No provider calls and no writes."""
    user = await _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    view = await _timing_view(request, user, scope, days, kind)
    if view.get("failed"):
        return Response(
            f"{view.get('read_error') or TIMING_READ_ERROR}\n",
            status_code=503,
            media_type="text/plain; charset=utf-8",
        )
    release_render_fields(view)
    body = render_timing_csv(view)
    del view
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="timing.csv"'},
    )


@app.get("/timing.json")
async def timing_json(request: Request, scope: str = "", days: str = "", kind: str = ""):
    """The timing view as JSON. Stored rows only. No provider calls and no writes."""
    user = await _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    view = await _timing_view(request, user, scope, days, kind)
    status = 503 if view.get("failed") else 200
    release_render_fields(view)
    body = render_timing_json(view)
    del view
    return Response(body, status_code=status, media_type="application/json")


@app.get("/timing", response_class=HTMLResponse)
async def timing_page(request: Request, scope: str = "", days: str = "", kind: str = ""):
    """Release and post timing for a watchlist or every tracked artist. No writes."""
    user = await _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    view = await _timing_view(request, user, scope, days, kind)
    return templates.TemplateResponse(request, "timing.html", {"user": user, **view})


@app.get("/charts", response_class=HTMLResponse)
async def charts_page(request: Request, date: str = ""):
    """Stored US iTunes charts for one date. No feed fetch on this request."""
    user = await _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    db = await _open_read_db(request.scope.get("env"))
    board = {
        "songs": [],
        "albums": [],
        "songs_date": "",
        "albums_date": "",
        "dates": [],
        "selected_date": "",
    }
    failed = False
    if db is not None:
        try:
            board = await asyncio.wait_for(load_chart_board(db, date), GROWTH_READ_TIMEOUT_S)
        except Exception:
            failed = True
    if db is None:
        reason = "Charts are stored by the crawler when D1 is bound. This page does not fetch Apple or Spotify."
    elif failed:
        reason = "Data temporarily unavailable. Stored chart positions could not be read on this request."
    elif board["songs"] or board["albums"]:
        reason = ""
    else:
        reason = CHARTS_PAGE_EMPTY
    return templates.TemplateResponse(
        request,
        "charts.html",
        {"user": user, "reason": reason, **board},
    )


@app.get("/scores", response_class=HTMLResponse)
async def scores_page(request: Request):
    """Rank tracked artists by the transparent audience score. No provider calls."""
    user = await _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    db = await _open_read_db(request.scope.get("env"))
    ranked: list[dict] = []
    failed = False
    if db is not None:
        try:
            ranked = await asyncio.wait_for(rank_scores(db), GROWTH_READ_TIMEOUT_S)
        except Exception:
            failed = True
    if db is None:
        reason = "Scores are computed from counts the crawler stored. This page does not call providers."
    elif failed:
        reason = "Data temporarily unavailable. Stored audience counts could not be read on this request."
    elif ranked:
        reason = ""
    else:
        reason = SCORE_RANK_EMPTY
    return templates.TemplateResponse(
        request,
        "scores.html",
        {"user": user, "scores": ranked, "reason": reason, "formula": SCORE_FORMULA},
    )


def _compare_slots(a: str, b: str, c: str, d: str) -> list[str]:
    return [a or "", b or "", c or "", d or ""]


@app.get("/compare.csv")
async def compare_csv(request: Request, a: str = "", b: str = "", c: str = "", d: str = ""):
    """The comparison table as CSV. Stored rows only. No provider calls and no writes."""
    user = await _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    db = await _open_read_db(request.scope.get("env"))
    if db is None:
        return Response(
            "Data temporarily unavailable. Stored history could not be read on this request.\n",
            status_code=503,
            media_type="text/plain; charset=utf-8",
        )
    try:
        view = await asyncio.wait_for(
            build_compare_view(
                db,
                slots=_compare_slots(a, b, c, d),
                user_id=int(user["id"]) if request.session.get("auth") == "d1" else None,
                redirect=False,
            ),
            GROWTH_READ_TIMEOUT_S,
        )
    except Exception as exc:
        log_read_failure("compare csv", exc)
        return Response(
            "Data temporarily unavailable. Stored history could not be read on this request.\n",
            status_code=503,
            media_type="text/plain; charset=utf-8",
        )
    if not view.get("ready"):
        return Response(
            "Pick at least two tracked artists.\n",
            status_code=404,
            media_type="text/plain; charset=utf-8",
        )
    return Response(
        content=render_compare_csv(view["artists"], view["rows"]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="compare.csv"'},
    )


@app.get("/compare", response_class=HTMLResponse)
async def compare_page(
    request: Request,
    a: str = "",
    b: str = "",
    c: str = "",
    d: str = "",
    metric: str = "",
    q: str = "",
):
    """Two to four stored artists, side by side. No provider calls and no writes."""
    user = await _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    db = await _open_read_db(request.scope.get("env"))
    if db is None:
        return templates.TemplateResponse(
            request,
            "compare.html",
            {"user": user, **empty_compare_view("Comparison reads stored crawl data when D1 is bound. This page does not call providers.")},
        )
    try:
        view = await asyncio.wait_for(
            build_compare_view(
                db,
                slots=_compare_slots(a, b, c, d),
                metric=metric,
                query=q,
                user_id=int(user["id"]) if request.session.get("auth") == "d1" else None,
            ),
            GROWTH_READ_TIMEOUT_S,
        )
    except Exception as exc:
        log_read_failure("compare", exc)
        view = empty_compare_view("Data temporarily unavailable. Stored history could not be read on this request.")
    if view.get("redirect"):
        return RedirectResponse(view["redirect"], status_code=303)
    return templates.TemplateResponse(request, "compare.html", {"user": user, **view})


@app.get("/artist/{key}/export.csv")
async def export_artist_csv(request: Request, key: str):
    """Stored snapshots, sightings, and related edges. No provider fan-out."""
    user = await _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    token = (key or "").strip()[:120]
    db = await _open_read_db(request.scope.get("env"))
    artist_id = None
    failed = False
    if db is not None and token.isdigit():
        artist_id = int(token)
    elif db is not None and token:
        try:
            artist = await asyncio.wait_for(find_artist(db, token), GROWTH_READ_TIMEOUT_S)
        except Exception:
            artist = None
            failed = True
        if artist is not None:
            return RedirectResponse(f"/artist/{int(artist['id'])}/export.csv", status_code=303)
    artist = None
    if db is not None and artist_id and not failed:
        try:
            artist = await asyncio.wait_for(find_artist_by_id(db, artist_id), GROWTH_READ_TIMEOUT_S)
        except Exception:
            artist = None
            failed = True
    if artist is None:
        if db is None or failed:
            text = "Data temporarily unavailable. Stored history could not be read on this request.\n"
        else:
            text = "This artist is not tracked.\n"
        return Response(text, status_code=404, media_type="text/plain; charset=utf-8")
    rows = None
    try:
        rows = await asyncio.wait_for(load_export_rows(db, int(artist["id"])), GROWTH_READ_TIMEOUT_S)
    except Exception:
        rows = None
    if rows is None:
        return Response(
            "Data temporarily unavailable. Stored history could not be read on this request.\n",
            status_code=503,
            media_type="text/plain; charset=utf-8",
        )
    body = render_export_csv(
        rows["metrics"],
        rows["sightings"],
        rows["edges"],
        rows.get("releases") or [],
        rows.get("tracks") or [],
        rows.get("charts") or [],
        rows.get("playlists") or [],
        rows.get("track_stats") or [],
    )
    filename = f"artist-{int(artist['id'])}.csv"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/artist/{key}/related", response_class=HTMLResponse)
async def related_graph_page(request: Request, key: str):
    """Ego network from related_edges. No provider fan-out and no queue send."""
    user = await _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    token = (key or "").strip()[:120]
    db = await _open_read_db(request.scope.get("env"))
    artist_id = None
    failed = False
    if db is not None and token.isdigit():
        artist_id = int(token)
    elif db is not None and token:
        try:
            artist = await asyncio.wait_for(find_artist(db, token), GROWTH_READ_TIMEOUT_S)
        except Exception:
            artist = None
            failed = True
        if artist is not None:
            return RedirectResponse(f"/artist/{int(artist['id'])}/related", status_code=303)
    graph = None
    if db is not None and artist_id:
        try:
            graph = await asyncio.wait_for(load_related_graph(db, artist_id), GROWTH_READ_TIMEOUT_S)
        except Exception:
            failed = True
    if graph is None:
        if db is None or failed:
            message = _unavailable("Crawl history is not available on this request. This page does not call providers.")
        else:
            message = "This artist is not tracked yet. This page does not look them up on Spotify."
        return templates.TemplateResponse(
            request,
            "artist.html",
            {
                "user": user,
                "found": False,
                "title": "Not tracked",
                "subtitle": "Stored artists only.",
                "message": message,
                "history": empty_history(message),
            },
            status_code=404,
        )
    return templates.TemplateResponse(
        request,
        "related.html",
        {"user": user, "title": graph["center_name"], "graph": graph},
    )


async def _resolve_stored_artist(request: Request, key: str, redirect_suffix: str = ""):
    """The artists row, or a Response when the page should stop.

    Name lookups redirect to the id and keep the query string. This does not call providers.
    """
    user = await _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    token = (key or "").strip()[:120]
    db = await _open_read_db(request.scope.get("env"))
    artist = None
    lookup_failed = False
    if db is not None and token.isdigit():
        try:
            artist = await asyncio.wait_for(find_artist_by_id(db, int(token)), GROWTH_READ_TIMEOUT_S)
        except Exception:
            artist = None
            lookup_failed = True
    elif db is not None and token:
        try:
            artist = await asyncio.wait_for(find_artist(db, token), GROWTH_READ_TIMEOUT_S)
        except Exception:
            artist = None
            lookup_failed = True
        if artist is not None:
            location = f"/artist/{int(artist['id'])}{redirect_suffix}"
            query = request.url.query
            if query:
                location = f"{location}?{query}"
            return RedirectResponse(location, status_code=303)
    if artist is None:
        if db is None or lookup_failed:
            message = _unavailable("Crawl history is not available on this request. This page does not call providers.")
        else:
            message = "This artist is not tracked yet. This page does not look them up on Spotify, MusicBrainz, or the other providers."
        return templates.TemplateResponse(
            request,
            "artist.html",
            {
                "user": user,
                "found": False,
                "title": "Not tracked",
                "subtitle": "Stored artists only.",
                "message": message,
                "history": empty_history(message),
            },
            status_code=404,
        )
    return user, artist, db


async def _read_deezer_card(db, artist: dict) -> dict:
    """After history is loaded. A timeout here keeps that history and says the card could not be read."""
    try:
        return await asyncio.wait_for(
            load_deezer_card(db, int(artist["id"]), artist.get("name"), artist),
            DEEZER_CARD_TIMEOUT_S,
        )
    except Exception as exc:
        log_read_failure("deezer card", exc)
        known = {"deezer_id": artist.get("deezer_id"), "deezer_nb_album": artist.get("deezer_nb_album")}
        if "deezer_id" not in artist and "deezer_nb_album" not in artist:
            known = None
        return present_deezer_card(str(artist.get("name") or ""), known, None, [], [], failed=True)


async def _load_stored_artist(request: Request, key: str, redirect_suffix: str = ""):
    """Artist row plus stored history, or a Response when the page should stop."""
    loaded = await _resolve_stored_artist(request, key, redirect_suffix)
    if not isinstance(loaded, tuple):
        return loaded
    user, artist, db = loaded
    kept: dict = {}
    try:
        history = await asyncio.wait_for(
            load_history_for_artist(
                db,
                artist,
                on_growth=kept.update,
                cache=history_page_cache(request.scope.get("env"), db),
            ),
            GROWTH_READ_TIMEOUT_S,
        )
    except Exception as exc:
        log_read_failure("artist history", exc)
        history = _history_from_saved(kept)
        if history is None:
            history = empty_history("Data temporarily unavailable. Stored history could not be read on this request.")
    # The card has its own short budget. A stalled history read must not skip it.
    history["deezer"] = await _read_deezer_card(db, artist)
    return user, artist, history


@app.get("/artist/{key}/calendar", response_class=HTMLResponse)
async def artist_calendar(request: Request, key: str, year: str | None = None, month: str | None = None):
    """One stored release year, or one month when `?month=` is set. No provider fan-out."""
    loaded = await _resolve_stored_artist(request, key, "/calendar")
    if not isinstance(loaded, tuple):
        return loaded
    user, artist, db = loaded
    artist_id = int(artist["id"])
    # A present month param, even an invalid one, selects that month. Missing stays on the year page.
    month_requested = "month" in request.query_params
    if month_requested:
        selected_year, selected_month = parse_calendar_month(month)
        year_arg: int | None = selected_year
    else:
        selected_year, selected_month = None, None
        year_arg = parse_calendar_period(year, request.query_params.get("from"))
    try:
        history = await asyncio.wait_for(load_calendar_page(db, artist, year_arg), GROWTH_READ_TIMEOUT_S)
    except Exception:
        history = empty_history("Data temporarily unavailable. Stored history could not be read on this request.")
        history["artist_id"] = artist_id
    calendar = history.get("calendar") if isinstance(history.get("calendar"), dict) else empty_release_calendar()
    if month_requested and selected_year is not None and selected_month is not None:
        history["calendar"] = focus_release_month(calendar, selected_year, selected_month, artist_id)
    else:
        history["calendar"] = ensure_month_nav(calendar, artist_id)
    return templates.TemplateResponse(
        request,
        "calendar.html",
        {
            "user": user,
            "title": artist.get("name") or "Artist",
            "artist_id": artist_id,
            "subtitle": "Stored dates only. Open a month, or a year. This page does not fetch providers.",
            "history": history,
        },
    )


@app.get("/artist/{key}", response_class=HTMLResponse)
async def artist_page(request: Request, key: str):
    """Stored history for one artist. No provider fan-out and no queue send."""
    loaded = await _load_stored_artist(request, key)
    if not isinstance(loaded, tuple):
        return loaded
    user, artist, history = loaded
    crawled = artist.get("last_crawled_at")
    subtitle = "Stored crawl history. This page does not fetch providers."
    if crawled:
        subtitle = f"Last crawled {crawled}. " + subtitle
    return templates.TemplateResponse(
        request,
        "artist.html",
        {
            "user": user,
            "found": True,
            "title": artist.get("name") or "Artist",
            "subtitle": subtitle,
            "message": "",
            "history": history,
            "deezer_card": render_source_cards([history["deezer"]]) if history.get("deezer") else "",
            "can_watch": request.session.get("auth") == "d1",
        },
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, artist: str | None = None):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    query = (artist or "").strip() or user["artist_name"]
    if len(query) > 120:
        query = query[:120]
    env = request.scope.get("env")
    # The crawl still runs in the background. This request does not wait on a slow send.
    enqueue_task = asyncio.create_task(enqueue_search(query, env))
    now = datetime.now(timezone.utc)
    youtube_key = bool(env_get(env, "YOUTUBE_API_KEY"))
    db = await _open_read_db(env)
    plan = None
    kept_growth: dict = {}
    if db is not None:
        try:
            plan = await asyncio.wait_for(
                load_dashboard_plan(
                    db,
                    query,
                    now,
                    youtube_key=youtube_key,
                    on_growth=kept_growth.update,
                    cache=history_page_cache(env, db),
                ),
                GROWTH_READ_TIMEOUT_S,
            )
        except Exception as exc:
            log_read_failure("dashboard plan", exc)
            plan = _plan_from_growth(kept_growth, now, youtube_key=youtube_key)
    if db is None:
        history = empty_history(
            "Growth is stored by the crawler when D1 is bound. This page does not write history."
        )
        bundle = queued_bundle()
        bundle["insights"] = [history["reason"]]
    elif plan is None:
        history = empty_history(GROWTH_READ_ERROR)
        bundle = queued_bundle()
        bundle["insights"] = ["No provider was fetched on this request."]
    elif plan["artist"] is None:
        history = plan["history"]
        bundle = queued_bundle()
    else:
        history = plan["history"]
        live = plan["live"]
        live_bundle = None
        failed = False
        if live:
            skip = [platform for platform in ("musicbrainz", "apple", "spotify", "youtube", "instagram", "tiktok") if platform not in live]
            try:
                live_bundle = await collect_artist(
                    query,
                    env=env,
                    known_links=known_links(plan["artist"]),
                    include_related=False,
                    skip_platforms=skip,
                )
            except Exception:
                failed = True
        if failed or not live:
            cards = plan["cards"] if not failed else cards_from_history(
                history, plan["clocks"], now, [], youtube_key=youtube_key
            )
            bundle = assemble_bundle(cards, None, [], stored_only=not failed and not live)
            if failed:
                bundle["insights"] = ["The live refresh failed. Stored counts are shown."] + bundle["insights"]
                bundle["insights"] = bundle["insights"][:8]
        else:
            bundle = assemble_bundle(plan["cards"], live_bundle, live, stored_only=False)
    for source in bundle.get("sources") or []:
        items = source.get("items") or []
        if len(items) > 20:
            source["items"] = items[:20]
    live_related = _live_related(bundle)
    if live_related and not history.get("related"):
        history["related"] = live_related
    try:
        await enqueue_task
    except Exception:
        pass
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "query": query,
            "sources": bundle["sources"],
            "source_cards": render_source_cards(bundle["sources"]),
            "patterns": bundle.get("patterns") or {
                "release_hours": [],
                "social_hours": [],
                "weekdays": [],
                "hour_labels": [],
                "weekday_labels": [],
            },
            "insights": bundle.get("insights") or [],
            "history": history,
        },
    )


@app.get("/admin/diag", response_class=HTMLResponse)
async def admin_diag(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    try:
        report = await diagnose(request.scope.get("env"), HttpClient(deadline=None))
    except Exception:
        report = {
            "secrets": [],
            "youtube": {
                "key_present": False,
                "built_query_url": None,
                "header_request": None,
                "query_request": None,
                "dashboard_sends": "The diagnostic could not run.",
            },
            "apple": {},
        }
    return templates.TemplateResponse(request, "diag.html", {"user": user, "report": report})


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    try:
        report = await status_report(request.scope.get("env"))
    except Exception:
        report = {
            "database": "unavailable",
            "reason": DATA_UNAVAILABLE,
            "mode": "cursor",
            "schedule": "*/15 * * * *",
            "artists": 0,
            "related_artists": 0,
            "snapshots": 0,
            "edges": 0,
            "cursor": 0,
            "runs": [],
            "settings": settings(request.scope.get("env")),
        }
    return templates.TemplateResponse(
        request,
        "admin.html",
        {"user": user, "report": report},
    )


@app.post("/admin/crawl")
async def admin_crawl(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    try:
        await run_scheduled(request.scope.get("env"))
    except Exception:
        pass
    return RedirectResponse("/admin", status_code=303)


class _Entrypoint:
    """Used when the workers runtime is not installed. Production subclasses the ASGI entrypoint."""

    def __init__(self, ctx=None, env=None):
        self.ctx = ctx
        self.env = env


if asgi is not None:
    _Entrypoint = asgi.entrypoint(app)


class Default(_Entrypoint):
    """Dashboard fetch handler only.

    Cron and the queue consumer run on artist-analytics-crawler so a CPU kill
    in this isolate cannot poison crawl invocations, and a crawl kill cannot
    poison dashboard isolates.
    """
