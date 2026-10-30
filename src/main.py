import asyncio
import secrets
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from crawl import (
    RETRY_DELAY_SECONDS,
    _exc_fields,
    _finish_message,
    _log,
    enqueue_search,
    handler_env,
    open_database,
    run_queue_batch,
    run_scheduled,
    status_report,
)
from diag import diagnose
from history import empty_history, load_growth
from http_client import HttpClient
from providers import collect_artist
from runtime import FALLBACK_SESSION_SECRET, WorkerSessionMiddleware

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

# Demo login stays available even when accounts are only in memory.
USERS = {
    "artist@demo.com": {
        "id": 1,
        "email": "artist@demo.com",
        "password": "demo123",
        "artist_name": "Demo Artist",
    }
}
NEXT_ID = 2

# Overlaps the provider fan-out. A stuck D1 read cannot stretch the page.
GROWTH_READ_TIMEOUT_S = 1.5


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


async def read_dashboard_history(name: str, env) -> dict:
    """At most one indexed read. Migrations and writes stay on the crawl."""
    try:
        db = await open_database(env, migrate_schema=False)
    except Exception:
        db = None
    if db is None:
        return empty_history(
            "Growth is stored by the crawler when D1 is bound. This page does not write history."
        )
    try:
        return await asyncio.wait_for(load_growth(db, name), GROWTH_READ_TIMEOUT_S)
    except Exception:
        return empty_history("Stored growth could not be read on this request.")


def get_current_user(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
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
        {"user": get_current_user(request)},
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    user = USERS.get(email.strip().lower())
    if user and secrets.compare_digest(user["password"], password):
        request.session["user_id"] = user["id"]
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
    if email in USERS:
        return templates.TemplateResponse(
            request,
            "register.html",
            {"error": "Email already registered"},
            status_code=400,
        )
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
    USERS[email] = {
        "id": NEXT_ID,
        "email": email,
        "password": password,
        "artist_name": artist_name,
    }
    request.session["user_id"] = NEXT_ID
    NEXT_ID += 1
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, artist: str | None = None):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    query = (artist or "").strip() or user["artist_name"]
    if len(query) > 120:
        query = query[:120]
    env = request.scope.get("env")
    # Enqueue and the growth read run beside the provider calls. Both are bounded.
    enqueue_task = asyncio.create_task(enqueue_search(query, env))
    history_task = asyncio.create_task(read_dashboard_history(query, env))
    try:
        bundle = await collect_artist(query, env=env)
    except Exception:
        bundle = {
            "sources": [],
            "patterns": {"release_hours": [], "social_hours": [], "weekdays": [], "hour_labels": [], "weekday_labels": []},
            "insights": ["The dashboard could not load sources. Refresh to try again."],
        }
    try:
        history = await history_task
    except Exception:
        history = empty_history("Stored growth could not be read on this request.")
    for source in bundle.get("sources") or []:
        items = source.get("items") or []
        if len(items) > 20:
            source["items"] = items[:20]
    live = _live_related(bundle)
    if live and not history.get("related"):
        history["related"] = live
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
    user = get_current_user(request)
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
    user = get_current_user(request)
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
    user = get_current_user(request)
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
    # The runtime calls scheduled(controller, env, ctx) and queue(batch, env, ctx).
    # env and ctx are optional so a runtime that only passes the event still runs.
    async def scheduled(self, controller=None, env=None, ctx=None):
        resolved = handler_env(self, env)
        _log("scheduled start", cron=getattr(controller, "cron", None))
        try:
            result = await run_scheduled(resolved)
        except Exception as exc:
            _log("scheduled error", **_exc_fields(exc))
            raise
        _log(
            "scheduled end",
            status=result.get("status"),
            attempted=result.get("attempted", 0),
            ok=result.get("ok", 0),
            error=result.get("error", 0),
            reason=result.get("reason"),
        )
        return result

    async def queue(self, batch=None, env=None, ctx=None):
        resolved = handler_env(self, env)
        messages = list(getattr(batch, "messages", None) or [])
        _log("queue start", messages=len(messages))
        try:
            result = await run_queue_batch(batch, resolved)
        except Exception as exc:
            # Do not raise. A thrown queue handler becomes an exception outcome and
            # drops the message after max_retries. Retry it and return.
            _log("queue error", **_exc_fields(exc))
            for message in messages:
                await _finish_message(message, "retry", delay_seconds=RETRY_DELAY_SECONDS)
            result = {"messages": len(messages), "acked": 0, "retried": len(messages), "snapshots": 0}
        _log(
            "queue end",
            messages=result.get("messages", 0),
            acked=result.get("acked", 0),
            retried=result.get("retried", 0),
            snapshots=result.get("snapshots", 0),
        )
        return result
