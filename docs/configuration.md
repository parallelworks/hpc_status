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
    queues: true              # Show queue health tab
    quota: true               # Show quota usage tab
    storage: true             # Show storage tab
  branding:
    title: "HPC Status"       # Page title
    logo: ""                  # Optional logo URL
```

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
