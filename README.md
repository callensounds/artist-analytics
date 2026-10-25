# Artist Analytics

Public release times and post times for an artist, on Cloudflare Python Workers (FastAPI).

The product difference is the timestamp itself: exact when a source publishes a clock time, and still exact about precision when it does not. Partial dates are not turned into fake hours, and missing numbers are shown as unavailable.

## What each source does

| Source | How it is read | Timestamp | Credentials |
| --- | --- | --- | --- |
| MusicBrainz | Official WS/2 API | Earliest release date, upgraded to a full day when a release event has one | None. Be polite: the client waits 1s between calls. |
| Apple Music | Keyless iTunes Search and Lookup (`itunes.apple.com`) | `releaseDate` as a full UTC timestamp | None. Lookup uses the US storefront. |
| Spotify | Public oEmbed, artist page, and embed page. Artist id comes from MusicBrainz. | Year (or whatever the page prints). Not turned into a clock time. | None. Optional Web API secrets stay off unless both are set. |
| YouTube | Data API v3 | `videoPublishedAt` / `publishedAt`, UTC to the second | `YOUTUBE_API_KEY` |
| Instagram | Public `/<handle>/embed/` page only | `taken_at_timestamp` (Unix seconds, UTC) | None. The handle comes from MusicBrainz. Login redirects are not followed. |
| TikTok | Public profile JSON, public embed listing, public video pages | `createTime` on the video page (Unix seconds, UTC) | None. The handle comes from MusicBrainz. |

Instagram and TikTok handles are never guessed from the artist name. If MusicBrainz has no official URL, that source is unavailable.

Spotify top tracks expose popularity only. They are not plotted as posts. Follower counts that a source does not publish stay unavailable.

Demo login: `artist@demo.com` / `demo123`.

Accounts are in memory and reset when the worker restarts. Search any artist from the dashboard; you do not need a matching account name.

## Local development

```bash
uv sync
cp .dev.vars.example .dev.vars   # optional secrets
uv run pywrangler dev
```

`pywrangler dev` reads `.dev.vars`. You can also run the FastAPI app directly:

```bash
uv run uvicorn main:app --app-dir src --host 127.0.0.1 --port 8000
```

Tests:

```bash
uv run pytest
```

## Deploy

Cloudflare Workers Builds (Git-connected) should deploy this repo from `main`.

**Build / deploy command**

```bash
uv run pywrangler deploy
```

The Worker config is `wrangler.jsonc` (`python_workers`, `main` = `src/main.py`). `public/` exists so the static assets directory in that config is valid. Routes are served by FastAPI (`run_worker_first`).

**Secrets**

Set these with Wrangler. Do not put the values in `wrangler.jsonc` or in git.

```bash
npx wrangler secret put SECRET_KEY
npx wrangler secret put YOUTUBE_API_KEY
```

| Secret | Required to boot | What it unlocks |
| --- | --- | --- |
| `SECRET_KEY` | No | Signs session cookies. Without it, a built-in demo secret is used so login works across isolates. Set a long random value before relying on sessions. |
| `YOUTUBE_API_KEY` | No | YouTube channel stats and exact upload timestamps. |

Apple Music is the exact release-timestamp source and needs no key. MusicBrainz, Spotify, Instagram, and TikTok also need no key. Spotify is read from public oEmbed and artist/embed pages using the MusicBrainz artist id. Spotify Web API client-credentials (`SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`) stay off unless both are set; this deploy does not need them, because creating a Web API app requires Spotify Premium. If the YouTube secret is missing, that card says so and the rest of the dashboard still loads.

**Optional KV cache**

Responses are cached in memory for about 30 minutes. To share that cache across isolates:

```bash
npx wrangler kv namespace create CACHE
```

Paste the id into the commented `kv_namespaces` block in `wrangler.jsonc` (binding name `CACHE`) and deploy again. A missing or broken KV binding falls back to memory.

## Caveats

- MusicBrainz dates are calendar dates. The weekday chart uses day-level dates only. The catalog sample is the first 100 release groups and 100 official releases.
- iTunes `releaseDate` is an absolute UTC time. On the US storefront it is often midnight Pacific (07:00 or 08:00 UTC). That spike is the storefront convention, not fan activity.
- Spotify's public artist page lists albums and singles with a year. That year is kept. The page's monthly listener count is shown only when Spotify prints it. Top tracks come from the embed payload, with play counts from the artist page when those rows are present. Undocumented anonymous Web API token endpoints are not called. If both optional Web API secrets are set, that official client-credentials path is used instead and still preserves `release_date_precision`.
- YouTube charts the 50 most recent public uploads. Search costs quota; channel ids from MusicBrainz avoid a search when they exist.
- Instagram's profile URL often redirects to a login wall. This app does not follow that redirect, does not solve challenges, and only reads the public embed page. That page includes a handful of `taken_at_timestamp` values and a follower count. The markup is not a stable API and can change or start returning a wall; the card then says unavailable.
- TikTok oEmbed identifies a creator or video and does not include follower counts or `createTime`. Those come from the public profile payload and public video pages (up to 6 videos from the public embed listing). Video ids are not decoded as snowflake timestamps. Markup changes and challenge pages become an unavailable state.
- Scraping public Instagram and TikTok HTML may still conflict with those sites' terms even when the request is logged-out, identified, cached, and rate-limited. Treat those two cards as best-effort.
- There is no artist OAuth. This is a public-data view, closer to a timestamp-focused Chartmetric than to a creator login.

## Next steps

- Persist accounts in D1 (do not add a placeholder database id to `wrangler.jsonc`; an unknown id fails deploy).
- Turn on the KV `CACHE` binding in production.
- Add the YouTube secret so that card goes live. Spotify already reads public pages without a secret.
- Let a viewer switch Apple's clock between UTC and the storefront timezone.
- Page further back through TikTok and Instagram than the public embed pages allow.
