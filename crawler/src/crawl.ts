/**
 * Queue and cron for artist-analytics-crawler.
 * One message is one provider. MusicBrainz stores ids, then fans the others out.
 */

import {
  FETCH_BYTES,
  HTTP_USER_AGENT,
  MB_USER_AGENT,
  type MbLinks,
  type Sighting,
  compactCount,
  emptyLinks,
  extractLinks,
  leadingInt,
  listenerSlices,
  namesMatch,
  normalizeName,
  parseDiscography,
  parseInstagram,
  parseItunesAlbums,
  parseMonthlyListeners,
  parseRelatedArtists,
  parseSpotifyFollowers,
  sanitizeQuery,
  shelfWindow,
  tiktokVideos,
  visibleLines,
} from "./parse";
import { type SavedSource, type Store, migrate, parseUtc, saveSource, utcNow } from "./store";

export const RETRY_DELAY_SECONDS = 180;
export const RETRY_DELAY_MIN_SECONDS = 120;
export const RETRY_DELAY_MAX_SECONDS = 300;
const STALE_SECONDS = 6 * 60 * 60;
const FRESH_SECONDS = 3 * 60 * 60;
const APPLE_BACKOFF_SECONDS = 30 * 60;
const SCAN_WINDOW = 50;
export const MB_INTERVAL_MIN_MS = 1500;
export const MB_INTERVAL_MAX_MS = 2000;

/** A 429, 503, or dropped connection. The queue message must be retried, not acked. */
export class RetryableFetch extends Error {
  constructor(readonly delaySeconds: number, message: string) {
    super(message);
    this.name = "RetryableFetch";
  }
}

export function retryDelaySeconds(retryAfter: string | null, now = Date.now()): number {
  const header = parseRetryAfter(retryAfter, now);
  if (header !== null && header > 0) return Math.min(3600, Math.max(RETRY_DELAY_MIN_SECONDS, Math.ceil(header)));
  const span = RETRY_DELAY_MAX_SECONDS - RETRY_DELAY_MIN_SECONDS;
  return RETRY_DELAY_MIN_SECONDS + Math.floor(Math.random() * (span + 1));
}

function parseRetryAfter(value: string | null, now: number): number | null {
  if (!value) return null;
  const seconds = Number(value.trim());
  if (Number.isFinite(seconds)) return seconds;
  const date = Date.parse(value);
  if (Number.isNaN(date)) return null;
  return Math.ceil((date - now) / 1000);
}

export interface CrawlEnv {
  DB: Store;
  CRAWL_QUEUE: { send: (body: unknown) => Promise<unknown> | unknown };
  YOUTUBE_API_KEY?: string;
  CRAWL_ENQUEUE_LIMIT?: string;
  CRAWL_STALE_SECONDS?: string;
  RELATED_AUTOTRACK_CAP?: string;
}

export interface CrawlMessage {
  artist_id?: number;
  name?: string;
  origin?: string;
  provider?: string;
  mbid?: string;
}

const PROVIDERS = new Set(["musicbrainz", "spotify", "instagram", "tiktok", "apple", "youtube"]);

let mbNext = 0;

export function resetMusicBrainzPace(): void {
  mbNext = 0;
}

async function paceMusicBrainz(): Promise<void> {
  const now = Date.now();
  const wait = mbNext - now;
  const gap = MB_INTERVAL_MIN_MS + Math.floor(Math.random() * (MB_INTERVAL_MAX_MS - MB_INTERVAL_MIN_MS + 1));
  mbNext = Math.max(now, mbNext) + gap;
  if (wait > 0) await new Promise((resolve) => setTimeout(resolve, wait));
}

function numberSetting(raw: string | undefined, fallback: number, upper: number): number {
  const value = raw === undefined ? fallback : Number(raw);
  if (!Number.isFinite(value)) return fallback;
  return Math.max(0, Math.min(Math.trunc(value), upper));
}

