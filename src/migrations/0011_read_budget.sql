-- Stop the daily D1 row-read blowups from charts, scores, and momentum.
-- Latest counts, source clocks, and chart freshness live in small tables.
-- Triggers keep them current. The INSERT...SELECT statements below fill them
-- once from history. The crawler applies this file on its next run. Until then:
--   npx wrangler d1 execute artist-analytics --remote --file=src/migrations/0011_read_budget.sql
-- Apply it once. Do not edit 0001_crawl.sql.

CREATE TABLE IF NOT EXISTS metric_latest (
    artist_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL,
    value_text TEXT,
    captured_at TEXT NOT NULL,
    snapshot_id INTEGER,
    PRIMARY KEY (artist_id, source, metric)
);

CREATE TABLE IF NOT EXISTS source_freshness (
    artist_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    PRIMARY KEY (artist_id, source)
);

-- Artists who already have an exact alert metric. The cron walks this table,
-- not metric_snapshots. YouTube subscribers are a score term, not an alert.
CREATE TABLE IF NOT EXISTS audience_artists (
    artist_id INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS chart_freshness (
    chart_source TEXT NOT NULL,
    territory TEXT NOT NULL,
    chart_date TEXT NOT NULL,
    collected_at TEXT,
    next_try_at TEXT,
    PRIMARY KEY (chart_source, territory, chart_date)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_artist_captured
    ON metric_snapshots (artist_id, captured_at, id);

CREATE INDEX IF NOT EXISTS idx_chart_entries_day
    ON chart_entries (territory, chart_date, chart_source);

CREATE INDEX IF NOT EXISTS idx_artists_origin_tracked
    ON artists (origin, tracked);

ALTER TABLE crawl_state ADD COLUMN alert_backfill_done INTEGER NOT NULL DEFAULT 0;

ALTER TABLE crawl_state ADD COLUMN related_tracked INTEGER;

CREATE TRIGGER IF NOT EXISTS metric_snapshots_fresh
AFTER INSERT ON metric_snapshots
FOR EACH ROW
BEGIN
    INSERT INTO source_freshness (artist_id, source, captured_at)
    VALUES (NEW.artist_id, NEW.source, NEW.captured_at)
    ON CONFLICT(artist_id, source) DO UPDATE SET
        captured_at = excluded.captured_at
    WHERE excluded.captured_at > source_freshness.captured_at;
END;

CREATE TRIGGER IF NOT EXISTS metric_snapshots_latest
AFTER INSERT ON metric_snapshots
FOR EACH ROW
WHEN NEW.value IS NOT NULL
BEGIN
    INSERT INTO metric_latest (
        artist_id, source, metric, value, value_text, captured_at, snapshot_id
    ) VALUES (
        NEW.artist_id, NEW.source, NEW.metric, NEW.value, NEW.value_text, NEW.captured_at, NEW.id
    )
    ON CONFLICT(artist_id, source, metric) DO UPDATE SET
        value = excluded.value,
        value_text = excluded.value_text,
        captured_at = excluded.captured_at,
        snapshot_id = excluded.snapshot_id
    WHERE excluded.captured_at > metric_latest.captured_at
       OR (
            excluded.captured_at = metric_latest.captured_at
            AND excluded.snapshot_id >= COALESCE(metric_latest.snapshot_id, 0)
       );
END;

CREATE TRIGGER IF NOT EXISTS metric_snapshots_audience
AFTER INSERT ON metric_snapshots
FOR EACH ROW
WHEN NEW.value IS NOT NULL AND (
       (NEW.source = 'spotify' AND NEW.metric IN ('Monthly listeners', 'Followers'))
    OR (NEW.source = 'instagram' AND NEW.metric = 'Followers')
    OR (NEW.source = 'tiktok' AND NEW.metric IN ('Followers', 'Likes'))
)
BEGIN
    INSERT INTO audience_artists (artist_id) VALUES (NEW.artist_id)
    ON CONFLICT(artist_id) DO NOTHING;
END;

INSERT INTO metric_latest (artist_id, source, metric, value, value_text, captured_at, snapshot_id)
SELECT artist_id, source, metric, value, value_text, captured_at, id
FROM (
    SELECT artist_id, source, metric, value, value_text, captured_at, id,
           ROW_NUMBER() OVER (
               PARTITION BY artist_id, source, metric
               ORDER BY captured_at DESC, id DESC
           ) AS rn
    FROM metric_snapshots
    WHERE value IS NOT NULL
) AS ranked
WHERE rn = 1
ON CONFLICT(artist_id, source, metric) DO UPDATE SET
    value = excluded.value,
    value_text = excluded.value_text,
    captured_at = excluded.captured_at,
    snapshot_id = excluded.snapshot_id
WHERE excluded.captured_at > metric_latest.captured_at
   OR (
        excluded.captured_at = metric_latest.captured_at
        AND excluded.snapshot_id >= COALESCE(metric_latest.snapshot_id, 0)
   );

INSERT INTO source_freshness (artist_id, source, captured_at)
SELECT artist_id, source, MAX(captured_at)
FROM metric_snapshots
GROUP BY artist_id, source
ON CONFLICT(artist_id, source) DO UPDATE SET
    captured_at = excluded.captured_at
WHERE excluded.captured_at > source_freshness.captured_at;

INSERT INTO audience_artists (artist_id)
SELECT DISTINCT artist_id
FROM metric_snapshots
WHERE value IS NOT NULL
  AND (
       (source = 'spotify' AND metric IN ('Monthly listeners', 'Followers'))
    OR (source = 'instagram' AND metric = 'Followers')
    OR (source = 'tiktok' AND metric IN ('Followers', 'Likes'))
  )
ON CONFLICT(artist_id) DO NOTHING;

INSERT INTO chart_freshness (chart_source, territory, chart_date, collected_at)
SELECT chart_source, territory, chart_date, MAX(collected_at)
FROM chart_entries
GROUP BY chart_source, territory, chart_date
ON CONFLICT(chart_source, territory, chart_date) DO UPDATE SET
    collected_at = excluded.collected_at
WHERE excluded.collected_at > COALESCE(chart_freshness.collected_at, '');
