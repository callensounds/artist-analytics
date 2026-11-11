# Average Analytics

Public release times and post times for an artist, on Cloudflare Python Workers (FastAPI).

The product difference is the timestamp itself: exact when a source publishes a clock time, and still exact about precision when it does not. Partial dates are not turned into fake hours, and missing numbers are shown as unavailable.

## What each source does

| Source | How it is read | Timestamp | Credentials |
| --- | --- | --- | --- |
| MusicBrainz | Official WS/2 API. Search asks for `url-rels` and looks the artist up only when the search omits them. A 429/503 or a dropped connection is tried once more with jitter. URL relations are kept in the Cache API for 7 days and still feed Spotify, Instagram, and TikTok if a later call fails. The card includes the HTTP status and response body. | Earliest release date, upgraded to a full day when a release event has one | None. Be polite: the client waits 1s between calls. |
| Apple Music | iTunes Search first. On the dashboard, HTTP 403 or 429 with nothing cached falls through to one public album-shelf page (byte-capped) and at most 3 album pages, and only after that error has finished propagating. A crawl does not fetch that shelf. After HTTP 403 the crawler skips iTunes for 30 minutes (`crawl_state.apple_blocked_until`). | iTunes `releaseDate`, or `music:release_date` from those album pages, or the shelf year | None. |
| Spotify | Public oEmbed, artist page, and embed page. Artist id comes from MusicBrainz. | Year (or whatever the page prints). Not turned into a clock time. | None. Optional Web API secrets stay off unless both are set. |
| YouTube | Data API v3. The key is sent in the `X-Goog-Api-Key` header. A placeholder such as `[REDACTED]` is not sent. An API rejection is shown as the HTTP status and reason code (`quotaExceeded`, `API_KEY_INVALID`, `API_KEY_HTTP_REFERRER_BLOCKED`), never the key value. | `videoPublishedAt` / `publishedAt`, UTC to the second | `YOUTUBE_API_KEY`, read from the request env. Logged-in `/admin/diag` shows the type, JS typeof, length, and first 4 characters. |
| Instagram | Public `/<handle>/embed/` page only | `taken_at_timestamp` (Unix seconds, UTC) | None. The handle comes from MusicBrainz. Login redirects are not followed. |
| TikTok | Public profile JSON, public embed listing, public video pages | `createTime` on the video page (Unix seconds, UTC) | None. The handle comes from MusicBrainz. |
| Deezer | Public `api.deezer.com` JSON: search, artist, top tracks, and albums. No token. The crawler stores the chosen artist id and reads it back on later runs. The artist page reads that stored card after the rest of the history and does not call Deezer. A slow read, a failed read, or no stored Deezer row leaves the other history on the page and marks the card unavailable or pending. | `release_date` as a year, month, or day. A time is never added. | None. |

Instagram and TikTok handles are never guessed from the artist name. If MusicBrainz has no official URL, that source is unavailable.

When MusicBrainz has no confident match, Apple (iTunes Search) and YouTube (Data API channel search) can still match an exact public name. Those cards say "Matched by name". Spotify's public search page is a JavaScript shell with no artist ids, and oEmbed rejects search URLs, so a Spotify id is not guessed. No source invents a count.

Spotify top tracks are stored per artist. The public page supplies the track id and name. A 0–100 popularity score is stored only when the Web API returns one, and a missing score stays blank. Play counts are not popularity. Those tracks are not plotted as posts. Follower counts that a source does not publish stay unavailable.

Demo login: `artist@demo.com` / `demo123`. Passwords are PBKDF2-HMAC-SHA256 built from `hashlib.sha256` (Pyodide has no `pbkdf2_hmac`). New hashes use 2,000 iterations, about the same CPU as the old 10,000-iteration C call. The stored text stays `pbkdf2_sha256$rounds$salt$digest`, so an existing row still verifies, including a 10,000-iteration one. A demo row that is not that format is re-hashed on login. When D1 is bound, that demo account is inserted if it is missing and login checks the stored hash. Without D1, the same hash check uses the in-memory demo user. That hash is created on the first login, not while the Worker is starting.

