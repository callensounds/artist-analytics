/**
 * Growth alerts from stored exact counts. Same window rule as Momentum:
 * latest value minus the earliest value inside 24h or 7d. One reading is not an alert.
 *
 * Percent is integer tenths (20 = 2.0%), matching the Python rounding.
 * A window alerts when that percent crosses the threshold, or the absolute
 * change is large. Baseline 0 has no percent, so only the absolute rule can fire.
 *
 * Thresholds:
 * - Spotify monthly listeners: 2% in 24h or 50,000; 5% in 7d or 100,000
 * - Spotify, Instagram, and TikTok followers: 2% in 24h or 5,000; 5% in 7d or 20,000
 * - TikTok likes: 2% in 24h or 10,000; 5% in 7d or 50,000
 */

import type { Store } from "./store";
import { utcNow } from "./store";

const WINDOW_24H_MS = 24 * 60 * 60 * 1000;
const WINDOW_7D_MS = 7 * 24 * 60 * 60 * 1000;
export const ALERT_WRITE_CAP = 4;
/** Artists checked on one cron tick. Each one reads only its own recent snapshots. */
export const BACKFILL_ARTISTS_PER_TICK = 5;
/** Alerts inserted on one cron tick, across that artist batch. */
export const BACKFILL_ALERTS_PER_TICK = 50;

// audience_artists is one row per artist who has an exact alert metric.
// The old form scanned every matching metric_snapshots row on each cron tick.
export const BACKFILL_SQL = `
SELECT audience_artists.artist_id AS artist_id
FROM audience_artists
WHERE NOT EXISTS (
  SELECT 1 FROM alert_backfill WHERE alert_backfill.artist_id = audience_artists.artist_id
)
ORDER BY audience_artists.artist_id
LIMIT ?
`;

export const ALERT_SERIES_SQL = `
SELECT source, metric, value, captured_at, id FROM metric_snapshots
WHERE artist_id = ? AND source = 'spotify' AND metric = 'Monthly listeners' AND captured_at >= ? AND value IS NOT NULL
UNION ALL
SELECT source, metric, value, captured_at, id FROM metric_snapshots
WHERE artist_id = ? AND source = 'spotify' AND metric = 'Followers' AND captured_at >= ? AND value IS NOT NULL
UNION ALL
SELECT source, metric, value, captured_at, id FROM metric_snapshots
WHERE artist_id = ? AND source = 'instagram' AND metric = 'Followers' AND captured_at >= ? AND value IS NOT NULL
UNION ALL
SELECT source, metric, value, captured_at, id FROM metric_snapshots
WHERE artist_id = ? AND source = 'tiktok' AND metric = 'Followers' AND captured_at >= ? AND value IS NOT NULL
UNION ALL
SELECT source, metric, value, captured_at, id FROM metric_snapshots
WHERE artist_id = ? AND source = 'tiktok' AND metric = 'Likes' AND captured_at >= ? AND value IS NOT NULL
ORDER BY captured_at ASC, id ASC
LIMIT ?
`;

const SERIES = [
  ["spotify", "Monthly listeners"],
  ["spotify", "Followers"],
  ["instagram", "Followers"],
  ["tiktok", "Followers"],
  ["tiktok", "Likes"],
] as const;

type Rule = { pct24: number; abs24: number; pct7: number; abs7: number };

const RULES: Record<string, Rule> = {
  "spotify\0Monthly listeners": { pct24: 20, abs24: 50_000, pct7: 50, abs7: 100_000 },
  "spotify\0Followers": { pct24: 20, abs24: 5_000, pct7: 50, abs7: 20_000 },
  "instagram\0Followers": { pct24: 20, abs24: 5_000, pct7: 50, abs7: 20_000 },
  "tiktok\0Followers": { pct24: 20, abs24: 5_000, pct7: 50, abs7: 20_000 },
  "tiktok\0Likes": { pct24: 20, abs24: 10_000, pct7: 50, abs7: 50_000 },
};

export interface AlertPoint {
  at: string;
  value: number;
}

