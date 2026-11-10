import { describe, expect, it, vi } from "vitest";
import { DeezerBusy, chooseDeezerArtist, dateOnly, fetchDeezer, parseAlbumRows, parseSearchArtists, parseTopTracks } from "../src/deezer";

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
