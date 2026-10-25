from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from workers import asgi
import requests
from dateutil import parser
import os
import secrets

app = FastAPI(title="Artist Analytics")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SECRET_KEY", secrets.token_hex(32)))

templates = Jinja2Templates(directory="templates")

# In-memory demo users (replace with D1/KV for persistence)
USERS = {
    "artist@demo.com": {
        "id": 1,
        "email": "artist@demo.com",
        "password": "demo123",  # plain for demo; hash in production
        "artist_name": "Demo Artist"
    }
}
NEXT_ID = 2

def get_current_user(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    for u in USERS.values():
        if u["id"] == user_id:
            return u
    return None

@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    user = get_current_user(request)
    return templates.TemplateResponse("index.html", {"request": request, "user": user})

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})

@app.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    user = USERS.get(email)
    if user and user["password"] == password:
        request.session["user_id"] = user["id"]
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid email or password"})

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "error": None})

@app.post("/register")
async def register(request: Request, email: str = Form(...), password: str = Form(...), artist_name: str = Form(...)):
    global NEXT_ID
    if email in USERS:
        return templates.TemplateResponse("register.html", {"request": request, "error": "Email already registered"})
    if len(password) < 6:
        return templates.TemplateResponse("register.html", {"request": request, "error": "Password must be at least 6 characters"})
    USERS[email] = {
        "id": NEXT_ID,
        "email": email,
        "password": password,
        "artist_name": artist_name
    }
    request.session["user_id"] = NEXT_ID
    NEXT_ID += 1
    return RedirectResponse("/dashboard", status_code=303)

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    
    releases = []
    try:
        search_url = f"https://musicbrainz.org/ws/2/artist/?query=artist:\"{requests.utils.quote(user['artist_name'])}\"&fmt=json&limit=1"
        search_resp = requests.get(search_url, headers={"User-Agent": "ArtistAnalyticsCF/1.0"}, timeout=8)
        if search_resp.ok:
            search_data = search_resp.json()
            if search_data.get("artists"):
                mbid = search_data["artists"][0]["id"]
                rg_url = f"https://musicbrainz.org/ws/2/release-group?artist={mbid}&fmt=json&limit=15"
                rg_resp = requests.get(rg_url, headers={"User-Agent": "ArtistAnalyticsCF/1.0"}, timeout=8)
                if rg_resp.ok:
                    for rg in rg_resp.json().get("release-groups", []):
                        date_str = rg.get("first-release-date", "")
                        if date_str:
                            try:
                                dt = parser.parse(date_str)
                                releases.append({
                                    "title": rg.get("title"),
                                    "type": rg.get("primary-type", "Unknown"),
                                    "date": date_str,
                                    "weekday": dt.strftime("%A") if date_str.count("-") >= 2 else "—"
                                })
                            except Exception:
                                releases.append({"title": rg.get("title"), "type": rg.get("primary-type"), "date": date_str, "weekday": "—"})
                    releases = sorted(releases, key=lambda x: x["date"], reverse=True)
    except Exception:
        pass

    demo_metrics = {
        "score": 72,
        "rank": "2,847th",
        "career_stage": "Developing",
        "momentum": "Growth",
        "monthly_listeners": "1.2M",
        "youtube_subs": "480K",
        "tiktok_followers": "2.1M",
        "instagram_followers": "890K"
    }

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "releases": releases,
        "demo_metrics": demo_metrics,
        "youtube_key_set": False
    })

# Cloudflare Workers ASGI entrypoint
Default = asgi.entrypoint(app)