function stale(row: Record<string, unknown>, now: Date, seconds: number): boolean {
  const crawled = parseUtc(row.last_crawled_at);
  if (!crawled) return true;
  return now.getTime() - crawled.getTime() >= seconds * 1000;
}

async function freshSources(store: Store, artistId: number, now: Date): Promise<Set<string>> {
  const rows = await store.all<{ source: string; captured_at: string }>(
    "SELECT source, MAX(captured_at) AS captured_at FROM metric_snapshots WHERE artist_id = ? GROUP BY source",
    [artistId],
  );
  const fresh = new Set<string>();
  for (const row of rows) {
    const captured = parseUtc(row.captured_at);
    if (captured && now.getTime() - captured.getTime() < FRESH_SECONDS * 1000 && row.source) fresh.add(row.source);
  }
  return fresh;
}

async function appleBlocked(store: Store, now: Date): Promise<boolean> {
  const row = await store.first<{ apple_blocked_until: string | null }>(
    "SELECT apple_blocked_until FROM crawl_state WHERE id = 1",
  );
  const until = parseUtc(row?.apple_blocked_until);
  return Boolean(until && until.getTime() > now.getTime());
}

async function blockApple(store: Store, now: Date): Promise<void> {
  const until = new Date(now.getTime() + APPLE_BACKOFF_SECONDS * 1000);
  await store.batch([
    {
      sql: `INSERT INTO crawl_state (id, next_artist_id, updated_at, apple_blocked_until)
            VALUES (1, 0, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              apple_blocked_until = excluded.apple_blocked_until,
              updated_at = excluded.updated_at`,
      params: [utcNow(now), utcNow(until)],
    },
  ]);
}

export async function enqueueStale(env: CrawlEnv, now = new Date()): Promise<{ status: string; attempted: number }> {
  await migrate(env.DB);
  const limit = Math.max(1, numberSetting(env.CRAWL_ENQUEUE_LIMIT, 10, 50));
  const staleSeconds = Math.max(60, numberSetting(env.CRAWL_STALE_SECONDS, STALE_SECONDS, 7 * 24 * 3600));
  const cursorRow = await env.DB.first<{ next_artist_id: number }>("SELECT next_artist_id FROM crawl_state WHERE id = 1");
  let cursor = Number(cursorRow?.next_artist_id || 0);
  let rows = await env.DB.all<Record<string, unknown>>(
    "SELECT * FROM artists WHERE tracked = 1 AND id > ? ORDER BY id LIMIT ?",
    [cursor, SCAN_WINDOW],
  );
  if (!rows.length) {
    rows = await env.DB.all<Record<string, unknown>>(
      "SELECT * FROM artists WHERE tracked = 1 ORDER BY id LIMIT ?",
      [SCAN_WINDOW],
    );
  }
  const chosen: Record<string, unknown>[] = [];
  let lastId = cursor;
  for (const row of rows) {
    lastId = Number(row.id);
    if (stale(row, now, staleSeconds)) chosen.push(row);
    if (chosen.length >= limit) break;
  }
  for (const row of chosen) {
    await env.CRAWL_QUEUE.send({ artist_id: Number(row.id), provider: "musicbrainz" });
  }
  const stamp = utcNow(now);
  await env.DB.batch([
    {
      sql: `INSERT INTO crawl_state (id, next_artist_id, updated_at) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET next_artist_id = excluded.next_artist_id, updated_at = excluded.updated_at`,
      params: [chosen.length || lastId !== cursor ? lastId : 0, stamp],
    },
    {
      sql: `INSERT INTO crawl_runs (started_at, finished_at, status, artists_attempted, cursor, detail)
            VALUES (?, ?, 'queued', ?, ?, ?)`,
      params: [stamp, stamp, chosen.length, String(chosen.length || lastId !== cursor ? lastId : cursor), JSON.stringify({ mode: "queue", sent: chosen.length })],
    },
  ]);
  return { status: "queued", attempted: chosen.length };
}

