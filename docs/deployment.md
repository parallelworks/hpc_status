# Deployment Guide

This guide covers deploying the HPC Status Monitor on various platforms.

## Prerequisites

- Python 3.9+
- Network access to HPC systems (for cluster monitoring)
- Parallel Works CLI (optional, for cluster data collection)

## Parallel Works (ACTIVATE Platform)

The recommended deployment method for Parallel Works users.

### Using the Workflow

Launch **HPC Status** from your workflows list, or from a clone of the repo:

```bash
pw workflows run hpcmp_status                 # the registered workflow
pw workflows run -i '{"platform":"hpcmp"}' ./workflow.yaml
```

Inputs:

| Input | Default | Meaning |
|---|---|---|
| `action` | `start` | `start` the dashboard, or `stop` a detached one |
| `platform` | `auto` | Which config to load: `auto`, `generic`, `hpcmp`, or `noaa` |
| `name` | `hpc-status` | Endpoint session name, shown in your sessions list |
| `subdomain` | *(blank)* | Public hostname label; blank derives `status-<username>` |
| `detach` | `false` | Keep serving after the run ends (see below) |
| `port` | `0` | Local port; `0` lets the CLI pick a free one |
| `theme` | `light` | Default color theme |
| `enable_cluster_pages` | `true` | Queue and quota pages |
| `enable_cluster_monitor` | `true` | Background cluster collection |
| `cluster_monitor_interval` | `120` | Seconds between collections |

The workflow publishes the dashboard with `pw endpoints run`, which:

- **assigns a free local port** and exports it as `PORT`, so nothing has to
  guess one. This matters: port 8080 on an ACTIVATE workspace is Grafana,
  and the old fixed-port workflow died on `Address already in use`.
- **dials out** to register a reverse tunnel, so the node needs no inbound
  network access.
- **owns the process tree** — when the run ends or is cancelled, the
  dashboard and its workers are killed with it, and the endpoint session
  is deleted. Nothing outlives the run.

### Who owns the dashboard

By default the run owns it: the step stays in the foreground for as long
as the dashboard serves, logs stream into the run, and **cancelling the
run stops everything** — tunnel, dashboard, workers, session.

The run showing as "running" for days is the cost of that, and it is what
the `detach` input trades away:

| | `detach: false` (default) | `detach: true` |
|---|---|---|
| Run status | running until you cancel | completed in seconds |
| Logs | stream into the run | `~/.hpc_status/endpoint.log` |
| To stop it | cancel the run | `scripts/stop-endpoint.sh` |
| If you forget | nothing to forget | it serves until stopped |

A detached start records its pid, waits for the URL rather than reporting
success blindly, and refuses to start a second dashboard over the first.
Stopping signals the whole process group, so the dashboard and its
workers go with the tunnel, and deletes the session if the tunnel did not
get there itself:

```bash
./scripts/stop-endpoint.sh
```

From the platform, where nobody has a shell on the workspace, launch the
same workflow with **Action: Stop a detached dashboard** — no other input
matters:

```bash
pw workflows run hpcmp_status -i '{"action":"stop"}'
```

### Choosing the configuration

`platform: auto` resolves the config from the host the run is on —
`*.hpc.mil` and `hpcmp.*` load `configs/config.hpcmp.yaml`, NOAA hosts
load `configs/config.noaa.yaml`, anything else loads
`configs/config.yaml`. This matters for marketplace launches: a
marketplace version that does not name a `yaml` resolves to
`workflow.yaml`, so without auto-detection an item titled "HPCMP Status"
would come up as a generic deployment with no fleet.

### A stable URL

Without a subdomain the platform assigns a random one per session, so the
dashboard moves between runs — `healthy-dingo` today, `expert-sponge`
tomorrow. Both the workflow and `scripts/serve-endpoint.sh` derive
`status-<username>` instead, lowercased and hyphenated so it is a valid
hostname label (`Matthew.Shaxted` becomes `status-matthew-shaxted`).

That is a request, not a claim. To keep the name reserved even while
nothing is serving it:

```bash
pw subdomains reserve status-mshaxted
```

Set the `subdomain` input, or `ENDPOINT_SUBDOMAIN`, to use a different
name. If the platform has no sessions domain or the name belongs to
someone else, the run says so and falls back to an assigned address
rather than failing.

### Publishing a dashboard by hand

The same mechanism works outside a workflow, from a clone of the repo:

```bash
./scripts/serve-endpoint.sh
```

