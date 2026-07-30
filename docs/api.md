# API Reference

The HPC Status Monitor exposes a REST API for programmatic access to fleet and cluster data.

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

### System Details

#### GET /api/system/{system}

Returns detailed information for a specific system.

**Parameters**

| Name | Type | Description |
|------|------|-------------|
| system | string | System name (case-insensitive) |

**Response**

```json
{
  "system": "Nautilus",
  "status": "UP",
  "dsrc": "NAVO",
  "login_node": "nautilus.navo.hpc.mil",
  "scheduler": "PBS",
  "observed_at": "2026-01-22T15:30:00Z",
  "details": {
    "architecture": "Cray EX",
    "cores": 123456,
    "memory_tb": 512,
    "description": "..."
  },
  "markdown_content": "..."
}
```

### Health Check

#### GET /api/health

Returns server health status.

**Response**

```json
{
  "status": "healthy",
  "uptime_seconds": 3600,
  "collectors": {
    "hpcmp_fleet": {
      "status": "ok",
      "last_run": "2026-01-22T15:28:00Z"
    },
    "pw_cluster": {
      "status": "ok",
      "last_run": "2026-01-22T15:29:00Z"
    }
  }
}
```

## Response Codes

| Code | Description |
|------|-------------|
| 200 | Success |
| 400 | Bad request (invalid parameters) |
| 404 | Resource not found |
| 429 | Rate limited |
| 500 | Server error |

## Rate Limiting

Default limits (configurable):
- Manual refresh: 30 second cooldown
- API requests: 60 per minute

Rate limit headers:
```
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 55
X-RateLimit-Reset: 1705936260
```

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
