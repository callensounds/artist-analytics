import asyncio
import html
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

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
    status_report,
)
from diag import diagnose
from export_csv import load_export_rows, render_export_csv
from graph import load_related_graph
from analytics import normalize_name
from dashboard_refresh import assemble_bundle, cards_from_history, load_dashboard_plan, queued_bundle
from history import (
    ALERTS_EMPTY,
    empty_history,
    find_artist,
    find_artist_by_id,
    list_tracked_artists,
    list_trending,
    load_alert_feed,
    load_dashboard_history,
    load_history_for_artist,
    search_artists,
)
from http_client import HttpClient
from passwords import hash_password, verify_password
from providers import collect_artist
from runtime import FALLBACK_SESSION_SECRET, WorkerSessionMiddleware, env_get

try:
    from workers import asgi
except ImportError:  # The workers runtime exists on Cloudflare, not in local tests.
    asgi = None

app = FastAPI(title="Average Analytics")
app.add_middleware(WorkerSessionMiddleware, fallback_secret=FALLBACK_SESSION_SECRET)

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


def render_source_cards(sources: list[dict] | None) -> str:
    """Source cards are plain strings. Jinja compiling that loop was the slow template step."""
    parts = ['<div class="grid lg:grid-cols-2 gap-6">']
    for source in sources or []:
        label = html.escape(str(source.get("label") or ""))
        status = html.escape(str(source.get("status") or "").replace("_", " "))
        parts.append('<section class="bg-slate-800 rounded-2xl p-5 border border-slate-700">')
        parts.append('<div class="flex items-start justify-between gap-3 mb-3"><div>')
        parts.append('<h2 class="text-lg font-semibold flex items-center gap-2">')
        image = source.get("image_url")
        if image:
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
                    '<p class="text-sm text-slate-300">'
                    f'<a href="{html.escape(str(url), quote=True)}" class="text-emerald-400 hover:underline">{safe_name}</a></p>'
                )
            else:
                parts.append(f'<p class="text-sm text-slate-300">{safe_name}</p>')
        parts.append("</div>")
        parts.append(f'<span class="text-xs uppercase tracking-wide text-slate-400">{status}</span></div>')
        reason = source.get("reason")
        if reason:
            parts.append(f'<p class="text-sm text-slate-300 mb-3">{html.escape(str(reason))}</p>')
        metrics = source.get("metrics") or []
        if metrics:
            parts.append('<dl class="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-4">')
            for metric in metrics:
                parts.append('<div class="bg-slate-900/50 rounded-xl p-3">')
                parts.append(f'<dt class="text-xs text-slate-400">{html.escape(str(metric.get("label") or ""))}</dt>')
                if metric.get("available") and metric.get("value") is not None:
                    parts.append(
                        f'<dd class="text-sm font-medium mt-1 break-words">{html.escape(str(metric.get("value")))}</dd>'
                    )
                else:
                    extra = f" — {html.escape(str(metric.get('reason')))}" if metric.get("reason") else ""
                    parts.append(f'<dd class="text-sm text-slate-500 mt-1">Unavailable{extra}</dd>')
                parts.append("</div>")
            parts.append("</dl>")
        items = source.get("items") or []
        if items:
            parts.append('<div class="max-h-80 overflow-y-auto divide-y divide-slate-700">')
            for item in items[:20]:
                title = html.escape(str(item.get("title") or ""))
                url = item.get("url")
                if url:
                    title = f'<a href="{html.escape(str(url), quote=True)}" class="hover:text-emerald-300">{title}</a>'
                detail = html.escape(str(item.get("detail") or item.get("kind") or ""))
                display = html.escape(str(item.get("display") or ""))
                precision = html.escape(str(item.get("precision_label") or ""))
                weekday = html.escape(str(item.get("weekday"))) if item.get("weekday") else ""
                clock = f"{precision} · {weekday}" if weekday else precision
                parts.append(
                    '<div class="py-2 flex justify-between gap-3"><div class="min-w-0">'
                    f'<div class="text-sm font-medium truncate">{title}</div>'
                    f'<div class="text-xs text-slate-500">{detail}</div></div>'
                    '<div class="text-right shrink-0">'
                    f'<div class="font-mono text-sm text-emerald-300">{display}</div>'
                    f'<div class="text-xs text-slate-500">{clock}</div></div></div>'
                )
            parts.append("</div>")
        elif source.get("status") == "ok":
            parts.append('<p class="text-sm text-slate-400">This source returned profile data, but no dated rows.</p>')
        for note in source.get("notices") or []:
            parts.append(f'<p class="text-xs text-slate-500 mt-3">{html.escape(str(note))}</p>')
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
        return await asyncio.wait_for(load_dashboard_history(db, name), GROWTH_READ_TIMEOUT_S)
    except Exception:
        return empty_history("Stored growth could not be read on this request.")


