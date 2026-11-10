/** Marker-window parsers. Same needles and caps as the Python providers. */

export const FETCH_BYTES = 260_000;
export const SHELF_WINDOW = 80_000;
export const MB_USER_AGENT = "AverageAnalytics/1.0 ( callen@callensounds.com )";
export const HTTP_USER_AGENT = "ArtistAnalytics/0.2 (+https://github.com/callensounds/artist-analytics)";

export interface RelatedArtist {
  name: string;
  spotify_id: string;
  rank: number;
}

export interface Sighting {
  title: string;
  url: string;
  kind: string;
  sortKey: string;
  precision: string;
}

export function normalizeName(value: string): string {
  return (value || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
}

export function namesMatch(left: string, right: string): boolean {
  const a = normalizeName(left);
  const b = normalizeName(right);
  return Boolean(a && b && a === b);
}

export function sanitizeQuery(value: string): string {
  const cleaned = (value || "")
    .replace(/[+\-!(){}\[\]^"~*?:\\/]|&&|\|\|/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return cleaned.slice(0, 120);
}

export function compactCount(value: number | null): string | null {
  if (value === null || value < 0) return null;
  const scales: [number, string][] = [
    [1_000_000_000, "B"],
    [1_000_000, "M"],
    [1_000, "K"],
  ];
  for (const [size, suffix] of scales) {
    if (value >= size) {
      const text = (value / size).toFixed(1).replace(/\.0$/, "");
      return `${text}${suffix}`;
    }
  }
  return String(value);
}

export function spotifyArtistId(url: string): string {
  const match = /open\.spotify\.com\/artist\/([A-Za-z0-9]+)/.exec(url || "");
  return match?.[1] ?? "";
}

export function appleArtistId(url: string): string {
  const match = /(?:music|itunes)\.apple\.com\/.*\/artist\/(?:[^/]+\/)?(\d+)/.exec(url || "");
  return match?.[1] ?? "";
}

export function instagramHandle(url: string): string {
  const match = /instagram\.com\/([A-Za-z0-9._]+)\/?/.exec(url || "");
  if (!match) return "";
  const handle = match[1];
  if (["p", "reel", "reels", "explore", "accounts", "embed", "stories"].includes(handle.toLowerCase())) {
    return "";
  }
  return handle;
}

export function tiktokHandle(url: string): string {
  const match = /tiktok\.com\/@([A-Za-z0-9._]+)/.exec(url || "");
  return match?.[1] ?? "";
}

export function youtubeChannelId(url: string): string {
  if ((url || "").includes("music.youtube.com")) return "";
  const match = /youtube\.com\/channel\/([A-Za-z0-9_-]+)/.exec(url || "");
  return match?.[1] ?? "";
}

export function deezerArtistId(url: string): string {
  const match = /deezer\.com\/(?:[a-z]{2}\/)?artist\/(\d+)/i.exec(url || "");
  return match?.[1] ?? "";
}

export interface MbLinks {
  spotify_id: string;
  apple_id: string;
  instagram: string;
  tiktok: string;
  deezer_id: string;
  youtube_channel_ids: string[];
  youtube_handles: string[];
  _mbid: string;
  _needs_lookup: string;
}

export function emptyLinks(): MbLinks {
  return {
    spotify_id: "",
    apple_id: "",
    instagram: "",
    tiktok: "",
    deezer_id: "",
    youtube_channel_ids: [],
    youtube_handles: [],
    _mbid: "",
    _needs_lookup: "",
  };
}

export function extractLinks(relations: { url?: { resource?: string } }[]): MbLinks {
  const links = emptyLinks();
  const seen = new Set<string>();
  for (const relation of relations || []) {
    const url = relation?.url?.resource || "";
    if (!links.spotify_id) links.spotify_id = spotifyArtistId(url);
    if (!links.apple_id) links.apple_id = appleArtistId(url);
    if (!links.instagram) links.instagram = instagramHandle(url);
    if (!links.tiktok) links.tiktok = tiktokHandle(url);
    if (!links.deezer_id) links.deezer_id = deezerArtistId(url);
    const channel = youtubeChannelId(url);
    if (channel && !seen.has(channel)) {
      seen.add(channel);
      links.youtube_channel_ids.push(channel);
    }
    const handle = /youtube\.com\/@([A-Za-z0-9._-]+)/.exec(url)?.[1];
    if (handle && !links.youtube_handles.includes(handle)) links.youtube_handles.push(handle);
  }
  return links;
}

function dropBlocks(text: string, startTag: string, endTag: string): string {
  if (!text) return "";
  const startL = startTag.toLowerCase();
  const endL = endTag.toLowerCase();
  const lowered =
    startTag === startTag.toLowerCase() && !text.includes(startTag.toUpperCase()) && !text.includes(startTag[0].toUpperCase() + startTag.slice(1).toLowerCase())
      ? text
      : text.toLowerCase();
  const parts: string[] = [];
  let cursor = 0;
  while (cursor < text.length) {
    const start = lowered.indexOf(startL, cursor);
    if (start < 0) {
      parts.push(text.slice(cursor));
      break;
    }
    parts.push(text.slice(cursor, start));
    const end = lowered.indexOf(endL, start + startL.length);
    if (end < 0) break;
    cursor = end + endL.length;
  }
  return parts.join("");
}

function stripTags(text: string): string {
  const parts: string[] = [];
  let cursor = 0;
  while (cursor < text.length) {
    const start = text.indexOf("<", cursor);
    if (start < 0) {
      parts.push(text.slice(cursor));
      break;
    }
    parts.push(text.slice(cursor, start));
    const end = text.indexOf(">", start + 1);
    if (end < 0) break;
    parts.push("\n");
    cursor = end + 1;
  }
  return parts.join("");
}

export function visibleLines(page: string): string[] {
  let text = dropBlocks(page || "", "<script", "</script>");
  text = dropBlocks(text, "<style", "</style>");
  text = stripTags(text);
  const lines: string[] = [];
  for (const line of text.split("\n")) {
    const cleaned = line.replace(/\s+/g, " ").trim();
    if (cleaned) lines.push(cleaned);
  }
  return lines;
}

export function listenerSlices(page: string): string {
  if (!page) return "";
  const needles = ["monthly listeners", "Monthly listeners", "Monthly Listeners"];
  const found: [number, string][] = [];
  for (const needle of needles) {
    let start = 0;
    while (found.length < 8) {
      const at = page.indexOf(needle, start);
      if (at < 0) break;
      found.push([at, needle]);
      start = at + needle.length;
    }
  }
  found.sort((a, b) => a[0] - b[0]);
  return found
    .slice(0, 8)
    .map(([at, needle]) => page.slice(Math.max(0, at - 48), at + needle.length + 8))
    .join("\n");
}

export function shelfWindow(page: string): string {
  if (!page) return "";
  let start = page.indexOf("monthly-listeners-label");
  if (start < 0) {
    for (const needle of ["monthly listeners", "Monthly listeners", "Monthly Listeners"]) {
      start = page.indexOf(needle);
      if (start >= 0) break;
    }
  }
  if (start < 0) return page.slice(0, SHELF_WINDOW);
  return page.slice(Math.max(0, start - 2000), start + SHELF_WINDOW);
}

export function parseMonthlyListeners(slices: string): { display: string | null; exact: number | null } {
  const exacts: number[] = [];
  for (const match of slices.matchAll(/([\d,]{4,})\s+monthly listeners/g)) {
    exacts.push(Number(match[1].replace(/,/g, "")));
  }
  if (exacts.length) {
    const exact = Math.max(...exacts);
    return { display: exact.toLocaleString("en-US"), exact };
  }
  const rounded = /([\d.]+[KMB])\s+monthly listeners/i.exec(slices);
  if (rounded) return { display: rounded[1], exact: null };
  return { display: null, exact: null };
}

const KIND_LINE =
  /^(?:(Album|Single|EP|Compilation)\s*[•·]\s*((?:19|20)\d{2})|((?:19|20)\d{2})\s*[•·]\s*(Album|Single|EP|Compilation))$/;

export function parseDiscography(lines: string[]): { title: string; kind: string; year: string }[] {
  const found: { title: string; kind: string; year: string }[] = [];
  const seen = new Set<string>();
  let previous: string | null = null;
  for (const line of lines) {
    const match = KIND_LINE.exec(line);
    if (!match) {
      previous = line;
      continue;
    }
    const kind = match[1] || match[4];
    const year = match[2] || match[3];
    const title = previous;
    previous = line;
    if (!title || title.includes("•")) continue;
    const key = `${title.toLowerCase()}|${kind}|${year}`;
    if (seen.has(key)) continue;
    seen.add(key);
    found.push({ title, kind, year });
  }
  return found;
}

export function parseSpotifyFollowers(lines: string[]): number | null {
  for (let index = 1; index < lines.length; index++) {
    if (lines[index] !== "Followers") continue;
    const previous = lines[index - 1];
    if (/^[\d,]+$/.test(previous)) return Number(previous.replace(/,/g, ""));
  }
  return null;
}

export interface ParsedTrack {
  name: string;
  externalId: string;
  rank: number;
  popularity: number | null;
  album: string | null;
}

const TRACK_CAP = 10;

export function nameTrackId(name: string): string {
  const normalized = (name || "").toLowerCase().replace(/[^a-z0-9]+/g, "").slice(0, 80);
  return normalized ? `name:${normalized}` : "";
}

function unescapeHtml(value: string): string {
  return value
    .replace(/&amp;/g, "&")
    .replace(/&quot;/g, '"')
    .replace(/&#39;|&apos;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">");
}

function popularityScore(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0 || value > 100) return null;
  return value;
}

function popularStart(page: string): number {
  const marker = ">Popular</span>";
  const marked = page.indexOf(marker);
  if (marked >= 0) return marked;
  let cursor = 0;
  while (cursor < page.length) {
    const at = page.indexOf("Popular", cursor);
    if (at < 0) return -1;
    if (page.startsWith("Popular releases", at)) {
      cursor = at + "Popular".length;
      continue;
    }
    return at;
  }
  return -1;
}

export function popularSlice(page: string): string {
  if (!page) return "";
  const start = popularStart(page);
  if (start < 0) return "";
  const end = page.indexOf("Popular releases", start + 7);
  return page.slice(start, end >= 0 ? end : Math.min(page.length, start + 30_000));
}

function trackRow(name: string, externalId: string, rank: number, popularity: number | null, album: string | null): ParsedTrack {
  return {
    name: name.slice(0, 200),
    externalId: externalId.slice(0, 80),
    rank,
    popularity,
    album: album ? album.slice(0, 200) : null,
  };
}

export function parsePopularTracks(page: string): ParsedTrack[] {
  const chunk = popularSlice(page);
  const found: ParsedTrack[] = [];
  const seen = new Set<string>();
  const trackId = /spotify:track:([A-Za-z0-9]{10,32})-\d+"\s+aria-label="([^"]*)"/g;
  for (const match of chunk.matchAll(trackId)) {
    const externalId = match[1];
    const name = unescapeHtml(match[2]).replace(/\s+/g, " ").trim();
    if (!name || name.length > 200 || seen.has(externalId)) continue;
    seen.add(externalId);
    found.push(trackRow(name, externalId, found.length + 1, null, null));
    if (found.length >= TRACK_CAP) return found;
  }
  if (found.length) return found;
  const lines = visibleLines(chunk);
  const popularAt = lines.indexOf("Popular");
  if (popularAt < 0) return [];
  for (let index = popularAt + 1; index < lines.length && found.length < TRACK_CAP; index++) {
    const title = lines[index];
    if (title === "Popular releases" || title === "Albums" || title === "Singles and EPs") break;
    const plays = lines[index + 1] || "";
    if (!/^[\d,]+$/.test(plays)) continue;
    const externalId = nameTrackId(title);
    index += 1;
    if (!externalId || seen.has(externalId)) continue;
    seen.add(externalId);
    found.push(trackRow(title, externalId, found.length + 1, null, null));
  }
  return found;
}

export function parseApiTracks(payload: unknown): ParsedTrack[] {
  if (!payload || typeof payload !== "object") return [];
  const tracks = (payload as { tracks?: unknown }).tracks;
  if (!Array.isArray(tracks)) return [];
  const found: ParsedTrack[] = [];
  const seen = new Set<string>();
  for (const item of tracks) {
    if (found.length >= TRACK_CAP || !item || typeof item !== "object") continue;
    const track = item as { id?: unknown; name?: unknown; popularity?: unknown; album?: { name?: unknown } };
    const name = String(track.name || "").replace(/\s+/g, " ").trim();
    if (!name) continue;
    const rawId = typeof track.id === "string" ? track.id.trim() : "";
    const externalId = /^[A-Za-z0-9]{10,32}$/.test(rawId) ? rawId : nameTrackId(name);
    if (!externalId || seen.has(externalId)) continue;
    seen.add(externalId);
    const albumName = track.album && typeof track.album === "object" ? String(track.album.name || "").replace(/\s+/g, " ").trim() : "";
    found.push(trackRow(name, externalId, found.length + 1, popularityScore(track.popularity), albumName || null));
  }
  return found;
}

export function mergeApiTracks(publicTracks: ParsedTrack[], apiTracks: ParsedTrack[]): ParsedTrack[] {
  if (!apiTracks.length) return publicTracks;
  if (!publicTracks.length) return apiTracks.slice(0, TRACK_CAP);
  const byId = new Map(apiTracks.map((track) => [track.externalId, track]));
  const byName = new Map(apiTracks.map((track) => [normalizeName(track.name), track]));
  return publicTracks.slice(0, TRACK_CAP).map((track) => {
    const match = byId.get(track.externalId) || byName.get(normalizeName(track.name));
    if (!match) return track;
    return {
      name: track.name || match.name,
      externalId: match.externalId.startsWith("name:") ? track.externalId : match.externalId,
      rank: track.rank,
      popularity: match.popularity,
      album: match.album || track.album,
    };
  });
}

export function parseRelatedArtists(page: string): RelatedArtist[] {
  if (!page) return [];
  const start = page.indexOf("Fans also like");
  if (start < 0) return [];
  const end = page.indexOf("Appears On", start);
  const chunk = page.slice(start, end > start ? end : Math.min(page.length, start + 8000));
  const found: RelatedArtist[] = [];
  const seen = new Set<string>();
  let cursor = 0;
  const marker = 'href="/artist/';
  while (found.length < 12) {
    const at = chunk.indexOf(marker, cursor);
    if (at < 0) break;
    let endId = at + marker.length;
    const limit = Math.min(chunk.length, endId + 32);
    while (endId < limit && /[A-Za-z0-9]/.test(chunk[endId])) endId++;
    if (endId === at + marker.length || chunk[endId] !== '"') {
      cursor = at + marker.length;
      continue;
    }
    const artistId = chunk.slice(at + marker.length, endId);
    const after = endId + 1;
    const window = chunk.slice(after, after + 900);
    const close = window.indexOf("</span>");
    if (close < 0) {
      cursor = after;
      continue;
    }
    const openGt = window.lastIndexOf(">", close);
    if (openGt < 0) {
      cursor = after;
      continue;
    }
    const name = window
      .slice(openGt + 1, close)
      .replace(/&amp;/g, "&")
      .replace(/\s+/g, " ")
      .trim();
    cursor = after + close + "</span>".length;
    if (!name || name.includes("<") || name.length > 80 || seen.has(artistId)) continue;
    seen.add(artistId);
    found.push({ name, spotify_id: artistId, rank: found.length + 1 });
  }
  return found;
}

export function parseInstagram(page: string, handle: string): { followers: number | null; posts: Sighting[] } {
  const windows = embedWindows(page);
  const posts: Sighting[] = [];
  const seen = new Set<string>();
  let followers: number | null = null;
  const stampRe = /taken_at_timestamp\\":(\d+)/g;
  let stamp: RegExpExecArray | null;
  const source = windows || page;
  while ((stamp = stampRe.exec(source))) {
    if (posts.length >= 4) break;
    const before = source.slice(Math.max(0, stamp.index - 8000), stamp.index);
    const after = source.slice(stamp.index + stamp[0].length, stamp.index + stamp[0].length + 2500);
    const shortcodes = [...before.matchAll(/shortcode\\":\\"([^"\\]+)\\"/g)];
    const username = /username\\":\\"([^"\\]+)\\"/.exec(after);
    if (!shortcodes.length || !username) continue;
    if (username[1].toLowerCase() !== handle.toLowerCase()) continue;
    const shortcode = shortcodes[shortcodes.length - 1][1];
    if (seen.has(shortcode)) continue;
    seen.add(shortcode);
    const followerMatch =
      /edge_followed_by\\":\{\\"count\\":(\d+)/.exec(after) ||
      /edge_followed_by\\":\{\\"count\\":(\d+)/.exec(before);
    if (followerMatch && followers === null) followers = Number(followerMatch[1]);
    const unix = Number(stamp[1]);
    posts.push({
      title: shortcode,
      url: `https://www.instagram.com/p/${shortcode}/`,
      kind: "post",
      sortKey: new Date(unix * 1000).toISOString().replace(/\.\d{3}Z$/, "Z"),
      precision: "time",
    });
  }
  if (followers === null) {
    const loose = /edge_followed_by\\":\{\\"count\\":(\d+)/.exec(source);
    if (loose) followers = Number(loose[1]);
  }
  return { followers, posts };
}

function embedWindows(page: string): string {
  if (!page) return "";
  const parts: string[] = [];
  const follower = page.indexOf("edge_followed_by");
  if (follower >= 0) parts.push(page.slice(Math.max(0, follower - 400), follower + 180));
  const token = 'taken_at_timestamp\\":';
  let start = 0;
  let found = 0;
  while (found < 6) {
    const at = page.indexOf(token, start);
    if (at < 0) break;
    parts.push(page.slice(Math.max(0, at - 6000), at + 2500));
    start = at + token.length;
    found += 1;
  }
  return parts.join("\n");
}

export function leadingInt(text: string, key: string): number | null {
  const token = `"${key}":`;
  const at = (text || "").indexOf(token);
  if (at < 0) return null;
  let end = at + token.length;
  const limit = Math.min(text.length, end + 16);
  while (end < limit && text[end] >= "0" && text[end] <= "9") end++;
  if (end === at + token.length) return null;
  return Number(text.slice(at + token.length, end));
}

export function tiktokVideos(page: string): Sighting[] {
  const items: Sighting[] = [];
  const seen = new Set<string>();
  const re = /"id":"(\d{8,})"[\s\S]{0,500}?"createTime":(\d{9,})/g;
  let match: RegExpExecArray | null;
  while ((match = re.exec(page)) && items.length < 6) {
    if (seen.has(match[1])) continue;
    seen.add(match[1]);
    const unix = Number(match[2]);
    items.push({
      title: match[1],
      url: `https://www.tiktok.com/video/${match[1]}`,
      kind: "post",
      sortKey: new Date(unix * 1000).toISOString().replace(/\.\d{3}Z$/, "Z"),
      precision: "time",
    });
  }
  return items;
}

export interface AlbumSighting extends Sighting {
  artistName: string | null;
}

export function parseItunesAlbums(payload: { results?: Record<string, unknown>[] }, artistId: string): {
  artistName: string | null;
  items: Sighting[];
} {
  let artistName: string | null = null;
  const items: Sighting[] = [];
  const seen = new Set<unknown>();
  for (const row of payload.results || []) {
    if (row.wrapperType === "artist" && String(row.artistId) === String(artistId)) {
      artistName = String(row.artistName || "") || null;
      continue;
    }
    if (row.wrapperType !== "collection") continue;
    if (String(row.artistId) !== String(artistId)) continue;
    if (seen.has(row.collectionId)) continue;
    seen.add(row.collectionId);
    const raw = String(row.releaseDate || "");
    items.push({
      title: String(row.collectionName || "Untitled"),
      url: String(row.collectionViewUrl || ""),
      kind: "release",
      sortKey: raw,
      precision: raw.includes("T") ? "time" : "day",
    });
  }
  return { artistName, items };
}
