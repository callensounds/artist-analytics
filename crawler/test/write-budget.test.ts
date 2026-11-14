import { DatabaseSync } from "node:sqlite";
import { describe, expect, it, vi } from "vitest";
import { parseTopTracks } from "../src/deezer";
import { evaluateAlerts } from "../src/alerts";
import { SLOW_SOURCE_MS, handleMessage, resetMusicBrainzPace, runScheduled } from "../src/crawl";
import {
  SNAPSHOT_REPEAT_MS,
  type Store,
  migrate,
  saveDeezerPlaylists,
  saveMusicBrainzReleases,
  saveSource,
  seenCutoff,
} from "../src/store";

function sqliteStore(db: DatabaseSync): Store & { changes: { sql: string; n: number }[] } {
  const changes: { sql: string; n: number }[] = [];
  return {
    changes,
    async all(sql, params = []) {
      return db.prepare(sql).all(...params) as Record<string, unknown>[];
    },
    async first(sql, params = []) {
      return (db.prepare(sql).get(...params) as Record<string, unknown> | undefined) ?? null;
    },
    async batch(statements) {
      db.exec("BEGIN");
      try {
        for (const statement of statements) {
          db.prepare(statement.sql).run(...(statement.params as never[]));
          const row = db.prepare("SELECT changes() AS n").get() as { n: number };
          changes.push({ sql: statement.sql, n: row.n });
        }
        db.exec("COMMIT");
      } catch (error) {
        db.exec("ROLLBACK");
        throw error;
      }
    },
  };
}

function changed(store: ReturnType<typeof sqliteStore>, table: string): number {
  return store.changes.filter((entry) => entry.sql.includes(table)).reduce((sum, entry) => sum + entry.n, 0);
}

