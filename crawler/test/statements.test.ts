import { DatabaseSync } from "node:sqlite";
import { describe, expect, it, vi } from "vitest";
import { handleMessage, runQueue } from "../src/crawl";
import {
  D1_MAX_PARAMS,
  type Statement,
  type Store,
  chunkedInsert,
  commitWrites,
  migrate,
  saveMusicBrainzReleases,
  saveSource,
} from "../src/store";

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

function assertBudget(statements: Statement[]) {
  expect(statements.length).toBeGreaterThan(0);
  for (const statement of statements) {
    expect(statement.params.length).toBeLessThanOrEqual(D1_MAX_PARAMS);
    expect(statement.params.length).toBeLessThanOrEqual(100);
    expect((statement.sql.match(/\?/g) || []).length).toBe(statement.params.length);
  }
}

describe("chunked inserts", () => {
  it("keeps 50 track rows and 100 releases under 100 bound parameters", () => {
    const tracks = Array.from({ length: 50 }, (_, index) => [
      1,
      "deezer",
      String(index),
      `Track ${index}`,
      null,
      "Album",
      index + 1,
      "2026-11-11T00:00:00Z",
      "2026-11-11T00:00:00Z",
    ]);
    const trackSql = chunkedInsert(
      "INSERT INTO artist_tracks (a, b, c, d, e, f, g, h, i)",
      tracks,
      "ON CONFLICT(a) DO UPDATE SET n = ?",
      ["fixed"],
    );
    assertBudget(trackSql);
    expect(trackSql.length).toBeGreaterThan(1);
    expect(trackSql.every((statement) => statement.params.at(-1) === "fixed")).toBe(true);
    const trackParams = trackSql.reduce((sum, statement) => sum + statement.params.length - 1, 0);
    expect(trackParams).toBe(50 * 9);

    const releases = Array.from({ length: 100 }, (_, index) => [
      1,
      `mb-${index}`,
      `Release ${index}`,
      "album",
      "2020-01-01",
      "day",
      "2026-11-11T00:00:00Z",
      "2026-11-11T00:00:00Z",
    ]);
    const releaseSql = chunkedInsert(
      "INSERT INTO musicbrainz_releases (a, b, c, d, e, f, g, h)",
      releases,
    );
    assertBudget(releaseSql);
    expect(releaseSql.reduce((sum, statement) => sum + statement.params.length, 0)).toBe(100 * 8);
    expect(() => chunkedInsert("INSERT INTO big (a)", [new Array(101).fill(1)])).toThrow(/big/);
  });

  it("stores a full Deezer payload in one batch without crossing the parameter cap", async () => {
    const db = new DatabaseSync(":memory:");
    const seen: Statement[][] = [];
    const real = sqliteStore(db);
    const store: Store = {
      all: (sql, params) => real.all(sql, params),
      first: (sql, params) => real.first(sql, params),
      async batch(statements) {
        seen.push(statements);
        await real.batch(statements);
      },
    };
    await migrate(store);
    db.prepare(
      "INSERT INTO artists (name, normalized_name, origin, tracked, created_at, deezer_id) VALUES (?, ?, 'search', 1, ?, ?)",
    ).run("Taylor Swift", "taylorswift", "2026-11-11T00:00:00Z", "12246");
    const before = seen.length;
    const tracks = Array.from({ length: 50 }, (_, index) => ({
      externalId: String(3000 + index),
      name: `Top ${index + 1}`,
      popularity: null,
      album: "Album",
      rank: index + 1,
    }));
    const sightings = Array.from({ length: 100 }, (_, index) => ({
      title: `Release ${index + 1}`,
      url: `https://www.deezer.com/album/${4000 + index}`,
      kind: "album",
      sortKey: `2020-01-${String((index % 28) + 1).padStart(2, "0")}`,
      precision: "day",
    }));
    const related = Array.from({ length: 12 }, (_, index) => ({
      name: `Neighbor ${index + 1}`,
      spotify_id: `spot${index}`,
      rank: index + 1,
    }));
    await saveSource(
      store,
      { id: 1, name: "Taylor Swift" },
      {
        platform: "deezer",
        status: "ok",
        metrics: [
          { label: "Fans", exact: 5000000, value: "5,000,000" },
          { label: "Albums", exact: 40, value: "40" },
        ],
        tracks,
        sightings,
        related,
        deezerId: "12246",
        deezerAlbumCount: 40,
      },
      new Date("2026-11-11T01:00:00Z"),
      0,
    );
    const writes = seen.slice(before);
    expect(writes.length).toBe(1);
    assertBudget(writes[0]);
    const sightingStatements = writes[0].filter((statement) => statement.sql.includes("content_sightings"));
    expect(sightingStatements.length).toBeGreaterThan(1);
    expect(db.prepare("SELECT value FROM metric_snapshots WHERE source = 'deezer' AND metric = 'Fans'").get()).toEqual({
      value: 5000000,
    });
    expect(db.prepare("SELECT COUNT(*) AS n FROM content_sightings").get()).toEqual({ n: 25 });
    expect(db.prepare("SELECT COUNT(*) AS n FROM artist_tracks").get()).toEqual({ n: 10 });
    expect(db.prepare("SELECT COUNT(*) AS n FROM related_edges").get()).toEqual({ n: 12 });
  });

  it("writes 100 MusicBrainz releases in one batch", async () => {
    const db = new DatabaseSync(":memory:");
    const real = sqliteStore(db);
    let calls = 0;
    const store: Store = {
      all: (sql, params) => real.all(sql, params),
      first: (sql, params) => real.first(sql, params),
      async batch(statements) {
        calls += 1;
        assertBudget(statements);
        await real.batch(statements);
      },
    };
    await migrate(real);
    const releases = Array.from({ length: 100 }, (_, index) => ({
      mbid: `mb-${index}`,
      title: `Release ${index}`,
      releaseType: "Album",
      releasedAt: "2020-01-01",
      precision: "day",
    }));
    calls = 0;
    await saveMusicBrainzReleases(store, 4, releases, new Date("2026-11-11T00:00:00Z"), "Taylor Swift");
    expect(calls).toBe(1);
    expect(db.prepare("SELECT COUNT(*) AS n FROM musicbrainz_releases").get()).toEqual({ n: 100 });
  });

  it("names the artist and table when a write fails, without bound values", async () => {
    const db = new DatabaseSync(":memory:");
    const real = sqliteStore(db);
    const store: Store = {
      all: (sql, params) => real.all(sql, params),
      first: (sql, params) => real.first(sql, params),
      async batch() {
        throw new Error("D1_ERROR: too many SQL variables at offset 474: SQLITE_ERROR");
      },
    };
    const secret = "super-secret-param";
    await expect(
      commitWrites(
        store,
        [{ sql: "INSERT INTO content_sightings (title) VALUES (?)", params: [secret] }],
        "Taylor Swift",
      ),
    ).rejects.toThrow(/Taylor Swift \| content_sightings \(1 param\): D1_ERROR: too many SQL variables/);
    try {
      await commitWrites(
        store,
        [{ sql: "INSERT INTO content_sightings (title) VALUES (?)", params: [secret] }],
        "Taylor Swift",
      );
      throw new Error("expected the write to fail");
    } catch (error) {
      expect(String(error)).toContain("Taylor Swift | content_sightings");
      expect(String(error)).not.toContain(secret);
    }

    await migrate(real);
    db.prepare(
      "INSERT INTO artists (name, normalized_name, origin, tracked, created_at, deezer_id) VALUES ('Taylor Swift', 'taylorswift', 'search', 1, '2026-11-11T00:00:00Z', '12246')",
    ).run();
    const failing: Store = {
      all: (sql, params) => real.all(sql, params),
      first: (sql, params) => real.first(sql, params),
      async batch(statements) {
        if (statements.some((statement) => statement.sql.includes("content_sightings"))) {
          throw new Error("D1_ERROR: too many SQL variables at offset 474: SQLITE_ERROR");
        }
        await real.batch(statements);
      },
    };
    vi.stubGlobal("fetch", async (input: string | URL) => {
      const url = String(input);
      if (url.endsWith("/artist/12246")) {
        return new Response(JSON.stringify({
          id: 12246,
          name: "Taylor Swift",
          nb_fan: 5000000,
          nb_album: 2,
          link: "https://www.deezer.com/artist/12246",
        }), { status: 200 });
      }
      if (url.includes("/top")) {
        return new Response(JSON.stringify({ data: [{ id: 11, title: "Blank Space", album: { title: "1989" } }] }), { status: 200 });
      }
      if (url.includes("/albums")) {
        const data = Array.from({ length: 25 }, (_, index) => ({
          id: 5000 + index,
          title: `Release ${index + 1}`,
          release_date: "2014-10-27",
          record_type: "album",
        }));
        return new Response(JSON.stringify({ data }), { status: 200 });
      }
      return new Response("{}", { status: 404 });
    });
    const message = {
      body: { artist_id: 1, provider: "deezer" },
      ack() {},
      retry() {},
    };
    await runQueue([message], { DB: failing, CRAWL_QUEUE: { send() {} } });
    const detail = db.prepare("SELECT detail, status FROM crawl_runs ORDER BY id DESC LIMIT 1").get() as {
      detail: string;
      status: string;
    };
    expect(detail.status).toBe("error");
    expect(detail.detail).toContain("Taylor Swift");
    expect(detail.detail).toContain("content_sightings");
    expect(detail.detail).toContain("too many SQL variables");
    expect(detail.detail).not.toMatch(/secret|password|api[_-]?key|bearer/i);
  });

  it("crawls an artist with a full Deezer payload and stores fans", async () => {
    const db = new DatabaseSync(":memory:");
    const seen: Statement[][] = [];
    const real = sqliteStore(db);
    const store: Store = {
      all: (sql, params) => real.all(sql, params),
      first: (sql, params) => real.first(sql, params),
      async batch(statements) {
        seen.push(statements.map((statement) => ({ ...statement, params: [...statement.params] })));
        await real.batch(statements);
      },
    };
    await migrate(store);
    db.prepare(
      "INSERT INTO artists (name, normalized_name, origin, tracked, created_at, deezer_id) VALUES ('Clairo', 'clairo', 'search', 1, '2026-11-11T00:00:00Z', '27')",
    ).run();
    vi.stubGlobal("fetch", async (input: string | URL) => {
      const url = String(input);
      if (url.endsWith("/artist/27")) {
        return new Response(JSON.stringify({
          id: 27,
          name: "Clairo",
          nb_fan: 4000,
          nb_album: 3,
          link: "https://www.deezer.com/artist/27",
        }), { status: 200 });
      }
      if (url.includes("/top")) {
        const data = Array.from({ length: 10 }, (_, index) => ({
          id: 7000 + index,
          title: `Track ${index + 1}`,
          album: { title: "Sling" },
        }));
        return new Response(JSON.stringify({ data }), { status: 200 });
      }
      if (url.includes("/albums")) {
        const data = Array.from({ length: 25 }, (_, index) => ({
          id: 8000 + index,
          title: `Release ${index + 1}`,
          release_date: `2019-06-${String((index % 28) + 1).padStart(2, "0")}`,
          record_type: "album",
        }));
        return new Response(JSON.stringify({ data }), { status: 200 });
      }
      return new Response("{}", { status: 404 });
    });
    const result = await handleMessage(
      { DB: store, CRAWL_QUEUE: { send() {} } },
      { artist_id: 1, provider: "deezer" },
    );
    expect(result.ok).toBe(true);
    expect(result.detail).toContain("Clairo");
    for (const batch of seen) assertBudget(batch);
    const fans = db.prepare("SELECT value FROM metric_snapshots WHERE source = 'deezer' AND metric = 'Fans'").get();
    expect(fans).toEqual({ value: 4000 });
    expect(db.prepare("SELECT COUNT(*) AS n FROM content_sightings WHERE source = 'deezer'").get()).toEqual({ n: 25 });
    expect(db.prepare("SELECT COUNT(*) AS n FROM artist_tracks WHERE source = 'deezer'").get()).toEqual({ n: 10 });
    const run = db.prepare("SELECT status FROM crawl_runs ORDER BY id DESC LIMIT 1").get() as { status: string };
    expect(run.status).toBe("ok");
  });
});
