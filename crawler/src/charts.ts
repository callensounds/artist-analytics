/** Public iTunes RSS charts. The dashboard reads chart_entries and does not fetch these URLs. */

import { type Statement, type Store, utcNow } from "./store";
import { HTTP_USER_AGENT } from "./parse";

export const CHART_TERRITORY = "us";
export const CHART_REFRESH_MS = 6 * 60 * 60 * 1000;
/** A failed feed waits this long before the next cron reads artists again. */
export const CHART_RETRY_MS = 60 * 60 * 1000;
export const CHART_MAX_BYTES = 450_000;
export const CHART_SOURCES = [
  {
    source: "itunes_topsongs",
    url: "https://itunes.apple.com/us/rss/topsongs/limit=100/json",
  },
  {
    source: "itunes_topalbums",
    url: "https://itunes.apple.com/us/rss/topalbums/limit=100/json",
  },
] as const;

export interface ChartRow {
  position: number;
  title: string;
  artistName: string;
  appleArtistId: string | null;
}

export interface ArtistRef {
  id: number;
  name: string;
  appleId: string | null;
}

export interface ChartIndex {
  byApple: Map<string, number[]>;
  byName: Map<string, number[]>;
}

export interface ChartIngestResult {
  status: "fresh" | "stored" | "partial" | "error";
  written: number;
  linked: number;
  errors: string[];
}

const ARTIST_HREF = /\/artist\/(?:[^/]+\/)?(\d+)/;

function labelOf(node: unknown): string {
  if (!node || typeof node !== "object") return "";
  const label = (node as { label?: unknown }).label;
  return typeof label === "string" ? label.trim() : "";
}

function readArtist(node: unknown): { name: string; id: string | null } {
  if (!node || typeof node !== "object") return { name: "", id: null };
  const record = node as { label?: unknown; attributes?: Record<string, unknown> };
  const name = typeof record.label === "string" ? record.label.trim() : "";
  const attributes = record.attributes && typeof record.attributes === "object" ? record.attributes : {};
  const explicit = attributes["im:id"];
  if (typeof explicit === "string" && /^\d+$/.test(explicit.trim())) return { name, id: explicit.trim() };
  if (typeof explicit === "number" && Number.isInteger(explicit) && explicit >= 0) return { name, id: String(explicit) };
  const href = typeof attributes.href === "string" ? attributes.href : "";
  const match = ARTIST_HREF.exec(href);
  return { name, id: match ? match[1] : null };
}

function artistFrom(entry: Record<string, unknown>): { name: string; id: string | null } {
  const raw = entry["im:artist"];
  const nodes = Array.isArray(raw) ? raw : raw ? [raw] : [];
  const parsed = nodes.map(readArtist).filter((item) => item.name);
  if (!parsed.length) return { name: "", id: null };
  if (parsed.length > 1) {
    return { name: parsed.map((item) => item.name).join(", ").slice(0, 200), id: null };
  }
  return { name: parsed[0].name.slice(0, 200), id: parsed[0].id };
}

function entriesOf(payload: unknown): unknown[] {
  if (!payload || typeof payload !== "object") return [];
  const feed = (payload as { feed?: { entry?: unknown } }).feed;
  if (!feed || typeof feed !== "object") return [];
  const entry = feed.entry;
  if (Array.isArray(entry)) return entry.slice(0, 100);
  if (entry && typeof entry === "object") return [entry];
  return [];
}

export function parseItunesRss(payload: unknown): ChartRow[] {
  const rows: ChartRow[] = [];
  entriesOf(payload).forEach((entry, index) => {
    if (!entry || typeof entry !== "object") return;
    const record = entry as Record<string, unknown>;
    const title = labelOf(record["im:name"]).slice(0, 200);
    const artist = artistFrom(record);
    if (!title || !artist.name) return;
    rows.push({
      position: index + 1,
      title,
      artistName: artist.name,
      appleArtistId: artist.id,
    });
  });
  return rows;
}

export function indexArtists(rows: ArtistRef[]): ChartIndex {
  const byApple = new Map<string, number[]>();
  const byName = new Map<string, number[]>();
  for (const row of rows) {
    const apple = (row.appleId || "").trim();
    if (/^\d+$/.test(apple)) {
      const list = byApple.get(apple) || [];
      list.push(row.id);
      byApple.set(apple, list);
    }
    const name = row.name.trim().toLowerCase();
    if (!name) continue;
    const list = byName.get(name) || [];
    list.push(row.id);
    byName.set(name, list);
  }
  return { byApple, byName };
}