describe("conditional keep-alive upserts", () => {
  it("rewrites a row only when content changed or last_seen_at is at least a day old", async () => {
    const db = new DatabaseSync(":memory:");
    const store = sqliteStore(db);
    await migrate(store);
    db.prepare(
      "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('A', 'a', 'search', 1, '2026-11-01T00:00:00Z')",
    ).run();
    const release = { mbid: "rg-1", title: "Sling", releaseType: "Album", releasedAt: "2021-06-16", precision: "day" };
    const first = new Date("2026-11-11T00:00:00Z");
    await saveMusicBrainzReleases(store, 1, [release], first, "A");
    expect(db.prepare("SELECT title, last_seen_at FROM musicbrainz_releases").get()).toEqual({
      title: "Sling",
      last_seen_at: "2026-11-11T00:00:00Z",
    });

    store.changes.length = 0;
    await saveMusicBrainzReleases(store, 1, [release], new Date("2026-11-11T06:00:00Z"), "A");
    expect(changed(store, "musicbrainz_releases")).toBe(0);
    expect(db.prepare("SELECT last_seen_at FROM musicbrainz_releases").get()).toEqual({
      last_seen_at: "2026-11-11T00:00:00Z",
    });

    store.changes.length = 0;
    await saveMusicBrainzReleases(
      store,
      1,
      [{ ...release, title: "Sling (deluxe)" }],
      new Date("2026-11-11T12:00:00Z"),
      "A",
    );
    expect(changed(store, "musicbrainz_releases")).toBe(1);
    expect(db.prepare("SELECT title, last_seen_at FROM musicbrainz_releases").get()).toEqual({
      title: "Sling (deluxe)",
      last_seen_at: "2026-11-11T12:00:00Z",
    });

    store.changes.length = 0;
    const dayLater = new Date("2026-11-12T12:00:00Z");
    await saveMusicBrainzReleases(store, 1, [{ ...release, title: "Sling (deluxe)" }], dayLater, "A");
    expect(changed(store, "musicbrainz_releases")).toBe(1);
    expect(db.prepare("SELECT last_seen_at FROM musicbrainz_releases").get()).toEqual({
      last_seen_at: dayLater.toISOString().replace(/\.\d{3}Z$/, "Z"),
    });
    expect(seenCutoff(dayLater) <= "2026-11-11T12:00:00Z").toBe(true);
  });

  it("skips an unchanged playlist and still records a renamed one", async () => {
    const db = new DatabaseSync(":memory:");
    const store = sqliteStore(db);
    await migrate(store);
    const placement = {
      id: "10",
      title: "Pop Hits",
      ownerName: "Deezer Charts",
      fans: 80,
      fansKnown: true,
      editorial: true,
    };
    await saveDeezerPlaylists(store, 7, "A", [placement], new Date("2026-11-11T00:00:00Z"));
    store.changes.length = 0;
    await saveDeezerPlaylists(store, 7, "A", [placement], new Date("2026-11-11T06:00:00Z"));
    expect(changed(store, "playlist_appearances")).toBe(0);
    expect(db.prepare("SELECT last_seen_at, follower_count FROM playlist_appearances").get()).toEqual({
      last_seen_at: "2026-11-11T00:00:00Z",
      follower_count: 80,
    });

    store.changes.length = 0;
    await saveDeezerPlaylists(
      store,
      7,
      "A",
      [{ ...placement, title: "Pop Hits Today", fans: 90 }],
      new Date("2026-11-11T07:00:00Z"),
    );
    expect(changed(store, "playlist_appearances")).toBe(1);
    expect(db.prepare("SELECT playlist_name, follower_count, first_seen_at, last_seen_at FROM playlist_appearances").get()).toEqual({
      playlist_name: "Pop Hits Today",
      follower_count: 90,
      first_seen_at: "2026-11-11T00:00:00Z",
      last_seen_at: "2026-11-11T07:00:00Z",
    });
  });

  it("skips an unchanged track and related edge, and writes when rank changes", async () => {
    const db = new DatabaseSync(":memory:");
    const store = sqliteStore(db);
    await migrate(store);
    db.prepare(
      "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('A', 'a', 'search', 1, '2026-11-01T00:00:00Z')",
    ).run();
    const track = { externalId: "track-1", name: "Be Sweet", popularity: 72, album: "Jubilee", rank: 1 };
    const related = [{ name: "Alvvays", spotify_id: "alvvays-id", rank: 1 }];
    await saveSource(
      store,
      { id: 1, name: "A" },
      { platform: "spotify", status: "ok", tracks: [track], related },
      new Date("2026-11-11T00:00:00Z"),
      0,
    );
    store.changes.length = 0;
    await saveSource(
      store,
      { id: 1, name: "A" },
      { platform: "spotify", status: "ok", tracks: [track], related },
      new Date("2026-11-11T06:00:00Z"),
      0,
    );
    expect(changed(store, "artist_tracks")).toBe(0);
    expect(changed(store, "related_edges")).toBe(0);
    expect(db.prepare("SELECT last_seen_at FROM artist_tracks").get()).toEqual({ last_seen_at: "2026-11-11T00:00:00Z" });
    expect(db.prepare("SELECT last_seen_at, rank FROM related_edges").get()).toEqual({
      last_seen_at: "2026-11-11T00:00:00Z",
      rank: 1,
    });

    store.changes.length = 0;
    await saveSource(
      store,
      { id: 1, name: "A" },
      { platform: "spotify", status: "ok", tracks: [{ ...track, rank: 2 }], related: [{ ...related[0], rank: 3 }] },
      new Date("2026-11-11T08:00:00Z"),
      0,
    );
    expect(changed(store, "artist_tracks")).toBe(1);
    expect(changed(store, "related_edges")).toBe(1);
    expect(db.prepare("SELECT rank, last_seen_at FROM artist_tracks").get()).toEqual({
      rank: 2,
      last_seen_at: "2026-11-11T08:00:00Z",
    });
    expect(db.prepare("SELECT rank FROM related_edges").get()).toEqual({ rank: 3 });
  });

  it("does not rewrite a confirmed list two days later, and marks only the row that disappeared", async () => {
    const db = new DatabaseSync(":memory:");
    const store = sqliteStore(db);
    await migrate(store);
    db.prepare(
      "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('A', 'a', 'search', 1, '2026-11-01T00:00:00Z')",
    ).run();
    const tracks = [
      { externalId: "kept", name: "Kept", popularity: 10, album: "Album", rank: 1 },
      { externalId: "gone", name: "Gone", popularity: 20, album: "Album", rank: 2 },
    ];
    const related = [
      { name: "Kept Pal", spotify_id: "kept-pal", rank: 1 },
      { name: "Gone Pal", spotify_id: "gone-pal", rank: 2 },
    ];
    const first = new Date("2026-11-11T00:00:00Z");
    await saveSource(
      store,
      { id: 1, name: "A" },
      { platform: "spotify", status: "ok", tracks, related, tracksConfirmed: true, relatedConfirmed: true },
      first,
      0,
    );
    await saveDeezerPlaylists(
      store,
      1,
      "A",
      [
        { id: "10", title: "Pop Hits", ownerName: "Deezer", fans: 80, fansKnown: true, editorial: true },
        { id: "12", title: "Old Mix", ownerName: "Sam", fans: 40, fansKnown: true, editorial: false },
      ],
      first,
    );

    store.changes.length = 0;
    const again = new Date("2026-11-13T00:00:00Z");
    await saveSource(
      store,
      { id: 1, name: "A" },
      { platform: "spotify", status: "ok", tracks, related, tracksConfirmed: true, relatedConfirmed: true },
      again,
      0,
    );
    await saveDeezerPlaylists(
      store,
      1,
      "A",
      [
        { id: "10", title: "Pop Hits", ownerName: "Deezer", fans: 80, fansKnown: true, editorial: true },
        { id: "12", title: "Old Mix", ownerName: "Sam", fans: 40, fansKnown: true, editorial: false },
      ],
      again,
    );
    expect(changed(store, "artist_tracks")).toBe(0);
    expect(changed(store, "related_edges")).toBe(0);
    expect(changed(store, "playlist_appearances")).toBe(0);
    expect(db.prepare("SELECT last_seen_at FROM artist_tracks WHERE external_id = 'kept'").get()).toEqual({
      last_seen_at: "2026-11-11T00:00:00Z",
    });

    store.changes.length = 0;
    await saveSource(
      store,
      { id: 1, name: "A" },
      {
        platform: "spotify",
        status: "ok",
        tracks: [tracks[0]],
        related: [related[0]],
        tracksConfirmed: true,
        relatedConfirmed: true,
      },
      new Date("2026-11-13T06:00:00Z"),
      0,
    );
    await saveDeezerPlaylists(
      store,
      1,
      "A",
      [{ id: "10", title: "Pop Hits", ownerName: "Deezer", fans: 90, fansKnown: true, editorial: true }],
      new Date("2026-11-13T06:00:00Z"),
    );
    expect(changed(store, "artist_tracks")).toBe(1);
    expect(changed(store, "related_edges")).toBe(1);
    expect(changed(store, "playlist_appearances")).toBe(2);
    expect(db.prepare("SELECT removed_at FROM artist_tracks WHERE external_id = 'gone'").get()).toEqual({
      removed_at: "2026-11-13T06:00:00Z",
    });
    expect(db.prepare("SELECT removed_at, last_seen_at FROM artist_tracks WHERE external_id = 'kept'").get()).toEqual({
      removed_at: null,
      last_seen_at: "2026-11-11T00:00:00Z",
    });
    expect(db.prepare("SELECT removed_at FROM related_edges WHERE to_spotify_id = 'gone-pal'").get()).toEqual({
      removed_at: "2026-11-13T06:00:00Z",
    });
    expect(db.prepare("SELECT removed_at FROM related_edges WHERE to_spotify_id = 'kept-pal'").get()).toEqual({
      removed_at: null,
    });
    expect(db.prepare("SELECT removed_at, follower_count FROM playlist_appearances WHERE playlist_id = '12'").get()).toEqual({
      removed_at: "2026-11-13T06:00:00Z",
      follower_count: 40,
    });
    expect(db.prepare("SELECT removed_at, follower_count, last_seen_at FROM playlist_appearances WHERE playlist_id = '10'").get()).toEqual({
      removed_at: null,
      follower_count: 90,
      last_seen_at: "2026-11-13T06:00:00Z",
    });
  });
});

