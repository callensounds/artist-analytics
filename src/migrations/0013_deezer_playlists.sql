-- Deezer public playlist placements.
-- source is the provider that reported the row ('deezer').
-- editorial is 1 when the owner is a Deezer editor account, else 0.
-- playlist_crawls.crawled_at is the last successful playlist read for that provider.
-- A placement missing from that read stays in playlist_appearances with its older last_seen_at.
-- The crawler applies this on its next run. To apply it before that run:
--   npx wrangler d1 execute artist-analytics --remote --file=src/migrations/0013_deezer_playlists.sql
-- Apply it once. Do not edit 0001_crawl.sql.

ALTER TABLE playlist_appearances ADD COLUMN source TEXT;

ALTER TABLE playlist_appearances ADD COLUMN editorial INTEGER;

CREATE TABLE IF NOT EXISTS playlist_crawls (
    artist_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    crawled_at TEXT NOT NULL,
    PRIMARY KEY (artist_id, source)
);