export function matchChartArtist(appleArtistId: string | null, artistName: string, index: ChartIndex): number | null {
  const apple = (appleArtistId || "").trim();
  if (/^\d+$/.test(apple)) {
    const ids = index.byApple.get(apple) || [];
    if (ids.length === 1) return ids[0];
    if (ids.length > 1) return null;
  }
  const name = artistName.trim().toLowerCase();
  const ids = name ? index.byName.get(name) || [] : [];
  if (ids.length === 1) return ids[0];
  return null;
}

function chartDay(now: Date): string {
  return now.toISOString().slice(0, 10);
}

function isFresh(collectedAt: string | null | undefined, now: Date, refreshMs: number): boolean {
  if (!collectedAt || refreshMs <= 0) return false;
  const at = Date.parse(collectedAt);
  if (Number.isNaN(at)) return false;
  return now.getTime() - at < refreshMs;
}

async function fetchChart(url: string): Promise<{ status: number; text: string; truncated: boolean } | { error: string }> {
  try {
    const response = await fetch(url, {
      headers: { "User-Agent": HTTP_USER_AGENT, Accept: "application/json" },
      redirect: "follow",
    });
    const buffer = await response.arrayBuffer();
    const truncated = buffer.byteLength > CHART_MAX_BYTES;
    const bytes = truncated ? buffer.slice(0, CHART_MAX_BYTES) : buffer;
    return { status: response.status, text: new TextDecoder().decode(bytes), truncated };
  } catch (error) {
    return { error: error instanceof Error ? error.message : "network error" };
  }
}

async function writeRows(
  store: Store,
  source: string,
  rows: ChartRow[],
  index: ChartIndex,
  day: string,
  collectedAt: string,
): Promise<{ written: number; linked: number }> {
  const values: unknown[][] = [];
  let linked = 0;
  let maxPosition = 0;
  for (const row of rows) {
    const artistId = matchChartArtist(row.appleArtistId, row.artistName, index);
    if (artistId !== null) linked += 1;
    if (row.position > maxPosition) maxPosition = row.position;
    values.push([
      source,
      CHART_TERRITORY,
      day,
      row.position,
      row.title,
      row.appleArtistId,
      row.artistName,
      artistId,
      collectedAt,
    ]);
  }
  const statements: Statement[] = [
    {
      sql: `DELETE FROM chart_entries
            WHERE chart_source = ? AND territory = ? AND chart_date = ? AND position > ?`,
      params: [source, CHART_TERRITORY, day, maxPosition],
    },
  ];
  const width = 9;
  const per = 8;
  for (let offset = 0; offset < values.length; offset += per) {
    const chunk = values.slice(offset, offset + per);
    const marks = chunk.map(() => "(" + new Array(width).fill("?").join(",") + ")").join(",");
    statements.push({
      sql: `INSERT INTO chart_entries (
              chart_source, territory, chart_date, position, track_or_album_name,
              apple_artist_id, artist_name, artist_id, collected_at
            ) VALUES ${marks}
            ON CONFLICT(chart_source, territory, chart_date, position) DO UPDATE SET
              track_or_album_name = excluded.track_or_album_name,
              apple_artist_id = excluded.apple_artist_id,
              artist_name = excluded.artist_name,
              artist_id = excluded.artist_id,
              collected_at = excluded.collected_at`,
      params: chunk.flat(),
    });
  }
  await store.batch(statements);
  return { written: values.length, linked };
}

function shouldFetch(
  row: { collected_at?: string | null; next_try_at?: string | null } | null,
  now: Date,
  refreshMs: number,
): boolean {
  if (refreshMs <= 0) return true;
  const retryAt = row?.next_try_at ? Date.parse(row.next_try_at) : NaN;
  if (!Number.isNaN(retryAt) && retryAt > now.getTime()) return false;
  return !isFresh(row?.collected_at, now, refreshMs);
}