describe("snapshot dedupe", () => {
  it("stores an identical audience value once per day and a changed value immediately", async () => {
    const db = new DatabaseSync(":memory:");
    const store = sqliteStore(db);
    await migrate(store);
    db.prepare(
      "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('A', 'a', 'search', 1, '2026-11-01T00:00:00Z')",
    ).run();
    const source = {
      platform: "spotify",
      status: "ok",
      metrics: [{ label: "Followers", exact: 1000, value: "1,000" }],
    };
    const start = new Date("2026-11-11T00:00:00Z");
    await saveSource(store, { id: 1, name: "A" }, source, start, 0);
    expect(db.prepare("SELECT COUNT(*) AS n FROM metric_snapshots").get()).toEqual({ n: 1 });

    await saveSource(store, { id: 1, name: "A" }, source, new Date(start.getTime() + 6 * 3600_000), 0);
    expect(db.prepare("SELECT COUNT(*) AS n FROM metric_snapshots").get()).toEqual({ n: 1 });
    const fresh = db.prepare("SELECT captured_at FROM source_freshness WHERE source = 'spotify'").get() as {
      captured_at: string;
    };
    expect(fresh.captured_at).toBe("2026-11-11T06:00:00Z");

    await saveSource(
      store,
      { id: 1, name: "A" },
      { ...source, metrics: [{ label: "Followers", exact: 1100, value: "1,100" }] },
      new Date(start.getTime() + 8 * 3600_000),
      0,
    );
    expect(db.prepare("SELECT value, captured_at FROM metric_snapshots ORDER BY captured_at").all()).toEqual([
      { value: 1000, captured_at: "2026-11-11T00:00:00Z" },
      { value: 1100, captured_at: "2026-11-11T08:00:00Z" },
    ]);

    await saveSource(
      store,
      { id: 1, name: "A" },
      { ...source, metrics: [{ label: "Followers", exact: 1100, value: "1,100" }] },
      new Date(start.getTime() + 8 * 3600_000 + SNAPSHOT_REPEAT_MS),
      0,
    );
    expect(db.prepare("SELECT COUNT(*) AS n FROM metric_snapshots").get()).toEqual({ n: 3 });
    expect(db.prepare("SELECT COUNT(*) AS n FROM crawl_runs").get()).toEqual({ n: 0 });
  });

  it("does not invent alert points between two real readings", () => {
    const alerts = evaluateAlerts(
      [
        { source: "spotify", metric: "Followers", value: 1000, captured_at: "2026-11-11T00:00:00Z" },
        { source: "spotify", metric: "Followers", value: 1200, captured_at: "2026-11-11T18:00:00Z" },
      ],
      new Date("2026-11-11T18:00:00Z"),
    );
    const day = alerts.find((alert) => alert.window === "24h");
    expect(day).toMatchObject({ baselineValue: 1000, latestValue: 1200, delta: 200 });
    const flat = evaluateAlerts(
      [
        { source: "spotify", metric: "Followers", value: 1000, captured_at: "2026-11-10T00:00:00Z" },
        { source: "spotify", metric: "Followers", value: 1000, captured_at: "2026-11-11T00:00:00Z" },
      ],
      new Date("2026-11-11T06:00:00Z"),
    );
    expect(flat).toEqual([]);
  });
});

