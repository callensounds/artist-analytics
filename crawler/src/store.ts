/** D1 writes. Same tables and metric labels as the Python crawler. */

export interface Statement {
  sql: string;
  params: unknown[];
}

export interface Store {
  all<T = Record<string, unknown>>(sql: string, params?: unknown[]): Promise<T[]>;
  first<T = Record<string, unknown>>(sql: string, params?: unknown[]): Promise<T | null>;
  batch(statements: Statement[]): Promise<void>;
}

interface D1Statement {
  all: () => Promise<{ results?: unknown[] }>;
  first: <T>() => Promise<T | null>;
  run: () => Promise<unknown>;
  bind: (...params: unknown[]) => D1Statement;
}

interface D1Prepared {
  bind: (...params: unknown[]) => D1Statement;
  all: () => Promise<{ results?: unknown[] }>;
  first: <T>() => Promise<T | null>;
  run: () => Promise<unknown>;
}

interface D1Like {
  prepare: (sql: string) => D1Prepared;
  batch: (statements: unknown[]) => Promise<unknown>;
}

export function fromD1(db: D1Like): Store {
  return {
    async all(sql, params = []) {
      const prepared = db.prepare(sql);
      const statement = params.length ? prepared.bind(...params) : prepared;
      const result = await statement.all();
      return (result.results ?? []) as Record<string, unknown>[] as never;
    },
    async first(sql, params = []) {
      const prepared = db.prepare(sql);
      const statement = params.length ? prepared.bind(...params) : prepared;
      return (await statement.first()) ?? null;
    },
    async batch(statements) {
      if (!statements.length) return;
      await db.batch(
        statements.map((statement) => {
          const prepared = db.prepare(statement.sql);
          return statement.params.length ? prepared.bind(...statement.params) : prepared;
        }),
      );
    },
  };
}

const SCHEMA = `
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
CREATE INDEX IF NOT EXISTS idx_related_from_artist
    ON related_edges (from_artist_id, rank, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_sightings_artist_seen
    ON content_sightings (artist_id, first_seen_at);
CREATE TABLE IF NOT EXISTS playlist_appearances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_id INTEGER NOT NULL,
    playlist_id TEXT NOT NULL,
    playlist_name TEXT NOT NULL,
    owner_name TEXT,
    follower_count INTEGER,
    position INTEGER,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE (artist_id, playlist_id)
);
CREATE INDEX IF NOT EXISTS idx_playlist_artist_seen
    ON playlist_appearances (artist_id, last_seen_at);
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
CREATE TABLE IF NOT EXISTS alert_backfill (
    artist_id INTEGER PRIMARY KEY,
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_artists (
    user_id INTEGER NOT NULL,
    artist_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, artist_id)
);
CREATE INDEX IF NOT EXISTS idx_user_artists_user
    ON user_artists (user_id, created_at);
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
CREATE TABLE IF NOT EXISTS artist_tracks (
    artist_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    track_name TEXT NOT NULL,
    popularity INTEGER,
    album_name TEXT,
    rank INTEGER,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (artist_id, source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_artist_tracks_seen
    ON artist_tracks (artist_id, source, last_seen_at);
CREATE TABLE IF NOT EXISTS track_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    popularity INTEGER NOT NULL,
    captured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_track_snapshots_lookup
    ON track_snapshots (artist_id, source, external_id, captured_at);
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
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);
`;

export function utcNow(date = new Date()): string {
  return date.toISOString().replace(/\.\d{3}Z$/, "Z");
}

