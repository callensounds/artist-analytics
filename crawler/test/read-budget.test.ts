import { DatabaseSync } from "node:sqlite";
import { describe, expect, it } from "vitest";
import { ALERT_SERIES_SQL, BACKFILL_SQL } from "../src/alerts";
import { LATEST_MIGRATION, migrate, type Store } from "../src/store";

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

function plan(db: DatabaseSync, sql: string, params: unknown[] = []): string {
  return db
    .prepare("EXPLAIN QUERY PLAN " + sql)
    .all(...params)
    .map((row) => String((row as { detail?: string }).detail || ""))
    .join(" ");
}

describe("read budget", () => {
  it("keeps alert selection and series reads off a full snapshot scan", async () => {
    const db = new DatabaseSync(":memory:");
    const store = sqliteStore(db);
    await migrate(store);
    const versionQueries: string[] = [];
    const wrapped: Store = {
      async all(sql, params) {
        if (sql.includes("schema_migrations")) versionQueries.push(sql);
        return store.all(sql, params);
      },
      async first(sql, params) {
        if (sql.includes("schema_migrations")) versionQueries.push(sql);
        return store.first(sql, params);
      },
      batch: (statements) => store.batch(statements),
    };
    await migrate(wrapped);
    expect(versionQueries).toEqual([`SELECT version FROM schema_migrations WHERE version = ?`]);
    const again = sqliteStore(db);
    const recorded = await again.first<{ version: string }>(
      "SELECT version FROM schema_migrations WHERE version = ?",
      [LATEST_MIGRATION],
    );
    expect(recorded?.version).toBe(LATEST_MIGRATION);

    db.prepare(
      "INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at) VALUES (1, 'spotify', 'Followers', 10, '10', '2026-11-01T00:00:00Z')",
    ).run();
    const backfill = plan(db, BACKFILL_SQL, [5]);
    expect(backfill).toContain("audience_artists");
    expect(backfill).not.toContain("metric_snapshots");
    const series = plan(db, ALERT_SERIES_SQL, [1, "2026-11-01T00:00:00Z", 1, "2026-11-01T00:00:00Z", 1, "2026-11-01T00:00:00Z", 1, "2026-11-01T00:00:00Z", 1, "2026-11-01T00:00:00Z", 2000]);
    expect(series).toContain("idx_snapshots_lookup");
    expect(series).not.toContain("SCAN metric_snapshots");
    const fresh = plan(db, "SELECT source, captured_at FROM source_freshness WHERE artist_id = ?", [1]);
    expect(fresh).toContain("source_freshness");
    expect(fresh).not.toContain("metric_snapshots");
    const latest = plan(db, "SELECT metric, value, captured_at FROM metric_latest WHERE artist_id = ? AND source = ?", [1, "spotify"]);
    expect(latest).toContain("metric_latest");
    expect(latest).not.toContain("SCAN");
  });

  it("adds the read indexes on a database that already has 0015 without replaying the snapshot backfill", async () => {
    const db = new DatabaseSync(":memory:");
    const store = sqliteStore(db);
    await migrate(store);
    for (const name of [
      "idx_chart_entries_title_date",
      "idx_metric_alerts_detected",
      "idx_metric_latest_value",
      "idx_playlist_artist_first",
    ]) {
      db.exec(`DROP INDEX ${name}`);
    }
    db.prepare("DELETE FROM schema_migrations WHERE version = ?").run("0016_read_indexes.sql");
    const seen: string[] = [];
    const wrapped: Store = {
      async all(sql, params) {
        return store.all(sql, params);
      },
      async first(sql, params) {
        return store.first(sql, params);
      },
      async batch(statements) {
        for (const statement of statements) seen.push(statement.sql);
        return store.batch(statements);
      },
    };
    await migrate(wrapped);
    const names = (db.prepare("SELECT name FROM sqlite_master WHERE type = 'index'").all() as { name: string }[]).map(
      (row) => row.name,
    );
    expect(names).toEqual(
      expect.arrayContaining([
        "idx_chart_entries_title_date",
        "idx_metric_alerts_detected",
        "idx_metric_latest_value",
        "idx_playlist_artist_first",
      ]),
    );
    expect(seen.some((sql) => sql.includes("INSERT INTO metric_latest"))).toBe(false);
    expect(seen.some((sql) => sql.includes("metric_snapshots"))).toBe(false);
    expect(db.prepare("SELECT version FROM schema_migrations WHERE version = ?").get("0016_read_indexes.sql")).toBeTruthy();
  });
});
