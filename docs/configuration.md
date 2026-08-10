# Configuration Reference

The HPC Status Monitor supports configuration through environment variables and YAML files.

## Environment Variables

### Server Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8080` | HTTP server port |
| `HOST` | `0.0.0.0` | Bind address |
| `URL_PREFIX` | `` | URL prefix for reverse proxy |

### UI Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `DEFAULT_THEME` | `dark` | Initial theme (`dark` or `light`) |

### Feature Flags

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_CLUSTER_PAGES` | `1` | Enable queue and quota pages |
| `ENABLE_CLUSTER_MONITOR` | `1` | Enable background cluster monitoring |
| `CLUSTER_MONITOR_INTERVAL` | `120` | Refresh interval in seconds |

### Paths

| Variable | Default | Description |
|----------|---------|-------------|
| `CONFIG_FILE` | `` | Path to YAML config file |
| `HPC_STATUS_DATA_DIR` | `~/.hpc_status` | Data storage directory |
| `HPC_STATUS_VENV` | `~/.venvs/hpc-status` | Virtual environment path |

### Runtime Options

| Variable | Default | Description |
|----------|---------|-------------|
| `USE_UV` | `1` | Use uv package manager |
| `PYTHON_BIN` | `python3` | Python interpreter path |
| `LOG_LEVEL` | `INFO` | Logging level |

## YAML Configuration

### File Structure

```yaml
deployment:
  name: "HPC Status Monitor"
  platform: generic

server:
  host: "0.0.0.0"
  port: 8080
  url_prefix: ""

collectors:
  hpcmp_fleet:
    enabled: true
    refresh_interval: 180
    url: "https://centers.hpc.mil/systems/unclassified.html"
  pw_cluster:
    enabled: true
    refresh_interval: 120
  storage:
    enabled: true
    refresh_interval: 300

ui:
  home_page: "overview"
  default_theme: "dark"
  tabs:
    overview: true
    queues: true
    quota: true
    storage: true

rate_limits:
  refresh_cooldown: 30
  max_requests_per_minute: 60

data:
  cache_max_age: 3600
  snapshot_retention_days: 30
```

### Deployment Section

```yaml
deployment:
  name: "My HPC Monitor"      # Display name
  platform: generic           # Platform type: generic, hpcmp, noaa
  version: "2.0.0"           # Optional version string
```

### Server Section

```yaml
server:
  host: "0.0.0.0"            # Bind address
  port: 8080                  # HTTP port
  url_prefix: "/status"       # URL prefix for reverse proxy
  workers: 4                  # Number of worker threads
```

### Collectors Section

Each collector can be individually configured:

```yaml
collectors:
  hpcmp_fleet:
    enabled: true             # Enable/disable this collector
    refresh_interval: 180     # Seconds between refreshes
    url: "https://..."        # Custom source URL
    timeout: 30               # Request timeout in seconds

  pw_cluster:
    enabled: true
    refresh_interval: 120
    clusters: []              # Empty = auto-discover, or list specific clusters

  storage:
    enabled: true
    refresh_interval: 300
    paths:                    # Paths to monitor
      - "$HOME"
      - "$WORKDIR"
      - "/scratch"
    warning_threshold: 80     # Percent usage for warning
    critical_threshold: 95    # Percent usage for critical
```

### UI Section

```yaml
ui:
  home_page: "overview"       # Default page: overview, queues, quota
  default_theme: "dark"       # dark or light
  tabs:
    overview: true            # Show fleet overview tab
    topology: true            # Show topology graph tab
    queues: true              # Show queue health tab
    quota: true               # Show quota usage tab
    storage: true             # Show storage tab
  branding:
    title: "HPC Status"       # Page title
    logo: ""                  # Optional logo URL
```

### Alerts Section

Notifies when a system changes state (UP → DOWN, recovery, and so on).
Delivery is a JSON POST, which covers Slack and Teams incoming webhooks as
well as your own receiver.

```yaml
alerts:
  enabled: false
  webhook_url: ""             # any endpoint accepting a JSON POST
  min_severity: warning       # info | warning | critical
  cooldown_seconds: 900       # per-system quiet period
  dashboard_url: ""           # optional link included in the alert text
```

The payload carries both a human-readable `text` field and the structured
`event`. Severity is derived from the transition: leaving a healthy state
for DOWN is `critical`, other departures from healthy are `warning`, and
recoveries are `info`. The first sighting of a system never alerts, so a
fresh install does not page you for every machine it discovers.

Recent transitions are always available at `GET /api/events`, whether or
not a webhook is configured. `webhook_url` is never exposed by
`/api/config` — it is treated as a credential.

### Topology Section

Controls the topology graph (`/topology.html`, `GET /api/topology`).

```yaml
topology:
  resolve_addresses: true     # Resolve login hostnames to IPs (background, cached)
  address_ttl_seconds: 3600   # How long a resolved address is trusted
  uptime_window_hours: 24     # Window for the per-system uptime percentage
  default_layout: hierarchy   # hierarchy | radial | force | lanes | geo
  wait_estimate_window_hours: 6  # trailing window for queue turnover
  sites: {}                   # Site metadata overrides (see below)
```

**A default cloud region.** A fleet that runs all its cloud in one place
can say so, which places instances whose hostname does not name a region:

```yaml
topology:
  cloud_region_default: usgovwest1   # AWS GovCloud (US-West)