async function ensureArtist(store: Store, name: string, origin: string, now: Date): Promise<Record<string, unknown> | null> {
  const cleaned = name.trim().slice(0, 120);
  const normalized = normalizeName(cleaned);
  if (!cleaned || !normalized) return null;
  const safeOrigin = origin === "related" ? "related" : "search";
  await store.batch([
    {
      sql: `INSERT INTO artists (name, normalized_name, origin, tracked, created_at)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(normalized_name) DO UPDATE SET
              name = excluded.name,
              tracked = 1,
              origin = CASE WHEN excluded.origin = 'search' THEN 'search' ELSE artists.origin END`,
      params: [cleaned, normalized, safeOrigin, utcNow(now)],
    },
  ]);
  return store.first("SELECT * FROM artists WHERE normalized_name = ?", [normalized]);
}

async function sendFollowups(env: CrawlEnv, artistId: number, links: MbLinks, fresh: Set<string>, blocked: boolean): Promise<void> {
  const providers: string[] = [];
  if (links.spotify_id && !fresh.has("spotify")) providers.push("spotify");
  if (links.instagram && !fresh.has("instagram")) providers.push("instagram");
  if (links.tiktok && !fresh.has("tiktok")) providers.push("tiktok");
  if (!blocked && !fresh.has("apple")) providers.push("apple");
  if (env.YOUTUBE_API_KEY && !fresh.has("youtube")) providers.push("youtube");
  for (const provider of providers) {
    await env.CRAWL_QUEUE.send({ artist_id: artistId, provider });
  }
}

async function fetchText(url: string, maxBytes: number, headers: Record<string, string> = {}, redirect: RequestRedirect = "manual"): Promise<{ status: number; ok: boolean; text: string }> {
  let response: Response;
  try {
    response = await fetch(url, {
      headers: { "User-Agent": HTTP_USER_AGENT, Accept: "application/json, text/html;q=0.9", ...headers },
      redirect,
    });
  } catch (error) {
    throw new RetryableFetch(retryDelaySeconds(null), error instanceof Error ? error.message : "network error");
  }
  if (response.status === 429 || response.status === 503) {
    throw new RetryableFetch(
      retryDelaySeconds(response.headers.get("retry-after")),
      `HTTP ${response.status}`,
    );
  }
  try {
    const reader = response.body?.getReader();
    const chunks: Uint8Array[] = [];
    let size = 0;
    if (reader) {
      while (size < maxBytes) {
        const part = await reader.read();
        if (part.done) break;
        const data = part.value;
        const room = maxBytes - size;
        chunks.push(data.byteLength > room ? data.slice(0, room) : data);
        size += Math.min(data.byteLength, room);
        if (data.byteLength > room) {
          await reader.cancel();
          break;
        }
      }
    }
    const text = new TextDecoder().decode(concat(chunks));
    return { status: response.status, ok: response.ok, text };
  } catch (error) {
    if (error instanceof RetryableFetch) throw error;
    throw new RetryableFetch(retryDelaySeconds(null), error instanceof Error ? error.message : "network error");
  }
}

function concat(chunks: Uint8Array[]): Uint8Array {
  const size = chunks.reduce((sum, chunk) => sum + chunk.byteLength, 0);
  const out = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return out;
}

async function fetchJson(url: string, headers: Record<string, string> = {}): Promise<{ status: number; ok: boolean; json: unknown }> {
  const response = await fetchText(url, 80_000, headers, "follow");
  let json: unknown = null;
  try {
    json = JSON.parse(response.text);
  } catch {
    json = null;
  }
  return { status: response.status, ok: response.ok, json };
}