export function parseUtc(value: unknown): Date | null {
  if (typeof value !== "string" || !value) return null;
  const parsed = new Date(value.endsWith("Z") ? value : `${value}Z`);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export async function migrate(store: Store): Promise<void> {
  for (const statement of SCHEMA.split(";").map((part) => part.trim()).filter(Boolean)) {
    await store.batch([{ sql: statement, params: [] }]);
  }
  const column = await store.first<{ name: string }>("PRAGMA table_info(crawl_state)");
  const columns = await store.all<{ name: string }>("PRAGMA table_info(crawl_state)");
  if (!columns.some((row) => row.name === "apple_blocked_until")) {
    await store.batch([{ sql: "ALTER TABLE crawl_state ADD COLUMN apple_blocked_until TEXT", params: [] }]);
  }
  void column;
  const stamp = utcNow();
  for (const version of ["0001_crawl.sql", "0002_apple_backoff.sql", "0003_dashboard_reads.sql", "0004_playlist_appearances.sql", "0005_metric_alerts.sql", "0006_alert_backfill.sql", "0007_musicbrainz_releases.sql", "0008_users.sql", "0009_itunes_charts.sql", "0010_artist_tracks.sql"]) {
    const applied = await store.first("SELECT version FROM schema_migrations WHERE version = ?", [version]);
    if (!applied) {
      await store.batch([
        { sql: "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)", params: [version, stamp] },
      ]);
    }
  }
}

const AUDIENCE = new Set([
  "spotify\0Monthly listeners",
  "spotify\0Followers",
  "instagram\0Followers",
  "tiktok\0Followers",
  "tiktok\0Likes",
  "youtube\0Subscribers",
]);

export interface MetricPoint {
  label: string;
  exact: number | null;
  value: string | null;
}

export interface SavedSource {
  platform: string;
  status: string;
  reason?: string | null;
  metrics?: MetricPoint[];
  sightings?: { title: string; url: string; kind: string; sortKey: string; precision: string }[];
  related?: { name: string; spotify_id: string; rank: number }[];
  tracks?: { externalId: string; name: string; popularity: number | null; album: string | null; rank: number | null }[];
  musicbrainzId?: string | null;
  spotifyId?: string | null;
  appleId?: string | null;
  youtubeChannelId?: string | null;
  instagram?: string | null;
  tiktok?: string | null;
  markCrawled?: boolean;
}

function popularityScore(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0 || value > 100) return null;
  return value;
}

function cleanTracks(raw: SavedSource["tracks"]) {
  const found: { externalId: string; name: string; popularity: number | null; album: string | null; rank: number | null }[] = [];
  const seen = new Set<string>();
  for (const track of raw || []) {
    if (found.length >= 10) break;
    const name = (track.name || "").replace(/\s+/g, " ").trim().slice(0, 200);
    const externalId = (track.externalId || "").trim().slice(0, 80);
    if (!name || !externalId || seen.has(externalId)) continue;
    seen.add(externalId);
    const rank =
      typeof track.rank === "number" && Number.isInteger(track.rank) && track.rank >= 1 && track.rank <= 100
        ? track.rank
        : null;
    const album = (track.album || "").replace(/\s+/g, " ").trim().slice(0, 200);
    found.push({ externalId, name, popularity: popularityScore(track.popularity), album: album || null, rank });
  }
  return found;
}

function whole(value: unknown): number | null {
  if (typeof value === "number" && Number.isInteger(value) && value >= 0) return value;
  if (typeof value === "string" && /^[\d,]+$/.test(value.trim())) return Number(value.replace(/,/g, ""));
  return null;
}