```

A hostname that *does* name a region still wins, and this never overrides
a physical site. The HPCMP config ships with this set, since HPCMP's cloud
resources are all in GovCloud (US-West).

**Cloud regions.** AWS regions are in the catalog too, GovCloud included,
and are read straight off an instance's own hostname — EC2 puts the region
in it (`ip-10-1-2-3.us-gov-west-1.compute.internal`), except in us-east-1
where the legacy `ec2.internal` suffix means the same thing. A cluster
labelled only `aws` keeps that label until a hostname can name the region,
at which point the region wins. Region coordinates are the published
locality, not a datacenter address: precise enough to pin the right state
and no more.

**Site overrides.** The builder ships with a catalog of known facilities
(HPCMP DSRCs, NOAA RDHPCS sites, AWS regions). Any site id your collector reports can be
named, described, and placed on the map:

```yaml
topology:
  sites:
    erdc:
      name: "ERDC DSRC"
      organization: "Engineer Research and Development Center"
      location: "Vicksburg, MS"
      lat: 32.30
      lon: -90.87
```

Sites without coordinates still render — they land in a "no location
reported" tray below the map in the geographic layout. Sites outside the
continental US get their own framed inset, so a single machine in Hawaii
does not turn the map into an ocean.

**Placing systems the collectors cannot.** A machine that is not on a
status page and whose login hostname gives no domain away can be assigned
outright:

```yaml
topology:
  system_sites:
    chessie: arl
    crux: mhpcc
```

This beats everything else. Otherwise the site is taken from the
collector's own label, then the login hostname's domain
(`crux.mhpcc.hpc.mil` → MHPCC), then a built-in system-name hint.

**What each deployment infers.** The last of those steps — guessing from a
short cluster name — is scoped to `deployment.platform`, because the names
belong to the deployment that chose them:

| `platform` | Facilities called | Name hints applied |
|---|---|---|
| `hpcmp` | DSRCs | chessie and janus at ARL, crux at MHPCC |
| `noaa` | Sites | hera and niagara at NESCC, gaea at ORNL, ppan at GFDL |
| anything else | Sites | none |

A generic deployment with a cluster called `janus` is not the Army
Research Laboratory's, and a pin on Aberdeen Proving Ground captioned with
somebody else's organization is worse than an honest "Unassigned". Use
`system_sites` to place your own.

The two steps above it are *not* scoped, because neither is a guess: a
site the collector reports is data, and a hostname like
`crux.mhpcc.hpc.mil` is the machine stating its own address. Cloud regions
resolve everywhere too. `GET /api/topology` reports which set was applied
as `meta.site_hints`, and every system carries a `site_source` saying
which step placed it.

Set `resolve_addresses: false` on networks where
outbound DNS lookups for site hostnames are undesirable.

### Marketplace Catalog

```yaml
collectors:
  pw_marketplace:
    enabled: true
    timeout: 30
```

Reads compute listings with `pw marketplace ls`, which is where the fleet
page gets its descriptions and the systems no status page publishes. It is
a catalog, not a status source: nothing here reports whether a machine is
up, and a system known only from a listing shows as `NOT MONITORED` rather
than `UNKNOWN` — nothing looked at it.

Only listings with subtype `existing` can introduce a system. The other
subtypes (`aws-slurm`, `google-slurm`, ...) are recipes for creating a
cluster rather than clusters, so they never invent a machine — though they
will still describe one the monitor is connected to. A listing's tags are
read for a scheduler and a facility, which is how a system tagged `mhpcc`
lands at MHPCC when nothing else can place it.

Set `enabled: false` on a deployment with no marketplace, or one where the
listings describe machines this dashboard should not advertise.

### Rate Limits Section

```yaml
rate_limits:
  refresh_cooldown: 30        # Minimum seconds between manual refreshes
  max_requests_per_minute: 60 # API rate limit
```

### Data Section

```yaml
data:
  cache_max_age: 3600         # Max cache age in seconds
  snapshot_retention_days: 30 # How long to keep historical snapshots
  database_path: ""           # Custom SQLite path (default: data_dir/status.db)
```

## Platform Presets

### Generic (`configs/config.yaml`)

Minimal configuration suitable for most deployments:
- HPCMP fleet scraping enabled
- PW cluster monitoring enabled
- All UI tabs enabled

### HPCMP (`configs/config.hpcmp.yaml`)

Optimized for DoD HPC centers:
- Full HPCMP fleet scraping
- PBS scheduler focus
- DSRC-specific terminology

### NOAA (`configs/config.noaa.yaml`)

Configured for NOAA RDHPCS:
- NOAA system definitions
- Slurm scheduler integration
- NOAA allocation tracking

## Configuration Precedence

Configuration values are applied in this order (later overrides earlier):

1. Built-in defaults
2. YAML config file
3. Environment variables
4. Command-line arguments

Example:
```bash
# YAML sets port to 8080
# Environment overrides to 9000
PORT=9000 CONFIG_FILE=configs/config.yaml ./scripts/run.sh
```

## Command-Line Arguments

The server accepts these arguments:

```
usage: python -m src.server.main [options]

options:
  --host HOST                 Bind address (default: 0.0.0.0)
  --port PORT                 HTTP port (default: 8080)
  --config FILE               Path to YAML config
  --url-prefix PREFIX         URL prefix for reverse proxy
  --default-theme THEME       Default theme: dark or light
  --enable-cluster-pages      Enable queue/quota pages
  --disable-cluster-pages     Disable queue/quota pages
  --enable-cluster-monitor    Enable background monitoring
  --disable-cluster-monitor   Disable background monitoring
  --cluster-monitor-interval  Refresh interval in seconds
```

## Validating Configuration

Test your configuration:

```bash
# Check config syntax
python -c "import yaml; yaml.safe_load(open('configs/config.yaml'))"

# Dry-run server startup
python -m src.server.main --config configs/config.yaml --help
```
