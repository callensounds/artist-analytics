import { describe, expect, it, vi } from "vitest";
import {
  DeezerBusy,
  applyPlaylistDetail,
  chooseDeezerArtist,
  dateOnly,
  detailPlaylistIds,
  fetchDeezer,
  fetchDeezerPlaylists,
  isDeezerEditorial,
  parseAlbumRows,
  parseArtistPlaylists,
  parseSearchArtists,
  parseTopTracks,
} from "../src/deezer";

const artist = (id: number, name: string, fans: number) => ({
  id,
  name,
  nb_fan: fans,
  nb_album: 2,
  link: `https://www.deezer.com/artist/${id}`,
});

describe("deezer matching", () => {
  it("keeps an exact name, prefers a stored id, and otherwise the most fans", () => {
    const rows = parseSearchArtists({
      data: [artist(8, "Daft Punk", 100), artist(27, "Daft Punk", 50), artist(4, "Daft", 999), artist(3, "daft-punk", 10)],
    });
    expect(chooseDeezerArtist(rows, "Daft Punk")?.id).toBe("8");
    expect(chooseDeezerArtist(rows, "Daft Punk", "27")?.id).toBe("27");
    expect(chooseDeezerArtist(rows, "Daft")?.id).toBe("4");
    expect(chooseDeezerArtist(rows, "Daft Punk Live")).toBeNull();
    expect(parseSearchArtists({ error: { message: "no data" } })).toEqual([]);
  });

  it("ranks top tracks by list order and drops a clock from release_date", () => {
    const tracks = parseTopTracks({
      data: [{ id: 11, title: "One More Time", rank: 900000, album: { title: "Discovery" } }],
    });
    expect(tracks[0]).toMatchObject({ rank: 1, popularity: null, name: "One More Time" });
    expect(dateOnly("2001-03-12T00:00:00")).toBe("2001-03-12");
    const parsed = parseAlbumRows({
      data: [{ id: 302127, title: "Discovery", release_date: "2001-03-12T07:00:00", record_type: "album" }],
    });
    expect(parsed.albums[0].releaseDate).toBe("2001-03-12");
  });

  it("returns unavailable when the name misses and errors when Deezer fails", async () => {
    vi.stubGlobal("fetch", async (input: string | URL) => {
      const url = String(input);
      if (url.includes("/search/artist")) {
        return new Response(JSON.stringify({ data: [artist(4, "Daft", 9)] }), { status: 200 });
      }
      return new Response("", { status: 404 });
    });
    const missed = await fetchDeezer("Daft Punk", "");
    expect(missed.status).toBe("unavailable");
    expect(missed.deezerId).toBeUndefined();

    vi.stubGlobal("fetch", async () => new Response("nope", { status: 500 }));
    const failed = await fetchDeezer("Daft Punk", "");
    expect(failed.status).toBe("error");

    vi.stubGlobal("fetch", async () => new Response("", { status: 429, headers: { "retry-after": "120" } }));
    await expect(fetchDeezer("Daft Punk", "")).rejects.toBeInstanceOf(DeezerBusy);
  });

  it("does not search when the stored id still matches the name", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", async (input: string | URL) => {
      const url = String(input);
      calls.push(url);
      if (url.endsWith("/artist/27")) return new Response(JSON.stringify(artist(27, "Daft Punk", 4000)), { status: 200 });
      if (url.includes("/top")) return new Response(JSON.stringify({ data: [] }), { status: 200 });
      if (url.includes("/albums")) {
        return new Response(JSON.stringify({ data: [{ id: 302127, title: "Discovery", release_date: "2001-03-12", record_type: "album" }] }), { status: 200 });
      }
      return new Response(JSON.stringify({ data: [artist(99, "Daft Punk", 1)] }), { status: 200 });
    });
    const saved = await fetchDeezer("Daft Punk", "27");
    expect(saved.status).toBe("ok");
    expect(saved.deezerId).toBe("27");
    expect(saved.metrics?.[0]).toMatchObject({ label: "Fans", exact: 4000 });
    expect(saved.sightings?.[0]).toMatchObject({ precision: "day", sortKey: "2001-03-12" });
    expect(calls.some((url) => url.includes("/search/"))).toBe(false);
  });
});

