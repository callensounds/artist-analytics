-- Time series, first-seen content, related artists, and crawl logs.
-- Apply with: npx wrangler d1 migrations apply artist-analytics --remote

CREATE TABLE IF NOT EXISTS artists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL UNIQUE,
    musicbrainz_id TEXT,
    spotify_id TEXT,
    apple_id TEXT,
    youtube_channel_id TEXT,
    instagram_handle TEXT,
    tiktok_handle TEXT,
    origin TEXT NOT NULL DEFAULT 'search',
    tracked INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_crawled_at TEXT
);

CREATE TABLE IF NOT EXISTS metric_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL,
    value_text TEXT,
    captured_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_lookup
    ON metric_snapshots (artist_id, source, metric, captured_at);

CREATE TABLE IF NOT EXISTS content_sightings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    kind TEXT,
    published_at TEXT,
    precision TEXT,
    first_seen_at TEXT NOT NULL,
    UNIQUE (artist_id, source, external_id)
);

CREATE TABLE IF NOT EXISTS related_edges (
    from_artist_id INTEGER NOT NULL,
    to_spotify_id TEXT NOT NULL,
    to_name TEXT NOT NULL,
    to_artist_id INTEGER,
    source TEXT NOT NULL DEFAULT 'spotify',
    rank INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (from_artist_id, to_spotify_id, source)
);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    artists_attempted INTEGER NOT NULL DEFAULT 0,
    artists_ok INTEGER NOT NULL DEFAULT 0,
    artists_error INTEGER NOT NULL DEFAULT 0,
    artists_skipped INTEGER NOT NULL DEFAULT 0,
    snapshots_written INTEGER NOT NULL DEFAULT 0,
    edges_written INTEGER NOT NULL DEFAULT 0,
    discovered INTEGER NOT NULL DEFAULT 0,
    cursor TEXT,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS crawl_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_artist_id INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