async function musicbrainz(name: string, mbid: string): Promise<{ status: string; reason: string | null; links: MbLinks; profile: string }> {
  await paceMusicBrainz();
  const headers = { Accept: "application/json", "User-Agent": MB_USER_AGENT };
  if (mbid) {
    const response = await fetchJson(`https://musicbrainz.org/ws/2/artist/${encodeURIComponent(mbid)}?inc=url-rels&fmt=json`, headers);
    if (!response.ok || !response.json || typeof response.json !== "object") {
      return { status: "error", reason: `MusicBrainz HTTP ${response.status}.`, links: emptyLinks(), profile: name };
    }
    const doc = response.json as { name?: string; relations?: { url?: { resource?: string } }[] };
    const links = extractLinks(doc.relations || []);
    links._mbid = mbid;
    return { status: "ok", reason: null, links, profile: doc.name || name };
  }
  const query = sanitizeQuery(name);
  if (!query) return { status: "unavailable", reason: "Enter an artist name to search MusicBrainz.", links: emptyLinks(), profile: name };
  const url = `https://musicbrainz.org/ws/2/artist/?${new URLSearchParams({ query: `artist:"${query}"`, fmt: "json", limit: "5" })}`;
  const response = await fetchJson(url, headers);
  if (!response.ok || !response.json || typeof response.json !== "object") {
    return { status: "error", reason: `MusicBrainz HTTP ${response.status}.`, links: emptyLinks(), profile: name };
  }
  const artists = ((response.json as { artists?: Record<string, unknown>[] }).artists || []);
  const chosen = artists.find((artist) => Number(artist.score || 0) >= 80 || namesMatch(String(artist.name || ""), query));
  if (!chosen) {
    return {
      status: "unavailable",
      reason: "No confident MusicBrainz artist match. Apple and YouTube can still be matched by name.",
      links: emptyLinks(),
      profile: name,
    };
  }
  const found = String(chosen.id);
  if (Array.isArray(chosen.relations)) {
    const links = extractLinks(chosen.relations as { url?: { resource?: string } }[]);
    links._mbid = found;
    return { status: "ok", reason: null, links, profile: String(chosen.name || name) };
  }
  const links = emptyLinks();
  links._mbid = found;
  links._needs_lookup = "1";
  return { status: "ok", reason: "lookup queued", links, profile: String(chosen.name || name) };
}

async function spotify(artistId: string): Promise<SavedSource> {
  const page = await fetchText(`https://open.spotify.com/artist/${artistId}`, FETCH_BYTES);
  if (page.status === 403) {
    return { platform: "spotify", status: "unavailable", reason: "Spotify blocked the public artist page (HTTP 403).", markCrawled: true };
  }
  if (!page.ok) return { platform: "spotify", status: "unavailable", reason: `The public Spotify page returned HTTP ${page.status}.`, markCrawled: true };
  const lines = visibleLines(shelfWindow(page.text));
  const listeners = parseMonthlyListeners(listenerSlices(page.text));
  const followers = parseSpotifyFollowers(lines);
  const related = parseRelatedArtists(page.text);
  const albums = parseDiscography(lines);
  return {
    platform: "spotify",
    status: "ok",
    metrics: [
      { label: "Monthly listeners", exact: listeners.exact, value: listeners.display },
      { label: "Followers", exact: followers, value: followers === null ? null : followers.toLocaleString("en-US") },
    ],
    sightings: albums.map((album) => ({
      title: album.title,
      url: "",
      kind: "release",
      sortKey: album.year,
      precision: "year",
    })),
    related,
    spotifyId: artistId,
    markCrawled: true,
  };
}

async function instagram(handle: string): Promise<SavedSource> {
  const page = await fetchText(`https://www.instagram.com/${handle}/embed/`, 300_000);
  if (!page.ok) {
    return { platform: "instagram", status: "unavailable", reason: `The public embed page returned HTTP ${page.status}.`, markCrawled: true };
  }
  const parsed = parseInstagram(page.text, handle);
  return {
    platform: "instagram",
    status: parsed.followers === null && !parsed.posts.length ? "unavailable" : "ok",
    reason: parsed.followers === null && !parsed.posts.length ? "The public embed page did not include follower counts or taken_at_timestamp values." : null,
    metrics: [{ label: "Followers", exact: parsed.followers, value: compactCount(parsed.followers) }],
    sightings: parsed.posts,
    instagram: handle,
    markCrawled: true,
  };
}

