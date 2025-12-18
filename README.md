# HPC Status Dashboard

This folder contains a lightweight static site for visualizing the HPC system status feed scraped by `storage/hpc_status_scraper.py`.

The source code for this dashboard is available at [the public github repo here](https://github.com/parallelworks/hpcmp_status_site) and we encourage collaborations, improvements and pull requests.

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

### Theme options

Use the moon/sun toggle in the header to switch between the dark (DoD HPC shell
matching) palette and a high-contrast light theme. The preference is stored in
the browser so reloading the page preserves the last choice. Server operators
can set the default by passing `--default-theme light|dark` when launching
`dashboard_server.py`; first-time visitors inherit that value until they switch.

When hosting behind a path prefix (e.g., `/session/<user>/status/`), launch the
server with the same prefix so static assets and APIs line up:

```bash
python storage/hpc_status_site/dashboard_server.py --url-prefix /session/<user>/status
```

The browser-side code also derives its API paths relative to the page URL. If
you need to point at a different backend entirely, expose
`window.API_BASE_URL = "https://example/api/root/"` before `app.js` loads or set
`data-api-base` on the `<html>` element.

## REST API

The server exposes a small REST surface so downstream automation (including LLM
agents or job-placement tools) can reason about the fleet without scraping the
UI.

| Endpoint | Description |
| --- | --- |
| `GET /api/status` | Original payload with the full table rendered by the dashboard. |
| `GET /api/fleet/summary` | Condensed list of systems plus aggregate counts (good starting point for placement rules). |
| `GET /api/cluster-usage` | Usage/quota/queue data for every connected cluster gathered by `cluster_monitor.py`. |
| `GET /api/cluster-usage/<cluster>` | Focused view for a single cluster (case-insensitive slug). |

Every response is JSON and includes timestamps so you can reason about data
freshness. An abridged example of the fleet summary:

```json
{
  "generated_at": "2025-12-17T22:59:01Z",
  "fleet_stats": {
    "total_systems": 10,
    "status_counts": {"UP": 10}
  },
  "systems": [
    {
      "system": "Barfoot",
      "status": "UP",
      "dsrc": "ERDC",
      "scheduler": "PBS",
      "login_node": "barfoot.erdc.hpc.mil",
      "observed_at": "2025-12-17T22:59:01Z"
    }
  ]
}
```

See `api_client.py` for a minimal Python example that calls every endpoint and
prints a short recommendation snapshot.

## Static export (optional)

If you prefer to pre-render `status.json` (e.g., for publishing to object
storage), you can still run:

```bash
python storage/hpc_status_site/generate_status_data.py
```

and host the `public/` directory however you choose. Just remember to rerun the
generator whenever you need fresh data.
