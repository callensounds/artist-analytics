# Average Analytics

Public release times and post times for an artist, on Cloudflare Python Workers (FastAPI).

The product difference is the timestamp itself: exact when a source publishes a clock time, and still exact about precision when it does not. Partial dates are not turned into fake hours, and missing numbers are shown as unavailable.

## What each source does

| Source | How it is read | Timestamp | Credentials |
| --- | --- | --- | --- |
| MusicBrainz | Official WS/2 API. Search asks for `url-rels` and looks the artist up only when the search omits them. A 429/503 or a dropped connection is tried once more with jitter. URL relations are kept in the Cache API for 7 days and still feed Spotify, Instagram, and TikTok if a later call fails. The card includes the HTTP status and response body. | Earliest release date, upgraded to a full day when a release event has one | None. Be polite: the client waits 1s between calls. |
| Apple Music | iTunes Search first. On the dashboard, HTTP 403 or 429 with nothing cached falls through to one public album-shelf page (byte-capped) and at most 3 album pages, and only after that error has finished propagating. A crawl does not fetch that shelf. After HTTP 403 the crawler skips iTunes for 30 minutes (`crawl_state.apple_blocked_until`). | iTunes `releaseDate`, or `music:release_date` from those album pages, or the shelf year | None. |
| Spotify | Public oEmbed, artist page, and embed page. Artist id comes from MusicBrainz. | Year (or whatever the page prints). Not turned into a clock time. | None. Optional Web API secrets stay off unless both are set. |
| YouTube | Data API v3. The key is sent in the `X-Goog-Api-Key` header. A value of `[REDACTED]`, or any value that does not start with `AIza`, is not sent to Google. | `videoPublishedAt` / `publishedAt`, UTC to the second | `YOUTUBE_API_KEY`, read from the request env. Logged-in `/admin/diag` shows the type, JS typeof, length, and first 4 characters. |
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

The dashboard and the crawler are separate Workers Builds triggers. Workers Builds overrides the Worker name from the trigger, so the crawler needs its own trigger or the upload is renamed to `artist-analytics`.

Dashboard trigger: root directory `/`, build command `uv sync`, deploy command `uv run pywrangler deploy`. Config is `wrangler.jsonc` (`python_workers`, `main` = `src/main.py`).

Crawler trigger `72c7852f` (`artist-analytics-crawler`): root directory `/`, build command `npm ci`, deploy command:

```bash
npx wrangler deploy -c wrangler.crawler.jsonc
```

`wrangler.crawler.jsonc` points at the TypeScript Worker `crawler/src/index.ts` and sets `"workers_dev": false`. It is not a Python Worker. A Free-plan CPU kill leaves a Pyodide isolate failing every later invocation, so the consumer runs in JavaScript. The dashboard trigger stays Python (`uv sync`, then `uv run pywrangler deploy`, config `wrangler.jsonc`). A root `package.json` makes the dashboard's Previews check run `npm` as well; that is expected.

The crawler binds the same D1 database, produces and consumes `artist-analytics-crawl` (`max_batch_size` 1, `max_concurrency` 1, `max_retries` 10, `retry_delay` 180, dead-letter queue `artist-analytics-crawl-dlq`), and owns the `*/15 * * * *` cron. The dashboard config only produces `CRAWL_QUEUE`.

A queue has one consumer. Deploy the dashboard (which no longer lists a consumer) before the first crawler deploy. If the crawler deploy says the queue already has a consumer:

```bash
npx wrangler queues consumer worker remove artist-analytics-crawl artist-analytics
npx wrangler deploy -c wrangler.crawler.jsonc
```