export async function saveSource(
  store: Store,
  row: Record<string, unknown>,
  source: SavedSource,
  now = new Date(),
  relatedCap = 0,
): Promise<{ snapshots: number; edges: number; discovered: number }> {
  const artistId = Number(row.id);
  const stamp = utcNow(now);
  const latest = await store.all<{ source: string; metric: string; value: number; captured_at: string }>(
    `SELECT source, metric, value, captured_at FROM metric_snapshots
     WHERE artist_id = ? AND source = ? ORDER BY captured_at DESC`,
    [artistId, source.platform],
  );
  const seenMetric = new Set<string>();
  const newest = new Map<string, { value: number; captured_at: string }>();
  for (const item of latest) {
    if (seenMetric.has(item.metric)) continue;
    seenMetric.add(item.metric);
    newest.set(item.metric, { value: whole(item.value) ?? Number(item.value), captured_at: item.captured_at });
  }
  const snapshots: unknown[][] = [];
  for (const metric of source.metrics || []) {
    const key = `${source.platform}\0${metric.label}`;
    if (!AUDIENCE.has(key) || source.status === "error" || metric.exact === null) continue;
    const previous = newest.get(metric.label);
    if (previous && previous.value === metric.exact) {
      const at = parseUtc(previous.captured_at);
      if (at && now.getTime() - at.getTime() < 3600_000) continue;
    }
    snapshots.push([artistId, source.platform, metric.label, metric.exact, String(metric.value ?? metric.exact), stamp]);
  }
  const sightings: unknown[][] = [];
  for (const item of (source.sightings || []).slice(0, 8)) {
    const external = (item.url || `${item.kind}:${item.title}:${item.sortKey}`).slice(0, 300);
    if (!external || !item.title) continue;
    sightings.push([
      artistId,
      source.platform,
      external,
      item.title.slice(0, 200),
      item.kind,
      item.sortKey,
      item.precision,
      stamp,
    ]);
  }
  const statements: Statement[] = [
    {
      sql: `UPDATE artists SET
          musicbrainz_id = COALESCE(?, musicbrainz_id),
          spotify_id = COALESCE(?, spotify_id),
          apple_id = COALESCE(?, apple_id),
          youtube_channel_id = COALESCE(?, youtube_channel_id),
          instagram_handle = COALESCE(?, instagram_handle),
          tiktok_handle = COALESCE(?, tiktok_handle),
          last_crawled_at = COALESCE(?, last_crawled_at)
        WHERE id = ?`,
      params: [
        source.musicbrainzId || null,
        source.spotifyId || null,
        source.appleId || null,
        source.youtubeChannelId || null,
        source.instagram || null,
        source.tiktok || null,
        source.markCrawled === false ? null : stamp,
        artistId,
      ],
    },
  ];
  if (snapshots.length) {
    const marks = snapshots.map(() => "(?, ?, ?, ?, ?, ?)").join(", ");
    statements.push({
      sql: `INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at) VALUES ${marks}`,
      params: snapshots.flat(),
    });
  }
  if (sightings.length) {
    const marks = sightings.map(() => "(?, ?, ?, ?, ?, ?, ?, ?)").join(", ");
    statements.push({
      sql: `INSERT OR IGNORE INTO content_sightings
        (artist_id, source, external_id, title, kind, published_at, precision, first_seen_at)
        VALUES ${marks}`,
      params: sightings.flat(),
    });
  }
  const related = (source.related || []).slice(0, 12);
  let edges = 0;
  let discovered = 0;
  const edgeRows: unknown[][] = [];
  for (const item of related) {
    if (!item.spotify_id || !item.name) continue;
    edges += 1;
    edgeRows.push([artistId, item.spotify_id, item.name.slice(0, 120), "spotify", item.rank, stamp, stamp]);
  }
  if (edgeRows.length) {
    const marks = edgeRows.map(() => "(?, ?, ?, ?, ?, ?, ?)").join(", ");
    statements.push({
      sql: `INSERT INTO related_edges
        (from_artist_id, to_spotify_id, to_name, source, rank, first_seen_at, last_seen_at)
        VALUES ${marks}
        ON CONFLICT(from_artist_id, to_spotify_id, source) DO UPDATE SET
          to_name = excluded.to_name, rank = excluded.rank, last_seen_at = excluded.last_seen_at`,
      params: edgeRows.flat(),
    });
  }
  if (relatedCap > 0 && edgeRows.length) {
    const count = await store.first<{ n: number }>(
      "SELECT COUNT(*) AS n FROM artists WHERE origin = 'related' AND tracked = 1",
    );
    let room = relatedCap - Number(count?.n || 0);
    const selfId = source.spotifyId || "";
    for (const item of related) {
      if (room <= 0) break;
      if (item.rank > 5 || !item.spotify_id || !item.name || item.spotify_id === selfId) continue;
      const normalized = normalizeName(item.name);
      const existing = await store.first<{ id: number }>(
        "SELECT id FROM artists WHERE spotify_id = ? OR normalized_name = ?",
        [item.spotify_id, normalized],
      );
      if (existing) {
        statements.push({
          sql: `UPDATE related_edges SET to_artist_id = ?
                WHERE from_artist_id = ? AND to_spotify_id = ? AND source = 'spotify'`,
          params: [existing.id, artistId, item.spotify_id],
        });
        continue;
      }
      statements.push({
        sql: `INSERT INTO artists (name, normalized_name, spotify_id, origin, tracked, created_at)
              VALUES (?, ?, ?, 'related', 1, ?)
              ON CONFLICT(normalized_name) DO NOTHING`,
        params: [item.name.slice(0, 120), normalized, item.spotify_id, stamp],
      });
      statements.push({
        sql: `UPDATE related_edges SET to_artist_id = (
                SELECT id FROM artists WHERE normalized_name = ? OR spotify_id = ? LIMIT 1
              )
              WHERE from_artist_id = ? AND to_spotify_id = ? AND source = 'spotify'`,
        params: [normalized, item.spotify_id, artistId, item.spotify_id],
      });
      discovered += 1;
      room -= 1;
    }
  }
  const tracks = cleanTracks(source.tracks);
  const trackRows: unknown[][] = [];
  const trackSnapshots: unknown[][] = [];
  if (tracks.length) {
    const prior = await store.all<{ external_id: string; popularity: number | null; captured_at: string }>(
      `SELECT external_id, popularity, captured_at FROM track_snapshots
       WHERE artist_id = ? AND source = ? ORDER BY captured_at DESC, id DESC LIMIT 40`,
      [artistId, source.platform],
    );
    const newest = new Map<string, { popularity: number | null; captured_at: string }>();
    for (const row of prior) {
      if (!newest.has(row.external_id)) newest.set(row.external_id, row);
    }
    for (const track of tracks) {
      trackRows.push([
        artistId,
        source.platform,
        track.externalId,
        track.name,
        track.popularity,
        track.album,
        track.rank,
        stamp,
        stamp,
      ]);
      if (track.popularity === null) continue;
      const previous = newest.get(track.externalId);
      if (previous && popularityScore(previous.popularity) === track.popularity) {
        const at = parseUtc(previous.captured_at);
        if (at && now.getTime() - at.getTime() < 3600_000) continue;
      }
      trackSnapshots.push([artistId, source.platform, track.externalId, track.popularity, stamp]);
    }
  }
  if (trackRows.length) {
    const marks = trackRows.map(() => "(?, ?, ?, ?, ?, ?, ?, ?, ?)").join(", ");
    statements.push({
      sql: `INSERT INTO artist_tracks (
          artist_id, source, external_id, track_name, popularity, album_name, rank, first_seen_at, last_seen_at
        ) VALUES ${marks}
        ON CONFLICT(artist_id, source, external_id) DO UPDATE SET
          track_name = excluded.track_name,
          popularity = COALESCE(excluded.popularity, artist_tracks.popularity),
          album_name = COALESCE(excluded.album_name, artist_tracks.album_name),
          rank = excluded.rank,
          last_seen_at = excluded.last_seen_at`,
      params: trackRows.flat(),
    });
  }
  if (trackSnapshots.length) {
    const marks = trackSnapshots.map(() => "(?, ?, ?, ?, ?)").join(", ");
    statements.push({
      sql: `INSERT INTO track_snapshots (artist_id, source, external_id, popularity, captured_at) VALUES ${marks}`,
      params: trackSnapshots.flat(),
    });
  }
  const wrote = snapshots.length || edges;
  const status = source.status === "error" ? (wrote ? "partial" : "error") : "ok";
  const detail = `${String(row.name || "")} | ${source.platform}=${source.status}${source.reason ? ` (${source.reason.slice(0, 140)})` : ""}`.slice(0, 2000);
  statements.push({
    sql: `INSERT INTO crawl_runs (
        started_at, finished_at, status, artists_attempted, artists_ok, artists_error,
        snapshots_written, edges_written, discovered, detail
      ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)`,
    params: [
      stamp,
      stamp,
      status,
      status === "ok" ? 1 : 0,
      status === "ok" ? 0 : 1,
      snapshots.length,
      edges,
      discovered,
      detail,
    ],
  });
  await store.batch(statements);
  return { snapshots: snapshots.length, edges, discovered };
}

export async function saveMusicBrainzReleases(
  store: Store,
  artistId: number,
  releases: { mbid: string; title: string; releaseType: string; releasedAt: string; precision: string }[],
  now = new Date(),
): Promise<void> {
  if (!releases.length) return;
  const stamp = utcNow(now);
  const width = 8;
  const per = 10;
  for (let offset = 0; offset < releases.length; offset += per) {
    const chunk = releases.slice(offset, offset + per);
    const marks = chunk.map(() => "(" + new Array(width).fill("?").join(",") + ")").join(",");
    const params: unknown[] = [];
    for (const release of chunk) {
      params.push(artistId, release.mbid, release.title, release.releaseType, release.releasedAt, release.precision, stamp, stamp);
    }
    await store.batch([
      {
        sql: `INSERT INTO musicbrainz_releases (
                artist_id, mbid, title, release_type, released_at, precision, first_seen_at, last_seen_at
              ) VALUES ${marks}
              ON CONFLICT(artist_id, mbid) DO UPDATE SET
                title = excluded.title,
                release_type = excluded.release_type,
                released_at = excluded.released_at,
                precision = excluded.precision,
                last_seen_at = excluded.last_seen_at`,
        params,
      },
    ]);
  }
}

function normalizeName(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, "");
}
