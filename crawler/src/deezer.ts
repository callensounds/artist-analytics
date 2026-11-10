/**
 * Keyless Deezer reads. Same match rules as src/providers/deezer.py.
 * Dates stay dates. Fan counts are nb_fan integers only.
 */

import { HTTP_USER_AGENT, namesMatch } from "./parse";
import type { SavedSource } from "./store";

export class DeezerBusy extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DeezerBusy";
  }
}

export interface DeezerArtist {
  id: string;
  name: string;
  nbFan: number | null;
  nbAlbum: number | null;
  link: string;
  picture: string | null;
}

const TRACK_LIMIT = 10;
const ALBUM_LIMIT = 25;
const ALBUM_DETAIL_LIMIT = 2;

export function wholeCount(value: unknown): number | null {
  if (typeof value === "boolean") return null;
  if (typeof value === "number" && (!Number.isInteger(value) || value < 0)) return null;
  if (typeof value === "number") return value;
  if (typeof value === "string" && /^\d+$/.test(value.trim())) return Number(value.trim());
  return null;
}

export function dateOnly(raw: unknown): string {
  let text = typeof raw === "string" ? raw.trim() : "";
  if (!text) return "";
  if (text.includes("T")) return text.split("T", 1)[0].trim();
  if (text.includes(" ")) {
    const [left, right] = text.split(" ", 2);
    if (right.includes(":")) return left.trim();
  }
  return text;
}

function classify(raw: string): "day" | "month" | "year" | "unknown" {
  const pieces = raw.split("-").filter(Boolean);
  if (!pieces.length || !/^\d{4}$/.test(pieces[0])) return "unknown";
  if (pieces.length >= 3 && /^\d{2}$/.test(pieces[1]) && /^\d{2}/.test(pieces[2].slice(0, 2))) return "day";
  if (pieces.length === 2 && /^\d{2}$/.test(pieces[1])) return "month";
  if (pieces.length === 1) return "year";
  return "unknown";
}

function artistId(value: unknown): string {
  if (typeof value === "boolean" || value === null || value === undefined) return "";
  const text = String(value).trim();
  return /^\d+$/.test(text) ? text : "";
}

function artistRow(row: Record<string, unknown>): DeezerArtist | null {
  const id = artistId(row.id);
  const name = String(row.name || "").replace(/\s+/g, " ").trim();
  if (!id || !name) return null;
  const picture = row.picture_medium || row.picture || row.picture_small;
  const link = typeof row.link === "string" && row.link.startsWith("https://www.deezer.com/artist/")
    ? row.link
    : `https://www.deezer.com/artist/${id}`;
  return {
    id,
    name: name.slice(0, 200),
    nbFan: wholeCount(row.nb_fan),
    nbAlbum: wholeCount(row.nb_album),
    link,
    picture: typeof picture === "string" && picture.startsWith("https://") ? picture : null,
  };
}

export function parseSearchArtists(payload: unknown): DeezerArtist[] {
  if (!payload || typeof payload !== "object" || (payload as { error?: unknown }).error) return [];
  const rows = (payload as { data?: unknown }).data;
  if (!Array.isArray(rows)) return [];
  const found: DeezerArtist[] = [];
  const seen = new Set<string>();
  for (const row of rows) {
    if (!row || typeof row !== "object") continue;
    const parsed = artistRow(row as Record<string, unknown>);
    if (!parsed || seen.has(parsed.id)) continue;
    seen.add(parsed.id);
    found.push(parsed);
  }
  return found;
}

export function chooseDeezerArtist(candidates: DeezerArtist[] | null | undefined, name: string, preferredId?: string | null): DeezerArtist | null {
  const exact = (candidates || []).filter((row) => row && row.id && namesMatch(row.name, name));
  if (!exact.length) return null;
  const preferred = String(preferredId || "").trim();
  if (/^\d+$/.test(preferred)) {
    const chosen = exact.find((row) => row.id === preferred);
    if (chosen) return chosen;
  }
  return exact.reduce((best, row) => {
    const fans = typeof row.nbFan === "number" ? row.nbFan : -1;
    const bestFans = typeof best.nbFan === "number" ? best.nbFan : -1;
    if (fans !== bestFans) return fans > bestFans ? row : best;
    return Number(row.id) < Number(best.id) ? row : best;
  });
}