`public/` exists so the dashboard asset directory is valid. HTML templates live in `src/templates` and the crawl schema lives in `src/migrations`. Routes are served by FastAPI (`run_worker_first`). The crawler entrypoint does not import FastAPI or Jinja.

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
npx wrangler secret put YOUTUBE_API_KEY -c wrangler.crawler.jsonc
```

Secrets are per Worker. `YOUTUBE_API_KEY` has to be set on `artist-analytics-crawler` as well as the dashboard, or the YouTube provider message stays skipped. Set `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` on the crawler too if they are set on the dashboard. `SECRET_KEY` is only the dashboard session cookie. `wrangler secret put` redeploys that Worker; set the crawler secrets after the crawler code deploy so the secret version keeps the new consumer.

| Secret | Required to boot | What it unlocks |
| --- | --- | --- |
| `SECRET_KEY` | No | Signs session cookies. Without it, a built-in demo secret is used so login works across isolates. Set a long random value before relying on sessions. |
| `YOUTUBE_API_KEY` | No | YouTube channel stats and exact upload timestamps. The Worker reads this secret from the request env and unwraps it to a plain string before it is placed in the Data API `key` query parameter. The value is never logged. |

Apple Music is the exact release-timestamp source and needs no key. MusicBrainz, Spotify, Instagram, and TikTok also need no key. Spotify is read from public oEmbed and artist/embed pages using the MusicBrainz artist id. Spotify Web API client-credentials (`SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`) stay off unless both are set; this deploy does not need them, because creating a Web API app requires Spotify Premium. If the YouTube secret is missing, that card says so and the rest of the dashboard still loads.

**Optional KV cache**

Successful provider results are cached for about 45 minutes (MusicBrainz URL relations and catalog for about 24 hours). Error and unavailable results are not stored in that cache, and cache keys include a version so an old error is not reused after a deploy. The cache is memory, then the Workers Cache API (`caches.default`), then KV if the `CACHE` binding exists. iTunes JSON is cached longer (about 6 hours fresh, 7 days stale) so a later 429 or 403 can say `Apple is rate-limiting; showing cached data from <time>`. MusicBrainz calls are serialized to about one per second with User-Agent `AverageAnalytics/1.0 ( callen@callensounds.com )`. The serializer is a lock created on first use and stored on the isolate's event loop, not a lock built at import. A queue crawl waits longer for a slot than a dashboard does. If MusicBrainz times out on a dashboard, Spotify, Instagram, and TikTok still use ids already stored on the `artists` row. To share KV across isolates:

```bash
npx wrangler kv namespace create CACHE
```

Paste the id into the commented `kv_namespaces` block in `wrangler.jsonc` (binding name `CACHE`) and deploy again. A missing or broken KV binding falls back to memory.

## Crawl

Both Workers bind the existing D1 database `artist-analytics` (`DB`, id `52a9190e-4147-402e-8f75-16f5de58bf84`, migrations in `src/migrations`). The queue is `artist-analytics-crawl`. The crawler is the consumer (`max_batch_size` 1, `max_concurrency` 1, `max_retries` 10, `retry_delay` 180) and the dashboard is only a producer. Failed deliveries go to `artist-analytics-crawl-dlq`. Wrangler creates that dead-letter queue on deploy when the name is new; if a deploy does not create queues, create `artist-analytics-crawl-dlq` once in the dashboard before relying on it. The crawler's cron is `*/15 * * * *` UTC. The deploy command does not run `wrangler d1 migrations apply`. Migration `0001_crawl.sql` is already applied on the remote database; do not rename or edit it. A later schema change is a new file under `src/migrations`. The crawler's scheduled handler and queue consumer apply any migration file not yet recorded in `schema_migrations`. The dashboard request does not. Every statement in `0001_crawl.sql` is `CREATE TABLE IF NOT EXISTS` or `CREATE INDEX IF NOT EXISTS`, so recording it again does not change the remote schema.

Each cron invocation enqueues one `{artist_id, provider: "musicbrainz"}` message per stale artist, up to `CRAWL_ENQUEUE_LIMIT` (default 10). That message does a single MusicBrainz search. If the search omits URL relations, it enqueues one lookup message (`mbid` set) instead of calling MusicBrainz again in the same invocation. When the ids are known, they are stored on the `artists` row and the handler enqueues one message per later provider: `spotify`, `instagram`, `tiktok`, `apple`, and `youtube` when a key is set. Each of those reads the stored ids, does one fetch and parse, and writes one `db.batch`. A provider with a snapshot younger than 3 hours is acked without a fetch. An artist is stale 6 hours after `last_crawled_at` (`CRAWL_STALE_SECONDS`). A MusicBrainz error does not mark the artist fresh. If `DB` is missing, the cron logs that it skipped and does not invent snapshots. If the queue binding is missing, the cron still crawls one artist in process. Local `uvicorn` uses `data/artist-analytics.sqlite` when it is not running under pytest.

Searching an artist on the dashboard does not write D1. It enqueues one `{name, origin: "search", provider: "musicbrainz"}` message on `CRAWL_QUEUE` (awaited, 150ms timeout, failures ignored) and may read growth with a single indexed query. The MusicBrainz message fans the other providers out. Snapshots and Spotify "Fans also like" edges are written by those provider messages. A fresh artist is acked without another identity crawl, so a burst does not fan the same name out. Related artists in the top 5 are auto-tracked until `RELATED_AUTOTRACK_CAP` (default 25). Set the cap to `0` to keep the edges without tracking anyone new. The public Spotify page's "Fans also like" list is parsed only on the Spotify crawl message, not while rendering the dashboard. Logged-in users can open `/admin` to see runs and refresh one batch.

Audience charts plot only exact integers (monthly listeners, followers, subscribers). A rounded `1.2M` string is not turned into a point. First-seen is when this app stored the row. The source's own timestamp stays separate.

## Caveats

- MusicBrainz dates are calendar dates. The weekday chart uses day-level dates only. The catalog sample is the first 100 release groups and 100 official releases.
- iTunes `releaseDate` is an absolute UTC time. On the US storefront it is often midnight Pacific (07:00 or 08:00 UTC). That spike is the storefront convention, not fan activity. Cloudflare egress is often answered with HTTP 429 or 403. The dashboard then shows cached albums, the public shelf, or asks you to try again shortly. A crawl records the 403 and skips iTunes for 30 minutes instead of fetching the shelf. There is no authenticated Apple API. Awaiting a JS promise from inside `except`, `finally`, or while that exception is still propagating raises `SystemError: Cannot enter a promising task from inside another running promising task` and fails the whole crawl.
- Providers run concurrently after MusicBrainz has returned URL relations. Each source has a 15 second timeout. The MusicBrainz catalog (release groups and releases) overlaps the other providers.
- Spotify's public artist page lists albums and singles with a year. That year is kept. The page's monthly listener count is shown only when Spotify prints it. Top tracks come from the embed payload, with play counts from the artist page when those rows are present. Undocumented anonymous Web API token endpoints are not called. If both optional Web API secrets are set, that official client-credentials path is used instead and still preserves `release_date_precision`.
- YouTube charts the 50 most recent public uploads. Search costs quota; channel ids from MusicBrainz avoid a search when they exist.
- Instagram's profile URL often redirects to a login wall. This app does not follow that redirect, does not solve challenges, and only reads the public embed page. That page includes a handful of `taken_at_timestamp` values and a follower count. The markup is not a stable API and can change or start returning a wall; the card then says unavailable.
- TikTok oEmbed identifies a creator or video and does not include follower counts or `createTime`. Those come from the public profile payload and public video pages (up to 6 videos from the public embed listing). Video ids are not decoded as snowflake timestamps. Markup changes and challenge pages become an unavailable state.
- Scraping public Instagram and TikTok HTML may still conflict with those sites' terms even when the request is logged-out, identified, cached, and rate-limited. Treat those two cards as best-effort.
- There is no artist OAuth. This is a public-data view, closer to a timestamp-focused Chartmetric than to a creator login.

## Next steps

- Accounts are still in memory. Crawl history uses the bound D1 database.
- Add a new file under `src/migrations/` for any schema change. Do not edit `0001_crawl.sql`.
- Turn on the KV `CACHE` binding in production.
- Add the YouTube secret so that card goes live. Spotify already reads public pages without a secret.
- Let a viewer switch Apple's clock between UTC and the storefront timezone.
- Page further back through TikTok and Instagram than the public embed pages allow.
