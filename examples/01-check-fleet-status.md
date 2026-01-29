# Check Fleet Status Before Job Submission

## Scenario

A researcher wants to verify that their target HPC system is operational before preparing and submitting a large batch job.

## Context

- **User role**: HPC end user (researcher)
- **Question**: "Is Nautilus up? Can I submit jobs there?"
- **Goal**: Avoid wasted effort if the system is down

## Request

```http
GET /api/status HTTP/1.1
Host: localhost:8080
Accept: application/json
```

Or using curl:
```bash
curl -s http://localhost:8080/api/status | jq '.systems[] | select(.system == "Nautilus")'
```

## Response

```json
{
  "meta": {
    "observed_at": "2026-01-29T15:30:00Z",
    "source": "hpcmp_collector",
    "from_cache": false
  },
  "summary": {
    "total_systems": 10,
    "status_counts": {
      "UP": 9,
      "DEGRADED": 1
    },
    "uptime_ratio": 0.9,
    "uptime_percent": 90
  },
  "systems": [
    {
      "system": "Nautilus",
      "system_id": "nautilus",
      "status": "UP",
      "scheduler": "PBS",
      "site": {
        "name": "NAVO",
        "organization": "DoD HPCMP"
      },
      "access": {
        "login_host": "nautilus.navo.hpc.mil"
      },
      "observed_at": "2026-01-29T15:30:00Z"
    },
    {
      "system": "Onyx",
      "system_id": "onyx",
      "status": "DEGRADED",
      "status_reason": "Scheduled maintenance affecting 20% of compute nodes",
      "scheduler": "PBS",
      "site": {
        "name": "ERDC"
      },
      "observed_at": "2026-01-29T15:30:00Z"
    }
  ]
}
```

## Interpretation

### Status Values

| Status | Meaning | Can Submit Jobs? |
|--------|---------|------------------|
| `UP` | System fully operational | Yes |
| `DEGRADED` | Reduced capacity or performance | Yes, but may experience delays |
| `MAINTENANCE` | Planned maintenance in progress | Usually no |
| `DOWN` | System offline | No |
| `UNKNOWN` | Status could not be determined | Check manually |

### Key Fields

- **`status`**: Current operational state
- **`status_reason`**: Explanation for non-UP status (present when applicable)
- **`access.login_host`**: Hostname to SSH to
- **`observed_at`**: How recent this information is

### Data Freshness

Check `meta.observed_at` to see how recent the status is. If it's more than a few minutes old and `meta.from_cache` is `true`, the data may be stale.

## Actions Based on Response

### If Status is UP

✅ **Proceed with job submission**

```bash
ssh nautilus.navo.hpc.mil
# ... prepare and submit jobs
```

### If Status is DEGRADED

⚠️ **Consider alternatives or proceed with caution**

- Read `status_reason` to understand the impact
- If capacity is reduced, expect longer wait times
- Consider using a different system if available

### If Status is DOWN or MAINTENANCE

❌ **Do not submit jobs**

- Check for alternative systems with `status: UP`
- Look for maintenance announcements for expected restore time

## Automation Example

Pre-flight check before job submission:

```python
import requests
import sys

def check_system_status(system_name):
    response = requests.get("http://localhost:8080/api/status")
    data = response.json()

    for system in data["systems"]:
        if system["system"].lower() == system_name.lower():
            if system["status"] == "UP":
                print(f"✅ {system_name} is operational")
                return True
            else:
                print(f"⚠️ {system_name} status: {system['status']}")
                if "status_reason" in system:
                    print(f"   Reason: {system['status_reason']}")
                return False

    print(f"❌ {system_name} not found in fleet")
    return False

if __name__ == "__main__":
    if not check_system_status("Nautilus"):
        sys.exit(1)
```

## Related Examples

- [02-find-available-queue.md](02-find-available-queue.md) - Check queue availability
- [03-check-allocation.md](03-check-allocation.md) - Verify you have hours remaining
- [10-pre-submit-validation.md](10-pre-submit-validation.md) - Full pre-submission validation