export function parseTopTracks(payload: unknown, limit = TRACK_LIMIT): { externalId: string; name: string; popularity: null; album: string | null; rank: number }[] {
  if (!payload || typeof payload !== "object" || (payload as { error?: unknown }).error) return [];
  const rows = (payload as { data?: unknown }).data;
  if (!Array.isArray(rows)) return [];
  const tracks = [];
  const seen = new Set<string>();
  for (const row of rows) {
    if (tracks.length >= limit || !row || typeof row !== "object") continue;
    const record = row as Record<string, unknown>;
    const externalId = artistId(record.id);
    const name = String(record.title || "").replace(/\s+/g, " ").trim();
    if (!externalId || !name || seen.has(externalId)) continue;
    seen.add(externalId);
    const album = record.album && typeof record.album === "object" ? (record.album as { title?: unknown }).title : "";
    const albumName = String(album || "").replace(/\s+/g, " ").trim();
    tracks.push({
      externalId,
      name: name.slice(0, 200),
      popularity: null,
      album: albumName.slice(0, 200) || null,
      rank: tracks.length + 1,
    });
  }
  return tracks;
}

export function parseAlbumRows(payload: unknown): { albums: { id: string; title: string; kind: string; releaseDate: string; url: string }[]; missing: string[] } {
  const empty = { albums: [], missing: [] as string[] };
  if (!payload || typeof payload !== "object" || (payload as { error?: unknown }).error) return empty;
  const record = payload as Record<string, unknown>;
  const rows = artistId(record.id) && record.title ? [record] : Array.isArray(record.data) ? record.data : [];
  const albums = [];
  const missing: string[] = [];
  const seen = new Set<string>();
  for (const row of rows) {
    if (!row || typeof row !== "object") continue;
    const item = row as Record<string, unknown>;
    const id = artistId(item.id);
    const title = String(item.title || "").replace(/\s+/g, " ").trim();
    if (!id || !title || seen.has(id)) continue;
    seen.add(id);
    const kind = String(item.record_type || "album").replace(/\s+/g, " ").trim().slice(0, 40) || "album";
    const released = dateOnly(item.release_date);
    if (classify(released) === "unknown") {
      missing.push(id);
      continue;
    }
    albums.push({
      id,
      title: title.slice(0, 200),
      kind,
      releaseDate: released,
      url: `https://www.deezer.com/album/${id}`,
    });
  }
  return { albums, missing };
}

function countMetric(label: string, number: number | null): { label: string; exact: number | null; value: string | null } {
  if (number === null) return { label, exact: null, value: null };
  return { label, exact: number, value: number.toLocaleString("en-US") };
}

async function readJson(url: string): Promise<{ status: number; ok: boolean; json: unknown }> {
  let response: Response;
  try {
    response = await fetch(url, {
      headers: { "User-Agent": HTTP_USER_AGENT, Accept: "application/json" },
      redirect: "follow",
    });
  } catch (error) {
    throw new DeezerBusy(error instanceof Error ? error.message : "network error");
  }
  if (response.status === 429 || response.status === 503) throw new DeezerBusy(`HTTP ${response.status}`);
  let text = "";
  try {
    text = await response.text();
  } catch (error) {
    throw new DeezerBusy(error instanceof Error ? error.message : "network error");
  }
  let json: unknown = null;
  try {
    json = JSON.parse(text.slice(0, 180_000));
  } catch {
    json = null;
  }
  return { status: response.status, ok: response.ok, json };
}

function errorMessage(payload: unknown): string | null {
  if (!payload || typeof payload !== "object") return null;
  const error = (payload as { error?: unknown }).error;
  if (!error || typeof error !== "object") return null;
  const message = String((error as { message?: unknown }).message || "Deezer returned an error.").replace(/\s+/g, " ").trim();
  return message.slice(0, 180) || "Deezer returned an error.";
}