It prints the public URL — `https://status-<you>.<sessions domain>/` — and
holds it open until you press Ctrl-C. That script is what the workflows
run, so a local session and a platform run behave identically. Add
`--keep` to leave the session registered after you exit, `--port N` to pin
the local port, and `--public` to allow access without logging in (only if
your organization permits public sessions).

`scripts/run.sh` reads two variables the endpoint exports:

- `PORT` — the assigned local port
- `PW_ENDPOINT_PATH` — the base path the session is served under, which
  becomes the server's `--url-prefix` so links resolve behind the prefix.
  It is `/` for a subdomain endpoint, which means no prefix at all.

## Manual Deployment

### Using uv (Recommended)

```bash
# Install uv if not present
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create virtual environment and install
uv venv ~/.venvs/hpc-status
uv pip install --python ~/.venvs/hpc-status/bin/python -e .

# Run
~/.venvs/hpc-status/bin/python -m src.server.main
```

### Using pip

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -e .

# Run
python -m src.server.main
```

### Using the Run Script

The `scripts/run.sh` script handles virtual environment setup automatically:

```bash
./scripts/run.sh
```

Environment variables for configuration:
```bash
PORT=8080 \
HOST=0.0.0.0 \
DEFAULT_THEME=dark \
CONFIG_FILE=configs/config.hpcmp.yaml \
./scripts/run.sh
```

## Docker Deployment

### Build the Image

```bash
docker build -t hpc-status-monitor .
```

### Run Container

```bash
# Basic run
docker run -p 8080:8080 hpc-status-monitor

# With configuration
docker run -p 8080:8080 \
  -e CONFIG_FILE=configs/config.hpcmp.yaml \
  -e DEFAULT_THEME=dark \
  hpc-status-monitor

# With persistent data
docker run -p 8080:8080 \
  -v ~/.hpc_status:/root/.hpc_status \
  hpc-status-monitor
```

### Docker Compose

```yaml
version: '3.8'
services:
  hpc-status:
    build: .
    ports:
      - "8080:8080"
    environment:
      - CONFIG_FILE=configs/config.hpcmp.yaml
      - DEFAULT_THEME=dark
      - ENABLE_CLUSTER_MONITOR=1
    volumes:
      - hpc-data:/root/.hpc_status
    restart: unless-stopped

volumes:
  hpc-data:
```

## Platform-Specific Deployments

### HPCMP (DoD)

Use the HPCMP configuration for DoD HPC centers:

```bash
CONFIG_FILE=configs/config.hpcmp.yaml ./scripts/run.sh
```

This enables:
- HPCMP fleet status scraping from centers.hpc.mil
- DoD DSRC site names and terminology
- PBS scheduler integration

### NOAA RDHPCS

Use the NOAA configuration for RDHPCS systems:

```bash
CONFIG_FILE=configs/config.noaa.yaml ./scripts/run.sh
```

This enables:
- NOAA system names (Hera, Jet, Gaea, etc.)
- Slurm scheduler integration
- NOAA-specific quota tracking

### Generic Deployment

For custom or mixed HPC environments:

```bash
CONFIG_FILE=configs/config.yaml ./scripts/run.sh
```

Customize `configs/config.yaml` for your specific systems.

## Reverse Proxy Setup

### nginx

```nginx
location /hpc-status/ {
    proxy_pass http://localhost:8080/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

When using a URL prefix, configure the server:
```bash
URL_PREFIX=/hpc-status ./scripts/run.sh
```

### Apache

```apache
<Location /hpc-status>
    ProxyPass http://localhost:8080
    ProxyPassReverse http://localhost:8080
</Location>
```

## Health Checks

The server exposes endpoints for monitoring:

- `GET /api/status` - Returns 200 if server is running
- `GET /api/cluster-usage` - Returns 200 with cluster data

Example health check script:
```bash
#!/bin/bash
curl -sf http://localhost:8080/api/status > /dev/null || exit 1
```

## Logging

Logs are written to:
- Console (stdout/stderr)
- `~/.hpc_status/logs/` (when data directory exists)

Set log level via environment:
```bash
LOG_LEVEL=DEBUG ./scripts/run.sh
```

## Troubleshooting

### Port Already in Use

The run script attempts cleanup, but if needed:
```bash
lsof -ti:8080 | xargs kill
```

### PW CLI Not Found

Cluster monitoring requires the Parallel Works CLI:
```bash
which pw  # Should return path
pw cluster list  # Should list clusters
```

### No Cluster Data

1. Verify PW CLI authentication: `pw auth status`
2. Check cluster connectivity: `pw ssh <cluster> hostname`
3. Enable debug logging: `LOG_LEVEL=DEBUG`

### Import Errors

Ensure the package is installed in editable mode:
```bash
pip install -e .
```