describe("deezer playlists", () => {
  it("keeps a missing fan count null, flags editors, and caps the list at 50", () => {
    const data = Array.from({ length: 60 }, (_, index) => ({
      id: 1000 + index,
      title: index === 3 ? "" : `List ${index}`,
      user: {
        id: index === 1 ? 1687950 : index === 2 ? 637006841 : 42,
        name: index === 2 ? "Deezer Charts" : index === 4 ? "Laeti - Deezer Pop Editor" : "Sam",
      },
    }));
    data.push({ id: 1001, title: "Duplicate", user: { id: 42, name: "Sam" } });
    const parsed = parseArtistPlaylists({ data, total: 100, next: "https://api.deezer.com/next" });
    expect(parsed).toHaveLength(50);
    expect(parsed.some((row) => row.title === "")).toBe(false);
    expect(parsed.filter((row) => row.id === "1001")).toHaveLength(1);
    expect(parsed.every((row) => row.fans === null && row.fansKnown === false && row.position === null && row.nbTracks === null)).toBe(true);
    expect(parsed.find((row) => row.id === "1001")?.editorial).toBe(true);
    expect(parsed.find((row) => row.id === "1002")?.editorial).toBe(true);
    expect(parsed.find((row) => row.id === "1004")?.editorial).toBe(true);
    expect(isDeezerEditorial("Selected.", "2642785342")).toBe(false);
    expect(parseArtistPlaylists({ error: { message: "no" } })).toEqual([]);

    const zero = parseArtistPlaylists({ data: [{ id: 8, title: "Quiet", fans: 0, user: { name: "Sam" } }] });
    expect(zero[0]).toMatchObject({ fans: 0, fansKnown: true, position: null });
    const junk = applyPlaylistDetail(zero[0], { id: 8, fans: false, nb_tracks: 4 });
    expect(junk.fansKnown).toBe(true);
    expect(junk.fans).toBeNull();
    expect(junk.nbTracks).toBe(4);
    const skipped = applyPlaylistDetail(zero[0], { id: 9, fans: 10 });
    expect(skipped.fans).toBe(0);
    expect(applyPlaylistDetail(zero[0], { error: { message: "no" } }).fansKnown).toBe(true);
  });

  it("asks for details of editorial playlists first and stops at the cap", () => {
    const rows = parseArtistPlaylists({
      data: [
        { id: 1, title: "User list", user: { id: 9, name: "Sam" } },
        { id: 2, title: "Already counted", fans: 12, user: { id: 9, name: "Sam" } },
        { id: 3, title: "Charts", user: { id: 637006841, name: "Deezer Charts" }, nb_tracks: 1 },
        { id: 4, title: "Bigger", user: { id: 9, name: "Sam" }, nb_tracks: 30 },
      ],
    });
    expect(detailPlaylistIds(rows, 2)).toEqual(["3", "4"]);
    expect(rows.find((row) => row.id === "2")?.fans).toBe(12);
  });

  it("reads list rows without fans and fills fans only from playlist details", async () => {
    const urls: string[] = [];
    vi.stubGlobal("fetch", async (input: string | URL) => {
      const url = String(input);
      urls.push(url);
      if (url.includes("/playlists")) {
        const data = Array.from({ length: 10 }, (_, index) => ({
          id: 100 + index,
          title: `List ${index}`,
          user: index === 9 ? { id: 637006841, name: "Deezer Charts" } : { id: 50, name: "Sam" },
        }));
        return new Response(JSON.stringify({ data, total: 80 }), { status: 200 });
      }
      if (url.endsWith("/107") || url.endsWith("/108")) {
        throw new Error("detail cap was exceeded");
      }
      const id = Number(url.split("/").pop());
      return new Response(JSON.stringify({ id, fans: id === 109 ? 80 : id, nb_tracks: 12 }), { status: 200 });
    });
    const fetched = await fetchDeezerPlaylists("27");
    expect(fetched.ok).toBe(true);
    const details = urls.filter((url) => url.includes("/playlist/"));
    expect(details).toHaveLength(8);
    expect(details[0]).toContain("/playlist/109");
    expect(details.some((url) => url.endsWith("/107") || url.endsWith("/108"))).toBe(false);
    expect(fetched.playlists.find((row) => row.id === "109")).toMatchObject({ fans: 80, fansKnown: true, editorial: true, position: null });
    expect(fetched.playlists.find((row) => row.id === "107")).toMatchObject({ fans: null, fansKnown: false });
    expect(urls.filter((url) => url.includes("/playlists"))).toHaveLength(1);
  });

  it("retries when Deezer is busy and does not treat an HTTP error as an empty list", async () => {
    vi.stubGlobal("fetch", async () => new Response("", { status: 429 }));
    await expect(fetchDeezerPlaylists("27")).rejects.toBeInstanceOf(DeezerBusy);
    vi.stubGlobal("fetch", async () => new Response("nope", { status: 503 }));
    await expect(fetchDeezerPlaylists("27")).rejects.toBeInstanceOf(DeezerBusy);
    vi.stubGlobal("fetch", async () => new Response("nope", { status: 500 }));
    const failed = await fetchDeezerPlaylists("27");
    expect(failed.ok).toBe(false);
    expect(failed.playlists).toEqual([]);
    expect(failed.reason).toContain("500");
  });
});
