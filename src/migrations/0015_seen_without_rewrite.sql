-- Current rows no longer need a last_seen_at bump on every crawl.
-- removed_at is set only on rows missing from a confirmed fetch.
-- absences_marked_at says that classification has run for the artist.
-- Until it is set, reads keep the previous last_seen_at window.
-- Old code ignores these columns. Apply this before the crawler that writes them:
--   npx wrangler d1 execute artist-analytics --remote --file=src/migrations/0015_seen_without_rewrite.sql
-- Apply the file once. If the worker starts after that and tries the same ALTER,
-- it skips the "duplicate column name" error and records the version.
-- Do not edit an earlier migration. This does not rewrite existing rows.

ALTER TABLE playlist_appearances ADD COLUMN removed_at TEXT;

ALTER TABLE artist_tracks ADD COLUMN removed_at TEXT;

ALTER TABLE related_edges ADD COLUMN removed_at TEXT;

ALTER TABLE playlist_crawls ADD COLUMN absences_marked_at TEXT;

-- playlist_crawls.source distinguishes the list that was classified:
--   deezer           playlist placements
--   spotify_tracks   Spotify top tracks
--   deezer_tracks    Deezer top tracks
--   spotify_related  Spotify related artists
-- A new source value is a new primary-key row. It does not move source_freshness.
