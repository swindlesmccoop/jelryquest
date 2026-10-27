# JelryQuest

Web UI to search movies and TV shows (Cinemeta + Torrentio), pick a stream, and
download it through Real-Debrid straight to your media folders.

Previous implementation (Tailscale/VPS egress + addon bridge) lives on the
`legacy` branch.

## Features

- **Movie / Show** toggle with poster search; shows drill into an episode list.
- Streams (first 10 from Torrentio) show magnet, Real-Debrid and direct links.
- Magnets and Torrentio resolve URLs are resolved through the RD API
  (add magnet → select file → unrestrict) when a token is available.
- **Paste link**: magnet, info hash, `real-debrid.com/d/…` page link or a direct URL.
- Movies and series save to separate folders; files are written as `.part` and
  renamed on completion.
- Download queue with progress, speed, and `MAX_ACTIVE_DOWNLOADS` concurrency.
  RD 403/429 responses trigger one retry with a freshly unrestricted link.
- Real-Debrid CDN selection: pin a host or let a background speed scan pick the
  fastest node (cached on disk).

## Quick start

```bash
cp .env.example .env   # set STREMIO_ADDON_MANIFEST_URL and the download paths
docker compose up -d --build
```

Open `http://<host>:${WEB_PORT:-8080}`.

The Torrentio manifest comes from [torrentio.strem.fun](https://torrentio.strem.fun)
with Real-Debrid configured:

```env
STREMIO_ADDON_MANIFEST_URL=https://torrentio.strem.fun/realdebrid=YOUR_TOKEN/manifest.json
```

Pre-built images are published to `ghcr.io/swindlesmccoop/jelryquest`.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `STREMIO_ADDON_MANIFEST_URL` | required | Torrentio manifest URL (contains RD token) |
| `REAL_DEBRID_API_TOKEN` | from manifest | Explicit RD API token |
| `HTTP_USER_AGENT` | Firefox UA | Shared HTTP client User-Agent (Torrentio blocks the default httpx UA) |
| `JELLYFIN_URL` | unset | Jellyfin URL reachable from the app container, including any base path |
| `JELLYFIN_API_KEY` | unset | Server API key for the library refresh button |
| `DOWNLOAD_DIR_MOVIES` / `DOWNLOAD_DIR_SERIES` | `/downloads` | In-container output folders (compose mounts `DOWNLOAD_PATH_MOVIES` / `DOWNLOAD_PATH_SERIES` here) |
| `MAX_ACTIVE_DOWNLOADS` | `1` | Concurrent file pulls |
| `WEB_PORT` | `8080` | Listener port |
| `CACHE_DIR` | `/cache` | CDN scan cache |
| `REAL_DEBRID_PREFERRED_CDN` | – | Pin a CDN host (skips the scan) |
| `REAL_DEBRID_AUTO_SCAN` | `true` | Background CDN benchmark |
| `REAL_DEBRID_SCAN_SECONDS` / `_WORKERS` / `_HOSTS` / `_CONNECT_TIMEOUT_SEC` / `_CACHE_TTL_SEC` | see `.env.example` | Scan tuning |
| `REAL_DEBRID_API_BASE`, `_REMOTE`, `_CONNECT_TIMEOUT_SEC`, `_READ_TIMEOUT_SEC` | see `.env.example` | RD API options |

## API

To enable **Refresh Jellyfin library**, create an API key in Jellyfin's admin
Dashboard → API Keys, set `JELLYFIN_URL` and `JELLYFIN_API_KEY` in `.env`, and
recreate the app container. The key stays on the backend. The button requests
a scan for new and updated files and displays progress from Jellyfin's scheduled
tasks. It checks for existing scans on page load and avoids restarting a running
scan. Progress updates every two seconds, pauses while the tab is hidden, and
stops after 30 minutes. A scan continues on Jellyfin when monitoring stops.
Anyone with access to JelryQuest can use this button once configured.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Status and CDN scan state |
| POST | `/api/jellyfin/refresh` | Request Jellyfin library scan (requires server configuration) |
| GET | `/api/jellyfin/status` | Scan configuration, state, and progress percentage |
| GET | `/api/search?q=&type=movie\|series` | Cinemeta search |
| GET | `/api/movie/{imdbId}/meta` | Movie metadata |
| GET | `/api/movie/{imdbId}/streams` | Movie streams |
| GET | `/api/show/{imdbId}/meta` | Series metadata |
| GET | `/api/show/{imdbId}/episodes` | Episode list |
| GET | `/api/show/{imdbId}/streams?season=&episode=` | Episode streams |
| POST | `/api/download` | `{"imdb_id","stream_index","title?","content_type?","season?","episode?"}` |
| POST | `/api/download/link` | `{"link","title?","content_type"}` |
| GET | `/api/downloads` | All jobs |
| GET | `/api/downloads/{id}` | One job |

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
.venv/bin/python -m backend.main   # needs STREMIO_ADDON_MANIFEST_URL in .env
```
