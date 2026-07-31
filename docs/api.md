# API Reference

The HPC Status Monitor exposes a REST API for programmatic access to fleet and cluster data.

The authoritative endpoint list is served by the API itself at `GET /api/endpoints`
and rendered, with a "Try it" button per endpoint, at `/api.html` in a running
deployment. `schemas/openapi.yaml` is generated from the same source
(`python scripts/build_openapi.py`). This document adds the worked examples that a
generated spec cannot.

## Base URL

```
http://localhost:8080/api
```

If using a URL prefix:
```
http://localhost:8080/prefix/api
```

## Authentication

No authentication required. Rate limiting may apply (configurable).

## Endpoints

### Fleet Status

#### GET /api/status

Returns the full fleet status payload used by the dashboard.

**Response**

```json
{
  "summary": {
    "total_systems": 10,
    "up_count": 9,
    "degraded_count": 1,
    "fleet_uptime": "90%",
    "last_observed": "2026-01-22T15:30:00Z"
  },
  "status_breakdown": {
    "UP": 9,
    "DEGRADED": 1
  },
  "dsrc_breakdown": {
    "ERDC": 3,
    "NAVO": 2,
    "AFRL": 2,
    "ARL": 2,
    "MHPCC": 1
  },
  "scheduler_breakdown": {
    "PBS": 8,
    "Slurm": 2
  },
  "systems": [
    {
      "system": "Nautilus",
      "status": "UP",
      "dsrc": "NAVO",
      "login_node": "nautilus.navo.hpc.mil",
      "scheduler": "PBS",
      "observed_at": "2026-01-22T15:30:00Z",
      "details_url": "/system/nautilus"
    }
  ]
}
```

#### GET /api/fleet/summary

Returns a condensed fleet overview optimized for automation.

**Response**

```json
{
  "generated_at": "2026-01-22T15:30:00Z",
  "fleet_stats": {
    "total_systems": 10,
    "status_counts": {
      "UP": 9,
      "DEGRADED": 1
    },
    "dsrc_counts": {
      "ERDC": 3,
      "NAVO": 2
    }
  },
  "systems": [
    {
      "system": "Nautilus",
      "status": "UP",
      "dsrc": "NAVO",
      "scheduler": "PBS",
      "login_node": "nautilus.navo.hpc.mil",
      "observed_at": "2026-01-22T15:30:00Z"
    }
  ]
}
```

### Cluster Data

#### GET /api/cluster-usage

Returns queue and quota data for all monitored clusters.

Each queue also carries a `wait_estimate` once there is enough recorded
history to derive one:

```json
{
  "wait_estimate": {
    "wait_seconds": 12600,
    "wait_display": "~3.5 hours",
    "pending_cores": 38000,
    "drain_rate_cores_per_hour": 10962,
    "samples": 12,
    "window_hours": 6,
    "confidence": "medium",
    "basis": "38,000 cores waiting ÷ 10,962 cores/h observed turnover over 3.7h"
  }
}
```

The estimate is backlog ÷ observed core turnover over a trailing window. A
queue with no observed turnover reports `wait_seconds: null` and
`confidence: "none"` rather than an invented number, and the `basis` string
always explains where the figure came from.

**Response**

```json
{
  "generated_at": "2026-01-22T15:30:00Z",
  "clusters": {
    "nautilus": {
      "name": "Nautilus",
      "status": "connected",
      "last_updated": "2026-01-22T15:28:00Z",
      "queues": [
        {
          "name": "standard",
          "state": "running",
          "total_jobs": 150,
          "running_jobs": 45,
          "queued_jobs": 105,
          "held_jobs": 0
        }
      ],
      "allocations": [
        {
          "project": "PROJ001",
          "allocated_hours": 100000,
          "used_hours": 45000,
          "remaining_hours": 55000,
          "percent_used": 45.0
        }
      ],
      "storage": [
        {
          "path": "/home/user",
          "total_gb": 50,
          "used_gb": 35,
          "available_gb": 15,
          "percent_used": 70.0
        }
      ]
    }
  },
  "insights": [
    {
      "type": "recommendation",
      "priority": "high",
      "cluster": "nautilus",
      "message": "Queue 'standard' has 105 jobs waiting. Consider using 'debug' queue for short jobs."
    }
  ]
}
```

#### GET /api/cluster-usage/{cluster}

Returns data for a specific cluster.

**Parameters**

| Name | Type | Description |
|------|------|-------------|
| cluster | string | Cluster name (case-insensitive) |

**Response**

```json
{
  "name": "Nautilus",
  "status": "connected",
  "last_updated": "2026-01-22T15:28:00Z",
  "queues": [...],
  "allocations": [...],
  "storage": [...],
  "insights": [...]
}
```

