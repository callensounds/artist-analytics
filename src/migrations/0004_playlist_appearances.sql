-- Playlist appearances for a tracked artist.
-- Spotify's Web API does not list playlists that include an artist's tracks.
-- This table is ready for a public source. The crawler does not invent rows.
-- The dashboard does not apply migrations. The crawler creates the table on its
-- next run. Until then:
--   npx wrangler d1 execute artist-analytics --remote --file=src/migrations/0004_playlist_appearances.sql
-- Do not edit 0001_crawl.sql.

CREATE TABLE IF NOT EXISTS playlist_appearances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_id INTEGER NOT NULL,
    playlist_id TEXT NOT NULL,
    playlist_name TEXT NOT NULL,
    owner_name TEXT,
    follower_count INTEGER,
    position INTEGER,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE (artist_id, playlist_id)
);

CREATE INDEX IF NOT EXISTS idx_playlist_artist_seen
    ON playlist_appearances (artist_id, last_seen_at);
