import { DatabaseSync } from "node:sqlite";
import { describe, expect, it, vi } from "vitest";
import {
  CHART_SOURCES,
  type ChartRow,
  indexArtists,
  ingestItunesCharts,
  matchChartArtist,
  parseItunesRss,
} from "../src/charts";
import { runScheduled } from "../src/crawl";
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

function entry(title: string, artist: string, href?: string, appleId?: string) {
  const attributes: Record<string, string> = {};
  if (href) attributes.href = href;
  if (appleId) attributes["im:id"] = appleId;
  return {
    "im:name": { label: title },
    "im:artist": { label: artist, attributes },
  };
}

describe("iTunes RSS parse and match", () => {
  it("reads position, title, and the artist id from the href or im:id", () => {
    const parsed = parseItunesRss({
      feed: {
        entry: [
          entry("Choosin' Texas", "Ella Langley", "https://music.apple.com/us/artist/ella-langley/1384373733?uo=2"),
          { "im:name": { label: "Untitled" } },
          entry("O My Beloved", "Kings of Leon", "https://music.apple.com/us/artist/1883403"),
          entry("Explicit id", "NEEDTOBREATHE", "https://music.apple.com/us/artist/someone/1", "113247214"),
        ],
      },
    });
    expect(parsed).toEqual([
      { position: 1, title: "Choosin' Texas", artistName: "Ella Langley", appleArtistId: "1384373733" },
      { position: 3, title: "O My Beloved", artistName: "Kings of Leon", appleArtistId: "1883403" },
      { position: 4, title: "Explicit id", artistName: "NEEDTOBREATHE", appleArtistId: "113247214" },
    ] satisfies ChartRow[]);

    const single = parseItunesRss({ feed: { entry: entry("One", "Solo", "https://music.apple.com/us/artist/solo/9") } });
    expect(single).toEqual([{ position: 1, title: "One", artistName: "Solo", appleArtistId: "9" }]);

    const collab = parseItunesRss({
      feed: {
        entry: {
          "im:name": { label: "Together" },
          "im:artist": [
            { label: "Alpha", attributes: { href: "https://music.apple.com/us/artist/alpha/1" } },
            { label: "Beta", attributes: { href: "https://music.apple.com/us/artist/beta/2" } },
          ],
        },
      },
    });
    expect(collab).toEqual([{ position: 1, title: "Together", artistName: "Alpha, Beta", appleArtistId: null }]);

    const tooMany = Array.from({ length: 101 }, (_, index) => entry(`Song ${index + 1}`, "Artist"));
    const capped = parseItunesRss({ feed: { entry: tooMany } });
    expect(capped).toHaveLength(100);
    expect(capped[99].position).toBe(100);
    expect(parseItunesRss({})).toEqual([]);
  });

  it("links an Apple id or one exact name and does not guess", () => {
    const index = indexArtists([
      { id: 1, name: "Ella Langley", appleId: "1384373733" },
      { id: 2, name: "Kings of Leon", appleId: null },
      { id: 3, name: "Drake", appleId: "50" },
      { id: 4, name: "Drake", appleId: null },
      { id: 5, name: "A$AP Rocky", appleId: null },
    ]);
    expect(matchChartArtist("1384373733", "Stage Name", index)).toBe(1);
    expect(matchChartArtist(null, "kings of leon", index)).toBe(2);
    expect(matchChartArtist(null, "Kings of Leon Official", index)).toBeNull();
    expect(matchChartArtist(null, "Drake", index)).toBeNull();
    expect(matchChartArtist("50", "Drake", index)).toBe(3);
    expect(matchChartArtist("999", "A$AP Rocky", index)).toBe(5);
    expect(matchChartArtist(null, "ASAP Rocky", index)).toBeNull();
    expect(matchChartArtist("50", "Someone Else", index)).toBe(3);
    const ambiguousApple = indexArtists([
      { id: 7, name: "One", appleId: "5" },
      { id: 8, name: "Two", appleId: "5" },
    ]);
    expect(matchChartArtist("5", "One", ambiguousApple)).toBeNull();
  });
});