**Error Response** (404)

```json
{
  "error": "Cluster not found",
  "cluster": "unknown"
}
```

### Topology

#### GET /api/topology

Returns the fleet as a graph: a monitor node, one node per site (DSRC or
data center), and one node per system. System nodes merge what the site
*reports* (status, scheduler, login node) with what the monitor *observes*
by connecting (cores, queues, GPUs, allocation, connection age).

Always answers `200`. When no collection has completed yet the graph
contains only the monitor node and `meta.ready` is `false`.

**Response**

```json
{
  "meta": {
    "generated_at": "2026-07-29T18:47:56Z",
    "platform": "hpcmp",
    "site_label": "DSRC",
    "fleet_observed_at": "2026-07-29T18:47:45Z",
    "telemetry_clusters": 3,
    "ready": true,
    "collection_progress": null
  },
  "summary": {
    "sites": 4,
    "systems": 12,
    "connected": 2,
    "alerts": 2,
    "up": 12,
    "uptime_ratio": 1.0,
    "status_counts": {"UP": 12},
    "scheduler_counts": {"SLURM": 7, "PBS": 5},
    "queues": 6,
    "capacity": {
      "cores_total": 470288,
      "cores_running": 328032,
      "cores_free": 142256,
      "nodes_total": 160,
      "gpus_total": 132,
      "utilization_percent": 69.8
    }
  },
  "sites": [
    {
      "id": "navy",
      "name": "Navy DSRC",
      "organization": "Naval Meteorology and Oceanography Command",
      "location": "Stennis Space Center, MS",
      "lat": 30.36,
      "lon": -89.6,
      "cloud": false,
      "systems": 3,
      "connected": 1,
      "status": "UP",
      "capacity": {"cores_total": 290000, "cores_running": 176000},
      "members": ["blueback", "narwhal", "nautilus"],
      "node_id": "site:navy"
    }
  ],
  "nodes": [
    {
      "id": "sys:narwhal",
      "kind": "system",
      "label": "Narwhal",
      "slug": "narwhal",
      "site": "navy",
      "site_label": "Navy DSRC",
      "status": "UP",
      "scheduler": "SLURM",
      "login": "narwhal.navydsrc.hpc.mil",
      "address": "140.32.36.136",
      "origin": "both",
      "connected": true,
      "connection": {
        "source": "pw",
        "uri": "pw://user/narwhal",
        "capabilities": ["sinfo", "squeue"],
        "connected_since": "2026-07-29T09:00:00Z",
        "connected_for_seconds": 35100,
        "uptime_ratio": 0.987,
        "uptime_window_hours": 24,
        "transitions": 0,
        "latency_ms": 212,
        "window_start": "2026-07-28T18:47:00Z",
        "window_end": "2026-07-29T18:47:00Z",
        "spans": [
          {"status": "UP", "from": "2026-07-28T18:47:00Z", "to": "2026-07-29T18:47:00Z", "seconds": 86400}
        ]
      },
      "capacity": {"cores_total": 290000, "cores_running": 176000, "utilization_percent": 60.7},
      "queues": {"count": 3, "running_jobs": 199, "pending_jobs": 49},
      "allocation": {"hours_remaining": 495000, "percent_remaining": 20.6},
      "alert": "warning",
      "insights": [
        {"type": "warning", "message": "narwhal/standard: ...", "priority": 4, "metric": "queue_depth"}
      ],
      "links": {"queues": "queues.html?cluster=narwhal"}
    }
  ],
  "edges": [
    {"id": "monitor->site:navy", "source": "monitor", "target": "site:navy", "kind": "site", "connected": true}
  ]
}
```

**Node kinds**

| Kind | Meaning |
|------|---------|
| `monitor` | The dashboard itself — the graph root |
| `site` | A DSRC / data center, with rolled-up status and capacity |
| `system` | One HPC system |

**`status_source`** says where a system's status came from: `status page`,
`live session` (we are logged in and collecting, which is direct evidence
it is up), or `control plane`. `reported_status` keeps whatever the status
page said, so a disagreement stays visible. A live session fills in an
unknown status and is the whole story for systems no status page covers,
but it never overrules a site that says its own machine is down or in
maintenance.

