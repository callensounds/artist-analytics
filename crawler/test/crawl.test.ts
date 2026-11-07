import { DatabaseSync } from "node:sqlite";
import { describe, expect, it, vi } from "vitest";
import {
  type CrawlEnv,
  MB_INTERVAL_MAX_MS,
  MB_INTERVAL_MIN_MS,
  RETRY_DELAY_MAX_SECONDS,
  RETRY_DELAY_MIN_SECONDS,
  RetryableFetch,
  handleMessage,
  resetMusicBrainzPace,
  retryDelaySeconds,
  runQueue,
  runScheduled,
} from "../src/crawl";
import { listenerSlices, mergeApiTracks, parseInstagram, parseMonthlyListeners, parsePopularTracks, parseRelatedArtists, leadingInt, tiktokVideos } from "../src/parse";
import { type Store, migrate, saveSource } from "../src/store";
import { resetSpotifyToken } from "../src/crawl";

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

function memoryEnv(
  pages: Record<string, { status?: number; body: string; headers?: Record<string, string> }>,
  key?: string,
): { env: CrawlEnv; sent: unknown[]; db: DatabaseSync } {
  const db = new DatabaseSync(":memory:");
  const sent: unknown[] = [];
  const env: CrawlEnv = {
    DB: sqliteStore(db),
    CRAWL_QUEUE: {
      send(body) {
        sent.push(body);
      },
    },
    YOUTUBE_API_KEY: key,
  };
  vi.stubGlobal("fetch", async (input: string | URL) => {
    const url = String(input);
    const match = Object.entries(pages).find(([needle]) => url.includes(needle));
    const page = match?.[1] ?? { status: 404, body: "" };
    const status = page.status ?? 200;
    return new Response(page.body, { status, headers: page.headers });
  });
  return { env, sent, db };
}

const pad = "<!--pad-->".repeat(9000);

describe("windowed parsers", () => {
  it("reads Spotify, Instagram, and TikTok markers past a 64KB head", () => {
    const spotify =
      pad +
      '<div data-testid="monthly-listeners-label">1,473,027 monthly listeners</div>' +
      "<div>Jubilee</div><div>Album • 2021</div>" +
      "<h2>Fans also like</h2>" +
      '<a href="/artist/3kzwYV3OCB010YfXMF0Avt"><span>Alvvays</span></a>' +
      "<div>Appears On</div>";
    expect(spotify.indexOf("1,473,027")).toBeGreaterThan(64_000);
    expect(parseMonthlyListeners(listenerSlices(spotify))).toEqual({ display: "1,473,027", exact: 1473027 });
    expect(parseRelatedArtists(spotify)[0]).toMatchObject({ name: "Alvvays", spotify_id: "3kzwYV3OCB010YfXMF0Avt", rank: 1 });

    const instagram =
      pad +
      'shortcode\\":\\"abc123\\" taken_at_timestamp\\":1792416619 username\\":\\"jbrekkie\\" edge_followed_by\\":{\\"count\\":574486}';
    expect(parseInstagram(instagram, "jbrekkie").followers).toBe(574486);
    expect(parseInstagram(instagram, "jbrekkie").posts).toHaveLength(1);

    const tiktok = pad + '"id":"7123456789012345678","createTime":1700000000,"followerCount":88000,"heartCount":929200';
    expect(leadingInt(tiktok, "followerCount")).toBe(88000);
    expect(leadingInt(tiktok, "heartCount")).toBe(929200);
    expect(tiktokVideos(tiktok)).toHaveLength(1);
  });
});

