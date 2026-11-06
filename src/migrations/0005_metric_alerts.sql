-- Growth alerts computed by the crawler from stored exact counts.
-- The same earliest/latest pair is unique, so a later cron does not insert it again.
-- The dashboard does not apply migrations. The crawler creates this table on its
-- next run. Until then:
--   npx wrangler d1 execute artist-analytics --remote --file=src/migrations/0005_metric_alerts.sql
-- Do not edit 0001_crawl.sql.

CREATE TABLE IF NOT EXISTS metric_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    metric TEXT NOT NULL,
    window TEXT NOT NULL,
    delta INTEGER NOT NULL,
    delta_pct INTEGER,
    baseline_value INTEGER NOT NULL,
    latest_value INTEGER NOT NULL,
    baseline_at TEXT NOT NULL,
    latest_at TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_metric_alerts_artist
    ON metric_alerts (artist_id, detected_at);
