import { DatabaseSync } from "node:sqlite";
import { describe, expect, it, vi } from "vitest";
import { handleMessage, resetMusicBrainzPace, runQueue } from "../src/crawl";
import { parseReleaseGroups, releaseDate } from "../src/releases";
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

describe("MusicBrainz release dates", () => {
  it("keeps year, month, and day and drops undated or unofficial groups", () => {
    expect(releaseDate("2020-03-15")).toEqual({ releasedAt: "2020-03-15", precision: "day" });
    expect(releaseDate("2020-03")).toEqual({ releasedAt: "2020-03", precision: "month" });
    expect(releaseDate("2020")).toEqual({ releasedAt: "2020", precision: "year" });
    expect(releaseDate("2020-03-15T00:00:00Z")).toBeNull();
    const groups = Array.from({ length: 60 }, (_, index) => ({
      id: `id-${index}`,
      title: `Album ${index}`,
      "primary-type": "Album",
      "first-release-date": "2020",
    }));
    const parsed = parseReleaseGroups({
      "release-groups": [
        { id: "day", title: "Sling", "primary-type": "Album", "first-release-date": "2021-06-16" },
        { id: "month", title: "Soft Sounds", "primary-type": "EP", "first-release-date": "2017-11" },
        { id: "year", title: "Immunity", "primary-type": "Single", "first-release-date": "2014" },
        { id: "boot", title: "Live", "primary-type": "Album", "secondary-types": ["Live", "Bootleg"], "first-release-date": "2019-01-01" },
        { id: "none", title: "Untitled", "primary-type": "Album", "first-release-date": "" },
        { id: "other", title: "Interview", "primary-type": "Other", "first-release-date": "2018-01-01" },
        ...groups,
      ],
    });
    expect(parsed.slice(0, 3)).toEqual([
      { mbid: "day", title: "Sling", releaseType: "Album", releasedAt: "2021-06-16", precision: "day" },
      { mbid: "month", title: "Soft Sounds", releaseType: "EP", releasedAt: "2017-11", precision: "month" },
      { mbid: "year", title: "Immunity", releaseType: "Single", releasedAt: "2014", precision: "year" },
    ]);
    expect(parsed).toHaveLength(50);
    expect(parsed.some((row) => row.mbid === "boot" || row.mbid === "none" || row.releasedAt.includes("T"))).toBe(false);
  });

  it("stores release groups after an identity lookup and retries a 503 without inventing a date", async () => {
    resetMusicBrainzPace();
    const timer = vi.spyOn(globalThis, "setTimeout").mockImplementation(((fn: TimerHandler) => {
      if (typeof fn === "function") fn();
      return 0 as unknown as ReturnType<typeof setTimeout>;
    }) as typeof setTimeout);
    const db = new DatabaseSync(":memory:");
    const store = sqliteStore(db);
    await migrate(store);
    db.prepare("INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Clairo', 'clairo', 'search', 1, '2020-01-01T00:00:00Z')").run();
    const calls: string[] = [];
    vi.stubGlobal("fetch", async (input: string | URL) => {
      const url = String(input);
      calls.push(url);
      if (url.includes("release-group")) {
        return new Response(JSON.stringify({
          "release-groups": [
            { id: "rg-sling", title: "Sling", "primary-type": "Album", "first-release-date": "2021-06-16" },
          ],
        }), { status: 200 });
      }
      return new Response(JSON.stringify({
        artists: [{ id: "mb-clairo", name: "Clairo", score: 100, relations: [] }],
      }), { status: 200 });
    });
    const sent: { body: unknown; delaySeconds?: number }[] = [];
    const env = {
      DB: store,
      CRAWL_QUEUE: {
        send(body: unknown, options?: { delaySeconds?: number }) {
          sent.push({ body, delaySeconds: options?.delaySeconds });
        },
      },
    };
    try {
      await handleMessage(env, { artist_id: 1, provider: "musicbrainz" });
      const stored = db.prepare("SELECT title, released_at, precision FROM musicbrainz_releases").get();
      expect(stored).toEqual({ title: "Sling", released_at: "2021-06-16", precision: "day" });
      expect(calls.some((url) => url.includes("release-group"))).toBe(true);

      db.prepare("DELETE FROM musicbrainz_releases").run();
      db.prepare("UPDATE artists SET last_crawled_at = NULL WHERE id = 1").run();
      vi.stubGlobal("fetch", async (input: string | URL) => {
        const url = String(input);
        if (url.includes("release-group")) return new Response("busy", { status: 503, headers: { "retry-after": "250" } });
        return new Response(JSON.stringify({
          id: "mb-clairo",
          name: "Clairo",
          relations: [],
        }), { status: 200 });
      });
      const message = {
        body: { artist_id: 1, provider: "musicbrainz", mbid: "mb-clairo" },
        acked: false,
        retried: false,
        delay: undefined as number | undefined,
        ack() { this.acked = true; },
        retry(options?: { delaySeconds?: number }) {
          this.retried = true;
          this.delay = options?.delaySeconds;
        },
      };
      await runQueue([message], env);
      expect(message.acked).toBe(true);
      expect(message.retried).toBe(false);
      expect(sent).toEqual([{
        body: {
          artist_id: 1,
          mbid: "mb-clairo",
          providers: ["musicbrainz", "spotify", "instagram", "tiktok", "apple", "deezer", "deezer_playlists"],
        },
        delaySeconds: 250,
      }]);
      expect(db.prepare("SELECT COUNT(*) AS n FROM musicbrainz_releases").get()).toEqual({ n: 0 });
    } finally {
      timer.mockRestore();
    }
  });
});
