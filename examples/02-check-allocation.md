# Check Allocation Before Job Submission

## Scenario

A researcher wants to verify they have sufficient compute hours remaining before submitting a batch of jobs that will consume significant allocation.

## Context

- **User role**: HPC end user (researcher)
- **Question**: "Do I have enough hours left for 100 jobs × 32 cores × 4 hours = 12,800 core-hours?"
- **Goal**: Avoid job rejection due to exhausted allocation

## Request

```http
GET /api/cluster-usage/nautilus HTTP/1.1
Host: localhost:8080
Accept: application/json
```

Or using curl:
```bash
curl -s http://localhost:8080/api/cluster-usage/nautilus | jq '.allocations'
```

## Response

```json
{
  "name": "Nautilus",
  "cluster_id": "nautilus",
  "connection_status": "CONNECTED",
  "last_updated": "2026-01-29T15:30:00Z",
  "allocations": [
    {
      "project": "ONRDC12345678",
      "project_id": "onrdc12345678",
      "system": "Nautilus",
      "allocation_type": "STANDARD",
      "period": {
        "start": "2025-10-01",
        "end": "2026-09-30",
        "label": "FY2026"
      },
      "hours": {
        "allocated": 250000,
        "used": 235000,
        "remaining": 15000,
        "pending": 3200
      },
      "usage": {
        "percent_used": 94.0,
        "percent_remaining": 6.0,
        "burn_rate_hours_per_day": 800,
        "projected_depletion_date": "2026-02-15",
        "days_remaining_at_current_rate": 18
      },
      "status": "CRITICAL"
    },
    {
      "project": "BACKUP_PROJ",
      "hours": {
        "allocated": 50000,
        "used": 10000,
        "remaining": 40000,
        "pending": 0
      },
      "usage": {
        "percent_used": 20.0,
        "percent_remaining": 80.0
      },
      "status": "HEALTHY"
    }
  ],
  "insights": [
    {
      "type": "ALERT",
      "severity": "CRITICAL",
      "message": "ONRDC12345678: Allocation nearly exhausted (6% remaining). Jobs may be rejected.",
      "scope": {
        "project": "ONRDC12345678"
      },
      "action": {
        "description": "Request additional allocation hours immediately"
      }
    }
  ]
}
```

## Interpretation

### Allocation Status Values

| Status | Meaning | Action |
|--------|---------|--------|
| `HEALTHY` | >20% remaining | No action needed |
| `LOW` | 5-20% remaining | Plan for renewal |
| `CRITICAL` | <5% remaining | Request hours immediately |
| `EXHAUSTED` | 0% remaining | Jobs will be rejected |
| `EXPIRED` | Period ended | Cannot use until renewed |

### Key Fields

- **`hours.remaining`**: Core-hours still available
- **`hours.pending`**: Hours committed to queued jobs (may exceed remaining!)
- **`usage.burn_rate_hours_per_day`**: Recent consumption rate
- **`usage.projected_depletion_date`**: When allocation will run out
- **`status`**: Health status based on percent_remaining

### Checking Sufficiency

Job requirements: 100 jobs × 32 cores × 4 hours = **12,800 core-hours**

From response:
- `hours.remaining`: 15,000 (enough for one batch)
- `hours.pending`: 3,200 (already queued jobs)
- Effective remaining: 15,000 - 3,200 = **11,800 core-hours**

⚠️ **Insufficient!** Need 12,800 but only 11,800 effectively available.

## Actions Based on Response

### If Sufficient Hours

✅ **Proceed with job submission**

```bash
# Submit jobs using primary allocation
qsub -A ONRDC12345678 -l select=1:ncpus=32 -l walltime=4:00:00 job.pbs
```

### If Insufficient Hours (Primary Allocation)

⚠️ **Options:**

1. **Use backup allocation** (if available and healthy):
   ```bash
   qsub -A BACKUP_PROJ -l select=1:ncpus=32 -l walltime=4:00:00 job.pbs
   ```

2. **Reduce job scope**: Submit fewer jobs now, more later

3. **Request additional hours**: Contact allocation committee

### If Status is CRITICAL or EXHAUSTED

❌ **Do not submit large batches**

- Small debug jobs may still work
- Request allocation renewal urgently
- Consider alternative systems with healthier allocations

## Pre-Submission Check Script

```python
import requests
import sys

def check_allocation(system, project, required_hours):
    """Check if allocation has sufficient hours for job batch."""
    response = requests.get(f"http://localhost:8080/api/cluster-usage/{system}")
    data = response.json()

    for alloc in data.get("allocations", []):
        if alloc.get("project") == project:
            remaining = alloc["hours"]["remaining"]
            pending = alloc["hours"].get("pending", 0)
            effective = remaining - pending
            status = alloc.get("status", "UNKNOWN")

            print(f"Project: {project}")
            print(f"  Remaining: {remaining:,} hours")
            print(f"  Pending: {pending:,} hours")
            print(f"  Effective: {effective:,} hours")
            print(f"  Required: {required_hours:,} hours")
            print(f"  Status: {status}")

            if effective >= required_hours:
                print(f"✅ Sufficient allocation")
                return True
            else:
                shortfall = required_hours - effective
                print(f"⚠️ Insufficient! Need {shortfall:,} more hours")
                return False

    print(f"❌ Project {project} not found")
    return False

if __name__ == "__main__":
    # 100 jobs × 32 cores × 4 hours
    required = 100 * 32 * 4
    if not check_allocation("nautilus", "ONRDC12345678", required):
        sys.exit(1)
```

## Related Examples

- [01-check-fleet-status.md](01-check-fleet-status.md) - Check if system is up
- [20-ai-where-to-run.md](20-ai-where-to-run.md) - AI-assisted system selection
- [03-find-best-queue.md](03-find-best-queue.md) - Choose optimal queue
