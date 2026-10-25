# Artist Analytics — Cloudflare Workers Version

This is a Cloudflare Python Workers (FastAPI) version of the artist analytics website.  
It can be deployed to your Cloudflare account and connected to a Git repository for continuous deploys on every push.

## Features
- Same landing, login, register, and dashboard as the local version
- Live MusicBrainz release dates with exact timestamps
- Demo metrics styled after industry tools
- Runs on the edge via Cloudflare Python Workers (GA)

## Prerequisites
- Cloudflare account
- Node.js + `uv` (or follow Cloudflare’s Python Workers setup)
- Optional: GitHub repo connected to Cloudflare Workers Builds for continuous edit → view

## Local Development
```bash
# Install uv if needed: https://docs.astral.sh/uv/
uv sync
uv run pywrangler dev
```
Visit the local URL shown (usually http://localhost:8787).

## Deploy to Cloudflare
1. Authenticate: `npx wrangler login` (or `uv run pywrangler` equivalent).
2. Deploy:
   ```bash
   uv run pywrangler deploy
   ```
3. Your site will be live at `https://artist-analytics.<your-subdomain>.workers.dev`.

## Continuous Editing & Viewing
**Recommended workflow:**
1. Push this project to a GitHub repository.
2. In the Cloudflare dashboard → Workers & Pages → Create application → Connect Git repository.
3. Select the repo, set the build/deploy command to `uv run pywrangler deploy` (or use the Workers Git integration which auto-detects).
4. Every push to the main branch automatically deploys. Preview deployments appear for pull requests.
5. You (or I, once GitHub is connected) can edit the code in the repo; the live site updates within minutes.

You can also edit directly in the Cloudflare dashboard editor for small changes, but Git is better for continuous collaboration.

## Notes / Limitations of this Beta
- User accounts are in-memory (reset on cold start). For production, switch to Cloudflare D1 or KV.
- Sessions use cookies via Starlette middleware (works on Workers).
- Templates are loaded from the `templates/` directory (included in the Worker bundle).
- Add secrets/env vars in the Cloudflare dashboard for YouTube/Spotify keys later.
- Static assets can be placed in `public/` if you expand the frontend.

## Next Steps
- Connect your GitHub so I can push updates directly.
- Add D1 for persistent users.
- Integrate more live data sources.
- Custom domain via Cloudflare.

Demo login after deploy: `artist@demo.com` / `demo123`