Registered accounts are stored in D1 (`users`, plus a `user_artists` watchlist at `/watch`). The dashboard does not apply that migration; the crawler creates the tables on its next run, or run `npx wrangler d1 execute artist-analytics --remote --file=src/migrations/0008_users.sql`. There is no email verification. Demo login lands on `/dashboard`. That page reads stored snapshots first. It live-fetches at most two providers, and only when that provider's newest snapshot or sighting is missing or older than six hours. `/search` and `/artist/<id>` read only artists the crawler has stored in D1. `/artist/<id>/related` draws that artist's stored related-artist edges and does not call Spotify. The Momentum card is the change in stored Spotify monthly listeners, Spotify followers, Instagram followers, and TikTok followers or likes over the points that fall inside the last 24 hours and the last 7 days. The crawler writes a growth alert after it stores a new snapshot when a 24h change reaches 2% (or 50,000 monthly listeners / 5,000 followers / 10,000 TikTok likes) or a 7d change reaches 5% (or 100,000 listeners / 20,000 followers / 50,000 likes). The same pair of readings is not inserted again. Each cron tick also checks up to 5 artists who already have exact snapshots and have not been checked, writing at most 50 alerts. After every such artist has been checked, that pass does nothing. A later snapshot still alerts from the crawl message. `/alerts` lists those stored rows. `/artists` pages through tracked artists in D1, by name or by the latest exact Spotify monthly listeners. `/trending` ranks stored growth alerts by percent change, then by the absolute change, and can filter to 24h, 7d, or one provider. The artist CSV includes MusicBrainz releases and, when the crawler has stored them, a tracks section. Spotify top tracks live in `artist_tracks` (the latest list) and `track_snapshots` (a popularity score over time, only when a 0–100 score was returned). The crawler creates those tables on its next run, or run `npx wrangler d1 execute artist-analytics --remote --file=src/migrations/0010_artist_tracks.sql`. Official MusicBrainz albums, EPs, and singles are stored with the release-group first-release-date at year, month, or day precision and listed on the artist page. `/artist/<id>/calendar` shows one stored year at a time (`?year=2014` or `?from=2014-06`). Previous and next jump to the nearest years that have a stored date, and the year list is only those years. A year with no stored release says so. A month-only or year-only date is not given a day or a clock. A window with fewer than two exact readings is omitted. `/artist/<id>/export.csv` downloads stored snapshots, sightings, and related edges (5,000 rows each). The Playlists card lists stored `playlist_appearances` rows only. Spotify's Web API does not publish which playlists include an artist, so the crawler does not fill that table. A name that is not in D1 is not tracked, and that request does not call providers. `/artist/<normalized-name>` redirects to the numeric id. `artists.normalized_name` is already unique, so a prefix search can use that index.

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

The crawler binds the same D1 database, produces and consumes `artist-analytics-crawl` (`max_batch_size` 1, `max_concurrency` 1, `max_retries` 3, `retry_delay` 180, dead-letter queue `artist-analytics-crawl-dlq`), and owns the `*/15 * * * *` cron. The dashboard config only produces `CRAWL_QUEUE`. MusicBrainz is paced to one request every 1.5–2 seconds on the isolate. HTTP 429 or 503 from any provider, and a dropped connection, ack that message and send one follow-up carrying the remaining providers. The delay is 120–300 seconds of jitter, or `Retry-After` when that is longer, and never sooner than 120 seconds. Those waits are not stored as a finished error.

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

Both Workers bind the existing D1 database `artist-analytics` (`DB`, id `52a9190e-4147-402e-8f75-16f5de58bf84`, migrations in `src/migrations`). The queue is `artist-analytics-crawl`. The crawler is the consumer (`max_batch_size` 1, `max_concurrency` 1, `max_retries` 3, `retry_delay` 180) and the dashboard is only a producer. Failed deliveries go to `artist-analytics-crawl-dlq`. Wrangler creates that dead-letter queue on deploy when the name is new; if a deploy does not create queues, create `artist-analytics-crawl-dlq` once in the dashboard before relying on it. The crawler's cron is `*/15 * * * *` UTC. The deploy command does not run `wrangler d1 migrations apply`. Migration `0001_crawl.sql` is already applied on the remote database; do not rename or edit it. A later schema change is a new file under `src/migrations`. The crawler's scheduled handler and queue consumer apply any migration file not yet recorded in `schema_migrations`. The dashboard request does not. Every statement in `0001_crawl.sql` is `CREATE TABLE IF NOT EXISTS` or `CREATE INDEX IF NOT EXISTS`, so recording it again does not change the remote schema.

Each cron invocation enqueues one `{artist_id, provider: "musicbrainz"}` message per stale artist, up to `CRAWL_ENQUEUE_LIMIT` (default 10). That message runs the artist in one invocation: MusicBrainz, then Spotify, Instagram, TikTok, Apple, YouTube when a key is set, Deezer, and Deezer playlists. A MusicBrainz search that omits URL relations looks the id up in the same invocation. Each provider reads the stored ids, does its fetch and parse, and writes as it finishes, so a later failure keeps the earlier snapshots. A provider with a snapshot younger than 3 hours is skipped. A missing link skips that provider. Apple is skipped during its 30 minute backoff. A retryable failure acks the message and sends one `{artist_id, providers: [...]}` follow-up for the steps still left. An artist is stale 6 hours after `last_crawled_at` (`CRAWL_STALE_SECONDS`). A MusicBrainz error does not mark the artist fresh and does not skip the other providers. If a queue send hits the daily write limit, that tick records the error once and still runs alert backfill and chart ingest. If `DB` is missing, the cron logs that it skipped and does not invent snapshots. If the queue binding is missing, the cron still crawls one artist in process. Local `uvicorn` uses `data/artist-analytics.sqlite` when it is not running under pytest.