describe("crawler messages", () => {
  it("enqueues one MusicBrainz message per stale artist and records the cursor", async () => {
    const { env, sent, db } = memoryEnv({});
    await migrate(env.DB);
    const stamp = "2020-01-01T00:00:00Z";
    db.prepare(
      "INSERT INTO artists (name, normalized_name, origin, tracked, created_at, last_crawled_at) VALUES (?, ?, 'search', 1, ?, ?)",
    ).run("Fresh", "fresh", stamp, new Date().toISOString().replace(/\.\d{3}Z$/, "Z"));
    db.prepare("INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES (?, ?, 'search', 1, ?)").run(
      "Stale",
      "stale",
      stamp,
    );
    const result = await runScheduled(env, new Date("2026-10-30T21:00:00Z"));
    expect(result).toMatchObject({ status: "queued", attempted: 1 });
    expect(sent).toEqual([{ artist_id: 2, provider: "musicbrainz" }]);
    const run = db.prepare("SELECT status, artists_attempted FROM crawl_runs").get() as { status: string; artists_attempted: number };
    expect(run.status).toBe("queued");
    expect(run.artists_attempted).toBe(1);
  });

  it("stores MusicBrainz ids, fans out providers, and writes Spotify, Instagram, and TikTok snapshots", async () => {
    resetMusicBrainzPace();
    const spotify =
      pad +
      '<div data-testid="monthly-listeners-label">1,473,027 monthly listeners</div>' +
      "<div>2,100,000</div><div>Followers</div>" +
      "<h2>Fans also like</h2>" +
      '<a href="/artist/3kzwYV3OCB010YfXMF0Avt"><span>Alvvays</span></a>' +
      "<div>Appears On</div>";
    const instagram =
      pad +
      'shortcode\\":\\"abc123\\" taken_at_timestamp\\":1792416619 username\\":\\"jbrekkie\\" edge_followed_by\\":{\\"count\\":574486}';
    const tiktok = pad + '"id":"7123456789012345678","createTime":1700000000,"followerCount":88000,"heartCount":929200';
    const musicbrainz = JSON.stringify({
      artists: [
        {
          id: "mb-jb",
          name: "Japanese Breakfast",
          score: 100,
          relations: [
            { url: { resource: "https://open.spotify.com/artist/7MoIc5s9KXolCBH1fy9kkw" } },
            { url: { resource: "https://www.instagram.com/jbrekkie/" } },
            { url: { resource: "https://www.tiktok.com/@jbrekkie" } },
            { url: { resource: "https://music.apple.com/us/artist/japanese-breakfast/12345" } },
          ],
        },
      ],
    });
    const { env, sent, db } = memoryEnv({
      "musicbrainz.org": { body: musicbrainz },
      "open.spotify.com/artist/": { body: spotify },
      "instagram.com": { body: instagram },
      "tiktok.com": { body: tiktok },
      "itunes.apple.com": { status: 403, body: "" },
    });
    await migrate(env.DB);
    db.prepare("INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES (?, ?, 'search', 1, ?)").run(
      "Japanese Breakfast",
      "japanesebreakfast",
      "2020-01-01T00:00:00Z",
    );
    await handleMessage(env, { artist_id: 1, provider: "musicbrainz" }, new Date("2026-10-30T21:00:00Z"));
    const artist = db.prepare("SELECT spotify_id, instagram_handle, tiktok_handle, apple_id, musicbrainz_id FROM artists WHERE id = 1").get() as Record<string, string>;
    expect(artist.spotify_id).toBe("7MoIc5s9KXolCBH1fy9kkw");
    expect(artist.instagram_handle).toBe("jbrekkie");
    expect(artist.tiktok_handle).toBe("jbrekkie");
    expect(artist.apple_id).toBe("12345");
    expect(artist.musicbrainz_id).toBe("mb-jb");
    expect(sent).toEqual([
      { artist_id: 1, provider: "spotify" },
      { artist_id: 1, provider: "instagram" },
      { artist_id: 1, provider: "tiktok" },
      { artist_id: 1, provider: "apple" },
    ]);
    for (const body of sent) {
      await handleMessage(env, body as { artist_id: number; provider: string }, new Date("2026-10-30T21:00:00Z"));
    }
    const snaps = db.prepare("SELECT source, metric, value FROM metric_snapshots").all() as { source: string; metric: string; value: number }[];
    const byKey = Object.fromEntries(snaps.map((row) => [`${row.source}:${row.metric}`, row.value]));
    expect(byKey["spotify:Monthly listeners"]).toBe(1473027);
    expect(byKey["spotify:Followers"]).toBe(2100000);
    expect(byKey["instagram:Followers"]).toBe(574486);
    expect(byKey["tiktok:Followers"]).toBe(88000);
    expect(byKey["tiktok:Likes"]).toBe(929200);
    const edge = db.prepare("SELECT to_name, rank FROM related_edges").get() as { to_name: string; rank: number };
    expect(edge).toMatchObject({ to_name: "Alvvays", rank: 1 });
    const related = db.prepare("SELECT name, origin FROM artists WHERE origin = 'related'").get() as { name: string };
    expect(related.name).toBe("Alvvays");
    const video = db.prepare("SELECT source, title FROM content_sightings WHERE source = 'tiktok'").get() as { title: string };
    expect(video.title).toBe("7123456789012345678");
    const state = db.prepare("SELECT apple_blocked_until FROM crawl_state WHERE id = 1").get() as { apple_blocked_until: string };
    expect(state.apple_blocked_until).toBeTruthy();
    const calls: string[] = [];
    vi.stubGlobal("fetch", async (input: string | URL) => {
      calls.push(String(input));
      return new Response("", { status: 200 });
    });
    await handleMessage(env, { artist_id: 1, provider: "apple" }, new Date("2026-10-30T21:10:00Z"));
    expect(calls).toEqual([]);
  });

  it("retries a dropped connection with 120–300s jitter and does not ack", async () => {
    resetMusicBrainzPace();
    const { env, db } = memoryEnv({});
    vi.stubGlobal("fetch", async () => {
      throw new Error("network down");
    });
    await migrate(env.DB);
    db.prepare("INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Broken', 'broken', 'search', 1, ?)").run(
      "2020-01-01T00:00:00Z",
    );
    const message = queueMessage({ artist_id: 1, provider: "musicbrainz" });
    const result = await runQueue([message], env);
    expect(result).toEqual({ acked: 0, retried: 1 });
    expect(message.acked).toBe(false);
    expect(message.delay).toBeGreaterThanOrEqual(RETRY_DELAY_MIN_SECONDS);
    expect(message.delay).toBeLessThanOrEqual(RETRY_DELAY_MAX_SECONDS);
    expect(db.prepare("SELECT COUNT(*) AS n FROM crawl_runs").get()).toEqual({ n: 0 });
    expect(db.prepare("SELECT last_crawled_at FROM artists WHERE id = 1").get()).toEqual({ last_crawled_at: null });
  });

  it("retries MusicBrainz 503 and honors Retry-After without recording an error", async () => {
    resetMusicBrainzPace();
    const { env, db } = memoryEnv({
      "musicbrainz.org": { status: 503, body: "upstream unavailable", headers: { "retry-after": "250" } },
    });
    await migrate(env.DB);
    db.prepare("INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Japanese Breakfast', 'japanesebreakfast', 'search', 1, ?)").run(
      "2020-01-01T00:00:00Z",
    );
    const message = queueMessage({ artist_id: 1, provider: "musicbrainz" });
    const result = await runQueue([message], env);
    expect(result).toEqual({ acked: 0, retried: 1 });
    expect(message.acked).toBe(false);
    expect(message.delay).toBe(250);
    expect(db.prepare("SELECT COUNT(*) AS n FROM crawl_runs").get()).toEqual({ n: 0 });
    expect(db.prepare("SELECT last_crawled_at FROM artists WHERE id = 1").get()).toEqual({ last_crawled_at: null });
  });

  it("floors a short Retry-After at 120 seconds and jitters when the header is absent", () => {
    const now = Date.parse("2026-10-30T21:00:00Z");
    expect(retryDelaySeconds("45", now)).toBe(RETRY_DELAY_MIN_SECONDS);
    expect(retryDelaySeconds("250", now)).toBe(250);
    expect(retryDelaySeconds("99999", now)).toBe(3600);
    expect(retryDelaySeconds(new Date(now + 180_000).toUTCString(), now)).toBe(180);
    expect(retryDelaySeconds(new Date(now + 90_000).toUTCString(), now)).toBe(RETRY_DELAY_MIN_SECONDS);
    const jitter = retryDelaySeconds(null, now);
    expect(jitter).toBeGreaterThanOrEqual(RETRY_DELAY_MIN_SECONDS);
    expect(jitter).toBeLessThanOrEqual(RETRY_DELAY_MAX_SECONDS);
  });

  it("retries Spotify 429 and Apple 503, and still acks an Apple 403", async () => {
    const { env, db } = memoryEnv({
      "open.spotify.com/artist/": { status: 429, body: "", headers: { "retry-after": "180" } },
    });
    await migrate(env.DB);
    db.prepare(
      "INSERT INTO artists (name, normalized_name, origin, tracked, created_at, spotify_id) VALUES ('Japanese Breakfast', 'japanesebreakfast', 'search', 1, ?, '7MoIc5s9KXolCBH1fy9kkw')",
    ).run("2020-01-01T00:00:00Z");
    const spotify = queueMessage({ artist_id: 1, provider: "spotify" });
    expect(await runQueue([spotify], env)).toEqual({ acked: 0, retried: 1 });
    expect(spotify.delay).toBe(180);
    expect(db.prepare("SELECT COUNT(*) AS n FROM crawl_runs").get()).toEqual({ n: 0 });

    vi.stubGlobal("fetch", async (input: string | URL) => {
      const url = String(input);
      if (url.includes("itunes.apple.com")) return new Response("busy", { status: 503, headers: { "retry-after": "30" } });
      return new Response("", { status: 404 });
    });
    const appleBusy = queueMessage({ artist_id: 1, provider: "apple" });
    expect(await runQueue([appleBusy], env)).toEqual({ acked: 0, retried: 1 });
    expect(appleBusy.delay).toBe(RETRY_DELAY_MIN_SECONDS);
    expect(db.prepare("SELECT apple_blocked_until FROM crawl_state WHERE id = 1").get()).toBeUndefined();

    vi.stubGlobal("fetch", async () => new Response("", { status: 403 }));
    const appleBlocked = queueMessage({ artist_id: 1, provider: "apple" });
    expect(await runQueue([appleBlocked], env)).toEqual({ acked: 1, retried: 0 });
    expect(appleBlocked.acked).toBe(true);
    const state = db.prepare("SELECT apple_blocked_until FROM crawl_state WHERE id = 1").get() as { apple_blocked_until: string };
    expect(state.apple_blocked_until).toBeTruthy();
  });

  it("retries a MusicBrainz body that fails mid-read", async () => {
    resetMusicBrainzPace();
    const { env, db } = memoryEnv({});
    vi.stubGlobal("fetch", async () => {
      return new Response(
        new ReadableStream({
          pull(controller) {
            controller.error(new Error("socket hang up"));
          },
        }),
        { status: 200 },
      );
    });
    await migrate(env.DB);
    db.prepare("INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Broken', 'broken', 'search', 1, ?)").run(
      "2020-01-01T00:00:00Z",
    );
    const message = queueMessage({ artist_id: 1, provider: "musicbrainz" });
    await runQueue([message], env);
    expect(message.acked).toBe(false);
    expect(message.retried).toBe(true);
    expect(message.delay).toBeGreaterThanOrEqual(RETRY_DELAY_MIN_SECONDS);
    expect(message.delay).toBeLessThanOrEqual(RETRY_DELAY_MAX_SECONDS);
  });

  it("spaces MusicBrainz requests 1.5–2s apart on the isolate", async () => {
    resetMusicBrainzPace();
    let clock = Date.parse("2026-10-30T21:00:00Z");
    const now = vi.spyOn(Date, "now").mockImplementation(() => clock);
    const sleeps: number[] = [];
    const timer = vi.spyOn(globalThis, "setTimeout").mockImplementation(((fn: TimerHandler, ms?: number) => {
      sleeps.push(ms ?? 0);
      if (typeof fn === "function") fn();
      return 0 as unknown as ReturnType<typeof setTimeout>;
    }) as typeof setTimeout);
    try {
      const { env, db } = memoryEnv({ "musicbrainz.org": { status: 503, body: "" } });
      await migrate(env.DB);
      db.prepare("INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Pace', 'pace', 'search', 1, ?)").run(
        "2020-01-01T00:00:00Z",
      );
      await expect(handleMessage(env, { artist_id: 1, provider: "musicbrainz" })).rejects.toBeInstanceOf(RetryableFetch);
      await expect(handleMessage(env, { artist_id: 1, provider: "musicbrainz" })).rejects.toBeInstanceOf(RetryableFetch);
      const paced = sleeps.filter((ms) => ms >= MB_INTERVAL_MIN_MS);
      expect(paced).toHaveLength(1);
      expect(paced[0]).toBeLessThanOrEqual(MB_INTERVAL_MAX_MS);
    } finally {
      timer.mockRestore();
      now.mockRestore();
    }
  });

  it("acks a MusicBrainz 500 as a finished error", async () => {
    resetMusicBrainzPace();
    const { env, db } = memoryEnv({ "musicbrainz.org": { status: 500, body: "nope" } });
    await migrate(env.DB);
    db.prepare("INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Broken', 'broken', 'search', 1, ?)").run(
      "2020-01-01T00:00:00Z",
    );
    const message = queueMessage({ artist_id: 1, provider: "musicbrainz" });
    expect(await runQueue([message], env)).toEqual({ acked: 1, retried: 0 });
    expect(message.acked).toBe(true);
    const run = db.prepare("SELECT status FROM crawl_runs").get() as { status: string };
    expect(run.status).toBe("error");
    expect(db.prepare("SELECT last_crawled_at FROM artists WHERE id = 1").get()).toEqual({ last_crawled_at: null });
  });
});