async function tiktok(handle: string): Promise<SavedSource> {
  const page = await fetchText(`https://www.tiktok.com/@${handle}`, 400_000);
  if (page.status === 403) {
    return { platform: "tiktok", status: "unavailable", reason: "TikTok blocked the profile request (HTTP 403).", markCrawled: true };
  }
  const followers = leadingInt(page.text, "followerCount");
  const likes = leadingInt(page.text, "heartCount");
  if (followers === null) {
    return { platform: "tiktok", status: "unavailable", reason: "TikTok's public profile page did not include user stats.", markCrawled: true };
  }
  return {
    platform: "tiktok",
    status: "ok",
    metrics: [
      { label: "Followers", exact: followers, value: compactCount(followers) },
      { label: "Likes", exact: likes, value: compactCount(likes) },
    ],
    sightings: tiktokVideos(page.text),
    tiktok: handle,
    markCrawled: true,
  };
}

async function apple(name: string, appleId: string): Promise<SavedSource & { forbidden: boolean }> {
  const lookupId = appleId;
  const url = lookupId
    ? `https://itunes.apple.com/lookup?${new URLSearchParams({ id: lookupId, entity: "album", limit: "25", country: "us" })}`
    : `https://itunes.apple.com/search?${new URLSearchParams({ term: name, entity: "musicArtist", limit: "5", country: "us" })}`;
  const response = await fetchJson(url);
  if (response.status === 403) {
    return {
      platform: "apple",
      status: "unavailable",
      reason: "Apple refused the lookup from this network (HTTP 403). Try again shortly.",
      forbidden: true,
      markCrawled: true,
    };
  }
  if (!response.ok || !response.json || typeof response.json !== "object") {
    return { platform: "apple", status: "error", reason: `iTunes Search returned HTTP ${response.status}.`, forbidden: false, markCrawled: true };
  }
  const payload = response.json as { results?: Record<string, unknown>[] };
  if (!lookupId) {
    const match = (payload.results || []).find((row) => namesMatch(String(row.artistName || ""), name));
    return {
      platform: "apple",
      status: match ? "ok" : "unavailable",
      reason: match ? null : "No exact iTunes artist match. A nearby name is not shown as this artist.",
      appleId: match ? String(match.artistId) : null,
      forbidden: false,
      markCrawled: true,
    };
  }
  const parsed = parseItunesAlbums(payload, lookupId);
  return {
    platform: "apple",
    status: parsed.items.length || parsed.artistName ? "ok" : "unavailable",
    reason: parsed.items.length || parsed.artistName ? null : "iTunes lookup returned no albums for that artist id.",
    sightings: parsed.items,
    appleId: lookupId,
    forbidden: false,
    markCrawled: true,
  };
}

async function youtube(name: string, channelId: string, apiKey: string): Promise<SavedSource> {
  if (!apiKey.startsWith("AIza") || apiKey === "[REDACTED]") {
    return { platform: "youtube", status: "error", reason: "YouTube key is misconfigured", markCrawled: true };
  }
  const params = new URLSearchParams({ part: "snippet,statistics", key: apiKey, maxResults: "1" });
  if (channelId) params.set("id", channelId);
  else params.set("forHandle", name.replace(/^@/, ""));
  const response = await fetchJson(`https://www.googleapis.com/youtube/v3/channels?${params}`);
  if (!response.ok || !response.json || typeof response.json !== "object") {
    return { platform: "youtube", status: "error", reason: `YouTube returned HTTP ${response.status}.`, markCrawled: true };
  }
  const item = ((response.json as { items?: Record<string, unknown>[] }).items || [])[0];
  if (!item) return { platform: "youtube", status: "unavailable", reason: "YouTube did not return a channel.", markCrawled: true };
  const stats = (item.statistics || {}) as { hiddenSubscriberCount?: boolean; subscriberCount?: string };
  const exact = stats.hiddenSubscriberCount ? null : Number(stats.subscriberCount);
  const subscribers = Number.isFinite(exact) ? exact : null;
  return {
    platform: "youtube",
    status: "ok",
    metrics: [{ label: "Subscribers", exact: subscribers, value: compactCount(subscribers) }],
    youtubeChannelId: String(item.id || channelId || ""),
    markCrawled: true,
  };
}