export interface AlertCandidate {
  provider: string;
  metric: string;
  window: "24h" | "7d";
  delta: number;
  deltaPct: number | null;
  baselineValue: number;
  latestValue: number;
  baselineAt: string;
  latestAt: string;
  dedupeKey: string;
}

export function percentTenths(change: number, baseline: number): number | null {
  if (!Number.isInteger(baseline) || baseline <= 0) return null;
  if (!Number.isInteger(change)) return null;
  const numerator = change * 1000;
  if (numerator >= 0) return Math.floor((numerator + Math.floor(baseline / 2)) / baseline);
  return -Math.floor((-numerator + Math.floor(baseline / 2)) / baseline);
}

function whole(value: unknown): number | null {
  if (typeof value === "number" && Number.isInteger(value) && value >= 0) return value;
  if (typeof value === "string" && /^[\d,]+$/.test(value.trim())) return Number(value.replace(/,/g, ""));
  return null;
}

export function windowDelta(
  points: AlertPoint[],
  nowMs: number,
  windowMs: number,
): { delta: number; baseline: number; latest: number; baselineAt: string; latestAt: string } | null {
  const start = nowMs - windowMs;
  const byTime = new Map<number, { at: string; value: number }>();
  for (const point of points) {
    const value = whole(point.value);
    const atMs = Date.parse(point.at);
    if (value === null || Number.isNaN(atMs) || atMs < start || atMs > nowMs) continue;
    byTime.set(atMs, { at: point.at, value });
  }
  if (byTime.size < 2) return null;
  const times = [...byTime.keys()].sort((left, right) => left - right);
  const first = byTime.get(times[0])!;
  const last = byTime.get(times[times.length - 1])!;
  return {
    delta: last.value - first.value,
    baseline: first.value,
    latest: last.value,
    baselineAt: first.at,
    latestAt: last.at,
  };
}

function crosses(rule: Rule, window: "24h" | "7d", delta: number, tenths: number | null): boolean {
  const pct = window === "24h" ? rule.pct24 : rule.pct7;
  const abs = window === "24h" ? rule.abs24 : rule.abs7;
  if (Math.abs(delta) >= abs) return true;
  return tenths !== null && Math.abs(tenths) >= pct;
}

export function evaluateAlerts(
  rows: { source: string; metric: string; value: unknown; captured_at: string }[],
  now: Date,
): AlertCandidate[] {
  const grouped = new Map<string, AlertPoint[]>();
  for (const row of rows) {
    const key = `${row.source}\0${row.metric}`;
    if (!RULES[key]) continue;
    const value = whole(row.value);
    if (value === null || !row.captured_at) continue;
    const list = grouped.get(key) ?? [];
    list.push({ at: row.captured_at, value });
    grouped.set(key, list);
  }
  const found: AlertCandidate[] = [];
  const nowMs = now.getTime();
  for (const [provider, metric] of SERIES) {
    const key = `${provider}\0${metric}`;
    const points = grouped.get(key) ?? [];
    const rule = RULES[key];
    for (const [window, windowMs] of [["24h", WINDOW_24H_MS], ["7d", WINDOW_7D_MS]] as const) {
      const delta = windowDelta(points, nowMs, windowMs);
      if (!delta) continue;
      const tenths = percentTenths(delta.delta, delta.baseline);
      if (!crosses(rule, window, delta.delta, tenths)) continue;
      found.push({
        provider,
        metric,
        window,
        delta: delta.delta,
        deltaPct: tenths,
        baselineValue: delta.baseline,
        latestValue: delta.latest,
        baselineAt: delta.baselineAt,
        latestAt: delta.latestAt,
        dedupeKey: `${provider}|${metric}|${window}|${delta.baselineAt}|${delta.latestAt}`,
      });
    }
  }
  found.sort((left, right) => Math.abs(right.deltaPct ?? 0) - Math.abs(left.deltaPct ?? 0) || Math.abs(right.delta) - Math.abs(left.delta));
  return found.slice(0, ALERT_WRITE_CAP);
}

