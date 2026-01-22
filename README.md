# HPC Status Monitor

Real-time dashboard for monitoring HPC fleet status, queue health, quota usage, and storage across multiple systems.

## Quick Start

```bash
./scripts/run.sh
```

Open [http://localhost:8080](http://localhost:8080) to view the dashboard.

## Local Mode with PW CLI

Monitor your own clusters locally by authenticating with the [Parallel Works CLI](https://docs.parallel.works/):

```bash
# Install PW CLI (if not already installed)
pip install pw-client

# Authenticate with your API key
pw auth

# Run the dashboard
./scripts/run.sh
```

The dashboard will automatically detect your authenticated PW CLI session and collect data from all connected clusters, including:
- HPC clusters with PBS/Slurm schedulers (queue depth, quota usage)
- GPU servers (nvidia-smi metrics, utilization, memory)
- Compute nodes (CPU, memory, load averages)

Your API key can be found in your ACTIVATE account under "API Keys".

## Features

- **Fleet Status** - Real-time system availability across sites
- **Queue Health** - PBS/Slurm queue depth and wait times
- **Quota Usage** - Allocation tracking with warnings
- **Storage Monitoring** - Disk capacity for home/work/scratch
- **Recommendations** - Queue scoring and load balancing suggestions

## Configuration

Use a custom config file:

```bash
CONFIG_FILE=configs/my-config.yaml ./scripts/run.sh
```

See [docs/configuration.md](docs/configuration.md) for all options.

## Documentation

| Guide | Description |
|-------|-------------|
| [Deployment](docs/deployment.md) | Installation, Docker, ACTIVATE workflow |
| [Configuration](docs/configuration.md) | YAML options, environment variables |
| [API Reference](docs/api.md) | REST endpoints and responses |

## Development

```bash
# Run tests
./scripts/test.sh

# Direct invocation
python -m src.server.main --config configs/config.yaml
```

## License

See LICENSE file for details.
