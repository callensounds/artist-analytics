"""D1 accounts. The in-memory demo user is only used when no database is bound."""

from __future__ import annotations

from db import utc_now
from passwords import hash_password, verify_password

DEMO_EMAIL = "artist@demo.com"
DEMO_PASSWORD = "demo123"


def public_user(row: dict, *, artist_name: str | None = None) -> dict:
    email = str(row.get("email") or "")
    return {
        "id": int(row["id"]),
        "email": email,
        "artist_name": artist_name or email,
    }


async def ensure_demo_user(db) -> dict | None:
    """Insert the public demo account once so login has a single password check."""
    existing = await db.query_one(
        "SELECT id, email, password_hash FROM users WHERE email = ?",
        (DEMO_EMAIL,),
    )
    if existing:
        return existing
    await db.execute(
        "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
        (DEMO_EMAIL, hash_password(DEMO_PASSWORD), utc_now()),
    )
    return await db.query_one(
        "SELECT id, email, password_hash FROM users WHERE email = ?",
        (DEMO_EMAIL,),
    )


async def authenticate(db, email: str, password: str) -> dict | None:
    await ensure_demo_user(db)
    row = await db.query_one(
        "SELECT id, email, password_hash FROM users WHERE email = ?",
        (email,),
    )
    if not row or not verify_password(password, str(row.get("password_hash") or "")):
        return None
    return public_user(row, artist_name="Demo Artist" if email == DEMO_EMAIL else email)


async def register_user(db, email: str, password: str) -> dict | None:
    """None when the email is already registered."""
    await ensure_demo_user(db)
    existing = await db.query_one("SELECT id FROM users WHERE email = ?", (email,))
    if existing:
        return None
    failed = False
    try:
        await db.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
            (email, hash_password(password), utc_now()),
        )
    except Exception:
        failed = True
    if failed:
        return None
    row = await db.query_one("SELECT id, email FROM users WHERE email = ?", (email,))
    return public_user(row) if row else None


async def list_watched(db, user_id: int) -> list[dict]:
    return await db.query(
        """
        SELECT artists.id AS id, artists.name AS name
        FROM user_artists
        JOIN artists ON artists.id = user_artists.artist_id
        WHERE user_artists.user_id = ?
        ORDER BY user_artists.created_at DESC, artists.name
        LIMIT 100
        """,
        (user_id,),
    )


async def watch_artist(db, user_id: int, artist_id: int) -> dict | None:
    artist = await db.query_one(
        "SELECT id, name FROM artists WHERE id = ? AND tracked = 1",
        (artist_id,),
    )
    if not artist:
        return None
    await db.execute(
        """
        INSERT INTO user_artists (user_id, artist_id, created_at) VALUES (?, ?, ?)
        ON CONFLICT(user_id, artist_id) DO NOTHING
        """,
        (user_id, artist_id, utc_now()),
    )
    return {"id": int(artist["id"]), "name": artist["name"]}


async def unwatch_artist(db, user_id: int, artist_id: int) -> None:
    await db.execute(
        "DELETE FROM user_artists WHERE user_id = ? AND artist_id = ?",
        (user_id, artist_id),
    )
