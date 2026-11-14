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

/** D1 rejects a statement with more than 100 bound parameters. */
export const D1_MAX_PARAMS = 100;

export function statementTable(sql: string): string {
  const match = /\b(?:INTO|UPDATE|FROM|TABLE(?:\s+IF\s+NOT\s+EXISTS)?)\s+["'`]?([A-Za-z_][A-Za-z0-9_]*)/i.exec(sql);
  return match?.[1] ?? "statement";
}

function placeholderCount(sql: string): number {
  let count = 0;
  for (const char of sql) if (char === "?") count += 1;
  return count;
}

/**
 * Multi-row INSERT statements that stay within D1's bound-parameter cap.
 * Placeholders in `prefix` and `suffix` count against the same cap.
 * `extra` is bound in source order: prefix placeholders, then each row, then suffix placeholders.
 */
export function chunkedInsert(prefix: string, rows: unknown[][], suffix = "", extra: unknown[] = []): Statement[] {
  if (!rows.length) return [];
  const width = rows[0].length;
  const table = statementTable(prefix);
  for (const row of rows) {
    if (row.length !== width) throw new Error(`${table}: uneven row`);
  }
  const outside = placeholderCount(prefix) + placeholderCount(suffix);
  if (extra.length !== outside) {
    throw new Error(`${table}: ${extra.length} fixed parameters for ${outside} placeholders`);
  }
  const room = D1_MAX_PARAMS - outside;
  if (width < 1 || width > room) {
    throw new Error(`${table}: one row needs ${width + outside} parameters, above ${D1_MAX_PARAMS}`);
  }
  const per = Math.floor(room / width);
  const marker = "(" + new Array(width).fill("?").join(",") + ")";
  const tail = suffix ? (suffix.startsWith(" ") || suffix.startsWith("\n") ? suffix : ` ${suffix}`) : "";
  const prefixSlots = placeholderCount(prefix);
  const head = extra.slice(0, prefixSlots);
  const foot = extra.slice(prefixSlots);
  const statements: Statement[] = [];
  for (let offset = 0; offset < rows.length; offset += per) {
    const chunk = rows.slice(offset, offset + per);
    const params = head.slice();
    for (const row of chunk) params.push(...row);
    params.push(...foot);
    statements.push({
      sql: `${prefix} VALUES ${chunk.map(() => marker).join(",")}${tail}`,
      params,
    });
  }
  return statements;
}

export function crawlLabel(name: unknown): string {
  const text = String(name || "").replace(/\s+/g, " ").trim().slice(0, 120);
  return text || "artist";
}

/** One batch. A failure names the artist and the tables in that batch, without parameters. */
export async function commitWrites(store: Store, statements: Statement[], artist = ""): Promise<void> {
  if (!statements.length) return;
  const who = artist ? `${crawlLabel(artist)} | ` : "";
  for (const statement of statements) {
    if (statement.params.length > D1_MAX_PARAMS) {
      const table = statementTable(statement.sql);
      throw new Error(`${who}${table}: ${statement.params.length} bound parameters exceeds ${D1_MAX_PARAMS}`);
    }
  }
  try {
    await store.batch(statements);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const tables = blameTables(statements, message);
    throw new Error(`${who}${tables}: ${message}`);
  }
}

function blameTables(statements: Statement[], message: string): string {
  // SQLite reports the overflow without naming the statement. The statement
  // with the most bound parameters is the one that hit the cap.
  if (/too many SQL variables/i.test(message)) {
    const worst = statements.reduce((best, statement) => (statement.params.length > best.params.length ? statement : best));
    const names = statements
      .filter((statement) => statement.params.length === worst.params.length)
      .map((statement) => statementTable(statement.sql));
    const count = worst.params.length;
    return `${[...new Set(names)].join(", ")} (${count} ${count === 1 ? "param" : "params"})`;
  }
  return [...new Set(statements.map((statement) => statementTable(statement.sql)))].join(", ");
}

export function fromD1(db: D1Like): Store {
  const store: Store & { binding?: object } = {
    binding: db,
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
  return store;
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
    deezer_id TEXT,
    deezer_nb_album INTEGER,
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
    source TEXT,
    editorial INTEGER,
    UNIQUE (artist_id, playlist_id)
);
CREATE INDEX IF NOT EXISTS idx_playlist_artist_seen
    ON playlist_appearances (artist_id, last_seen_at);
CREATE TABLE IF NOT EXISTS playlist_crawls (
    artist_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    crawled_at TEXT NOT NULL,
    PRIMARY KEY (artist_id, source)
);
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
CREATE TABLE IF NOT EXISTS track_stats (
    artist_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    metric TEXT NOT NULL CHECK (metric IN ('deezer_rank', 'spotify_playcount', 'spotify_popularity')),
    value INTEGER NOT NULL,
    captured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_track_stats_lookup
    ON track_stats (artist_id, source, external_id, metric, captured_at);
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

/** An unchanged row is rewritten only when last_seen_at is at least this old. */
export const SEEN_KEEP_ALIVE_MS = 24 * 60 * 60 * 1000;
/**
 * An audience value identical to metric_latest is stored again only after this
 * long, so a flat series still has one real point per day. A different value
 * is stored immediately. Nothing is interpolated.
 */
export const SNAPSHOT_REPEAT_MS = 24 * 60 * 60 * 1000;

export function seenCutoff(now: Date): string {
  return utcNow(new Date(now.getTime() - SEEN_KEEP_ALIVE_MS));
}

export const LATEST_MIGRATION = "0014_track_stats.sql";

const MIGRATION_VERSIONS = [
  "0001_crawl.sql",
  "0002_apple_backoff.sql",
  "0003_dashboard_reads.sql",
  "0004_playlist_appearances.sql",
  "0005_metric_alerts.sql",
  "0006_alert_backfill.sql",
  "0007_musicbrainz_releases.sql",
  "0008_users.sql",
  "0009_itunes_charts.sql",
  "0010_artist_tracks.sql",
  "0011_read_budget.sql",
  "0012_deezer.sql",
  "0013_deezer_playlists.sql",
  LATEST_MIGRATION,
];

// Same statements as src/migrations/0011_read_budget.sql, without the ALTER
// lines. Those are applied only when the column is missing.
const READ_BUDGET_SQL = [
  `CREATE TABLE IF NOT EXISTS metric_latest (
    artist_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL,
    value_text TEXT,
    captured_at TEXT NOT NULL,
    snapshot_id INTEGER,
    PRIMARY KEY (artist_id, source, metric)
  )`,
  `CREATE TABLE IF NOT EXISTS source_freshness (
    artist_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    PRIMARY KEY (artist_id, source)
  )`,
  `CREATE TABLE IF NOT EXISTS audience_artists (
    artist_id INTEGER PRIMARY KEY
  )`,
  `CREATE TABLE IF NOT EXISTS chart_freshness (
    chart_source TEXT NOT NULL,
    territory TEXT NOT NULL,
    chart_date TEXT NOT NULL,
    collected_at TEXT,
    next_try_at TEXT,
    PRIMARY KEY (chart_source, territory, chart_date)
  )`,
  `CREATE INDEX IF NOT EXISTS idx_snapshots_artist_captured
    ON metric_snapshots (artist_id, captured_at, id)`,
  `CREATE INDEX IF NOT EXISTS idx_chart_entries_day
    ON chart_entries (territory, chart_date, chart_source)`,
  `CREATE INDEX IF NOT EXISTS idx_artists_origin_tracked
    ON artists (origin, tracked)`,
  `CREATE TRIGGER IF NOT EXISTS metric_snapshots_fresh
    AFTER INSERT ON metric_snapshots
    FOR EACH ROW
    BEGIN
      INSERT INTO source_freshness (artist_id, source, captured_at)
      VALUES (NEW.artist_id, NEW.source, NEW.captured_at)
      ON CONFLICT(artist_id, source) DO UPDATE SET
        captured_at = excluded.captured_at
      WHERE excluded.captured_at > source_freshness.captured_at;
    END`,
  `CREATE TRIGGER IF NOT EXISTS metric_snapshots_latest
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
    END`,
  `CREATE TRIGGER IF NOT EXISTS metric_snapshots_audience
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
    END`,
  `INSERT INTO metric_latest (artist_id, source, metric, value, value_text, captured_at, snapshot_id)
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
       )`,
  `INSERT INTO source_freshness (artist_id, source, captured_at)
    SELECT artist_id, source, MAX(captured_at)
    FROM metric_snapshots
    GROUP BY artist_id, source
    ON CONFLICT(artist_id, source) DO UPDATE SET
      captured_at = excluded.captured_at
    WHERE excluded.captured_at > source_freshness.captured_at`,
  `INSERT INTO audience_artists (artist_id)
    SELECT DISTINCT artist_id
    FROM metric_snapshots
    WHERE value IS NOT NULL
      AND (
           (source = 'spotify' AND metric IN ('Monthly listeners', 'Followers'))
        OR (source = 'instagram' AND metric = 'Followers')
        OR (source = 'tiktok' AND metric IN ('Followers', 'Likes'))
      )
    ON CONFLICT(artist_id) DO NOTHING`,
  `INSERT INTO chart_freshness (chart_source, territory, chart_date, collected_at)
    SELECT chart_source, territory, chart_date, MAX(collected_at)
    FROM chart_entries
    GROUP BY chart_source, territory, chart_date
    ON CONFLICT(chart_source, territory, chart_date) DO UPDATE SET
      collected_at = excluded.collected_at
    WHERE excluded.collected_at > COALESCE(chart_freshness.collected_at, '')`,
];

const migrated = new WeakSet<object>();

function migrateKey(store: Store): object {
  const binding = (store as { binding?: object }).binding;
  return binding ?? store;
}

async function ensureColumn(store: Store, table: string, column: string, ddl: string): Promise<void> {
  const columns = await store.all<{ name: string }>(`PRAGMA table_info(${table})`);
  if (!columns.some((row) => row.name === column)) {
    await store.batch([{ sql: ddl, params: [] }]);
  }
}

export async function migrate(store: Store): Promise<void> {
  const key = migrateKey(store);
  if (migrated.has(key)) return;
  try {
    const latest = await store.first("SELECT version FROM schema_migrations WHERE version = ?", [LATEST_MIGRATION]);
    if (latest) {
      migrated.add(key);
      return;
    }
  } catch {
    // schema_migrations does not exist on a brand new database.
  }
  for (const statement of SCHEMA.split(";").map((part) => part.trim()).filter(Boolean)) {
    await store.batch([{ sql: statement, params: [] }]);
  }
  await ensureColumn(store, "crawl_state", "apple_blocked_until", "ALTER TABLE crawl_state ADD COLUMN apple_blocked_until TEXT");
  await ensureColumn(
    store,
    "crawl_state",
    "alert_backfill_done",
    "ALTER TABLE crawl_state ADD COLUMN alert_backfill_done INTEGER NOT NULL DEFAULT 0",
  );
  await ensureColumn(store, "crawl_state", "related_tracked", "ALTER TABLE crawl_state ADD COLUMN related_tracked INTEGER");
  await ensureColumn(store, "artists", "deezer_id", "ALTER TABLE artists ADD COLUMN deezer_id TEXT");
  await ensureColumn(store, "artists", "deezer_nb_album", "ALTER TABLE artists ADD COLUMN deezer_nb_album INTEGER");
  await ensureColumn(store, "playlist_appearances", "source", "ALTER TABLE playlist_appearances ADD COLUMN source TEXT");
  await ensureColumn(store, "playlist_appearances", "editorial", "ALTER TABLE playlist_appearances ADD COLUMN editorial INTEGER");
  for (const sql of READ_BUDGET_SQL) {
    await store.batch([{ sql, params: [] }]);
  }
  const applied = await store.all<{ version: string }>("SELECT version FROM schema_migrations");
  const have = new Set(applied.map((row) => row.version));
  const stamp = utcNow();
  const missing = MIGRATION_VERSIONS.filter((version) => !have.has(version)).map((version) => ({
    sql: "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
    params: [version, stamp],
  }));
  if (missing.length) await store.batch(missing);
  migrated.add(key);
}

const AUDIENCE = new Set([
  "spotify\0Monthly listeners",
  "spotify\0Followers",
  "instagram\0Followers",
  "tiktok\0Followers",
  "tiktok\0Likes",
  "youtube\0Subscribers",
  "deezer\0Fans",
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
  tracks?: {
    externalId: string;
    name: string;
    popularity: number | null;
    album: string | null;
    rank: number | null;
    /** Deezer's public `rank` score. Not the 1-based list position. */
    deezerRank?: number | null;
    /** Spotify play count from the public page. Never a guessed figure. */
    playcount?: number | null;
  }[];
  musicbrainzId?: string | null;
  spotifyId?: string | null;
  appleId?: string | null;
  youtubeChannelId?: string | null;
  instagram?: string | null;
  tiktok?: string | null;
  deezerId?: string | null;
  deezerAlbumCount?: number | null;
  markCrawled?: boolean;
}

function popularityScore(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0 || value > 100) return null;
  return value;
}

/** A published non-negative integer. Commas, floats, and bools are not counts. */
export function publishedInteger(value: unknown): number | null {
  if (typeof value === "boolean" || value === null || value === undefined) return null;
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value) || value < 0) return null;
    return value;
  }
  if (typeof value === "string") {
    const text = value.trim();
    if (!/^\d+$/.test(text) || text.length > 15) return null;
    const number = Number(text);
    if (!Number.isSafeInteger(number) || number < 0) return null;
    return number;
  }
  return null;
}

