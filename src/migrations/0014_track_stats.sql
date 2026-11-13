-- Per-track stats over time. These are not Spotify's 0–100 popularity.
-- deezer_rank is the integer Deezer publishes as `rank` on api.deezer.com/artist/{id}/top.
-- spotify_playcount is a play count only when the public artist page included one.
-- spotify_popularity is reserved for a real 0–100 score. track_snapshots.popularity
-- stays that 0–100 column and is not reused for ranks or play counts.
-- The crawler writes at most one row per track and metric per UTC day, and skips a
-- value that matches the latest stored row. The dashboard only reads this table.
-- The crawler applies this on its next run. To apply it before that run:
--   npx wrangler d1 execute artist-analytics --remote --file=src/migrations/0014_track_stats.sql
-- Apply it once. Do not edit 0001_crawl.sql.

CREATE TABLE IF NOT EXISTS track_stats (
    artist_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    metric TEXT NOT NULL CHECK (metric IN ('deezer_rank', 'spotify_playcount', 'spotify_popularity')),
    value INTEGER NOT NULL,
    captured_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_track_stats_lookup
    ON track_stats (artist_id, source, external_id, metric, captured_at);
