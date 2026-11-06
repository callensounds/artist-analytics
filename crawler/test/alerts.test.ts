import { DatabaseSync } from "node:sqlite";
import { describe, expect, it } from "vitest";
import { evaluateAlerts, percentTenths, recordAlerts, windowDelta } from "../src/alerts";
import { type Store, migrate } from "../src/store";

function sqliteStore(db: DatabaseSync): Store {
  return {
    async all(sql, params = []) {
      return db.prepare(sql).all(...params) as Record<string, unknown>[];
    },
    async first(sql, params = []) {
      return (db.prepare(sql).get(...params) as Record<string, unknown> | undefined) ?? null;
    },
    async batch(statements) {
      db.exec("BEGIN");
      try {
        for (const statement of statements) db.prepare(statement.sql).run(...statement.params);
        db.exec("COMMIT");
      } catch (error) {
        db.exec("ROLLBACK");
        throw error;
      }
    },
  };
}

const now = new Date("2026-11-06T12:00:00Z");

describe("growth alert rules", () => {
  it("matches Momentum percent rounding and ignores a single point", () => {
    expect(percentTenths(20, 1000)).toBe(20);
    expect(percentTenths(1, 3)).toBe(333);
    expect(percentTenths(10, 0)).toBeNull();
    expect(windowDelta([{ at: "2026-11-06T10:00:00Z", value: 1000 }], now.getTime(), 24 * 3600 * 1000)).toBeNull();
  });

  it("alerts on a 2% 24h move, a large absolute move, and not on a gap peak", () => {
    const twoPercent = evaluateAlerts(
      [
        { source: "spotify", metric: "Followers", value: 1000, captured_at: "2026-11-06T00:00:00Z" },
        { source: "spotify", metric: "Followers", value: null, captured_at: "2026-11-06T06:00:00Z" },
        { source: "spotify", metric: "Followers", value: "1.2M", captured_at: "2026-11-06T07:00:00Z" },
        { source: "spotify", metric: "Followers", value: 1020, captured_at: "2026-11-06T11:00:00Z" },
      ],
      now,
    );
    expect(twoPercent.map((alert) => alert.window)).toEqual(["24h"]);
    expect(twoPercent[0]).toMatchObject({ delta: 20, deltaPct: 20, baselineValue: 1000, latestValue: 1020 });
    const week = evaluateAlerts(
      [
        { source: "instagram", metric: "Followers", value: 1000, captured_at: "2026-11-03T12:00:00Z" },
        { source: "instagram", metric: "Followers", value: 1060, captured_at: "2026-11-06T11:00:00Z" },
      ],
      now,
    );
    expect(week.map((alert) => alert.window)).toEqual(["7d"]);
    expect(week[0].deltaPct).toBe(60);

    const onePercent = evaluateAlerts(
      [
        { source: "spotify", metric: "Followers", value: 1000, captured_at: "2026-11-06T00:00:00Z" },
        { source: "spotify", metric: "Followers", value: 1010, captured_at: "2026-11-06T11:00:00Z" },
      ],
      now,
    );
    expect(onePercent).toEqual([]);

    const absolute = evaluateAlerts(
      [
        { source: "spotify", metric: "Monthly listeners", value: 5_000_000, captured_at: "2026-11-06T00:00:00Z" },
        { source: "spotify", metric: "Monthly listeners", value: 5_060_000, captured_at: "2026-11-06T11:00:00Z" },
      ],
      now,
    );
    expect(absolute[0]).toMatchObject({ delta: 60_000, window: "24h" });
    expect(Math.abs(absolute[0].deltaPct ?? 0)).toBeLessThan(20);

    const peak = evaluateAlerts(
      [
        { source: "instagram", metric: "Followers", value: 10_000, captured_at: "2026-11-06T00:00:00Z" },
        { source: "instagram", metric: "Followers", value: 80_000, captured_at: "2026-11-06T06:00:00Z" },
        { source: "instagram", metric: "Followers", value: 10_100, captured_at: "2026-11-06T11:00:00Z" },
      ],
      now,
    );
    expect(peak).toEqual([]);
  });

  it("inserts an alert once for the same two readings", async () => {
    const db = new DatabaseSync(":memory:");
    const store = sqliteStore(db);
    await migrate(store);
    db.prepare(
      "INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at) VALUES (?, 'spotify', 'Followers', ?, ?, ?)",
    ).run(1, 1000, "1,000", "2026-11-06T00:00:00Z");
    db.prepare(
      "INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at) VALUES (?, 'spotify', 'Followers', ?, ?, ?)",
    ).run(1, 1020, "1,020", "2026-11-06T11:00:00Z");
    await recordAlerts(store, 1, now);
    await recordAlerts(store, 1, now);
    const rows = db.prepare("SELECT window, delta FROM metric_alerts").all();
    expect(rows).toEqual([{ window: "24h", delta: 20 }]);
  });
});
