-- Official MusicBrainz release groups (album, EP, single) and their first-release-date.
-- Precision is year, month, or day. A missing day is not stored as the first of the month.
-- The dashboard does not apply migrations. The crawler creates this table on its next run.
-- Until then:
--   npx wrangler d1 execute artist-analytics --remote --file=src/migrations/0007_musicbrainz_releases.sql
-- Do not edit 0001_crawl.sql.

CREATE TABLE IF NOT EXISTS musicbrainz_releases (
    artist_id INTEGER NOT NULL,
    mbid TEXT NOT NULL,
    title TEXT NOT NULL,
    release_type TEXT,
    released_at TEXT,
    precision TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (artist_id, mbid)
);

CREATE INDEX IF NOT EXISTS idx_mb_releases_artist
    ON musicbrainz_releases (artist_id, released_at);
