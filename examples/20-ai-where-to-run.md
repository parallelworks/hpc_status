# AI Assistant: "Where Should I Run My Job?"

## Scenario

A user asks an AI assistant: "I need to run a 32-core job for about 4 hours. Where should I submit it?"

This example demonstrates how an AI assistant should use the HPC Status Monitor API to answer this question.

## AI Reasoning Process

### Step 1: Understand the Job Requirements

Extract from user query:
- **Cores needed**: 32
- **Walltime**: 4 hours
- **Special requirements**: None mentioned (no GPUs, no high memory)

### Step 2: Fetch System Status

**Request:**
```http
GET /api/status HTTP/1.1
```

**Response (partial):**
```json
{
  "summary": {
    "total_systems": 10,
    "status_counts": { "UP": 9, "DEGRADED": 1 }
  },
  "systems": [
    { "system": "Nautilus", "status": "UP", "scheduler": "PBS" },
    { "system": "Jean", "status": "UP", "scheduler": "PBS" },
    { "system": "Onyx", "status": "DEGRADED", "scheduler": "PBS" },
    { "system": "Gaffney", "status": "UP", "scheduler": "SLURM" }
  ]
}
```

**AI filters to UP systems**: Nautilus, Jean, Gaffney

### Step 3: Fetch Queue Information

**Request:**
```http
GET /api/cluster-usage HTTP/1.1
```

**Response (partial):**
```json
{
  "clusters": {
    "nautilus": {
      "connection_status": "CONNECTED",
      "queues": [
        {
          "name": "standard",
          "state": "ACTIVE",
          "queue_type": "BATCH",
          "constraints": {
            "max_walltime_seconds": 86400,
            "max_walltime_display": "24 hours",
            "max_nodes": 100
          },
          "jobs": { "running": 145, "pending": 89 },
          "wait_estimate": {
            "median_seconds": 300,
            "display": "~5 minutes"
          },
          "resources": {
            "availability": { "idle": 8500, "allocated": 38000, "pending": 12000 }
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
          "jobs": { "running": 5, "pending": 2 },
          "wait_estimate": { "display": "< 1 minute" }
        }
      ],
      "allocations": [
        {
          "project": "PROJ001",
          "hours": { "allocated": 250000, "used": 180000, "remaining": 70000 },
          "status": "HEALTHY"
        }
      ]
    },
    "jean": {
      "connection_status": "CONNECTED",
      "queues": [
        {
          "name": "standard",
          "state": "ACTIVE",
          "constraints": { "max_walltime_seconds": 86400 },
          "jobs": { "running": 200, "pending": 150 },
          "wait_estimate": { "median_seconds": 1800, "display": "~30 minutes" }
        }
      ],
      "allocations": [
        {
          "project": "PROJ001",
          "hours": { "remaining": 25000 },
          "status": "LOW"
        }
      ]
    },
    "gaffney": {
      "connection_status": "CONNECTED",
      "queues": [
        {
          "name": "batch",
          "state": "ACTIVE",
          "constraints": { "max_walltime_seconds": 172800 },
          "jobs": { "running": 80, "pending": 200 },
          "wait_estimate": { "median_seconds": 3600, "display": "~1 hour" }
        }
      ]
    }
  }
}
```

### Step 4: Evaluate Options

| System | Queue | Walltime OK? | Wait Estimate | Allocation Status | Score |
|--------|-------|--------------|---------------|-------------------|-------|
| Nautilus | standard | ✅ 24h > 4h | ~5 min | HEALTHY | ⭐⭐⭐ |
| Nautilus | debug | ❌ 30m < 4h | ~1 min | HEALTHY | N/A |
| Jean | standard | ✅ 24h > 4h | ~30 min | LOW | ⭐⭐ |
| Gaffney | batch | ✅ 48h > 4h | ~1 hour | Unknown | ⭐ |

**Elimination logic:**
- Nautilus debug: 30 min max walltime < 4 hour job requirement ❌
- Jean: Wait is 6x longer than Nautilus, allocation is LOW
- Gaffney: Longest wait time

**Winner: Nautilus standard queue**

### Step 5: Verify Allocation

The user's allocation on Nautilus:
- 70,000 hours remaining
- Job needs: 32 cores × 4 hours = 128 core-hours
- Plenty of headroom ✅

## AI Response to User

---

**User:** I need to run a 32-core job for about 4 hours. Where should I submit it?

**AI Assistant:**

Based on current system status and queue availability, I recommend **Nautilus** using the **standard** queue.

**Why Nautilus?**
- ✅ System is UP and fully operational
- ✅ Standard queue accepts jobs up to 24 hours (your job is 4 hours)
- ✅ Shortest wait time: ~5 minutes (vs 30 min on Jean, 1 hour on Gaffney)
- ✅ Your PROJ001 allocation has 70,000 hours remaining

**To submit:**
```bash
ssh nautilus.navo.hpc.mil
qsub -q standard -l select=1:ncpus=32 -l walltime=4:00:00 your_script.pbs
```

**Alternatives if Nautilus is busy later:**
- **Jean standard queue**: ~30 min wait, but allocation is running low
- **Gaffney batch queue**: ~1 hour wait, longer max walltime if needed

Would you like me to help prepare your PBS job script?

---

## Key Patterns for AI Assistants

### Information to Always Check

1. **System status** - Is the target system UP?
2. **Queue constraints** - Does max walltime accommodate the job?
3. **Queue depth** - How long is the wait?
4. **Allocation status** - Are there sufficient hours?
5. **Resource availability** - Are cores actually free?

### Red Flags to Warn About

- System status is DEGRADED or DOWN
- Allocation is LOW or CRITICAL
- Queue has very long wait times
- Job requirements exceed queue limits

### Confidence Indicators

When giving recommendations, AI should indicate confidence:
- **High confidence**: All systems checked, clear winner
- **Medium confidence**: Multiple viable options, user preference may matter
- **Low confidence**: Data is stale, or unusual conditions

### Follow-up Questions to Ask

If requirements are unclear:
- "Do you need GPU resources?"
- "Is this a debug/test run or a production job?"
- "Do you have a specific project allocation to use?"
- "Does the job have high memory requirements?"
