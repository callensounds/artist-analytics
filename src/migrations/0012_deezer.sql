-- Deezer artist id and the latest album count. Fan counts stay in metric_snapshots.
-- The crawler applies this file on its next run. To apply it before that run:
--   npx wrangler d1 execute artist-analytics --remote --file=src/migrations/0012_deezer.sql
-- Apply it once. Do not edit 0001_crawl.sql.

ALTER TABLE artists ADD COLUMN deezer_id TEXT;

ALTER TABLE artists ADD COLUMN deezer_nb_album INTEGER;
