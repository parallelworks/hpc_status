# Find the Best Queue for Your Job

## Scenario

A user needs to submit a job with specific requirements and wants to find the queue with the shortest wait time that can accommodate their job.

## Context

- **User role**: HPC end user
- **Job requirements**: 64 cores, 8-hour walltime, no GPUs
- **Goal**: Minimize time-to-completion (submission to finish)

## Request

```http
GET /api/cluster-usage HTTP/1.1
Host: localhost:8080
Accept: application/json
```

Or using curl to filter to relevant data:
```bash
curl -s http://localhost:8080/api/cluster-usage | jq '
  .clusters | to_entries[] |
  select(.value.connection_status == "CONNECTED") |
  {
    system: .key,
    queues: [.value.queues[] | select(.state == "ACTIVE") | {
      name: .name,
      max_walltime: .constraints.max_walltime_display,
      pending_jobs: .jobs.pending,
      wait_estimate: .wait_estimate.display
    }]
  }
'
```

## Response

```json
{
  "meta": {
    "observed_at": "2026-01-29T15:30:00Z"
  },
  "clusters": {
    "nautilus": {
      "name": "Nautilus",
      "connection_status": "CONNECTED",
      "scheduler": "PBS",
      "queues": [
        {
          "name": "standard",
          "state": "ACTIVE",
          "queue_type": "BATCH",
          "constraints": {
            "max_walltime_seconds": 86400,
            "max_walltime_display": "24 hours",
            "max_nodes": 100,
            "max_cores": null
          },
          "jobs": {
            "running": 145,
            "pending": 89,
            "held": 2
          },
          "wait_estimate": {
            "median_seconds": 1200,
            "display": "~20 minutes"
          },
          "resources": {
            "availability": {
              "idle": 2500,
              "allocated": 45000
            }
          }
        },
        {
          "name": "debug",
          "state": "ACTIVE",
          "queue_type": "DEBUG",
          "constraints": {
            "max_walltime_seconds": 1800,
            "max_walltime_display": "30 minutes",
            "max_nodes": 4
          },
          "jobs": {
            "running": 5,
            "pending": 1
          },
          "wait_estimate": {
            "display": "< 1 minute"
          }
        },
        {
          "name": "gpu",
          "state": "ACTIVE",
          "queue_type": "GPU",
          "constraints": {
            "max_walltime_seconds": 172800,
            "max_walltime_display": "48 hours"
          },
          "jobs": {
            "running": 20,
            "pending": 45
          },
          "wait_estimate": {
            "display": "~2 hours"
          }
        }
      ]
    },
    "jean": {
      "name": "Jean",
      "connection_status": "CONNECTED",
      "scheduler": "PBS",
      "queues": [
        {
          "name": "standard",
          "state": "ACTIVE",
          "constraints": {
            "max_walltime_seconds": 86400,
            "max_walltime_display": "24 hours"
          },
          "jobs": {
            "pending": 200
          },
          "wait_estimate": {
            "median_seconds": 7200,
            "display": "~2 hours"
          }
        },
        {
          "name": "background",
          "state": "ACTIVE",
          "queue_type": "BATCH",
          "constraints": {
            "max_walltime_seconds": 172800,
            "max_walltime_display": "48 hours"
          },
          "jobs": {
            "pending": 50
          },
          "wait_estimate": {
            "display": "~4 hours"
          }
        }
      ]
    },
    "gaffney": {
      "name": "Gaffney",
      "connection_status": "CONNECTED",
      "scheduler": "SLURM",
      "queues": [
        {
          "name": "batch",
          "state": "DRAINING",
          "constraints": {
            "max_walltime_seconds": 86400
          },
          "jobs": {
            "pending": 10
          }
        }
      ]
    }
  }
}
```

## Queue Selection Logic

### Step 1: Filter by State