**`site_source`** on a system node says how its site was decided:
`config` (`topology.system_sites`), `collector` (the collector's own
label), `hostname` (the login hostname's domain), `cloud-default`
(`topology.cloud_region_default`), `provider` (a cloud provider with no
known region), `name-hint`, or `none`. It exists so "why is this machine
here?" is answerable without reading the resolver.

**`cloud`** marks a site that is a cloud region rather than a physical
facility; its coordinates are the region's published locality.

**`origin`** tells you where a system node came from: `fleet` (site status
page only), `pw` (a connected cluster the status page never mentioned), or
`both` (matched and merged).

**`insights`** are the same objects `/api/insights` returns, filtered to the
ones filed against that system; **`alert`** is the worst severity among them
(`critical`, `warning`, `info`, or `null`).

**`connection.spans`** is the recorded status timeline inside the uptime
window — enough to draw a strip chart without a second request. Capped at
the 60 most recent transitions.

**`connection.latency_ms`** is one no-op round trip over the control plane
(PW CLI start-up + auth + SSH), measured once per collection sweep. It
predicts how slow collection will be; it is **not** a network ping.

### History

#### GET /api/history

Replayable frames of fleet status and utilization, for the topology
timeline. Two recorded series are combined: status transitions, carried
forward across the gaps between them, and queue depth, bucketed onto the
frame times.

**Parameters:** `window` (hours, default 24), `step` (minutes, default 15).
The step is widened automatically rather than returning thousands of
frames.

```json
{
  "window_hours": 24,
  "step_minutes": 15,
  "from": "2026-07-29T12:00:00Z",
  "to": "2026-07-30T12:00:00Z",
  "systems": ["carpenter", "narwhal"],
  "frames": [
    {
      "at": "2026-07-30T11:45:00Z",
      "systems": {
        "narwhal": {
          "status": "UP",
          "cores_running": 176000,
          "cores_pending": 38000,
          "cores_total": 290000,
          "utilization_percent": 60.7
        }
      }
    }
  ]
}
```

A system missing from a frame had no recorded reading at that instant —
the timeline shows it as unknown rather than implying the current status
held back then. A gap between collection sweeps holds the previous
reading, because a sweep that did not run is not a cluster at zero.

### Storage

#### GET /api/storage

Returns filesystem capacity and usage for every connected cluster. Shares the
warming-up envelope described under `/api/cluster-usage`.

**Response**

```json
{
  "cluster_metadata": {
    "narwhal": {"display_name": "Narwhal", "scheduler": "PBS"}
  },
  "storage_data": {
    "narwhal": [
      {
        "filesystem": "/p/home",
        "used_gb": 412.5,
        "quota_gb": 1024.0,
        "percent_used": 40.3,
        "files": 182344,
        "file_limit": 500000
      }
    ]
  }
}
```

### Insights

#### GET /api/insights

Returns generated findings — allocation burn-down, queue backlog, idle GPUs and
status changes — sorted by priority.

**Response**

```json
{
  "insights": [
    {
      "severity": "WARNING",
      "category": "allocation",
      "title": "Project ABC1234 is 92% spent with 4 months left",
      "detail": "...",
      "system": "narwhal"
    }
  ],
  "generated_at": "2026-01-22T15:30:00Z"
}
```

### Placement

#### GET /api/placement

Ranks `(cluster, queue)` pairs for a specific job shape.

**Parameters**

| Name | Default | Meaning |
|------|---------|---------|
| `cores` | 1 | Cores the job needs |
| `hours` | 1 | Walltime in hours |
| `gpus` | 0 | GPUs the job needs |
| `queue_type` | — | Restrict to a queue type or name |
| `limit` | 5 | Maximum candidates returned |

**Response**

```json
{
  "request": {"cores": 1024, "hours": 6, "gpus": 0, "core_hours": 6144},
  "candidates": [
    {
      "cluster": "narwhal",
      "queue": "standard",
      "score": 88.7,
      "components": {"capacity": 40, "wait": 21.3, "backlog": 17.4, "allocation": 10},
      "cores_free": 114000,
      "cores_pending": 38000,
      "max_walltime": "24:00:00",
      "allocation_hours_remaining": 495000,
      "wait_hours": 3.47,
      "wait_estimate": {"wait_display": "~3.5 hours", "confidence": "medium"},
      "reasons": [
        "114,000 cores idle now — enough to start immediately",
        "estimated start ~3.5 hours"
      ],
      "links": {"queues": "queues.html?cluster=narwhal"}
    }
  ],
  "blocked": [
    {
      "cluster": "narwhal",
      "queue": "debug",
      "blockers": ["walltime limit is 1h, job needs 6h"]
    }
  ],
  "considered": 6
}
```

Scoring is out of 100: idle capacity (40), measured time-to-start (30),
backlog relative to cluster size (20), and allocation headroom (10). A queue
that *cannot* run the job — walltime too short, per-queue core cap too low,
cluster down, allocation exhausted — is returned under `blocked` with the
reason rather than ranked last.

### Events

#### GET /api/events

Recent system state changes, newest first. Populated whether or not
alerting is configured.

```json
{
  "events": [
    {
      "entity": "system:narwhal",
      "name": "Narwhal",
      "previous": "UP",
      "status": "DOWN",
      "kind": "degraded",
      "severity": "critical",
      "at": "2026-07-30T14:02:11Z"
    }
  ],
  "alerting_enabled": true
}
```

`kind` is one of `degraded`, `recovered`, or `changed`. The first sighting
of a system is not an event — otherwise a fresh install would report every
system as new.

### Data Refresh

#### POST /api/refresh

Triggers an immediate data refresh.

**Request**

No body required.

**Response**

```json
{
  "status": "refreshing",
  "message": "Data refresh initiated"
}
```

**Error Response** (429 - Rate Limited)

```json
{
  "error": "Rate limited",
  "retry_after": 30
}
```

### Reference

#### GET /api/system-markdown/{system}

Returns the scraped briefing for a system, keyed by slug (lowercase, non-alphanumerics
removed — `Nautilus` is `nautilus`).

**Response**

```json
{
  "slug": "nautilus",
  "content": "# Nautilus\n\nCray EX, NAVO DSRC..."
}
```

#### GET /api/config

Returns the deployment configuration the frontend needs: branding, enabled tabs,
feature flags and topology settings. Secrets are never included — the alert webhook
URL in particular is deliberately withheld.

`GET /app-config.js` returns the same data as JavaScript that assigns
`window.APP_CONFIG`, for pages that need it before any module loads.

#### GET /api/endpoints

Returns this catalog: every endpoint with its parameters, description and top-level
response keys, plus the group ordering the API page renders.

```json
{
  "endpoints": [
    {
      "method": "GET",
      "path": "/api/topology",
      "group": "Fleet",
      "summary": "The fleet as a graph: monitor, sites, systems",
      "description": "...",
      "returns": ["meta", "summary", "sites", "nodes", "edges"]
    }
  ],
  "groups": ["Fleet", "Clusters", "Decisions", "Reference", "Control"],
  "generated_at": "2026-01-22T15:30:00Z"
}
```

#### GET /api/v2/collectors/status

Reports whether each configured collector has produced data yet — the endpoint to
poll while a deployment is starting up.

```json
{
  "collectors": {
    "hpcmp_fleet": {"ready": true, "systems": 12},
    "pw_cluster": {"ready": false}
  }
}
```

## Response Codes

| Code | Description |
|------|-------------|
| 200 | Success |
| 404 | No such system, cluster, or path |
| 503 | Data not collected yet (`/api/status` only — other endpoints return a warming-up envelope with 200) |
| 500 | Server error |

## Caching

Responses are sent with `Cache-Control: no-cache, must-revalidate`. Each request
reflects the most recent completed collection; the API does not collect on demand
except for `POST /api/refresh`.

## Concurrency Limits

`rate_limiting` in the configuration bounds how hard the monitor works the clusters
it connects to (`max_concurrent_ssh`, `ssh_timeout`, `failure_threshold`). There is
no per-client limit on HTTP requests and no `X-RateLimit-*` headers.

## Example Usage

### Python

```python
import requests

base_url = "http://localhost:8080/api"

# Get fleet summary
response = requests.get(f"{base_url}/fleet/summary")
data = response.json()

for system in data["systems"]:
    if system["status"] == "UP":
        print(f"{system['system']}: {system['login_node']}")

# Get cluster usage
response = requests.get(f"{base_url}/cluster-usage")
clusters = response.json()["clusters"]

for name, cluster in clusters.items():
    for alloc in cluster.get("allocations", []):
        if alloc["percent_used"] > 80:
            print(f"Warning: {name} allocation {alloc['project']} at {alloc['percent_used']}%")
```

### curl

```bash
# Get fleet status
curl http://localhost:8080/api/status

# Get specific cluster
curl http://localhost:8080/api/cluster-usage/nautilus

# Trigger refresh
curl -X POST http://localhost:8080/api/refresh

# Pretty print with jq
curl -s http://localhost:8080/api/fleet/summary | jq '.systems[] | {name: .system, status}'
```

### JavaScript

```javascript
async function getFleetStatus() {
  const response = await fetch('/api/fleet/summary');
  const data = await response.json();

  const upSystems = data.systems.filter(s => s.status === 'UP');
  console.log(`${upSystems.length} systems operational`);

  return data;
}
```

## Webhooks (Future)

Webhook support for status change notifications is planned for a future release.

## Versioning

The API is currently unversioned. Breaking changes will be documented in release notes.
