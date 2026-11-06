-- Persistent accounts. Passwords are PBKDF2-HMAC-SHA256 hashes, never plaintext.
-- The dashboard does not apply migrations. The crawler creates these tables on
-- its next run. Until then:
--   npx wrangler d1 execute artist-analytics --remote --file=src/migrations/0008_users.sql
-- Do not edit 0001_crawl.sql.

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_artists (
    user_id INTEGER NOT NULL,
    artist_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, artist_id)
);

CREATE INDEX IF NOT EXISTS idx_user_artists_user
    ON user_artists (user_id, created_at);