Only consider queues with `state: "ACTIVE"`. Exclude:
- `DRAINING` - Not accepting new jobs (Gaffney batch)
- `INACTIVE` - Disabled
- `OFFLINE` - Unavailable

### Step 2: Filter by Constraints

Job requires: 64 cores, 8-hour walltime

| System | Queue | Max Walltime | Meets Walltime? |
|--------|-------|--------------|-----------------|
| Nautilus | standard | 24 hours | ✅ Yes |
| Nautilus | debug | 30 minutes | ❌ No (8h > 0.5h) |
| Nautilus | gpu | 48 hours | ✅ Yes |
| Jean | standard | 24 hours | ✅ Yes |
| Jean | background | 48 hours | ✅ Yes |
| Gaffney | batch | 24 hours | ❌ Draining |

Remaining candidates: Nautilus standard, Nautilus gpu, Jean standard, Jean background

### Step 3: Rank by Wait Time

| System | Queue | Wait Estimate | Pending Jobs | Score |
|--------|-------|---------------|--------------|-------|
| Nautilus | standard | ~20 minutes | 89 | ⭐⭐⭐⭐ |
| Jean | background | ~4 hours | 50 | ⭐⭐ |
| Jean | standard | ~2 hours | 200 | ⭐⭐⭐ |
| Nautilus | gpu | ~2 hours | 45 | ⭐⭐⭐ (but not needed) |

### Step 4: Consider Job Type

- Job has **no GPUs** → Avoid GPU queue (wastes resources, may wait longer)
- Standard batch job → Prefer `standard` or `batch` queues

### Recommendation

**Best choice: Nautilus standard queue**
- Meets walltime requirement (24h > 8h)
- Shortest wait time (~20 minutes)
- Appropriate queue type for non-GPU batch job

## Submission Command

```bash
# PBS (Nautilus, Jean)
qsub -q standard -l select=2:ncpus=32 -l walltime=8:00:00 job.pbs

# Slurm (if using a Slurm system)
sbatch --partition=batch --nodes=2 --ntasks-per-node=32 --time=8:00:00 job.sh
```

## Decision Matrix Template

For systematic queue selection, use this template:

```python
def score_queue(queue, requirements):
    """Score a queue for given job requirements."""
    # Check hard constraints
    max_walltime_hours = queue.get("constraints", {}).get("max_walltime_seconds", 0) / 3600
    if requirements["walltime_hours"] > max_walltime_hours:
        return 0  # Cannot use this queue

    if queue.get("state") != "ACTIVE":
        return 0  # Queue not accepting jobs

    # Score based on wait time (lower is better)
    wait_seconds = queue.get("wait_estimate", {}).get("median_seconds", 3600)
    wait_score = max(0, 100 - (wait_seconds / 60))  # Penalize long waits

    # Bonus for matching queue type
    queue_type = queue.get("queue_type", "").upper()
    type_bonus = 0
    if requirements.get("gpus", 0) > 0 and queue_type == "GPU":
        type_bonus = 20
    elif requirements.get("gpus", 0) == 0 and queue_type != "GPU":
        type_bonus = 10  # Prefer non-GPU queues for non-GPU jobs

    # Penalty for high pending jobs
    pending = queue.get("jobs", {}).get("pending", 0)
    pending_penalty = min(30, pending * 0.2)

    return wait_score + type_bonus - pending_penalty

# Example usage
requirements = {"cores": 64, "walltime_hours": 8, "gpus": 0}
for cluster, data in clusters.items():
    for queue in data.get("queues", []):
        score = score_queue(queue, requirements)
        if score > 0:
            print(f"{cluster}/{queue['name']}: score={score:.1f}")
```

## Related Examples

- [01-check-fleet-status.md](01-check-fleet-status.md) - Check system availability
- [02-check-allocation.md](02-check-allocation.md) - Verify allocation hours
- [20-ai-where-to-run.md](20-ai-where-to-run.md) - AI-assisted decision making
