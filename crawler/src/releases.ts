/** Official MusicBrainz release groups. Dates stay at the precision MusicBrainz sent. */

const PRIMARY = new Set(["Album", "EP", "Single"]);
const EXCLUDED_SECONDARY = new Set(["Bootleg", "Demo", "Interview", "Audiobook"]);
export const RELEASE_CAP = 50;

export interface MbRelease {
  mbid: string;
  title: string;
  releaseType: string;
  releasedAt: string;
  precision: "year" | "month" | "day";
}

export function releaseDate(value: unknown): { releasedAt: string; precision: "year" | "month" | "day" } | null {
  const text = String(value || "").trim();
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return { releasedAt: text, precision: "day" };
  if (/^\d{4}-\d{2}$/.test(text)) return { releasedAt: text, precision: "month" };
  if (/^\d{4}$/.test(text)) return { releasedAt: text, precision: "year" };
  return null;
}

export function parseReleaseGroups(payload: unknown): MbRelease[] {
  const groups = (payload as { "release-groups"?: Record<string, unknown>[] } | null)?.["release-groups"] || [];
  const found: MbRelease[] = [];
  for (const group of groups) {
    const primary = String(group["primary-type"] || "");
    if (!PRIMARY.has(primary)) continue;
    const secondary = Array.isArray(group["secondary-types"]) ? group["secondary-types"].map((item) => String(item)) : [];
    if (secondary.some((item) => EXCLUDED_SECONDARY.has(item))) continue;
    const dated = releaseDate(group["first-release-date"]);
    const mbid = String(group.id || "").trim();
    const title = String(group.title || "").trim();
    if (!dated || !mbid || !title) continue;
    found.push({
      mbid: mbid.slice(0, 64),
      title: title.slice(0, 200),
      releaseType: secondary.length ? `${primary} · ${secondary.join(", ")}`.slice(0, 80) : primary,
      releasedAt: dated.releasedAt,
      precision: dated.precision,
    });
    if (found.length >= RELEASE_CAP) break;
  }
  return found;
}
