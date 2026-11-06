"""Persistent accounts. Demo login uses the same password hash check."""

import asyncio
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

import main
from accounts import authenticate
from crawl import reset_pool
from db import SqliteDatabase, migrate
from passwords import verify_password


def test_import_does_not_draw_randomness():
    src = Path(main.__file__).resolve().parent
    code = (
        "import secrets\n"
        "def boom(n=16):\n"
        "    raise OSError('Randomness is not allowed while a Worker is starting')\n"
        "secrets.token_bytes = boom\n"
        "import main\n"
        "user = main.USERS['artist@demo.com']\n"
        "assert not user.get('password_hash')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=src, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_memory_demo_login_hashes_on_first_use():
    main.USERS.clear()
    main.USERS["artist@demo.com"] = {
        "id": 1,
        "email": "artist@demo.com",
        "password_hash": "",
        "artist_name": "Demo Artist",
    }
    client = TestClient(main.app)
    logged = client.post(
        "/login",
        data={"email": "artist@demo.com", "password": "demo123"},
        follow_redirects=False,
    )
    assert logged.status_code == 303
    stored = main.USERS["artist@demo.com"]["password_hash"]
    assert stored.startswith("pbkdf2_sha256$")
    assert verify_password("demo123", stored)
    assert "demo123" not in stored


def test_register_login_logout_and_duplicate_email(monkeypatch, tmp_path):
    path = tmp_path / "users.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    asyncio.run(migrate(SqliteDatabase(str(path))))
    client = TestClient(main.app)
    created = client.post(
        "/register",
        data={"email": "New@Example.com", "password": "secret1", "artist_name": "New"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert created.headers["location"] == "/dashboard"
    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "new@example.com" in page.text

    database = SqliteDatabase(str(path))

    async def stored():
        return await database.query_one("SELECT email, password_hash FROM users WHERE email = ?", ("new@example.com",))

    row = asyncio.run(stored())
    assert row["email"] == "new@example.com"
    assert row["password_hash"] != "secret1"
    assert verify_password("secret1", row["password_hash"])
    assert "new@example.com" not in main.USERS

    client.get("/logout", follow_redirects=False)
    locked = client.get("/dashboard", follow_redirects=False)
    assert locked.status_code == 303

    again = client.post(
        "/register",
        data={"email": "new@example.com", "password": "secret1", "artist_name": "New"},
    )
    assert again.status_code == 400
    assert "already registered" in again.text

    logged = client.post(
        "/login",
        data={"email": "new@example.com", "password": "secret1"},
        follow_redirects=False,
    )
    assert logged.status_code == 303
    denied = client.post("/login", data={"email": "new@example.com", "password": "wrong-password"})
    assert denied.status_code == 400
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)


def test_demo_login_uses_the_stored_hash(monkeypatch, tmp_path):
    path = tmp_path / "demo.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))
    asyncio.run(migrate(database))
    client = TestClient(main.app)
    logged = client.post(
        "/login",
        data={"email": "artist@demo.com", "password": "demo123"},
        follow_redirects=False,
    )
    assert logged.status_code == 303

    async def demo():
        return await authenticate(database, "artist@demo.com", "demo123")

    user = asyncio.run(demo())
    assert user["email"] == "artist@demo.com"
    assert user["artist_name"] == "Demo Artist"
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)


def test_watchlist_lists_a_stored_artist(monkeypatch, tmp_path):
    path = tmp_path / "watch.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))

    async def seed():
        await migrate(database)
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Clairo', 'clairo', 'search', 1, '2026-11-01T00:00:00Z')"
        )
        return int((await database.query_one("SELECT id FROM artists WHERE normalized_name = 'clairo'"))["id"])

    artist_id = asyncio.run(seed())
    client = TestClient(main.app)
    client.post(
        "/register",
        data={"email": "fan@example.com", "password": "secret1", "artist_name": "Fan"},
        follow_redirects=False,
    )
    watched = client.post(f"/watch/{artist_id}", follow_redirects=False)
    assert watched.status_code == 303
    page = client.get("/watch")
    assert page.status_code == 200
    assert "Clairo" in page.text
    assert f'href="/artist/{artist_id}"' in page.text
    client.post(f"/unwatch/{artist_id}", follow_redirects=False)
    empty = client.get("/watch")
    assert "not watching" in empty.text
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)
