# Deployment Guide

This guide covers deploying the HPC Status Monitor on various platforms.

## Prerequisites

- Python 3.9+
- Network access to HPC systems (for cluster monitoring)
- Parallel Works CLI (optional, for cluster data collection)

## Parallel Works (ACTIVATE Platform)

The recommended deployment method for Parallel Works users.

### Using the Workflow

1. Clone the repository to your PW workspace:
   ```bash
   git clone https://github.com/parallelworks/hpcmp_status_site.git
   cd hpcmp_status_site
   ```

2. Launch the workflow:
   ```bash
   pw workflow launch workflow.yaml
   ```

3. Configure via workflow inputs:
   - `platform`: Select `generic`, `hpcmp`, or `noaa`
   - `port`: Server port (default: 8080)
   - `theme`: Default theme `dark` or `light`
   - `enable_cluster_pages`: Enable queue/quota pages
   - `enable_cluster_monitor`: Enable background monitoring

### Workflow Configuration

The `workflow.yaml` file supports these inputs:

```yaml
inputs:
  platform:
    type: string
    default: "generic"
    enum: [generic, hpcmp, noaa]
  port:
    type: number
    default: 8080
  theme:
    type: string
    default: "dark"
  enable_cluster_pages:
    type: boolean
    default: true
  enable_cluster_monitor:
    type: boolean
    default: true
  cluster_monitor_interval:
    type: number
    default: 120
```

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
