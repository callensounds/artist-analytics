-- Skip iTunes for a while after HTTP 403. The crawler reads this; the dashboard does not.
-- Apply by deploying: the Worker runs new files in src/migrations itself.
-- Do not edit 0001_crawl.sql.

ALTER TABLE crawl_state ADD COLUMN apple_blocked_until TEXT;