describe("playlist schema", () => {
  it("creates an empty playlist_appearances table", async () => {
    const db = new DatabaseSync(":memory:");
    await migrate(sqliteStore(db));
    const table = db.prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'playlist_appearances'").get();
    expect(table).toEqual({ name: "playlist_appearances" });
    expect(db.prepare("SELECT COUNT(*) AS n FROM playlist_appearances").get()).toEqual({ n: 0 });
    const version = db.prepare("SELECT version FROM schema_migrations WHERE version = ?").get("0004_playlist_appearances.sql");
    expect(version).toBeTruthy();
  });
});

describe("spotify tracks", () => {
  it("parses public ids and does not treat a play count as popularity", () => {
    const page = `
      <span>Popular</span>
      <div aria-labelledby="listrow-title-track-spotify:track:0dpyzcT3RMNNSd2xKBf35I-0" aria-label="Be Sweet">
        <span>113,493,783</span>
      </div>
      <h2>Popular releases</h2>
    `;
    const rows = parsePopularTracks(page);
    expect(rows).toEqual([
      { name: "Be Sweet", externalId: "0dpyzcT3RMNNSd2xKBf35I", rank: 1, popularity: null, album: null },
    ]);
    const named = parsePopularTracks("Popular\nExample Song\n9,876,543\nPopular releases\n");
    expect(named[0]).toMatchObject({ externalId: "name:examplesong", popularity: null, name: "Example Song" });
    expect(JSON.stringify(named)).not.toContain("9876543");
  });

  it("merges a Web API score onto the public track and leaves a missing score null", () => {
    const merged = mergeApiTracks(
      [
        { name: "Be Sweet", externalId: "name:besweet", rank: 1, popularity: null, album: null },
        { name: "No Score", externalId: "0O4sIQ728ugLlwBVaxF8UM", rank: 2, popularity: null, album: null },
      ],
      [
        { name: "Be Sweet", externalId: "0dpyzcT3RMNNSd2xKBf35I", rank: 1, popularity: 80, album: "Jubilee" },
        { name: "No Score", externalId: "0O4sIQ728ugLlwBVaxF8UM", rank: 2, popularity: null, album: null },
      ],
    );
    expect(merged[0]).toMatchObject({ externalId: "0dpyzcT3RMNNSd2xKBf35I", popularity: 80, album: "Jubilee", rank: 1 });
    expect(merged[1].popularity).toBeNull();
  });

  it("stores public track ids without calling the Web API and leaves popularity null", async () => {
    resetSpotifyToken();
    const html = `
      <div data-testid="monthly-listeners-label">1,473,027 monthly listeners</div>
      <span>Popular</span>
      <div aria-labelledby="listrow-title-track-spotify:track:0dpyzcT3RMNNSd2xKBf35I-0" aria-label="Be Sweet">
        <span>113,493,783</span>
      </div>
      <div aria-labelledby="listrow-title-track-spotify:track:0O4sIQ728ugLlwBVaxF8UM-1" aria-label="No Score"></div>
      <h2>Popular releases</h2>
    `;
    const urls: string[] = [];
    const { env, db } = memoryEnv({ "open.spotify.com/artist/": { body: html } });
    vi.stubGlobal("fetch", async (input: string | URL) => {
      const url = String(input);
      urls.push(url);
      if (url.includes("open.spotify.com/artist/")) return new Response(html, { status: 200 });
      return new Response("nope", { status: 500 });
    });
    await migrate(env.DB);
    db.prepare(
      "INSERT INTO artists (name, normalized_name, origin, tracked, created_at, spotify_id) VALUES ('Japanese Breakfast', 'japanesebreakfast', 'search', 1, ?, '7MoIc5s9KXolCBH1fy9kkw')",
    ).run("2020-01-01T00:00:00Z");
    const version = db.prepare("SELECT version FROM schema_migrations WHERE version = ?").get("0010_artist_tracks.sql");
    expect(version).toBeTruthy();
    await handleMessage(env, { artist_id: 1, provider: "spotify" }, new Date("2026-11-06T00:00:00Z"));
    const rows = db.prepare("SELECT external_id, track_name, popularity, album_name, rank FROM artist_tracks ORDER BY rank").all();
    expect(rows).toEqual([
      { external_id: "0dpyzcT3RMNNSd2xKBf35I", track_name: "Be Sweet", popularity: null, album_name: null, rank: 1 },
      { external_id: "0O4sIQ728ugLlwBVaxF8UM", track_name: "No Score", popularity: null, album_name: null, rank: 2 },
    ]);
    expect(db.prepare("SELECT COUNT(*) AS n FROM track_snapshots").get()).toEqual({ n: 0 });
    expect(urls.some((url) => url.includes("api.spotify.com") || url.includes("accounts.spotify.com"))).toBe(false);
  });

  it("fills popularity from the top-tracks API when credentials exist", async () => {
    resetSpotifyToken();
    const html = `
      <div data-testid="monthly-listeners-label">1,473,027 monthly listeners</div>
      <span>Popular</span>
      <div aria-labelledby="listrow-title-track-spotify:track:0dpyzcT3RMNNSd2xKBf35I-0" aria-label="Be Sweet"></div>
      <div aria-labelledby="listrow-title-track-spotify:track:0O4sIQ728ugLlwBVaxF8UM-1" aria-label="No Score"></div>
      <h2>Popular releases</h2>
    `;
    const { env, db } = memoryEnv({
      "open.spotify.com/artist/": { body: html },
      "accounts.spotify.com": { body: JSON.stringify({ access_token: "tok", expires_in: 3600 }) },
      "api.spotify.com": {
        body: JSON.stringify({
          tracks: [
            { id: "0dpyzcT3RMNNSd2xKBf35I", name: "Be Sweet", popularity: 72, album: { name: "Jubilee" } },
            { id: "0O4sIQ728ugLlwBVaxF8UM", name: "No Score", popularity: null },
          ],
        }),
      },
    });
    env.SPOTIFY_CLIENT_ID = "cid";
    env.SPOTIFY_CLIENT_SECRET = "sec";
    await migrate(env.DB);
    db.prepare(
      "INSERT INTO artists (name, normalized_name, origin, tracked, created_at, spotify_id) VALUES ('Japanese Breakfast', 'japanesebreakfast', 'search', 1, ?, '7MoIc5s9KXolCBH1fy9kkw')",
    ).run("2020-01-01T00:00:00Z");
    await handleMessage(env, { artist_id: 1, provider: "spotify" }, new Date("2026-11-06T00:00:00Z"));
    const sweet = db.prepare("SELECT popularity, album_name FROM artist_tracks WHERE external_id = ?").get("0dpyzcT3RMNNSd2xKBf35I") as {
      popularity: number;
      album_name: string;
    };
    const quiet = db.prepare("SELECT popularity FROM artist_tracks WHERE external_id = ?").get("0O4sIQ728ugLlwBVaxF8UM") as {
      popularity: number | null;
    };
    expect(sweet).toEqual({ popularity: 72, album_name: "Jubilee" });
    expect(quiet.popularity).toBeNull();
    expect(db.prepare("SELECT COUNT(*) AS n FROM track_snapshots").get()).toEqual({ n: 1 });
    const detail = db.prepare("SELECT detail FROM crawl_runs").all() as { detail: string }[];
    expect(JSON.stringify(detail)).not.toContain("sec");
  });

  it("does not snapshot a missing score, keeps a stored score, and skips an unchanged one inside an hour", async () => {
    const db = new DatabaseSync(":memory:");
    const store = sqliteStore(db);
    await migrate(store);
    db.prepare("INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('A', 'a', 'search', 1, ?)").run(
      "2020-01-01T00:00:00Z",
    );
    const missing = { externalId: "0dpyzcT3RMNNSd2xKBf35I", name: "Be Sweet", popularity: null as number | null, album: null as string | null, rank: 1 };
    await saveSource(store, { id: 1, name: "A" }, { platform: "spotify", status: "ok", tracks: [missing] }, new Date("2026-11-06T00:00:00Z"), 0);
    expect(db.prepare("SELECT popularity FROM artist_tracks").get()).toEqual({ popularity: null });
    expect(db.prepare("SELECT COUNT(*) AS n FROM track_snapshots").get()).toEqual({ n: 0 });
    await saveSource(
      store,
      { id: 1, name: "A" },
      { platform: "spotify", status: "ok", tracks: [{ ...missing, popularity: 113493783 }] },
      new Date("2026-11-06T00:30:00Z"),
      0,
    );
    expect(db.prepare("SELECT popularity FROM artist_tracks").get()).toEqual({ popularity: null });
    await saveSource(
      store,
      { id: 1, name: "A" },
      { platform: "spotify", status: "ok", tracks: [{ ...missing, popularity: 72, album: "Jubilee" }] },
      new Date("2026-11-06T01:00:00Z"),
      0,
    );
    expect(db.prepare("SELECT popularity, album_name FROM artist_tracks").get()).toEqual({ popularity: 72, album_name: "Jubilee" });
    expect(db.prepare("SELECT COUNT(*) AS n FROM track_snapshots").get()).toEqual({ n: 1 });
    await saveSource(
      store,
      { id: 1, name: "A" },
      { platform: "spotify", status: "ok", tracks: [{ ...missing, popularity: 72, album: "Jubilee" }] },
      new Date("2026-11-06T01:20:00Z"),
      0,
    );
    expect(db.prepare("SELECT COUNT(*) AS n FROM track_snapshots").get()).toEqual({ n: 1 });
    await saveSource(
      store,
      { id: 1, name: "A" },
      { platform: "spotify", status: "ok", tracks: [{ ...missing, popularity: null, album: null }] },
      new Date("2026-11-06T02:00:00Z"),
      0,
    );
    expect(db.prepare("SELECT popularity, album_name FROM artist_tracks").get()).toEqual({ popularity: 72, album_name: "Jubilee" });
    expect(db.prepare("SELECT COUNT(*) AS n FROM track_snapshots").get()).toEqual({ n: 1 });
  });
});

function queueMessage(body: unknown) {
  return {
    body,
    acked: false,
    retried: false,
    delay: undefined as number | undefined,
    ack() {
      this.acked = true;
    },
    retry(options?: { delaySeconds?: number }) {
      this.retried = true;
      this.delay = options?.delaySeconds;
    },
  };
}