describe("slow source cadence and empty cron rows", () => {
  it("fetches MusicBrainz releases and Deezer playlists at most once a day", async () => {
    expect(SLOW_SOURCE_MS).toBe(24 * 60 * 60 * 1000);
    resetMusicBrainzPace();
    const timer = vi.spyOn(globalThis, "setTimeout").mockImplementation(((fn: TimerHandler) => {
      if (typeof fn === "function") fn();
      return 0 as unknown as ReturnType<typeof setTimeout>;
    }) as typeof setTimeout);
    const db = new DatabaseSync(":memory:");
    const store = sqliteStore(db);
    await migrate(store);
    db.prepare(
      "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Clairo', 'clairo', 'search', 1, '2020-01-01T00:00:00Z')",
    ).run();
    const urls: string[] = [];
    vi.stubGlobal("fetch", async (input: string | URL) => {
      const url = String(input);
      urls.push(url);
      if (url.includes("release-group")) {
        return new Response(
          JSON.stringify({
            "release-groups": [
              { id: "rg-sling", title: "Sling", "primary-type": "Album", "first-release-date": "2021-06-16" },
            ],
          }),
          { status: 200 },
        );
      }
      if (url.includes("musicbrainz.org")) {
        return new Response(
          JSON.stringify({ artists: [{ id: "mb-clairo", name: "Clairo", score: 100, relations: [] }] }),
          { status: 200 },
        );
      }
      if (url.includes("/playlists")) {
        return new Response(JSON.stringify({ data: [{ id: 10, title: "Pop Hits", user: { id: 637006841, name: "Deezer" } }] }), {
          status: 200,
        });
      }
      if (url.endsWith("/10")) return new Response(JSON.stringify({ id: 10, fans: 80, nb_tracks: 4 }), { status: 200 });
      return new Response("{}", { status: 404 });
    });
    const env = { DB: store, CRAWL_QUEUE: { send() {} } };
    try {
      const start = new Date("2026-11-11T00:00:00Z");
      await handleMessage(env, { artist_id: 1, provider: "musicbrainz" }, start);
      expect(urls.some((url) => url.includes("release-group"))).toBe(true);
      const releaseCalls = urls.filter((url) => url.includes("release-group")).length;
      urls.length = 0;
      await handleMessage(env, { artist_id: 1, provider: "musicbrainz" }, new Date("2026-11-11T06:00:00Z"));
      expect(urls.some((url) => url.includes("release-group"))).toBe(false);
      expect(db.prepare("SELECT title FROM musicbrainz_releases").get()).toEqual({ title: "Sling" });

      urls.length = 0;
      db.prepare("UPDATE artists SET deezer_id = '27' WHERE id = 1").run();
      await handleMessage(env, { artist_id: 1, provider: "deezer_playlists" }, new Date("2026-11-11T06:00:00Z"));
      expect(urls.some((url) => url.includes("/playlists"))).toBe(true);
      urls.length = 0;
      await handleMessage(env, { artist_id: 1, provider: "deezer_playlists" }, new Date("2026-11-11T18:00:00Z"));
      expect(urls).toEqual([]);
      expect(releaseCalls).toBe(1);
    } finally {
      timer.mockRestore();
    }
  });

  it("does not write a crawl_runs row when a cron tick sends nothing", async () => {
    const db = new DatabaseSync(":memory:");
    const store = sqliteStore(db);
    const sent: unknown[] = [];
    vi.stubGlobal("fetch", async () => new Response("", { status: 404 }));
    const env = {
      DB: store,
      CRAWL_QUEUE: {
        send(body: unknown) {
          sent.push(body);
        },
      },
    };
    await migrate(store);
    db.prepare(
      "INSERT INTO artists (name, normalized_name, origin, tracked, created_at, last_crawled_at) VALUES ('Fresh', 'fresh', 'search', 1, ?, ?)",
    ).run("2026-11-11T00:00:00Z", "2026-11-11T12:00:00Z");
    const result = await runScheduled(env, new Date("2026-11-11T14:00:00Z"));
    expect(result.attempted).toBe(0);
    expect(sent).toEqual([]);
    expect(db.prepare("SELECT COUNT(*) AS n FROM crawl_runs").get()).toEqual({ n: 0 });
    expect(db.prepare("SELECT next_artist_id FROM crawl_state WHERE id = 1").get()).toEqual({ next_artist_id: 1 });
  });
});

