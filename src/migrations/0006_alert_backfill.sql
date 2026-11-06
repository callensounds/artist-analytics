-- Marks an artist whose stored snapshots were already checked for growth alerts.
-- A later cron tick skips them. New snapshots still alert from the crawl message.
-- The dashboard does not apply migrations. The crawler creates this table on its
-- next run. Until then:
--   npx wrangler d1 execute artist-analytics --remote --file=src/migrations/0006_alert_backfill.sql
-- Do not edit 0001_crawl.sql.

CREATE TABLE IF NOT EXISTS alert_backfill (
    artist_id INTEGER PRIMARY KEY,
    checked_at TEXT NOT NULL
);
