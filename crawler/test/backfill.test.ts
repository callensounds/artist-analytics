import { DatabaseSync } from "node:sqlite";
import { describe, expect, it } from "vitest";
import { backfillAlerts } from "../src/alerts";
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

function snapshot(db: DatabaseSync, artistId: number, source: string, metric: string, value: number | null, at: string) {
  db.prepare(
    "INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at) VALUES (?, ?, ?, ?, ?, ?)",
  ).run(artistId, source, metric, value, value === null ? null : String(value), at);
}

describe("alert backfill selection", () => {
  it("checks artists with exact snapshots once and then does nothing", async () => {
    const db = new DatabaseSync(":memory:");
    const store = sqliteStore(db);
    await migrate(store);
    snapshot(db, 1, "spotify", "Followers", 1000, "2026-11-06T00:00:00Z");
    snapshot(db, 1, "spotify", "Followers", 1020, "2026-11-06T11:00:00Z");
    snapshot(db, 2, "apple", "Albums returned", 4, "2026-11-06T11:00:00Z");
    snapshot(db, 3, "instagram", "Followers", null, "2026-11-06T11:00:00Z");
    snapshot(db, 4, "tiktok", "Followers", 10, "2026-11-06T11:00:00Z");
    const now = new Date("2026-11-06T12:00:00Z");

    const first = await backfillAlerts(store, now, { artists: 10, alerts: 50 });
    expect(first.artists).toBe(2);
    expect(first.alerts).toBe(1);
    const checked = db.prepare("SELECT artist_id FROM alert_backfill ORDER BY artist_id").all() as { artist_id: number }[];
    expect(checked.map((row) => row.artist_id)).toEqual([1, 4]);
    expect(db.prepare("SELECT COUNT(*) AS n FROM metric_alerts").get()).toEqual({ n: 1 });

    const second = await backfillAlerts(store, now, { artists: 10, alerts: 50 });
    expect(second).toEqual({ artists: 0, alerts: 0 });
    expect(db.prepare("SELECT COUNT(*) AS n FROM metric_alerts").get()).toEqual({ n: 1 });
  });

  it("stops at the artist cap", async () => {
    const db = new DatabaseSync(":memory:");
    const store = sqliteStore(db);
    await migrate(store);
    snapshot(db, 1, "tiktok", "Followers", 10, "2026-11-06T11:00:00Z");
    snapshot(db, 2, "tiktok", "Followers", 10, "2026-11-06T11:00:00Z");
    snapshot(db, 3, "tiktok", "Followers", 10, "2026-11-06T11:00:00Z");
    const result = await backfillAlerts(store, new Date("2026-11-06T12:00:00Z"), { artists: 2, alerts: 50 });
    expect(result.artists).toBe(2);
    expect(result.alerts).toBe(0);
    const checked = db.prepare("SELECT artist_id FROM alert_backfill ORDER BY artist_id").all() as { artist_id: number }[];
    expect(checked.map((row) => row.artist_id)).toEqual([1, 2]);
  });
});