describe("track_stats write budget", () => {
  it("writes one row per track metric per UTC day and skips an unchanged value", async () => {
    const db = new DatabaseSync(":memory:");
    const store = sqliteStore(db);
    let statReads = 0;
    const read = store.all;
    store.all = async (sql, params) => {
      if (String(sql).includes("track_stats")) statReads += 1;
      return read(sql, params);
    };
    await migrate(store);
    db.prepare(
      "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Daft Punk', 'daftpunk', 'search', 1, '2026-11-01T00:00:00Z')",
    ).run();
    const parsed = parseTopTracks({
      data: [
        { id: 11, title: "One More Time", rank: 896666, album: { title: "Discovery" } },
        { id: 12, title: "Digital Love", rank: 800000, album: { title: "Discovery" } },
        ...Array.from({ length: 12 }, (_, index) => ({ id: 100 + index, title: `Extra ${index}`, rank: 1000 + index })),
      ],
    });
    const tracks = parsed.map((track) => ({
      externalId: track.externalId,
      name: track.name,
      popularity: track.popularity,
      album: track.album,
      rank: track.rank,
      deezerRank: track.deezerRank,
    }));
    const start = new Date("2026-11-13T00:00:00Z");
    await saveSource(store, { id: 1, name: "Daft Punk" }, { platform: "deezer", status: "ok", tracks }, start, 0);
    expect(statReads).toBe(1);
    expect(db.prepare("SELECT COUNT(*) AS n FROM track_stats").get()).toEqual({ n: 10 });
    expect(db.prepare("SELECT value FROM track_stats WHERE external_id = '11'").get()).toEqual({ value: 896666 });
    expect(db.prepare("SELECT rank, popularity FROM artist_tracks WHERE external_id = '11'").get()).toEqual({
      rank: 1,
      popularity: null,
    });
    expect(db.prepare("SELECT COUNT(*) AS n FROM track_snapshots").get()).toEqual({ n: 0 });

    store.changes.length = 0;
    statReads = 0;
    const changedRank = tracks.map((track) => (track.externalId === "11" ? { ...track, deezerRank: 900000 } : track));
    await saveSource(
      store,
      { id: 1, name: "Daft Punk" },
      { platform: "deezer", status: "ok", tracks: changedRank },
      new Date("2026-11-13T06:00:00Z"),
      0,
    );
    expect(statReads).toBe(1);
    expect(changed(store, "track_stats")).toBe(0);
    expect(db.prepare("SELECT COUNT(*) AS n FROM track_stats").get()).toEqual({ n: 10 });
    expect(db.prepare("SELECT value FROM track_stats WHERE external_id = '11'").get()).toEqual({ value: 896666 });

    store.changes.length = 0;
    await saveSource(store, { id: 1, name: "Daft Punk" }, { platform: "deezer", status: "ok", tracks }, new Date("2026-11-14T00:00:00Z"), 0);
    expect(changed(store, "track_stats")).toBe(0);
    expect(db.prepare("SELECT COUNT(*) AS n FROM track_stats").get()).toEqual({ n: 10 });

    store.changes.length = 0;
    await saveSource(
      store,
      { id: 1, name: "Daft Punk" },
      { platform: "deezer", status: "ok", tracks: changedRank },
      new Date("2026-11-14T01:00:00Z"),
      0,
    );
    expect(changed(store, "track_stats")).toBe(1);
    expect(db.prepare("SELECT value, captured_at FROM track_stats WHERE external_id = '11' ORDER BY captured_at").all()).toEqual([
      { value: 896666, captured_at: "2026-11-13T00:00:00Z" },
      { value: 900000, captured_at: "2026-11-14T01:00:00Z" },
    ]);
  });

  it("stores a Spotify play count only when the field was present", async () => {
    const db = new DatabaseSync(":memory:");
    const store = sqliteStore(db);
    let statReads = 0;
    const read = store.all;
    store.all = async (sql, params) => {
      if (String(sql).includes("track_stats")) statReads += 1;
      return read(sql, params);
    };
    await migrate(store);
    db.prepare(
      "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('A', 'a', 'search', 1, '2026-11-01T00:00:00Z')",
    ).run();
    const track = { externalId: "0dpyzcT3RMNNSd2xKBf35I", name: "Be Sweet", popularity: null, album: null, rank: 1 };
    const start = new Date("2026-11-13T00:00:00Z");
    await saveSource(store, { id: 1, name: "A" }, { platform: "spotify", status: "ok", tracks: [track] }, start, 0);
    expect(statReads).toBe(0);
    expect(db.prepare("SELECT COUNT(*) AS n FROM track_stats").get()).toEqual({ n: 0 });
    expect(db.prepare("SELECT popularity FROM artist_tracks").get()).toEqual({ popularity: null });

    await saveSource(
      store,
      { id: 1, name: "A" },
      { platform: "spotify", status: "ok", tracks: [{ ...track, playcount: 113493783 }] },
      start,
      0,
    );
    expect(db.prepare("SELECT metric, value FROM track_stats").get()).toEqual({
      metric: "spotify_playcount",
      value: 113493783,
    });
    expect(db.prepare("SELECT popularity FROM artist_tracks").get()).toEqual({ popularity: null });
    store.changes.length = 0;
    await saveSource(
      store,
      { id: 1, name: "A" },
      { platform: "spotify", status: "ok", tracks: [{ ...track, playcount: 113493800 }] },
      new Date("2026-11-13T08:00:00Z"),
      0,
    );
    expect(changed(store, "track_stats")).toBe(0);
    expect(db.prepare("SELECT COUNT(*) AS n FROM track_stats").get()).toEqual({ n: 1 });
  });

  it("reads latest stats for 12 tracks in one non-compound statement", async () => {
    const db = new DatabaseSync(":memory:");
    const store = sqliteStore(db);
    const reads: { sql: string; params: unknown[] }[] = [];
    const read = store.all;
    store.all = async (sql, params = []) => {
      if (String(sql).includes("track_stats")) reads.push({ sql: String(sql), params });
      return read(sql, params);
    };
    await migrate(store);
    db.prepare(
      "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Kendrick Lamar', 'kendricklamar', 'search', 1, '2026-11-01T00:00:00Z')",
    ).run();
    const tracks = Array.from({ length: 12 }, (_, index) => ({
      externalId: String(index + 1),
      name: `Track ${index + 1}`,
      popularity: null,
      album: "Album",
      rank: index + 1,
      deezerRank: 1000 + index,
    }));
    const insert = db.prepare(
      "INSERT INTO track_stats (artist_id, source, external_id, metric, value, captured_at) VALUES (1, 'deezer', ?, 'deezer_rank', ?, ?)",
    );
    for (const track of tracks) {
      // Latest row is inserted first, and an older row has the larger value.
      insert.run(track.externalId, track.deezerRank, "2026-11-12T00:00:00Z");
      insert.run(track.externalId, track.deezerRank + 500, "2026-11-10T00:00:00Z");
      insert.run(track.externalId, track.deezerRank - 50, "2026-11-01T00:00:00Z");
    }
    store.changes.length = 0;
    await saveSource(
      store,
      { id: 1, name: "Kendrick Lamar" },
      { platform: "deezer", status: "ok", tracks },
      new Date("2026-11-13T00:00:00Z"),
      0,
    );
    expect(reads).toHaveLength(1);
    const sql = reads[0].sql.toUpperCase();
    expect(sql).not.toMatch(/\bUNION\b/);
    expect(sql).not.toMatch(/\bINTERSECT\b/);
    expect(sql).not.toMatch(/\bEXCEPT\b/);
    expect(reads[0].params.length).toBeLessThan(100);
    // The list cap stores 10 of these 12. Those 10 share one statement.
    const idParams = reads[0].params.filter((value) => typeof value === "string" && /^\d+$/.test(value));
    expect(idParams).toEqual(["1", "2", "3", "4", "5", "6", "7", "8", "9", "10"]);
    const plan = db.prepare(`EXPLAIN QUERY PLAN ${reads[0].sql}`).all(...(reads[0].params as never[])) as { detail: string }[];
    const detail = plan.map((row) => row.detail).join(" ");
    expect(detail).toContain("idx_track_stats_lookup");
    expect(detail).not.toContain("SCAN track_stats");
    expect(changed(store, "track_stats")).toBe(0);
    expect(db.prepare("SELECT COUNT(*) AS n FROM track_stats").get()).toEqual({ n: 36 });

    store.changes.length = 0;
    reads.length = 0;
    const changedRank = tracks.map((track) => (track.externalId === "1" ? { ...track, deezerRank: 1100 } : track));
    await saveSource(
      store,
      { id: 1, name: "Kendrick Lamar" },
      { platform: "deezer", status: "ok", tracks: changedRank },
      new Date("2026-11-14T00:00:00Z"),
      0,
    );
    expect(reads).toHaveLength(1);
    expect(reads[0].sql.toUpperCase()).not.toMatch(/\b(UNION|INTERSECT|EXCEPT)\b/);
    expect(changed(store, "track_stats")).toBe(1);
    expect(db.prepare("SELECT value, captured_at FROM track_stats WHERE external_id = '1' ORDER BY captured_at").all()).toEqual([
      { value: 950, captured_at: "2026-11-01T00:00:00Z" },
      { value: 1500, captured_at: "2026-11-10T00:00:00Z" },
      { value: 1000, captured_at: "2026-11-12T00:00:00Z" },
      { value: 1100, captured_at: "2026-11-14T00:00:00Z" },
    ]);
    expect(db.prepare("SELECT COUNT(*) AS n FROM track_stats WHERE external_id = '2'").get()).toEqual({ n: 3 });
  });
});
