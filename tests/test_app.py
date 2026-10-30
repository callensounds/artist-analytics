import asyncio
import builtins
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

import main
from cache import MemoryCache
from http_client import HttpResult
from providers.youtube import fetch_youtube
from runtime import WorkerSessionMiddleware, env_get, env_string

EMPTY = {
    "sources": [
        {
            "platform": "youtube",
            "label": "YouTube",
            "status": "missing_credentials",
            "reason": "Set the Worker secret YOUTUBE_API_KEY.",
            "profile_name": None,
            "profile_url": None,
            "metrics": [],
            "items": [],
            "notices": [],
        }
    ],
    "patterns": {
        "hour_labels": [],
        "weekday_labels": [],
        "release_hours": [],
        "social_hours": [],
        "weekdays": [],
    },
    "insights": ["No day-level dates or exact timestamps were returned for this artist yet."],
}


def setup_function():
    main.USERS.clear()
    main.USERS["artist@demo.com"] = {
        "id": 1,
        "email": "artist@demo.com",
        "password": "demo123",
        "artist_name": "Demo Artist",
    }
    main.NEXT_ID = 2


def test_templates_and_migrations_live_beside_the_worker():
    from pathlib import Path

    import db

    module_dir = Path(main.__file__).resolve().parent
    template_dir = Path(main.templates.env.loader.searchpath[0]).resolve()
    assert template_dir == module_dir / "templates"
    assert db.MIGRATIONS.resolve() == module_dir / "migrations"
    assert (template_dir / "login.html").is_file()
    assert (template_dir / "admin.html").is_file()
    assert (template_dir / "diag.html").is_file()
    assert (db.MIGRATIONS / "0001_crawl.sql").is_file()
    wrangler = (module_dir.parent / "wrangler.jsonc").read_text()
    assert '"preview_urls": false' in wrangler
    assert '"binding": "DB"' in wrangler
    assert '"database_name": "artist-analytics"' in wrangler
    assert "52a9190e-4147-402e-8f75-16f5de58bf84" in wrangler
    assert '"migrations_dir": "src/migrations"' in wrangler
    assert '"binding": "CRAWL_QUEUE"' in wrangler
    assert '"queue": "artist-analytics-crawl"' in wrangler
    assert '"consumers"' not in wrangler
    assert '"crons"' not in wrangler
    assert '\n  "limits"' not in wrangler
    crawler = (module_dir.parent / "wrangler.crawler.jsonc").read_text()
    assert '"name": "artist-analytics-crawler"' in crawler
    assert '"main": "crawler/src/index.ts"' in crawler
    assert '"workers_dev": false' in crawler
    assert '"compatibility_flags"' not in crawler
    assert '"queue": "artist-analytics-crawl"' in crawler
    assert '"max_batch_size": 1' in crawler
    assert '"max_concurrency": 1' in crawler
    assert '"max_retries": 10' in crawler
    assert '"retry_delay": 180' in crawler
    assert '"dead_letter_queue": "artist-analytics-crawl-dlq"' in crawler
    assert '"crons": ["*/15 * * * *"]' in crawler
    assert '\n  "limits"' not in crawler
    for name in ("base.html", "index.html", "login.html", "register.html", "dashboard.html"):
        assert (template_dir / name).is_file()
    client = TestClient(main.app)
    home = client.get("/")
    assert home.status_code == 200
    assert "Average Analytics" in home.text
    login = client.get("/login")
    assert login.status_code == 200
    assert "Welcome back" in login.text
    assert "Average Analytics" in login.text
    assert "cdn.tailwindcss.com" not in home.text
    assert "/static/app.css" in home.text
    assert "typeof Chart" in (module_dir / "templates" / "dashboard.html").read_text()
    icon = client.get("/favicon.ico")
    assert icon.status_code == 200
    assert icon.headers["content-type"].startswith("image/svg+xml")
    assert "<svg" in icon.text
    css = client.get("/static/app.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    assert ".bg-slate-900" in css.text
    assert '"fallthrough": true' in wrangler
    assert "**/*.css" in wrangler


def test_demo_login_and_dashboard_do_not_fabricate_metrics(monkeypatch):
    async def fake_collect(name, env=None, http=None, cache=None):
        return EMPTY

    monkeypatch.setattr(main, "collect_artist", fake_collect)
    client = TestClient(main.app)
    login = client.post(
        "/login",
        data={"email": "Artist@Demo.com", "password": "demo123"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "1.2M" not in page.text
    assert "Analytics Score" not in page.text
    assert "Needs secret" in page.text
    assert "YOUTUBE_API_KEY" in page.text
    match = re.search(r'<script id="pattern-data" type="application/json">(.*?)</script>', page.text)
    assert json.loads(match.group(1))["release_hours"] == []


def test_artist_query_renders_exact_timestamps(monkeypatch):
    async def fake_collect(name, env=None, http=None, cache=None):
        assert name == "Taylor Swift"
        return {
            "sources": [
                {
                    "platform": "apple",
                    "label": "Apple Music",
                    "status": "ok",
                    "reason": None,
                    "profile_name": "Taylor Swift",
                    "profile_url": "https://music.apple.com/us/artist/taylor-swift/159260351",
                    "metrics": [{"label": "Exact timestamps", "available": True, "value": "1", "reason": None}],
                    "items": [
                        {
                            "title": "Lover",
                            "kind": "release",
                            "precision": "time",
                            "precision_label": "Exact time",
                            "display": "2019-08-23 07:00 UTC",
                            "weekday": "Friday",
                            "hour_utc": 7,
                            "timezone": "UTC",
                            "url": "https://music.apple.com/album/9",
                            "detail": "Album",
                            "sort_key": "2019-08-23T07:00:00",
                        }
                    ],
                    "notices": ["US storefront midnight."],
                }
            ],
            "patterns": {
                "hour_labels": [f"{hour:02d}" for hour in range(24)],
                "weekday_labels": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
                "release_hours": [
                    {"platform": "apple", "label": "Apple Music", "data": [0] * 7 + [1] + [0] * 16, "color": "#fb7185"}
                ],
                "social_hours": [],
                "weekdays": [
                    {"platform": "apple", "label": "Apple Music", "data": [0, 0, 0, 0, 1, 0, 0], "color": "#fb7185"}
                ],
            },
            "insights": ["Apple Music peaks at 07:00 UTC (1 of 1 exact timestamps)."],
        }

    monkeypatch.setattr(main, "collect_artist", fake_collect)
    client = TestClient(main.app)
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"})
    page = client.get("/dashboard", params={"artist": "Taylor Swift"})
    assert page.status_code == 200
    assert "2019-08-23 07:00 UTC" in page.text
    assert "Exact time" in page.text
    assert "releaseHourChart" in page.text
    assert "07:00 UTC" in page.text
    assert "typeof Chart" in page.text


def test_diag_is_logged_in_only_and_does_not_print_a_secret(monkeypatch):
    async def fake_diagnose(env, http):
        return {
            "secrets": [
                {
                    "name": "YOUTUBE_API_KEY",
                    "chosen_path": "env-attr:json-stringify",
                    "chosen": {
                        "length": 39,
                        "prefix4": "AIza",
                        "usable": True,
                        "is_object_object": False,
                        "is_undefined": False,
                    },
                    "reads": [
                        {
                            "path": "env-attr",
                            "present": True,
                            "python_type": "JsProxy",
                            "js_typeof": "object",
                            "js_constructor": "String",
                            "error": None,
                            "coercions": [
                                {
                                    "method": "str",
                                    "length": 15,
                                    "prefix4": "[obj",
                                    "usable": False,
                                    "is_object_object": True,
                                    "is_undefined": False,
                                }
                            ],
                        }
                    ],
                }
            ],
            "youtube": {
                "key_present": True,
                "built_query_url": "https://www.googleapis.com/youtube/v3/channels?part=id&forHandle=%40TaylorSwift&key=AIza…(39)",
                "header_request": {
                    "status": 200,
                    "url": "https://www.googleapis.com/youtube/v3/channels?part=id&forHandle=%40TaylorSwift",
                    "message": None,
                    "error": None,
                    "has_server_data": False,
                    "has_release_meta": False,
                },
                "query_request": {
                    "status": 400,
                    "url": "https://www.googleapis.com/youtube/v3/channels?part=id&key=AIza…(39)",
                    "message": "API key not valid. Please pass a valid API key.",
                    "error": None,
                    "has_server_data": False,
                    "has_release_meta": False,
                },
                "dashboard_sends": "X-Goog-Api-Key header, no key query parameter",
            },
            "apple": {
                "itunes_lookup": {
                    "status": 403,
                    "url": "https://itunes.apple.com/lookup?id=159260351",
                    "message": None,
                    "error": None,
                    "has_server_data": False,
                    "has_release_meta": False,
                }
            },
        }

    monkeypatch.setattr(main, "diagnose", fake_diagnose)
    client = TestClient(main.app)
    anon = client.get("/admin/diag", follow_redirects=False)
    assert anon.status_code == 303
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"})
    page = client.get("/admin/diag")
    assert page.status_code == 200
    assert "env-attr:json-stringify" in page.text
    assert "JsProxy" in page.text
    assert "AIza…(39)" in page.text
    assert "AIzaExactKey123" not in page.text
    assert "HTTP 403" in page.text or "403" in page.text


def test_bad_login_and_short_password():
    client = TestClient(main.app)
    denied = client.post("/login", data={"email": "artist@demo.com", "password": "nope"})
    assert denied.status_code == 400
    assert "Invalid email or password" in denied.text
    short = client.post(
        "/register",
        data={"email": "new@example.com", "password": "123", "artist_name": "New"},
    )
    assert short.status_code == 400
    assert "at least 6" in short.text
    created = client.post(
        "/register",
        data={"email": "new@example.com", "password": "secret1", "artist_name": "New"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert "new@example.com" in main.USERS


def test_middleware_and_env_get_never_import_js(monkeypatch):
    import sys

    assert "diag_secrets" not in sys.modules
    assert "js" not in sys.modules
    source = Path("src/runtime.py").read_text(encoding="utf-8")
    assert re.search(r"(?m)^\s*(import|from)\s+js\b", source) is None
    assert re.search(r"(?m)^\s*(import|from)\s+workers\b", source) is None

    imported = []
    real_import = builtins.__import__

    def tracking(name, globals=None, locals=None, fromlist=(), level=0):
        root = name.split(".", 1)[0]
        if root in {"js", "workers", "diag_secrets"}:
            imported.append(name)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", tracking)

    class Trap:
        def to_py(self):
            import js

            return "AIza" + ("x" * 35)

        def valueOf(self):
            import js

            return "AIza" + ("y" * 35)

        def __str__(self):
            return "[object Object]"

    class Env:
        YOUTUBE_API_KEY = Trap()
        SECRET_KEY = Trap()

    assert env_get(Env(), "YOUTUBE_API_KEY") is None
    assert env_get(Env(), "SECRET_KEY") is None
    assert env_string(Env(), "YOUTUBE_API_KEY") is None
    assert env_get({"SECRET_KEY": "[REDACTED]"}, "SECRET_KEY") is None
    assert env_get({"YOUTUBE_API_KEY": "[object Object]"}, "YOUTUBE_API_KEY") is None
    assert env_get({"YOUTUBE_API_KEY": "undefined"}, "YOUTUBE_API_KEY") is None
    assert env_get({"YOUTUBE_API_KEY": "null"}, "YOUTUBE_API_KEY") is None

    calls = []

    async def call(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def drive():
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await WorkerSessionMiddleware(call)(
            {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b"", "env": Env()},
            receive,
            send,
        )

    asyncio.run(drive())

    def respond(method, url, params, data):
        calls.append(url)
        return HttpResult(200, "{}", url, {})

    class Recording:
        async def get_text(self, url, **kwargs):
            return respond("GET", url, kwargs.get("params"), kwargs.get("data"))

        async def get_json(self, url, **kwargs):
            result = await self.get_text(url, **kwargs)
            return result, {}

        async def post_form(self, url, data, **kwargs):
            return respond("POST", url, None, data), {}

    missed = asyncio.run(fetch_youtube("Example", {}, Env(), Recording(), MemoryCache({})))
    assert missed.status == "missing_credentials"
    redacted = asyncio.run(
        fetch_youtube("Example", {}, {"YOUTUBE_API_KEY": "[REDACTED]"}, Recording(), MemoryCache({}))
    )
    assert redacted.reason == "YouTube key is misconfigured"
    assert calls == []

    async def fake_collect(name, env=None, http=None, cache=None):
        assert env_get(env, "YOUTUBE_API_KEY") is None
        return EMPTY

    monkeypatch.setattr(main, "collect_artist", fake_collect)
    client = TestClient(main.app)
    for path in ("/", "/login", "/favicon.ico", "/static/app.css"):
        assert client.get(path).status_code == 200
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"})
    assert client.get("/dashboard", params={"artist": "Billie Eilish"}).status_code == 200
    assert imported == []
