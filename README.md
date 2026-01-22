# HPC Status Monitor

Real-time HPC fleet status dashboard with queue health, quota usage, and system recommendations.

## Quick Start

```bash
# Using the run script (recommended)
./scripts/run.sh

# With specific configuration
CONFIG_FILE=configs/config.hpcmp.yaml ./scripts/run.sh

# Direct Python invocation
python -m src.server.main --config configs/config.yaml
```

Open [http://localhost:8080](http://localhost:8080) to view the dashboard.

## Features

- **Fleet Status**: Real-time view of HPC system availability across sites
- **Queue Health**: PBS/Slurm queue depth and wait time monitoring
- **Quota Usage**: Allocation tracking with usage warnings
- **Storage Monitoring**: Disk capacity alerts for home/work/scratch
- **Recommendations**: Intelligent queue scoring and load balancing suggestions
- **Multi-Platform**: Supports HPCMP, NOAA RDHPCS, and generic deployments

## Architecture

```
src/
├── collectors/         # Data collection modules
│   ├── base.py         # Abstract collector interface
│   ├── hpcmp.py        # HPCMP fleet status scraper
│   ├── pw_cluster.py   # Parallel Works cluster monitor
│   └── storage.py      # Storage capacity collector
├── data/
│   ├── models.py       # Data models
│   └── persistence.py  # SQLite + JSON cache storage
├── insights/
│   └── recommendations.py  # Queue scoring engine
└── server/
    ├── main.py         # Entry point
    ├── routes.py       # HTTP request handlers
    ├── workers.py      # Background refresh workers
    └── config.py       # Configuration management

web/                    # Frontend assets
├── index.html          # Fleet overview
├── queues.html         # Queue health page
├── quota.html          # Quota usage page
└── assets/
    ├── css/styles.css
    └── js/app.js
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | 8080 | Server port |
| `HOST` | 0.0.0.0 | Bind address |
| `DEFAULT_THEME` | dark | UI theme (dark/light) |
| `CONFIG_FILE` | - | Path to YAML config |
| `ENABLE_CLUSTER_PAGES` | 1 | Enable queue/quota pages |
| `ENABLE_CLUSTER_MONITOR` | 1 | Enable background monitoring |
| `CLUSTER_MONITOR_INTERVAL` | 120 | Refresh interval (seconds) |

### YAML Configuration

Create a config file for your deployment:

```yaml
deployment:
  name: "My HPC Monitor"
  platform: generic  # generic, hpcmp, or noaa

server:
  host: "0.0.0.0"
  port: 8080

collectors:
  hpcmp_fleet:
    enabled: true
    refresh_interval: 180
  pw_cluster:
    enabled: true
    refresh_interval: 120

ui:
  default_theme: "dark"
  tabs:
    overview: true
    queues: true
    quota: true
    storage: true
```

Pre-built configs: `configs/config.yaml` (generic), `configs/config.hpcmp.yaml`, `configs/config.noaa.yaml`

## REST API

| Endpoint | Description |
|----------|-------------|
| `GET /api/status` | Fleet status with system details |
| `GET /api/fleet/summary` | Condensed fleet overview |
| `GET /api/cluster-usage` | Queue and quota data for all clusters |
| `GET /api/cluster-usage/<cluster>` | Single cluster data |
| `POST /api/refresh` | Trigger immediate data refresh |

Example response from `/api/fleet/summary`:

```json
{
  "generated_at": "2026-01-22T15:30:00Z",
  "fleet_stats": {
    "total_systems": 10,
    "status_counts": {"UP": 9, "DEGRADED": 1}
  },
  "systems": [
    {
      "system": "Nautilus",
      "status": "UP",
      "dsrc": "NAVO",
      "scheduler": "PBS",
      "login_node": "nautilus.navo.hpc.mil"
    }
  ]
}
```

## Deployment

### Parallel Works (ACTIVATE)

The included `workflow.yaml` configures deployment on the Parallel Works platform:

```bash
# Launch with default settings
pw workflow launch workflow.yaml

# With platform-specific config
pw workflow launch workflow.yaml --platform hpcmp
```

### Docker

```bash
docker build -t hpc-status .
docker run -p 8080:8080 hpc-status
```

### Manual Installation

```bash
# Install dependencies
pip install -e .

# Or with uv
uv pip install -e .

# Run
python -m src.server.main
```

## Development

```bash
# Run tests
./scripts/test.sh

# Or directly
pytest tests/ -v
```

## Contributing

Source code: [github.com/parallelworks/hpcmp_status_site](https://github.com/parallelworks/hpcmp_status_site)

Pull requests welcome.

## License

See LICENSE file for details.