async def get_current_user(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    if request.session.get("auth") == "d1":
        db = await _open_read_db(request.scope.get("env"))
        if db is None:
            return None
        row = None
        try:
            row = await db.query_one("SELECT id, email FROM users WHERE id = ?", (int(user_id),))
        except Exception:
            return None
        if not row:
            return None
        email = str(row["email"])
        return {"id": int(row["id"]), "email": email, "artist_name": "Demo Artist" if email == "artist@demo.com" else email}
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
    db = await _open_read_db(request.scope.get("env"))
    if db is not None:
        user = await authenticate(db, email, password)
        if user:
            request.session["user_id"] = user["id"]
            request.session["auth"] = "d1"
            return RedirectResponse("/dashboard", status_code=303)
    else:
        user = _memory_user(email)
        if user and verify_password(password, str(user.get("password_hash") or "")):
            request.session["user_id"] = user["id"]
            request.session["auth"] = "memory"
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
        created = await register_user(db, email, password)
        if created is None:
            return templates.TemplateResponse(
                request,
                "register.html",
                {"error": "Email already registered"},
                status_code=400,
            )
        request.session["user_id"] = created["id"]
        request.session["auth"] = "d1"
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
    request.session["user_id"] = NEXT_ID
    request.session["auth"] = "memory"
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
            note = "The watchlist could not be read on this request."
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
                note = "Search could not be read on this request."
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
        note = "The artist directory could not be read on this request."
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
        reason = "Stored alerts could not be read on this request."
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
        reason = "Stored alerts could not be read on this request."
    elif alerts:
        reason = ""
    else:
        reason = ALERTS_EMPTY
    return templates.TemplateResponse(
        request,
        "alerts.html",
        {"user": user, "alerts": alerts, "reason": reason},
    )


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
            text = "Stored history could not be read on this request.\n"
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
            "Stored history could not be read on this request.\n",
            status_code=503,
            media_type="text/plain; charset=utf-8",
        )
    body = render_export_csv(rows["metrics"], rows["sightings"], rows["edges"], rows.get("releases") or [])
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
            message = "Crawl history is not available on this request. This page does not call providers."
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


@app.get("/artist/{key}", response_class=HTMLResponse)
async def artist_page(request: Request, key: str):
    """Stored history for one artist. No provider fan-out and no queue send."""
    user = await _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    token = (key or "").strip()[:120]
    db = await _open_read_db(request.scope.get("env"))
    artist = None
    if db is not None and token.isdigit():
        try:
            artist = await asyncio.wait_for(find_artist_by_id(db, int(token)), GROWTH_READ_TIMEOUT_S)
        except Exception:
            artist = None
    elif db is not None and token:
        try:
            artist = await asyncio.wait_for(find_artist(db, token), GROWTH_READ_TIMEOUT_S)
        except Exception:
            artist = None
        if artist is not None:
            return RedirectResponse(f"/artist/{int(artist['id'])}", status_code=303)
    if artist is None:
        if db is None:
            message = "Crawl history is not available on this request. This page does not call providers."
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
    try:
        history = await asyncio.wait_for(load_history_for_artist(db, artist), GROWTH_READ_TIMEOUT_S)
    except Exception:
        history = empty_history("Stored history could not be read on this request.")
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
    if db is not None:
        try:
            plan = await asyncio.wait_for(
                load_dashboard_plan(db, query, now, youtube_key=youtube_key),
                GROWTH_READ_TIMEOUT_S,
            )
        except Exception:
            plan = None
    if db is None:
        history = empty_history(
            "Growth is stored by the crawler when D1 is bound. This page does not write history."
        )
        bundle = queued_bundle()
        bundle["insights"] = [history["reason"]]
    elif plan is None:
        history = empty_history("Stored growth could not be read on this request.")
        bundle = queued_bundle()
        bundle["insights"] = [history["reason"]]
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
    report = await status_report(request.scope.get("env"))
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