async function knownRow(store: Store, fields: CrawlMessage, now: Date): Promise<Record<string, unknown> | null> {
  if (fields.artist_id !== undefined && fields.artist_id !== null) {
    return store.first("SELECT * FROM artists WHERE id = ? AND tracked = 1", [fields.artist_id]);
  }
  if (fields.name) return ensureArtist(store, fields.name, fields.origin || "search", now);
  return null;
}

export async function handleMessage(env: CrawlEnv, fields: CrawlMessage, now = new Date()): Promise<{ ok: boolean; detail: string }> {
  await migrate(env.DB);
  const provider = PROVIDERS.has(fields.provider || "") ? fields.provider || "musicbrainz" : "musicbrainz";
  const row = await knownRow(env.DB, fields, now);
  if (!row) return { ok: true, detail: "missing artist" };
  const artistId = Number(row.id);
  if (provider === "musicbrainz" && !fields.mbid && !stale(row, now, numberSetting(env.CRAWL_STALE_SECONDS, STALE_SECONDS, 7 * 24 * 3600))) {
    return { ok: true, detail: `${row.name} | musicbrainz=ok (fresh)` };
  }
  if (provider === "musicbrainz") {
    const identity = await musicbrainz(String(row.name), fields.mbid || "");
    if (identity.links._needs_lookup && identity.links._mbid) {
      await env.CRAWL_QUEUE.send({ artist_id: artistId, provider: "musicbrainz", mbid: identity.links._mbid });
      await saveSource(env.DB, row, {
        platform: "musicbrainz",
        status: "ok",
        reason: "lookup queued",
        markCrawled: false,
      }, now, 0);
      return { ok: true, detail: `${row.name} | musicbrainz=ok (lookup queued)` };
    }
    await saveSource(env.DB, row, {
      platform: "musicbrainz",
      status: identity.status,
      reason: identity.reason,
      musicbrainzId: identity.links._mbid || null,
      spotifyId: identity.links.spotify_id || null,
      appleId: identity.links.apple_id || null,
      youtubeChannelId: identity.links.youtube_channel_ids[0] || null,
      instagram: identity.links.instagram || null,
      tiktok: identity.links.tiktok || null,
      markCrawled: identity.status !== "error",
    }, now, 0);
    if (identity.status === "ok" || identity.status === "unavailable") {
      const fresh = await freshSources(env.DB, artistId, now);
      const blocked = await appleBlocked(env.DB, now);
      const links = identity.status === "ok" ? identity.links : emptyLinks();
      await sendFollowups(env, artistId, links, fresh, blocked);
    }
    return { ok: identity.status !== "error", detail: `${row.name} | musicbrainz=${identity.status}` };
  }
  if (provider === "apple" && (await appleBlocked(env.DB, now))) {
    return { ok: true, detail: `${row.name} | apple=ok (skipped during Apple backoff)` };
  }
  const fresh = await freshSources(env.DB, artistId, now);
  if (fresh.has(provider)) return { ok: true, detail: `${row.name} | ${provider}=ok (snapshot is fresh)` };
  const links = {
    spotify_id: String(row.spotify_id || ""),
    instagram: String(row.instagram_handle || ""),
    tiktok: String(row.tiktok_handle || ""),
    apple_id: String(row.apple_id || ""),
    youtube_channel_id: String(row.youtube_channel_id || ""),
  };
  if (provider === "spotify" && !links.spotify_id) return { ok: true, detail: `${row.name} | spotify=unavailable` };
  if (provider === "instagram" && !links.instagram) return { ok: true, detail: `${row.name} | instagram=unavailable` };
  if (provider === "tiktok" && !links.tiktok) return { ok: true, detail: `${row.name} | tiktok=unavailable` };
  if (provider === "youtube" && !env.YOUTUBE_API_KEY) return { ok: true, detail: `${row.name} | youtube=missing_credentials` };
  let saved: SavedSource & { forbidden?: boolean };
  if (provider === "spotify") saved = await spotify(links.spotify_id);
  else if (provider === "instagram") saved = await instagram(links.instagram);
  else if (provider === "tiktok") saved = await tiktok(links.tiktok);
  else if (provider === "apple") saved = await apple(String(row.name), links.apple_id);
  else saved = await youtube(String(row.name), links.youtube_channel_id, env.YOUTUBE_API_KEY || "");
  if (saved.forbidden) await blockApple(env.DB, now);
  const relatedCap = provider === "spotify" ? numberSetting(env.RELATED_AUTOTRACK_CAP, 25, 500) : 0;
  await saveSource(env.DB, row, saved, now, relatedCap);
  return { ok: saved.status !== "error", detail: `${row.name} | ${saved.platform}=${saved.status}` };
}

