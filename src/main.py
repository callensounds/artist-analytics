import secrets
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from providers import collect_artist
from runtime import FALLBACK_SESSION_SECRET, WorkerSessionMiddleware

try:
    from workers import asgi
except ImportError:  # The workers runtime exists on Cloudflare, not in local tests.
    asgi = None

app = FastAPI(title="Average Analytics")
app.add_middleware(WorkerSessionMiddleware, fallback_secret=FALLBACK_SESSION_SECRET)

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR if _TEMPLATE_DIR.is_dir() else "templates"))

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


def get_current_user(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    for user in USERS.values():
        if user["id"] == user_id:
            return user
    return None


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
    try:
        bundle = await collect_artist(query, env=request.scope.get("env"))
    except Exception:
        bundle = {
            "sources": [],
            "patterns": {"release_hours": [], "social_hours": [], "weekdays": [], "hour_labels": [], "weekday_labels": []},
            "insights": ["The dashboard could not load sources. Refresh to try again."],
        }
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "query": query,
            "sources": bundle["sources"],
            "patterns": bundle["patterns"],
            "insights": bundle["insights"],
        },
    )


if asgi is not None:
    Default = asgi.entrypoint(app)