async function noteChartAttempt(store: Store, source: string, day: string, now: Date, ok: boolean, collectedAt: string): Promise<void> {
  if (ok) {
    await store.batch([
      {
        sql: `INSERT INTO chart_freshness (chart_source, territory, chart_date, collected_at, next_try_at)
              VALUES (?, ?, ?, ?, NULL)
              ON CONFLICT(chart_source, territory, chart_date) DO UPDATE SET
                collected_at = excluded.collected_at,
                next_try_at = NULL`,
        params: [source, CHART_TERRITORY, day, collectedAt],
      },
    ]);
    return;
  }
  const next = new Date(now.getTime() + CHART_RETRY_MS).toISOString().replace(/\.\d{3}Z$/, "Z");
  await store.batch([
    {
      sql: `INSERT INTO chart_freshness (chart_source, territory, chart_date, collected_at, next_try_at)
            VALUES (?, ?, ?, NULL, ?)
            ON CONFLICT(chart_source, territory, chart_date) DO UPDATE SET
              next_try_at = excluded.next_try_at`,
      params: [source, CHART_TERRITORY, day, next],
    },
  ]);
}

export async function ingestItunesCharts(store: Store, now = new Date(), refreshMs = CHART_REFRESH_MS): Promise<ChartIngestResult> {
  const day = chartDay(now);
  const result: ChartIngestResult = { status: "fresh", written: 0, linked: 0, errors: [] };
  const due = [];
  for (const chart of CHART_SOURCES) {
    const row = await store.first<{ collected_at: string | null; next_try_at: string | null }>(
      `SELECT collected_at, next_try_at FROM chart_freshness
       WHERE chart_source = ? AND territory = ? AND chart_date = ?`,
      [chart.source, CHART_TERRITORY, day],
    );
    if (shouldFetch(row, now, refreshMs)) due.push(chart);
  }
  if (!due.length) return result;
  const parsed: { source: string; rows: ChartRow[] }[] = [];
  for (const chart of due) {
    let page: { status: number; text: string; truncated: boolean } | { error: string };
    try {
      page = await fetchChart(chart.url);
    } catch (error) {
      result.errors.push(`${chart.source}: ${error instanceof Error ? error.message : "fetch failed"}`);
      await noteChartAttempt(store, chart.source, day, now, false, "");
      continue;
    }
    if ("error" in page) {
      result.errors.push(`${chart.source}: ${page.error}`);
      await noteChartAttempt(store, chart.source, day, now, false, "");
      continue;
    }
    if (page.truncated) {
      result.errors.push(`${chart.source}: response exceeded ${CHART_MAX_BYTES} bytes`);
      await noteChartAttempt(store, chart.source, day, now, false, "");
      continue;
    }
    if (page.status !== 200) {
      result.errors.push(`${chart.source}: HTTP ${page.status}`);
      await noteChartAttempt(store, chart.source, day, now, false, "");
      continue;
    }
    let payload: unknown;
    try {
      payload = JSON.parse(page.text);
    } catch {
      result.errors.push(`${chart.source}: response was not JSON`);
      await noteChartAttempt(store, chart.source, day, now, false, "");
      continue;
    }
    const rows = parseItunesRss(payload);
    if (!rows.length) {
      result.errors.push(`${chart.source}: feed had no chart rows`);
      await noteChartAttempt(store, chart.source, day, now, false, "");
      continue;
    }
    parsed.push({ source: chart.source, rows });
  }
  const collectedAt = utcNow(now);
  if (parsed.length) {
    const artists = await store.all<{ id: number; name: string; apple_id: string | null }>(
      "SELECT id, name, apple_id FROM artists WHERE tracked = 1",
    );
    const index = indexArtists(artists.map((row) => ({ id: Number(row.id), name: String(row.name || ""), appleId: row.apple_id })));
    for (const chart of parsed) {
      try {
        const written = await writeRows(store, chart.source, chart.rows, index, day, collectedAt);
        result.written += written.written;
        result.linked += written.linked;
        await noteChartAttempt(store, chart.source, day, now, true, collectedAt);
      } catch (error) {
        result.errors.push(`${chart.source}: ${error instanceof Error ? error.message : "write failed"}`);
        await noteChartAttempt(store, chart.source, day, now, false, "");
      }
    }
  }
  if (result.errors.length && result.written) result.status = "partial";
  else if (result.errors.length) result.status = "error";
  else if (parsed.length) result.status = "stored";
  else result.status = "fresh";
  if (result.errors.length) {
    console.log(JSON.stringify({ message: "chart ingest", status: result.status, written: result.written, errors: result.errors }));
  }
  return result;
}