export async function fetchDeezer(name: string, preferredId: string): Promise<SavedSource> {
  const cleaned = name.replace(/\s+/g, " ").trim().slice(0, 120);
  if (!cleaned) {
    return { platform: "deezer", status: "unavailable", reason: "Enter an artist name to search Deezer.", markCrawled: true };
  }
  const preferred = /^\d+$/.test(preferredId) ? preferredId : "";
  let artist: DeezerArtist | null = null;
  let matched = "fans";
  if (preferred) {
    const profile = await readJson(`https://api.deezer.com/artist/${preferred}`);
    const row = profile.ok && profile.json && typeof profile.json === "object" && !errorMessage(profile.json)
      ? artistRow(profile.json as Record<string, unknown>)
      : null;
    if (row && namesMatch(row.name, cleaned)) {
      artist = row;
      matched = "stored";
    }
  }
  if (!artist) {
    const search = await readJson(`https://api.deezer.com/search/artist?q=${encodeURIComponent(cleaned)}&limit=25`);
    if (!search.ok && search.status !== 404) {
      return { platform: "deezer", status: "error", reason: `Deezer search returned HTTP ${search.status}.`, markCrawled: true };
    }
    const problem = errorMessage(search.json);
    if (problem) return { platform: "deezer", status: "error", reason: problem, markCrawled: true };
    const chosen = chooseDeezerArtist(parseSearchArtists(search.json), cleaned, preferred);
    if (!chosen) {
      return {
        platform: "deezer",
        status: "unavailable",
        reason: "No Deezer artist with this exact name. A different name is not shown as this artist.",
        markCrawled: true,
      };
    }
    const profile = await readJson(`https://api.deezer.com/artist/${chosen.id}`);
    const confirmed = profile.ok && profile.json && typeof profile.json === "object" && !errorMessage(profile.json)
      ? artistRow(profile.json as Record<string, unknown>)
      : null;
    if (confirmed && !namesMatch(confirmed.name, cleaned)) {
      return {
        platform: "deezer",
        status: "unavailable",
        reason: "No Deezer artist with this exact name. A different name is not shown as this artist.",
        markCrawled: true,
      };
    }
    artist = confirmed || chosen;
  }
  const notes = [matched === "stored"
    ? "Using the Deezer artist id already stored for this artist."
    : "Matched by exact name. Among those, the artist with the most Deezer fans was kept."];
  let tracks: SavedSource["tracks"] = [];
  let albums: { id: string; title: string; kind: string; releaseDate: string; url: string }[] = [];
  try {
    const top = await readJson(`https://api.deezer.com/artist/${artist.id}/top?limit=${TRACK_LIMIT}`);
    if (top.ok && !errorMessage(top.json)) tracks = parseTopTracks(top.json);
    else notes.push("Deezer top tracks were not available.");
  } catch (error) {
    if (error instanceof DeezerBusy) throw error;
    notes.push("Deezer top tracks timed out.");
  }
  let missing: string[] = [];
  try {
    const page = await readJson(`https://api.deezer.com/artist/${artist.id}/albums?limit=${ALBUM_LIMIT}`);
    if (page.ok && !errorMessage(page.json)) {
      const parsed = parseAlbumRows(page.json);
      albums = parsed.albums;
      missing = parsed.missing;
    } else notes.push("Deezer album dates were not available.");
  } catch (error) {
    if (error instanceof DeezerBusy) throw error;
    notes.push("Deezer album dates timed out.");
  }
  for (const albumId of missing.slice(0, ALBUM_DETAIL_LIMIT)) {
    try {
      const detail = await readJson(`https://api.deezer.com/album/${albumId}`);
      if (!detail.ok || errorMessage(detail.json)) continue;
      albums = albums.concat(parseAlbumRows(detail.json).albums);
    } catch (error) {
      if (error instanceof DeezerBusy) throw error;
      notes.push("One Deezer album page timed out.");
      break;
    }
  }
  const sightings = albums
    .map((album) => {
      const precision = classify(album.releaseDate);
      if (precision === "unknown") return null;
      return {
        title: album.title,
        url: album.url,
        kind: album.kind,
        sortKey: album.releaseDate,
        precision,
      };
    })
    .filter((item): item is NonNullable<typeof item> => item !== null)
    .sort((left, right) => (left.sortKey < right.sortKey ? 1 : left.sortKey > right.sortKey ? -1 : 0));
  if (sightings.length) notes.push("Deezer publishes a release date, not a time of day. No hour was added.");
  if (artist.nbAlbum !== null && albums.length < artist.nbAlbum) {
    notes.push("Album dates are the first page Deezer returned. Later pages were not fetched.");
  }
  return {
    platform: "deezer",
    status: "ok",
    reason: null,
    metrics: [countMetric("Fans", artist.nbFan), countMetric("Albums", artist.nbAlbum)],
    sightings,
    tracks,
    deezerId: artist.id,
    deezerAlbumCount: artist.nbAlbum,
    markCrawled: true,
    notices: notes,
  } as SavedSource & { notices?: string[] };
}
