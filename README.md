# Average Analytics

Public release times and post times for an artist, on Cloudflare Python Workers (FastAPI).

The product difference is the timestamp itself: exact when a source publishes a clock time, and still exact about precision when it does not. Partial dates are not turned into fake hours, and missing numbers are shown as unavailable.

## What each source does

| Source | How it is read | Timestamp | Credentials |
| --- | --- | --- | --- |
| MusicBrainz | Official WS/2 API | Earliest release date, upgraded to a full day when a release event has one | None. Be polite: the client waits 1s between calls. |
| Apple Music | iTunes Search first. If that is HTTP 403 or 429 with nothing cached, public `music.apple.com` artist and album pages. | iTunes `releaseDate`, or the album page `music:release_date`, or the shelf year | None. |
| Spotify | Public oEmbed, artist page, and embed page. Artist id comes from MusicBrainz. | Year (or whatever the page prints). Not turned into a clock time. | None. Optional Web API secrets stay off unless both are set. |
| YouTube | Data API v3. The key is sent in the `X-Goog-Api-Key` header. | `videoPublishedAt` / `publishedAt`, UTC to the second | `YOUTUBE_API_KEY`, read from the request env. Logged-in `/admin/diag` shows the type, JS typeof, length, and first 4 characters. |
| Instagram | Public `/<handle>/embed/` page only | `taken_at_timestamp` (Unix seconds, UTC) | None. The handle comes from MusicBrainz. Login redirects are not followed. |
| TikTok | Public profile JSON, public embed listing, public video pages | `createTime` on the video page (Unix seconds, UTC) | None. The handle comes from MusicBrainz. |

Instagram and TikTok handles are never guessed from the artist name. If MusicBrainz has no official URL, that source is unavailable.

When MusicBrainz has no confident match, Apple (iTunes Search) and YouTube (Data API channel search) can still match an exact public name. Those cards say "Matched by name". Spotify's public search page is a JavaScript shell with no artist ids, and oEmbed rejects search URLs, so a Spotify id is not guessed. No source invents a count.

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

The Worker config is `wrangler.jsonc` (`python_workers`, `main` = `src/main.py`, `preview_urls` false). `public/` exists so the static assets directory in that config is valid. HTML templates live in `src/templates`. Wrangler uploads `**/*.html` from that directory with the Worker (its default module rules). A `templates/` directory at the repo root is outside the bundle, which is why HTML routes 500'd after deploy. Routes are served by FastAPI (`run_worker_first`).

Styles are the prebuilt file `src/static/app.css`, served at `/static/app.css`. A Text rule with `fallthrough: true` uploads `**/*.css` without replacing the default HTML rule. `/favicon.ico` is a small inline SVG. Rebuild CSS after template class changes:

```bash
npx tailwindcss@3.4.17 -c tailwind.config.js -i input.css -o src/static/app.css --minify
```

Use `content: ["./src/templates/**/*.html"]` in that Tailwind config. The Play CDN is not used.

**Secrets**

Set these with Wrangler. Do not put the values in `wrangler.jsonc` or in git.

```bash
npx wrangler secret put SECRET_KEY
npx wrangler secret put YOUTUBE_API_KEY
```

| Secret | Required to boot | What it unlocks |
| --- | --- | --- |
| `SECRET_KEY` | No | Signs session cookies. Without it, a built-in demo secret is used so login works across isolates. Set a long random value before relying on sessions. |
| `YOUTUBE_API_KEY` | No | YouTube channel stats and exact upload timestamps. The Worker reads this secret from the request env and unwraps it to a plain string before it is placed in the Data API `key` query parameter. The value is never logged. |

Apple Music is the exact release-timestamp source and needs no key. MusicBrainz, Spotify, Instagram, and TikTok also need no key. Spotify is read from public oEmbed and artist/embed pages using the MusicBrainz artist id. Spotify Web API client-credentials (`SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`) stay off unless both are set; this deploy does not need them, because creating a Web API app requires Spotify Premium. If the YouTube secret is missing, that card says so and the rest of the dashboard still loads.

**Optional KV cache**

Successful provider results are cached for about 45 minutes (MusicBrainz URL relations and catalog for about 24 hours). Error and unavailable results are not stored in that cache, and cache keys include a version so an old error is not reused after a deploy. The cache is memory, then the Workers Cache API (`caches.default`), then KV if the `CACHE` binding exists. iTunes JSON is cached longer (about 6 hours fresh, 7 days stale) so a later 429 or 403 can say `Apple is rate-limiting; showing cached data from <time>`. MusicBrainz calls are serialized to about one per second with User-Agent `AverageAnalytics/1.0 ( callen@callensounds.com )`. To share KV across isolates:

```bash
npx wrangler kv namespace create CACHE
```

Paste the id into the commented `kv_namespaces` block in `wrangler.jsonc` (binding name `CACHE`) and deploy again. A missing or broken KV binding falls back to memory.

## Caveats

- MusicBrainz dates are calendar dates. The weekday chart uses day-level dates only. The catalog sample is the first 100 release groups and 100 official releases.
- iTunes `releaseDate` is an absolute UTC time. On the US storefront it is often midnight Pacific (07:00 or 08:00 UTC). That spike is the storefront convention, not fan activity. Cloudflare egress is often answered with HTTP 429 or 403. The card then shows cached albums, or asks you to try again shortly. There is no authenticated Apple API.
- Providers run concurrently after MusicBrainz has returned URL relations. Each source has a 15 second timeout. The MusicBrainz catalog (release groups and releases) overlaps the other providers.
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