export function messageFields(body: unknown): CrawlMessage {
  const record = typeof body === "string" ? safeJson(body) : (body as Record<string, unknown>) || {};
  const provider = String(record.provider || "musicbrainz");
  return {
    artist_id: record.artist_id === undefined || record.artist_id === null ? undefined : Number(record.artist_id),
    name: record.name ? String(record.name) : undefined,
    origin: record.origin ? String(record.origin) : "search",
    provider: PROVIDERS.has(provider) ? provider : "musicbrainz",
    mbid: record.mbid ? String(record.mbid) : "",
  };
}

function safeJson(body: string): Record<string, unknown> {
  try {
    return JSON.parse(body) as Record<string, unknown>;
  } catch {
    return {};
  }
}

export async function runScheduled(env: CrawlEnv): Promise<{ status: string; attempted: number }> {
  if (!env.DB) return { status: "skipped", attempted: 0 };
  return enqueueStale(env);
}

export interface QueueMessage {
  body: unknown;
  ack: () => void;
  retry: (options?: { delaySeconds?: number }) => void;
}

export async function runQueue(messages: QueueMessage[], env: CrawlEnv): Promise<{ acked: number; retried: number }> {
  let acked = 0;
  let retried = 0;
  const [first, ...rest] = messages;
  for (const extra of rest) {
    extra.retry({ delaySeconds: RETRY_DELAY_SECONDS });
    retried += 1;
  }
  if (!first) return { acked, retried };
  try {
    await handleMessage(env, messageFields(first.body));
    first.ack();
    acked += 1;
  } catch (error) {
    const retryable = error instanceof RetryableFetch;
    const delay = retryable ? error.delaySeconds : retryDelaySeconds(null);
    console.log(JSON.stringify({
      message: retryable ? "queue message retry" : "queue message failed",
      delaySeconds: delay,
      error: error instanceof Error ? error.message : String(error),
    }));
    if (!retryable) {
      try {
        await env.DB.batch([
          {
            sql: `INSERT INTO crawl_runs (started_at, finished_at, status, artists_attempted, artists_ok, artists_error, detail)
                  VALUES (?, ?, 'error', 1, 0, 1, ?)`,
            params: [utcNow(), utcNow(), String(error).slice(0, 2000)],
          },
        ]);
      } catch {
        // The retry still has to happen if the run row cannot be written.
      }
    }
    first.retry({ delaySeconds: delay });
    retried += 1;
  }
  return { acked, retried };
}
