-- Dashboard reads for Fans also like and first-seen rows.
-- 0001 already indexes metric_snapshots (artist_id, source, metric, captured_at).
-- related_edges PRIMARY KEY already starts with from_artist_id.
-- The dashboard Worker does not apply migrations. The crawler runs this file
-- on its next invocation after deploy. Until then, apply it once:
--   npx wrangler d1 execute artist-analytics --remote --file=src/migrations/0003_dashboard_reads.sql
-- Do not edit 0001_crawl.sql.

CREATE INDEX IF NOT EXISTS idx_related_from_artist
    ON related_edges (from_artist_id, rank, last_seen_at);

CREATE INDEX IF NOT EXISTS idx_sightings_artist_seen
    ON content_sightings (artist_id, first_seen_at);