export async function recordAlerts(
  store: Store,
  artistId: number,
  now = new Date(),
  maxAlerts = ALERT_WRITE_CAP,
): Promise<{ written: number; complete: boolean }> {
  const cutoff = new Date(now.getTime() - WINDOW_7D_MS).toISOString().replace(/\.\d{3}Z$/, "Z");
  const rows = await store.all<{ source: string; metric: string; value: unknown; captured_at: string }>(
    ALERT_SERIES_SQL,
    [artistId, cutoff, artistId, cutoff, artistId, cutoff, artistId, cutoff, artistId, cutoff, 2000],
  );
  const pending = evaluateAlerts(rows, now);
  const alerts = pending.slice(0, Math.max(0, maxAlerts));
  if (alerts.length) {
    const detected = utcNow(now);
    await store.batch(
      alerts.map((alert) => ({
        sql: `INSERT INTO metric_alerts (
                artist_id, provider, metric, window, delta, delta_pct,
                baseline_value, latest_value, baseline_at, latest_at, detected_at, dedupe_key
              ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
              ON CONFLICT(dedupe_key) DO NOTHING`,
        params: [
          artistId,
          alert.provider,
          alert.metric,
          alert.window,
          alert.delta,
          alert.deltaPct,
          alert.baselineValue,
          alert.latestValue,
          alert.baselineAt,
          alert.latestAt,
          detected,
          `${artistId}|${alert.dedupeKey}`,
        ],
      })),
    );
  }
  return { written: alerts.length, complete: alerts.length === pending.length };
}

export async function markAlertChecked(store: Store, artistId: number, now = new Date()): Promise<void> {
  await store.batch([
    {
      sql: `INSERT INTO alert_backfill (artist_id, checked_at) VALUES (?, ?)
            ON CONFLICT(artist_id) DO NOTHING`,
      params: [artistId, utcNow(now)],
    },
  ]);
}

async function backfillFinished(store: Store): Promise<boolean> {
  const row = await store.first<{ alert_backfill_done: number | null }>(
    "SELECT alert_backfill_done FROM crawl_state WHERE id = 1",
  );
  return Number(row?.alert_backfill_done || 0) === 1;
}

async function setBackfillDone(store: Store, now: Date, done: boolean): Promise<void> {
  await store.batch([
    {
      sql: `INSERT INTO crawl_state (id, next_artist_id, updated_at, alert_backfill_done)
            VALUES (1, 0, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              alert_backfill_done = excluded.alert_backfill_done,
              updated_at = excluded.updated_at`,
      params: [utcNow(now), done ? 1 : 0],
    },
  ]);
}

export async function clearBackfillDone(store: Store, now = new Date()): Promise<void> {
  await setBackfillDone(store, now, false);
}

export async function backfillAlerts(
  store: Store,
  now = new Date(),
  limits: { artists?: number; alerts?: number } = {},
): Promise<{ artists: number; alerts: number }> {
  const artistCap = Math.max(0, Math.min(limits.artists ?? BACKFILL_ARTISTS_PER_TICK, 20));
  const alertCap = Math.max(0, Math.min(limits.alerts ?? BACKFILL_ALERTS_PER_TICK, 50));
  if (!artistCap || !alertCap) return { artists: 0, alerts: 0 };
  if (await backfillFinished(store)) return { artists: 0, alerts: 0 };
  const pending = await store.all<{ artist_id: number }>(BACKFILL_SQL, [artistCap]);
  if (!pending.length) {
    await setBackfillDone(store, now, true);
    return { artists: 0, alerts: 0 };
  }
  let artists = 0;
  let alerts = 0;
  let finished = true;
  for (const row of pending) {
    if (alerts >= alertCap) {
      finished = false;
      break;
    }
    const artistId = Number(row.artist_id);
    const result = await recordAlerts(store, artistId, now, Math.min(ALERT_WRITE_CAP, alertCap - alerts));
    alerts += result.written;
    artists += 1;
    if (!result.complete) {
      finished = false;
      break;
    }
    await markAlertChecked(store, artistId, now);
  }
  if (finished && pending.length < artistCap) await setBackfillDone(store, now, true);
  return { artists, alerts };
}
