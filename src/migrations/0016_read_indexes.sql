-- Indexes for the page reads that walked whole tables on 2026-11-20.
-- CREATE INDEX IF NOT EXISTS only. Do not edit an earlier migration.
-- The crawler applies this on its next run. Until then:
--   npx wrangler d1 execute artist-analytics --remote --file=src/migrations/0016_read_indexes.sql
-- Apply the file once. Recording it in schema_migrations makes the next run a no-op.

-- Debut check: one seek for an older row of the same title, then stop.
CREATE INDEX IF NOT EXISTS idx_chart_entries_title_date
    ON chart_entries (artist_id, chart_source, territory, track_or_album_name, chart_date);

-- Alert page walks newest first and stops at LIMIT.
-- A filtered artist read stays on idx_metric_alerts_artist.
CREATE INDEX IF NOT EXISTS idx_metric_alerts_detected
    ON metric_alerts (detected_at, id);

-- Top tracked artists by one Spotify metric. The primary key stays the per-artist lookup.
CREATE INDEX IF NOT EXISTS idx_metric_latest_value
    ON metric_latest (source, metric, value);

-- Digest playlist window. Partial so a read that does not require a name
-- keeps idx_playlist_artist_seen. first_seen_at is not rewritten on a keep-alive update.
CREATE INDEX IF NOT EXISTS idx_playlist_artist_first
    ON playlist_appearances (artist_id, first_seen_at)
    WHERE playlist_id != '' AND playlist_name != '';
