# HPC Status Monitor - JSON Schemas

This directory contains JSON Schema definitions for all data models and API responses used by the HPC Status Monitor.

## Purpose

These schemas serve multiple purposes:

1. **Machine-readable contracts** - Enable automated validation and code generation
2. **AI-assist readiness** - Provide structure for LLMs to understand and generate correct API interactions
3. **Documentation** - Serve as authoritative reference for field types and constraints
4. **Integration testing** - Validate API responses against expected shapes

## Schema Organization

### Core Models (`models/`)

Data structures representing HPC concepts:

- `system-status.schema.json` - HPC system operational status
- `queue-info.schema.json` - Scheduler queue/partition state
- `allocation-info.schema.json` - Compute time allocation tracking
- `storage-info.schema.json` - Filesystem capacity tracking
- `cluster-data.schema.json` - Complete PW cluster information
- `fleet-summary.schema.json` - Aggregated fleet statistics
- `system-insight.schema.json` - Generated recommendations and alerts

### API Responses (`api/`)

Complete response shapes for each endpoint:

- `status-response.schema.json` - `/api/status`
- `fleet-summary-response.schema.json` - `/api/fleet/summary`
- `cluster-usage-response.schema.json` - `/api/cluster-usage`
- `health-response.schema.json` - `/api/health`

### Common Definitions (`common/`)

Shared type definitions:

- `units.schema.json` - Standard units (bytes, hours, cores)
- `status-enum.schema.json` - Canonical status values
- `timestamp.schema.json` - ISO 8601 timestamp format

## Schema Conventions

### Field Naming

- Use `snake_case` for all field names
- Use `_count` suffix for integer counts: `job_count`, `core_count`
- Use `_at` suffix for timestamps: `observed_at`, `updated_at`
- Use `_ratio` or `_percent` suffix for fractions: `uptime_ratio`, `used_percent`

### Units Convention

All numeric fields that represent physical quantities include explicit units:

```json
{
  "capacity_cores": {
    "type": "integer",
    "description": "Total CPU cores",
    "x-unit": "cores"
  },
  "walltime_max_seconds": {
    "type": "integer",
    "description": "Maximum job walltime",
    "x-unit": "seconds"
  }
}
```

The `x-unit` extension provides machine-readable unit information.

### Capacity vs Availability

We distinguish between:

- **Capacity**: Total/maximum resources (static, changes with hardware)
- **Availability**: Currently usable resources (dynamic, changes with load)

Example:
```json
{
  "cores": {
    "capacity": 10000,    // Total cores in system
    "available": 1500,    // Cores free right now
    "pending": 3000       // Cores requested by queued jobs
  }
}
```

## Validation

Validate a response against a schema:

```bash
# Using ajv-cli
npx ajv validate -s schemas/api/status-response.schema.json -d response.json

# Using Python jsonschema
python -c "
import json
from jsonschema import validate
schema = json.load(open('schemas/api/status-response.schema.json'))
data = json.load(open('response.json'))
validate(data, schema)
print('Valid!')
"
```

## Version

Schema version: 1.0.0

Schemas follow semantic versioning. Breaking changes increment the major version.