const TRACK_STAT_METRICS = new Set(["deezer_rank", "spotify_playcount", "spotify_popularity"]);

function cleanTracks(raw: SavedSource["tracks"]) {
  const found: {
    externalId: string;
    name: string;
    popularity: number | null;
    album: string | null;
    rank: number | null;
    deezerRank: number | null;
    playcount: number | null;
  }[] = [];
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
    found.push({
      externalId,
      name,
      popularity: popularityScore(track.popularity),
      album: album || null,
      rank,
      deezerRank: publishedInteger(track.deezerRank),
      playcount: publishedInteger(track.playcount),
    });
  }
  return found;
}

function utcDay(stamp: string): string {
  return stamp.length >= 10 ? stamp.slice(0, 10) : "";
}

/**
 * Latest stored stat for each track the crawl is about to write.
 * One statement, one indexed LIMIT 1 per track, not one round trip per track.
 */
async function latestTrackStats(
  store: Store,
  artistId: number,
  source: string,
  stats: { externalId: string; metric: string }[],
): Promise<Map<string, { value: number; capturedAt: string }>> {
  const found = new Map<string, { value: number; capturedAt: string }>();
  const parts: string[] = [];
  const params: unknown[] = [];
  const seen = new Set<string>();
  for (const stat of stats) {
    if (!TRACK_STAT_METRICS.has(stat.metric)) continue;
    const key = `${stat.externalId}\0${stat.metric}`;
    if (seen.has(key)) continue;
    seen.add(key);
    parts.push(
      `SELECT external_id, metric, value, captured_at FROM (
         SELECT external_id, metric, value, captured_at
         FROM track_stats
         WHERE artist_id = ? AND source = ? AND external_id = ? AND metric = ?
         ORDER BY captured_at DESC
         LIMIT 1
       )`,
    );
    params.push(artistId, source, stat.externalId, stat.metric);
  }
  if (!parts.length) return found;
  const rows = await store.all<{ external_id: string; metric: string; value: unknown; captured_at: unknown }>(
    parts.join(" UNION ALL "),
    params,
  );
  for (const row of rows) {
    const value = publishedInteger(row.value);
    const capturedAt = typeof row.captured_at === "string" ? row.captured_at : "";
    if (value === null || !capturedAt) continue;
    found.set(`${row.external_id}\0${row.metric}`, { value, capturedAt });
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
  const cutoff = seenCutoff(now);
  const latest = await store.all<{ metric: string; value: number; captured_at: string }>(
    `SELECT metric, value, captured_at FROM metric_latest
     WHERE artist_id = ? AND source = ?`,
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
  let confirmedUnchanged = false;
  for (const metric of source.metrics || []) {
    const key = `${source.platform}\0${metric.label}`;
    if (!AUDIENCE.has(key) || source.status === "error" || metric.exact === null) continue;
    const previous = newest.get(metric.label);
    if (previous && previous.value === metric.exact) {
      const at = parseUtc(previous.captured_at);
      if (at && now.getTime() - at.getTime() < SNAPSHOT_REPEAT_MS) {
        confirmedUnchanged = true;
        continue;
      }
    }
    snapshots.push([artistId, source.platform, metric.label, metric.exact, String(metric.value ?? metric.exact), stamp]);
  }
  const sightings: unknown[][] = [];
  const sightingCap = source.platform === "deezer" ? 25 : 8;
  for (const item of (source.sightings || []).slice(0, sightingCap)) {
    if (source.platform === "deezer" && item.precision === "time") continue;
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
          deezer_id = COALESCE(?, deezer_id),
          deezer_nb_album = COALESCE(?, deezer_nb_album),
          last_crawled_at = COALESCE(?, last_crawled_at)
        WHERE id = ?`,
      params: [
        source.musicbrainzId || null,
        source.spotifyId || null,
        source.appleId || null,
        source.youtubeChannelId || null,
        source.instagram || null,
        source.tiktok || null,
        source.deezerId || null,
        source.deezerAlbumCount ?? null,
        source.markCrawled === false ? null : stamp,
        artistId,
      ],
    },
  ];
  statements.push(
    ...chunkedInsert(
      "INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)",
      snapshots,
    ),
  );
  // The chart keeps the real snapshot. This clock only tells the dashboard the
  // value was checked, so a flat series does not live-fetch on every page view.
  if (confirmedUnchanged && snapshots.length === 0) {
    statements.push({
      sql: `INSERT INTO source_freshness (artist_id, source, captured_at) VALUES (?, ?, ?)
            ON CONFLICT(artist_id, source) DO UPDATE SET captured_at = excluded.captured_at
            WHERE excluded.captured_at > source_freshness.captured_at`,
      params: [artistId, source.platform, stamp],
    });
  }
  statements.push(
    ...chunkedInsert(
      `INSERT OR IGNORE INTO content_sightings
        (artist_id, source, external_id, title, kind, published_at, precision, first_seen_at)`,
      sightings,
    ),
  );
  const related = (source.related || []).slice(0, 12);
  let edges = 0;
  let discovered = 0;
  const edgeRows: unknown[][] = [];
  for (const item of related) {
    if (!item.spotify_id || !item.name) continue;
    edges += 1;
    edgeRows.push([artistId, item.spotify_id, item.name.slice(0, 120), "spotify", item.rank, stamp, stamp]);
  }
  statements.push(
    ...chunkedInsert(
      `INSERT INTO related_edges
        (from_artist_id, to_spotify_id, to_name, source, rank, first_seen_at, last_seen_at)`,
      edgeRows,
      `ON CONFLICT(from_artist_id, to_spotify_id, source) DO UPDATE SET
          to_name = excluded.to_name, rank = excluded.rank, last_seen_at = excluded.last_seen_at
        WHERE excluded.to_name IS NOT related_edges.to_name
           OR excluded.rank IS NOT related_edges.rank
           OR related_edges.last_seen_at <= ?`,
      [cutoff],
    ),
  );
  if (relatedCap > 0 && edgeRows.length) {
    let room = relatedCap - (await relatedTracked(store, now));
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
                WHERE from_artist_id = ? AND to_spotify_id = ? AND source = 'spotify'
                  AND to_artist_id IS NOT ?`,
          params: [existing.id, artistId, item.spotify_id, existing.id],
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
    if (discovered) {
      statements.push({
        sql: "UPDATE crawl_state SET related_tracked = COALESCE(related_tracked, 0) + ? WHERE id = 1",
        params: [discovered],
      });
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
        if (at && now.getTime() - at.getTime() < SNAPSHOT_REPEAT_MS) continue;
      }
      trackSnapshots.push([artistId, source.platform, track.externalId, track.popularity, stamp]);
    }
  }
  statements.push(
    ...chunkedInsert(
      `INSERT INTO artist_tracks (
          artist_id, source, external_id, track_name, popularity, album_name, rank, first_seen_at, last_seen_at
        )`,
      trackRows,
      `ON CONFLICT(artist_id, source, external_id) DO UPDATE SET
          track_name = excluded.track_name,
          popularity = COALESCE(excluded.popularity, artist_tracks.popularity),
          album_name = COALESCE(excluded.album_name, artist_tracks.album_name),
          rank = excluded.rank,
          last_seen_at = excluded.last_seen_at
        WHERE excluded.track_name IS NOT artist_tracks.track_name
           OR (excluded.popularity IS NOT NULL AND excluded.popularity IS NOT artist_tracks.popularity)
           OR (excluded.album_name IS NOT NULL AND excluded.album_name IS NOT artist_tracks.album_name)
           OR excluded.rank IS NOT artist_tracks.rank
           OR artist_tracks.last_seen_at <= ?`,
      [cutoff],
    ),
  );
  statements.push(
    ...chunkedInsert(
      "INSERT INTO track_snapshots (artist_id, source, external_id, popularity, captured_at)",
      trackSnapshots,
    ),
  );
  // Deezer's rank score and a Spotify play count. Popularity stays in track_snapshots.
  // At most one row per track and metric per UTC day, and none when the value is unchanged.
  const statCandidates: { externalId: string; metric: string; value: number }[] = [];
  for (const track of tracks) {
    if (source.platform === "deezer" && track.deezerRank !== null) {
      statCandidates.push({ externalId: track.externalId, metric: "deezer_rank", value: track.deezerRank });
    } else if (source.platform === "spotify" && track.playcount !== null) {
      statCandidates.push({ externalId: track.externalId, metric: "spotify_playcount", value: track.playcount });
    }
  }
  const statRows: unknown[][] = [];
  if (statCandidates.length) {
    const priorStats = await latestTrackStats(store, artistId, source.platform, statCandidates);
    const day = utcDay(stamp);
    for (const stat of statCandidates) {
      const previous = priorStats.get(`${stat.externalId}\0${stat.metric}`);
      if (previous && (utcDay(previous.capturedAt) === day || previous.value === stat.value)) continue;
      statRows.push([artistId, source.platform, stat.externalId, stat.metric, stat.value, stamp]);
    }
  }
  statements.push(
    ...chunkedInsert(
      "INSERT INTO track_stats (artist_id, source, external_id, metric, value, captured_at)",
      statRows,
    ),
  );
  const wrote = snapshots.length || edges;
  const status = source.status === "error" ? (wrote ? "partial" : "error") : "ok";
  const detail = `${String(row.name || "")} | ${source.platform}=${source.status}${source.reason ? ` (${source.reason.slice(0, 140)})` : ""}`.slice(0, 2000);
  // Success is the steady state. The admin page still reads errors and partials.
  if (status !== "ok") {
    statements.push({
      sql: `INSERT INTO crawl_runs (
          started_at, finished_at, status, artists_attempted, artists_ok, artists_error,
          snapshots_written, edges_written, discovered, detail
        ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)`,
      params: [stamp, stamp, status, 0, 1, snapshots.length, edges, discovered, detail],
    });
  }
  await commitWrites(store, statements, String(row.name || ""));
  return { snapshots: snapshots.length, edges, discovered };
}

export async function saveMusicBrainzReleases(
  store: Store,
  artistId: number,
  releases: { mbid: string; title: string; releaseType: string; releasedAt: string; precision: string }[],
  now = new Date(),
  artistName = "",
): Promise<void> {
  const stamp = utcNow(now);
  const cutoff = seenCutoff(now);
  const rows = releases.map((release) => [
    artistId,
    release.mbid,
    release.title,
    release.releaseType,
    release.releasedAt,
    release.precision,
    stamp,
    stamp,
  ]);
  // playlist_crawls is the slow-source clock. source 'musicbrainz' is the release
  // catalog, not a playlist. An empty catalog still marks the clock so the next
  // cycles skip the release-group request.
  await commitWrites(
    store,
    [
      ...chunkedInsert(
        `INSERT INTO musicbrainz_releases (
                artist_id, mbid, title, release_type, released_at, precision, first_seen_at, last_seen_at
              )`,
        rows,
        `ON CONFLICT(artist_id, mbid) DO UPDATE SET
                title = excluded.title,
                release_type = excluded.release_type,
                released_at = excluded.released_at,
                precision = excluded.precision,
                last_seen_at = excluded.last_seen_at
              WHERE excluded.title IS NOT musicbrainz_releases.title
                 OR excluded.release_type IS NOT musicbrainz_releases.release_type
                 OR excluded.released_at IS NOT musicbrainz_releases.released_at
                 OR excluded.precision IS NOT musicbrainz_releases.precision
                 OR musicbrainz_releases.last_seen_at <= ?`,
        [cutoff],
      ),
      {
        sql: `INSERT INTO playlist_crawls (artist_id, source, crawled_at) VALUES (?, 'musicbrainz', ?)
              ON CONFLICT(artist_id, source) DO UPDATE SET crawled_at = excluded.crawled_at
              WHERE playlist_crawls.crawled_at < excluded.crawled_at`,
        params: [artistId, stamp],
      },
    ],
    artistName,
  );
}

export const PLAYLIST_WRITE_CAP = 50;

export interface PlaylistPlacement {
  id: string;
  title: string;
  ownerName: string | null;
  fans: number | null;
  fansKnown: boolean;
  editorial: boolean;
}

/** Upsert Deezer placements. first_seen_at stays on conflict. A missing fan count is not written as 0.
 *  An unchanged row is rewritten only when last_seen_at is at least a day old.
 */
export async function saveDeezerPlaylists(
  store: Store,
  artistId: number,
  artistName: string,
  playlists: PlaylistPlacement[],
  now = new Date(),
): Promise<number> {
  const stamp = utcNow(now);
  const cutoff = seenCutoff(now);
  const seen = new Set<string>();
  const known: unknown[][] = [];
  const unchecked: unknown[][] = [];
  for (const playlist of playlists) {
    if (known.length + unchecked.length >= PLAYLIST_WRITE_CAP) break;
    const id = String(playlist.id || "").trim();
    const title = String(playlist.title || "").replace(/\s+/g, " ").trim().slice(0, 200);
    if (!/^\d{1,32}$/.test(id) || !title || seen.has(id)) continue;
    seen.add(id);
    const owner = String(playlist.ownerName || "").replace(/\s+/g, " ").trim().slice(0, 120) || null;
    const fans = playlist.fansKnown ? whole(playlist.fans) : null;
    const row = [artistId, id, title, owner, fans, null, stamp, stamp, "deezer", playlist.editorial ? 1 : 0];
    if (playlist.fansKnown) known.push(row);
    else unchecked.push(row);
  }
  const kept = known.length + unchecked.length;
  const updateKnown = `ON CONFLICT(artist_id, playlist_id) DO UPDATE SET
      playlist_name = excluded.playlist_name,
      owner_name = COALESCE(excluded.owner_name, playlist_appearances.owner_name),
      follower_count = excluded.follower_count,
      position = COALESCE(excluded.position, playlist_appearances.position),
      last_seen_at = excluded.last_seen_at,
      source = excluded.source,
      editorial = excluded.editorial
    WHERE excluded.playlist_name IS NOT playlist_appearances.playlist_name
       OR (excluded.owner_name IS NOT NULL AND excluded.owner_name IS NOT playlist_appearances.owner_name)
       OR excluded.follower_count IS NOT playlist_appearances.follower_count
       OR (excluded.position IS NOT NULL AND excluded.position IS NOT playlist_appearances.position)
       OR excluded.source IS NOT playlist_appearances.source
       OR excluded.editorial IS NOT playlist_appearances.editorial
       OR playlist_appearances.last_seen_at <= ?`;
  const updateUnchecked = `ON CONFLICT(artist_id, playlist_id) DO UPDATE SET
      playlist_name = excluded.playlist_name,
      owner_name = COALESCE(excluded.owner_name, playlist_appearances.owner_name),
      position = COALESCE(excluded.position, playlist_appearances.position),
      last_seen_at = excluded.last_seen_at,
      source = excluded.source,
      editorial = excluded.editorial
    WHERE excluded.playlist_name IS NOT playlist_appearances.playlist_name
       OR (excluded.owner_name IS NOT NULL AND excluded.owner_name IS NOT playlist_appearances.owner_name)
       OR (excluded.position IS NOT NULL AND excluded.position IS NOT playlist_appearances.position)
       OR excluded.source IS NOT playlist_appearances.source
       OR excluded.editorial IS NOT playlist_appearances.editorial
       OR playlist_appearances.last_seen_at <= ?`;
  const insert = `INSERT INTO playlist_appearances (
      artist_id, playlist_id, playlist_name, owner_name, follower_count, position,
      first_seen_at, last_seen_at, source, editorial
    )`;
  await commitWrites(
    store,
    [
      ...chunkedInsert(insert, known, updateKnown, [cutoff]),
      ...chunkedInsert(insert, unchecked, updateUnchecked, [cutoff]),
      {
        sql: `INSERT INTO playlist_crawls (artist_id, source, crawled_at) VALUES (?, 'deezer', ?)
              ON CONFLICT(artist_id, source) DO UPDATE SET crawled_at = excluded.crawled_at
              WHERE playlist_crawls.crawled_at < excluded.crawled_at`,
        params: [artistId, stamp],
      },
    ],
    artistName,
  );
  return kept;
}

async function relatedTracked(store: Store, now: Date): Promise<number> {
  const row = await store.first<{ related_tracked: number | null }>(
    "SELECT related_tracked FROM crawl_state WHERE id = 1",
  );
  if (row && row.related_tracked !== null && row.related_tracked !== undefined) return Number(row.related_tracked);
  const count = await store.first<{ n: number }>(
    "SELECT COUNT(*) AS n FROM artists WHERE origin = 'related' AND tracked = 1",
  );
  const total = Number(count?.n || 0);
  await store.batch([
    {
      sql: `INSERT INTO crawl_state (id, next_artist_id, updated_at, related_tracked)
            VALUES (1, 0, ?, ?)
            ON CONFLICT(id) DO UPDATE SET related_tracked = excluded.related_tracked`,
      params: [utcNow(now), total],
    },
  ]);
  return total;
}

function normalizeName(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, "");
}