describe("chart ingest", () => {
  it("upserts today's songs, links matches, and keeps going when albums fail", async () => {
    const db = new DatabaseSync(":memory:");
    const store = sqliteStore(db);
    await migrate(store);
    db.prepare(
      "INSERT INTO artists (name, normalized_name, apple_id, origin, tracked, created_at) VALUES ('Ella Langley', 'ellalangley', '1384373733', 'search', 1, '2026-11-01T00:00:00Z')",
    ).run();
    db.prepare(
      "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Olivia Rodrigo', 'oliviarodrigo', 'search', 1, '2026-11-01T00:00:00Z')",
    ).run();
    db.prepare(
      "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Ghost', 'ghost', 'search', 0, '2026-11-01T00:00:00Z')",
    ).run();
    const songs = JSON.stringify({
      feed: {
        entry: [
          entry("Choosin' Texas", "Ella Langley", "https://music.apple.com/us/artist/ella-langley/1384373733?uo=2"),
          entry("take me home", "Olivia Rodrigo", "https://music.apple.com/us/artist/olivia-rodrigo/979458609?uo=2"),
          entry("Other", "Ghost"),
        ],
      },
    });
    const fetches: string[] = [];
    vi.stubGlobal("fetch", async (input: string) => {
      const url = String(input);
      fetches.push(url);
      if (url.includes("topsongs")) return new Response(songs, { status: 200 });
      return new Response("down", { status: 502 });
    });
    const now = new Date("2026-11-06T16:00:00Z");
    const first = await ingestItunesCharts(store, now);
    expect(first.status).toBe("partial");
    expect(first.written).toBe(3);
    expect(first.linked).toBe(2);
    expect(first.errors[0]).toContain("HTTP 502");
    const rows = db.prepare("SELECT position, track_or_album_name, artist_id FROM chart_entries ORDER BY position").all() as {
      position: number;
      track_or_album_name: string;
      artist_id: number | null;
    }[];
    expect(rows).toEqual([
      { position: 1, track_or_album_name: "Choosin' Texas", artist_id: 1 },
      { position: 2, track_or_album_name: "take me home", artist_id: 2 },
      { position: 3, track_or_album_name: "Other", artist_id: null },
    ]);

    const again = await ingestItunesCharts(store, new Date("2026-11-06T17:00:00Z"));
    expect(again.status).toBe("error");
    expect(fetches.filter((url) => url.includes("topsongs"))).toHaveLength(1);
    expect(fetches.filter((url) => url.includes("topalbums"))).toHaveLength(2);

    const updatedSongs = JSON.stringify({
      feed: { entry: [entry("New Title", "Ella Langley", "https://music.apple.com/us/artist/ella-langley/1384373733?uo=2")] },
    });
    vi.stubGlobal("fetch", async (input: string) => {
      const url = String(input);
      if (url.includes("topsongs")) return new Response(updatedSongs, { status: 200 });
      return new Response("[]", { status: 200 });
    });
    const replaced = await ingestItunesCharts(store, now, 0);
    expect(replaced.written).toBeGreaterThan(0);
    const count = db.prepare("SELECT COUNT(*) AS n FROM chart_entries WHERE chart_source = 'itunes_topsongs'").get() as { n: number };
    expect(count.n).toBe(1);
    const title = db.prepare("SELECT track_or_album_name FROM chart_entries WHERE position = 1 AND chart_source = 'itunes_topsongs'").get() as {
      track_or_album_name: string;
    };
    expect(title.track_or_album_name).toBe("New Title");
    expect(CHART_SOURCES.map((chart) => chart.source)).toEqual(["itunes_topsongs", "itunes_topalbums"]);
  });

  it("does not fail the cron when a chart feed errors", async () => {
    const db = new DatabaseSync(":memory:");
    vi.stubGlobal("fetch", async () => new Response("nope", { status: 500 }));
    const result = await runScheduled(
      { DB: sqliteStore(db), CRAWL_QUEUE: { send() {} } },
      new Date("2026-11-06T12:00:00Z"),
    );
    expect(result).toMatchObject({ status: "queued", attempted: 0 });
    expect(result.charts.status).toBe("error");
    expect(result.charts.written).toBe(0);
  });
});