Each cron tick also stores the public US iTunes top songs and top albums charts when that UTC day's newest row is older than 6 hours. The feeds are `https://itunes.apple.com/us/rss/topsongs/limit=100/json` and `https://itunes.apple.com/us/rss/topalbums/limit=100/json`. A feed error is logged and does not stop the artist enqueue. Rows match a tracked artist by the stored Apple artist id, or by an exact case-insensitive `artists.name` when that name belongs to one tracked artist. A different name is not linked. `/charts` and the artist page read `chart_entries` from D1 and do not fetch the feeds. Spotify charts are not collected: there is no public chart JSON that works without a login, and this app does not scrape charts.spotify.com.

The audience score is the sum of log10 of the latest exact stored counts, each term rounded to 2 decimal places. The counts are Spotify monthly listeners, Spotify followers, Instagram followers, TikTok followers, TikTok likes, YouTube subscribers, and Deezer fans. A missing source is left out. Zero and rounded text such as 1.2M are left out. Nothing is estimated. Deezer fan counts are stored by the crawler as `metric_snapshots` rows (`source` `deezer`, metric `Fans`). Album count is the latest `nb_album` on the artists row, not an audience series. The artist page shows the score and each term. `/artists` shows it for the artists on the page. `/scores` ranks tracked artists that have a score. Those latest counts are kept in `metric_latest` (one row per artist, source, and metric) so a page does not scan `metric_snapshots`. The number is still computed from the stored count. It is not estimated.

Migration `0011_read_budget.sql` adds that table, plus `source_freshness`, `audience_artists`, and `chart_freshness`, and the indexes the per-artist history reads use. The crawler applies it on the next scheduled or queue run. To apply it before that run:

```bash
npx wrangler d1 execute artist-analytics --remote --file=src/migrations/0011_read_budget.sql
```

Run that file once. If the crawler has already recorded `0011_read_budget.sql` in `schema_migrations`, skip the command.

Migration `0012_deezer.sql` adds `artists.deezer_id` and `artists.deezer_nb_album`. The crawler applies it on the next scheduled or queue run. To apply it before that run:

```bash
npx wrangler d1 execute artist-analytics --remote --file=src/migrations/0012_deezer.sql
```

Run that file once. If `0012_deezer.sql` is already in `schema_migrations`, skip the command.

Searching an artist on the dashboard does not write D1. It enqueues one `{name, origin: "search", provider: "musicbrainz"}` message on `CRAWL_QUEUE` (awaited, 150ms timeout, failures ignored) and may read growth with a single indexed query. The crawler runs the other providers in that same message. Snapshots and Spotify "Fans also like" edges are written there. A fresh artist is acked without another identity crawl, so a burst does not crawl the same name again. Related artists in the top 5 are auto-tracked until `RELATED_AUTOTRACK_CAP` (default 25). Set the cap to `0` to keep the edges without tracking anyone new. The public Spotify page's "Fans also like" list is parsed only on the Spotify crawl message, not while rendering the dashboard. Logged-in users can open `/admin` to see runs and refresh one batch.

Audience charts plot only exact integers (monthly listeners, followers, subscribers). A rounded `1.2M` string is not turned into a point. First-seen is when this app stored the row. The source's own timestamp stays separate.

## Caveats

- MusicBrainz dates are calendar dates. The weekday chart uses day-level dates only. The catalog sample is the first 100 release groups and 100 official releases.
- iTunes `releaseDate` is an absolute UTC time. On the US storefront it is often midnight Pacific (07:00 or 08:00 UTC). That spike is the storefront convention, not fan activity. Cloudflare egress is often answered with HTTP 429 or 403. The dashboard then shows cached albums, the public shelf, or asks you to try again shortly. A crawl records the 403 and skips iTunes for 30 minutes instead of fetching the shelf. There is no authenticated Apple API. Awaiting a JS promise from inside `except`, `finally`, or while that exception is still propagating raises `SystemError: Cannot enter a promising task from inside another running promising task` and fails the whole crawl.
- Providers run concurrently after MusicBrainz has returned URL relations. Each source has a 15 second timeout. The MusicBrainz catalog (release groups and releases) overlaps the other providers.
- Spotify's public artist page lists albums and singles with a year. That year is kept. The page's monthly listener count is shown only when Spotify prints it. The crawler stores the Popular shelf (track id and name). Play counts on that shelf are not a 0–100 popularity score, so popularity stays empty unless `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` are both set on the crawler. Those secrets call the official top-tracks endpoint and fill popularity and album name. They are not required. Undocumented anonymous Web API token endpoints are not called. The artist page lists the stored tracks and does not keep a second comma-joined top-track metric.
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
