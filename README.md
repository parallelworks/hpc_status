# HPCMP Status Dashboard

This folder contains a lightweight static site for visualizing the HPCMP
unclassified system status feed scraped by `storage/hpc_status_scraper.py`.

## Components

1. `dashboard_data.py` reuses the Python scraper helpers to build the JSON
   payload consumed by the UI (summary metrics + system rows).
2. `dashboard_server.py` runs the whole experience as a single script: it serves
   the static assets, refreshes the data every three minutes, exposes
   `/api/status`, and lets the front-end trigger `/api/refresh`.
3. `public/index.html`, `styles.css`, and `app.js` render the dashboard and call
   the API endpoints.

## Quick start (single script)

```bash
python storage/hpc_status_site/dashboard_server.py --port 8080
```

Open [http://localhost:8080](http://localhost:8080) to view the dashboard. The
server refreshes the data every three minutes automatically; clicking the UI
refresh button hits `/api/refresh`, which immediately re-scrapes the upstream
page and pushes the latest metrics to `/api/status`.

When hosting behind a path prefix (e.g., `/session/<user>/status/`), launch the
server with the same prefix so static assets and APIs line up:

```bash
python storage/hpc_status_site/dashboard_server.py --url-prefix /session/<user>/status
```

The browser-side code also derives its API paths relative to the page URL. If
you need to point at a different backend entirely, expose
`window.API_BASE_URL = "https://example/api/root/"` before `app.js` loads or set
`data-api-base` on the `<html>` element.

## Static export (optional)

If you prefer to pre-render `status.json` (e.g., for publishing to object
storage), you can still run:

```bash
python storage/hpc_status_site/generate_status_data.py
```

and host the `public/` directory however you choose. Just remember to rerun the
generator whenever you need fresh data.
