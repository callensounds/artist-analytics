-- Public US iTunes chart snapshots. The crawler writes these. The dashboard only reads them.
-- The crawler creates this table on its next run. Until then:
--   npx wrangler d1 execute artist-analytics --remote --file=src/migrations/0009_itunes_charts.sql
-- Do not edit 0001_crawl.sql.

CREATE TABLE IF NOT EXISTS chart_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chart_source TEXT NOT NULL,
    territory TEXT NOT NULL,
    chart_date TEXT NOT NULL,
    position INTEGER NOT NULL,
    track_or_album_name TEXT NOT NULL,
    apple_artist_id TEXT,
    artist_name TEXT NOT NULL,
    artist_id INTEGER REFERENCES artists(id),
    collected_at TEXT NOT NULL,
    UNIQUE (chart_source, territory, chart_date, position)
);

CREATE INDEX IF NOT EXISTS idx_chart_entries_artist
    ON chart_entries (artist_id, chart_date);

CREATE INDEX IF NOT EXISTS idx_chart_entries_source_date
    ON chart_entries (chart_source, chart_date);
