-- Spotify top tracks for a tracked artist. The crawler writes these. The dashboard only reads them.
-- external_id is the Spotify track id when the public page or Web API included one.
-- When no id was published, it is a stable name: key for that title, not a guessed Spotify id.
-- popularity is Spotify's 0–100 score, or NULL when that score was not returned.
-- A play count is not stored as popularity. album_name stays NULL until a source prints one.
-- The crawler creates these tables on its next run. Until then:
--   npx wrangler d1 execute artist-analytics --remote --file=src/migrations/0010_artist_tracks.sql
-- Do not edit 0001_crawl.sql.

CREATE TABLE IF NOT EXISTS artist_tracks (
    artist_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    track_name TEXT NOT NULL,
    popularity INTEGER,
    album_name TEXT,
    rank INTEGER,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (artist_id, source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_artist_tracks_seen
    ON artist_tracks (artist_id, source, last_seen_at);

CREATE TABLE IF NOT EXISTS track_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    popularity INTEGER NOT NULL,
    captured_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_track_snapshots_lookup
    ON track_snapshots (artist_id, source, external_id, captured_at);
