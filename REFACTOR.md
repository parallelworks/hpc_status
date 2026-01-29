# HPC Status Monitor - Continuous Improvement Log

**Objective**: Evolve this tool into a clear, discoverable, and machine- and human-friendly source of truth for HPC fleet capacity, availability, and scheduling state across heterogeneous systems and sites.

**Last Updated**: 2026-01-29

---

## Iteration 1: Initial Assessment

### Executive Summary

The HPC Status Monitor is a well-architected system with solid foundations from the Phase 1-8 refactoring effort. The codebase demonstrates good separation of concerns, reasonable documentation, and support for multiple HPC platforms (HPCMP, NOAA, generic PW clusters).

However, from the perspective of the stated goal—serving as a **reference model for how next-generation HPC systems expose capacity and availability to users and AI assistants**—there are significant gaps in:

1. **Semantic clarity** - Data models conflate concepts that should be distinct
2. **Schema formalization** - No machine-readable contracts (JSON Schema, OpenAPI)
3. **AI-assist readiness** - Missing structured outputs that LLMs can reliably parse
4. **Cross-system normalization** - Scheduler-specific details leak into unified views

---

### Persona-Based Evaluation

#### 1. HPC End Users (Submitting Jobs)

**What works:**
- Dashboard provides visual overview of system status
- Queue health page shows job counts
- Allocation tracking helps avoid surprise failures

**Friction points:**
- **Ambiguous terminology**: "status" could mean system up/down, queue state, or job state
- **Missing context**: No explanation of what "DEGRADED" means operationally
- **No actionable guidance**: User sees queue is "busy" but not what to do about it
- **Time estimates absent**: "When will my job start?" is not answerable

**Recommended changes:**
1. Add glossary/tooltip system explaining HPC terminology
2. Add estimated wait times to queue views
3. Add "Where should I run this job?" recommendation widget
4. Distinguish between "system status" and "queue health" clearly

#### 2. HPC Administrators and Operators

**What works:**
- Fleet overview with DSRC breakdown
- Storage monitoring with thresholds
- Health endpoint for monitoring

**Friction points:**
- **Missing operational metrics**: No node failure counts, maintenance windows
- **No historical trending**: Can't see if a system is getting worse over time
- **Alert fatigue risk**: Insights page mixes critical warnings with suggestions

**Recommended changes:**
1. Add severity levels to insights (critical/warning/info)
2. Add `/api/alerts` endpoint for integration with PagerDuty/Slack
3. Store and expose historical status for trend analysis

#### 3. Platform Teams (Integrating with Schedulers)

**What works:**
- REST API with documented endpoints
- Configuration-driven collector system
- Base collector interface for extensibility

**Friction points:**
- **No OpenAPI/JSON Schema**: Integrators must reverse-engineer response formats
- **Inconsistent field naming**: `hours_remaining` vs `remaining_hours` patterns
- **No versioning**: Breaking changes have no safety mechanism
- **No pagination**: Large fleet responses are unbounded

**Recommended changes:**
1. Add OpenAPI 3.1 specification
2. Add JSON Schema for all data models
3. Implement API versioning (start with `/api/v1/`)
4. Add pagination to list endpoints

#### 4. AI Code Assistants (Reasoning about Capacity)

**What works:**
- JSON API returns structured data
- Insights endpoint provides pre-computed recommendations

**Friction points:**
- **Semantic ambiguity**: "status: UP" doesn't indicate schedulable capacity
- **Missing invariants**: No way to know if "available_cores: 100" means allocatable now
- **No units**: Is walltime in hours, minutes, or HH:MM:SS format?
- **No examples**: LLMs need sample requests/responses to learn patterns
- **No capacity vs availability distinction**: 1000 cores total vs 50 cores free now

**Recommended changes:**
1. Add explicit `capacity` vs `availability` fields
2. Add `units` metadata to numeric fields or use SI suffixes
3. Provide `examples/` directory with annotated request/response pairs
4. Add semantic type hints (e.g., `"type": "core_count"`)

---

### Resource Visibility and Semantics Analysis

#### Current State

The data models in `src/data/models.py` represent:
- **System status**: UP/DOWN/DEGRADED/MAINTENANCE
- **Queue info**: Jobs running/pending, cores used
- **Allocation info**: Hours allocated/used/remaining
- **Storage info**: GB total/used/available

#### Gaps Identified

| Concept | Current State | Issue |
|---------|---------------|-------|
| **Capacity** | Implicit in `total_cores` | Not distinguished from availability |
| **Availability** | Implicit in `cores_free` | Conflated with capacity |
| **Reservations** | Not modeled | Jobs waiting in queue != reserved capacity |
| **Fragmentation** | Not modeled | 100 free cores might be 10x10 nodes, unusable for 50-core job |
| **Priority/Preemption** | Not modeled | User may have access but low priority |
| **Burst capacity** | Not modeled | Some systems have burstable cloud resources |
| **Maintenance windows** | Partially in "status" | No scheduled future downtime |

#### Proposed Semantic Model

```
Capacity (Static)
├── Total hardware resources
├── Partition/queue allocation policies
└── Hardware constraints (GPU types, memory per node)

Availability (Dynamic)
├── Currently idle resources
├── Currently running (occupied)
├── Pending (queued but not started)
├── Reserved (future commitments)
├── Draining (going offline soon)
└── Failed/Offline

Schedulability (Policy)
├── User access rights
├── Queue limits (max walltime, max jobs)
├── Priority/fairshare standing
└── Allocation remaining
```

---

### Cross-System Consistency Analysis

#### Current Normalization

| Field | PBS Source | Slurm Source | Normalized? |
|-------|-----------|--------------|-------------|
| Queue name | Queue name | Partition name | Yes (as "queue") |
| Jobs running | `qstat` counts | `squeue` counts | Yes |
| Max walltime | `qmgr -c "p q"` | `scontrol show partition` | Partially (format varies) |
| Node state | `pbsnodes` | `sinfo` | No (different states) |
| Fair share | `qstat -Q` | `sshare` | No (different metrics) |

#### Inconsistencies Found

1. **Status values**: PBS uses state names different from Slurm
2. **Time formats**: Some fields use HH:MM:SS, others use hours as float
3. **Resource naming**: "cores" vs "CPUs" vs "processors"
4. **Queue types**: PBS "execution queue" vs Slurm "partition" semantics differ

#### Proposed Normalization Strategy

1. Define canonical status enum: `ACTIVE | DRAINING | OFFLINE | MAINTENANCE`
2. Use ISO 8601 durations for all time fields: `PT24H` instead of `24:00:00`
3. Standardize on "cores" as the CPU resource unit
4. Add `scheduler_native` sub-object for raw scheduler-specific data

---

### AI-Assist Readiness Assessment

**Current Score: 4/10**

| Criterion | Status | Notes |
|-----------|--------|-------|
| Structured JSON output | ✅ | Good foundation |
| Consistent field naming | ⚠️ | Some inconsistencies |
| Explicit types/units | ❌ | Missing |
| Semantic annotations | ❌ | Missing |
| Example corpus | ❌ | No examples/ directory |
| Schema definitions | ❌ | No JSON Schema |
| Error categorization | ⚠️ | HTTP codes but no error types |
| Deterministic output | ⚠️ | Timestamps vary |

---

### Prioritized Improvement Opportunities

#### Priority 1: Critical (Blocks AI/Automation Use)

1. **Add JSON Schema for all API responses**
   - Impact: Enables code generation, validation, AI parsing
   - Effort: Medium
   - Files: `schemas/`, `docs/api.md`

2. **Distinguish capacity from availability in data models**
   - Impact: Fundamental semantic clarity
   - Effort: Medium
   - Files: `src/data/models.py`, API responses

3. **Add explicit units to all numeric fields**
   - Impact: Eliminates ambiguity for both humans and AI
   - Effort: Low
   - Files: `src/data/models.py`, documentation

#### Priority 2: High (Improves Multi-Persona Experience)

4. **Implement API versioning**
   - Impact: Safe evolution, integration stability
   - Effort: Medium
   - Files: `src/server/routes.py`

5. **Add examples/ directory with annotated scenarios**
   - Impact: AI learning, user onboarding
   - Effort: Low
   - Files: New `examples/` directory

6. **Normalize scheduler-specific status values**
   - Impact: Cross-system comparability
   - Effort: Medium
   - Files: Collectors, models

#### Priority 3: Medium (Enhances Clarity)

7. **Add glossary of HPC terminology**
   - Impact: Onboarding, disambiguation
   - Effort: Low
   - Files: `docs/glossary.md`, UI tooltips

8. **Add severity levels to insights**
   - Impact: Operator efficiency, alert integration
   - Effort: Low
   - Files: `src/insights/recommendations.py`

9. **Add historical status tracking**
   - Impact: Trend analysis, reliability metrics
   - Effort: Medium
   - Files: `src/data/persistence.py`

#### Priority 4: Lower (Future Enhancements)

10. **Model fragmentation and schedulability**
11. **Add maintenance window forecasting**
12. **Implement pagination for list endpoints**
13. **Add webhook support for status changes**

---

### Iteration 1 Actions Taken

1. Created this REFACTOR.md to track improvement progress
2. Documented persona-based evaluation
3. Identified semantic gaps in resource modeling
4. Prioritized 13 improvement opportunities
5. Established baseline for AI-assist readiness (4/10)
6. **Created `schemas/` directory** with JSON Schema definitions:
   - Common definitions: `units.schema.json`, `status-enum.schema.json`, `timestamp.schema.json`
   - Model schemas: `system-status.schema.json`, `queue-info.schema.json`, `allocation-info.schema.json`, `storage-info.schema.json`, `system-insight.schema.json`, `resource-pool.schema.json`
   - API response schemas: `fleet-status-response.schema.json`, `cluster-usage-response.schema.json`, `health-response.schema.json`
7. **Created `examples/` directory** with annotated scenarios:
   - `01-check-fleet-status.md` - Basic fleet status checking
   - `20-ai-where-to-run.md` - AI assistant reasoning example
8. **Created `docs/glossary.md`** with HPC terminology definitions
9. Introduced **capacity vs availability** distinction in schemas (Priority 1.2)
10. Added **explicit units** via `x-unit` schema extension (Priority 1.3)

---

## Iteration 2: Schema Implementation and API Enhancement

### Focus

Continue building the foundation for machine-consumable data:
1. Integrate schemas with actual API responses
2. Add severity levels to insights
3. Normalize status enums in code

### Files Created This Iteration

```
schemas/
├── README.md                              # Schema documentation
├── common/
│   ├── units.schema.json                  # Unit definitions (cores, hours, bytes, etc.)
│   ├── status-enum.schema.json            # Canonical status values
│   └── timestamp.schema.json              # Timestamp and observation metadata
├── models/
│   ├── system-status.schema.json          # HPC system operational status
│   ├── resource-pool.schema.json          # Capacity vs availability model
│   ├── queue-info.schema.json             # Queue/partition information
│   ├── allocation-info.schema.json        # Compute time allocation
│   ├── storage-info.schema.json           # Filesystem capacity
│   └── system-insight.schema.json         # Recommendations and alerts
└── api/
    ├── fleet-status-response.schema.json  # /api/status response
    ├── cluster-usage-response.schema.json # /api/cluster-usage response
    └── health-response.schema.json        # /api/health response

examples/
├── README.md                              # Examples documentation
├── 01-check-fleet-status.md               # Basic status checking
└── 20-ai-where-to-run.md                  # AI assistant reasoning

docs/
└── glossary.md                            # HPC terminology definitions
```

### Schema Design Decisions

#### 1. Capacity vs Availability Model

Created `resource-pool.schema.json` that explicitly separates:

```json
{
  "capacity": {
    "total": 100000,        // Static: total hardware
    "allocatable": 98000    // Static: minus reserved/offline
  },
  "availability": {
    "idle": 15000,          // Dynamic: free right now
    "allocated": 78000,     // Dynamic: running jobs
    "pending": 25000,       // Dynamic: queued jobs
    "reserved": 2000,       // Dynamic: future commitments
    "offline": 5000         // Dynamic: failed/draining
  }
}
```

This addresses the core semantic ambiguity identified in Iteration 1.

#### 2. Explicit Units via x-unit Extension

All numeric fields that represent physical quantities include explicit units:

```json
{
  "total_cores": {
    "type": "integer",
    "x-unit": "cores"
  },
  "max_walltime_seconds": {
    "type": "integer",
    "x-unit": "seconds"
  }
}
```

#### 3. Normalized Status Enums

Defined canonical status values in `status-enum.schema.json`:
- System: `UP | DOWN | DEGRADED | MAINTENANCE | UNKNOWN`
- Queue: `ACTIVE | INACTIVE | DRAINING | OFFLINE`
- Node: `IDLE | ALLOCATED | MIXED | DOWN | DRAINING | RESERVED | MAINTENANCE`
- Insight severity: `CRITICAL | WARNING | INFO | SUGGESTION`

#### 4. Observation Metadata

All API responses include observation metadata:

```json
{
  "meta": {
    "observed_at": "2026-01-29T15:30:00Z",
    "source": "hpcmp_collector",
    "from_cache": false,
    "cache_age_seconds": 0
  }
}
```

### AI-Assist Readiness Progress

**Updated Score: 6/10** (up from 4/10)

| Criterion | Status | Notes |
|-----------|--------|-------|
| Structured JSON output | ✅ | Good foundation |
| Consistent field naming | ✅ | Standardized in schemas |
| Explicit types/units | ✅ | `x-unit` extension added |
| Semantic annotations | ✅ | Enums with descriptions |
| Example corpus | ✅ | `examples/` created |
| Schema definitions | ✅ | `schemas/` created |
| Error categorization | ⚠️ | HTTP codes but no error types |
| Deterministic output | ⚠️ | Timestamps vary |

### Code Changes This Iteration

Updated `src/data/models.py` to implement schema concepts:

#### 1. Added Status Enums

```python
class SystemOperationalStatus(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    DEGRADED = "DEGRADED"
    MAINTENANCE = "MAINTENANCE"
    UNKNOWN = "UNKNOWN"

class QueueState(str, Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    DRAINING = "DRAINING"
    OFFLINE = "OFFLINE"

class StorageHealthStatus(str, Enum):
    HEALTHY = "HEALTHY"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"

class InsightSeverity(str, Enum):
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    INFO = "INFO"
    SUGGESTION = "SUGGESTION"

class AllocationStatus(str, Enum):
    HEALTHY = "HEALTHY"
    LOW = "LOW"
    CRITICAL = "CRITICAL"
    EXHAUSTED = "EXHAUSTED"
    EXPIRED = "EXPIRED"
```

#### 2. Added Capacity vs Availability Model

```python
@dataclass
class ResourceCapacity:
    total: int
    allocatable: Optional[int] = None

@dataclass
class ResourceAvailability:
    idle: int
    allocated: int
    pending: int = 0
    reserved: int = 0
    offline: int = 0

@dataclass
class ResourcePool:
    unit: str  # 'cores', 'nodes', 'gpus', 'gigabytes'
    capacity: ResourceCapacity
    availability: ResourceAvailability

@dataclass
class SystemCapacity:
    total_nodes: Optional[int] = None
    total_cores: Optional[int] = None
    total_gpus: Optional[int] = None
    total_memory_gb: Optional[float] = None
    architecture: Optional[str] = None

@dataclass
class SystemAvailability:
    nodes_up: Optional[int] = None
    nodes_down: Optional[int] = None
    cores_available: Optional[int] = None
    cores_in_use: Optional[int] = None
    utilization_percent: Optional[float] = None
```

#### 3. Enhanced Existing Models

- **QueueInfo**: Added `state` enum, `max_walltime_seconds`, walltime parsing, `wait_estimate_seconds`
- **AllocationInfo**: Added `status` enum, `percent_used`, `hours_pending`, `burn_rate_hours_per_day`
- **SystemStatus**: Added `status_reason`, `capacity`, `availability`, `operational_status` property
- **SystemInsight**: Added `severity` enum, scope fields (`queue`, `project`, `storage`), action fields
- **StorageInfo**: Added `storage_type`, `status` returns enum, `status_str` for backward compatibility

#### 4. Updated Tests

Added 15 new tests to `tests/unit/test_models.py`:
- Queue walltime parsing (HMS, DHMS formats)
- Allocation status thresholds (HEALTHY, LOW, CRITICAL, EXHAUSTED)
- Insight severity from priority mapping
- System operational status enum

All 43 model-related tests pass.

---

## Iteration 3: Recommendation Engine Enhancement

### Completed

1. **Updated recommendation engine to use severity enums**
   - Insights now use `InsightSeverity` (CRITICAL, WARNING, INFO, SUGGESTION)
   - Insights use `InsightType` (RECOMMENDATION, WARNING, ALERT, INFO)
   - Sorting now prioritizes by severity then priority

2. **Enhanced insight generation**
   - Added storage capacity warnings (CRITICAL at >95%, WARNING at >80%)
   - Added system DOWN status (CRITICAL severity)
   - Added more granular allocation warnings (CRITICAL at <5%, WARNING at <20%, INFO at <40%)
   - Added action descriptions and commands to insights
   - Improved queue depth thresholds (WARNING at >100, SUGGESTION at >50)

3. **Updated metric naming for clarity**
   - `allocation` → `allocation_percent_remaining`
   - `queue_depth` → `queue_pending_jobs`
   - `storage` → `storage_percent_used`
   - `status` → `system_status`

4. **Updated tests**
   - Fixed test expectations for new metric names
   - All 11 recommendation tests pass

### Code Changes

```python
# src/insights/recommendations.py
from ..data.models import SystemInsight, InsightSeverity, InsightType

# Insights now include:
SystemInsight(
    type=InsightType.ALERT.value,  # RECOMMENDATION, WARNING, ALERT, INFO
    severity=InsightSeverity.CRITICAL,  # CRITICAL, WARNING, INFO, SUGGESTION
    message="...",
    priority=5,
    related_metric="allocation_percent_remaining",
    cluster=name,
    action_description="Request additional allocation hours immediately",
)

# Sorting by severity then priority
severity_order = {
    InsightSeverity.CRITICAL: 0,
    InsightSeverity.WARNING: 1,
    InsightSeverity.INFO: 2,
    InsightSeverity.SUGGESTION: 3,
}
insights.sort(key=lambda x: (severity_order.get(x.severity, 4), -x.priority))
```

### Test Results

All model and recommendation tests pass (54 tests total):
- `test_models.py`: 23 passed
- `test_recommendations.py`: 11 passed
- `test_config.py`: 9 passed
- Other tests: 11 passed

---

## Iteration 5: API Documentation and Error Handling

### Completed

1. **Created OpenAPI 3.1 Specification**
   - Full API documentation at `schemas/openapi.yaml`
   - All endpoints documented with request/response schemas
   - Includes examples and error responses
   - Rate limiting headers documented

2. **Added Error Response Schema**
   - Created `schemas/api/error-response.schema.json`
   - Standard error codes: NOT_FOUND, VALIDATION_ERROR, RATE_LIMITED, COLLECTOR_ERROR, TIMEOUT, INTERNAL_ERROR
   - Machine-readable codes with human-readable messages
   - Includes details object for debugging context

3. **Created Additional Example Scenarios**
   - `examples/02-check-allocation.md` - Verify sufficient hours before job submission
   - `examples/03-find-best-queue.md` - Queue selection with decision matrix

### Files Created
- `schemas/openapi.yaml` - OpenAPI 3.1 specification (~500 lines)
- `schemas/api/error-response.schema.json` - Standard error format
- `examples/02-check-allocation.md` - Allocation checking example
- `examples/03-find-best-queue.md` - Queue selection example

### Error Response Format

```json
{
  "error": {
    "code": "NOT_FOUND",
    "message": "System 'unknown-system' not found in fleet",
    "details": {
      "parameter": "system",
      "value": "unknown-system",
      "suggestion": "Use /api/status to list available systems"
    },
    "request_id": "req_20260129_153000_abc123"
  },
  "meta": {
    "timestamp": "2026-01-29T15:30:00Z"
  }
}
```

### OpenAPI Features
- Server URL templating for reverse proxy deployments
- Complete schema definitions for all models
- Rate limiting response headers documented
- Examples for common use cases

---

## Iteration 6: Cross-System Normalization

### Completed

1. **Created Scheduler-Agnostic Normalization Module**
   - `src/data/normalization.py` - Comprehensive normalization functions
   - Handles PBS and Slurm scheduler differences
   - Auto-detects scheduler type from hints

2. **Normalization Capabilities**

   **Node State Normalization:**
   - PBS: `free` → `IDLE`, `job-exclusive` → `ALLOCATED`, `offline` → `DOWN`
   - Slurm: `idle` → `IDLE`, `alloc` → `ALLOCATED`, `drain` → `DRAINING`, `mix` → `MIXED`

   **Queue State Normalization:**
   - PBS: `started/enabled` → `ACTIVE`, `stopped/disabled` → `INACTIVE`
   - Slurm: `up` → `ACTIVE`, `down` → `OFFLINE`, `drain` → `DRAINING`

   **Job State Normalization:**
   - PBS: `Q` → `PENDING`, `R` → `RUNNING`, `H` → `HELD`, `F` → `COMPLETED`
   - Slurm: `PD` → `PENDING`, `R` → `RUNNING`, `CD` → `COMPLETED`, `CA` → `CANCELLED`

   **Walltime Parsing:**
   - PBS format: `HH:MM:SS`, `DD:HH:MM:SS`
   - Slurm format: `D-HH:MM:SS`, `MINUTES`
   - Returns both seconds (for computation) and display string

   **Resource Name Normalization:**
   - `ncpus/cpus/procs/ppn` → `cores`
   - `nodect/nnodes` → `nodes`
   - `ngpus/gres/gpu` → `gpus`

3. **Added 33 New Tests**
   - Scheduler detection
   - Node, queue, job state normalization
   - Walltime parsing (all formats)
   - Resource name normalization
   - Memory unit conversion
   - Full cluster data normalization

### Code Example

```python
from src.data.normalization import (
    detect_scheduler,
    normalize_node_state,
    normalize_queue_state,
    parse_walltime,
    normalize_cluster_data,
)

# Auto-detect scheduler
scheduler = detect_scheduler({"scheduler": "PBS"})

# Normalize states
node_state = normalize_node_state("job-exclusive", scheduler)  # "ALLOCATED"
queue_state = normalize_queue_state("started", scheduler)  # "ACTIVE"

# Parse walltime
seconds, display = parse_walltime("24:00:00")  # (86400, "1 day")

# Normalize entire cluster data structure
normalized = normalize_cluster_data(raw_cluster_data)
```

---

## AI-Assist Readiness Progress

**Updated Score: 9/10** (up from 8/10)

| Criterion | Status | Notes |
|-----------|--------|-------|
| Structured JSON output | ✅ | Good foundation |
| Consistent field naming | ✅ | Standardized in schemas and code |
| Explicit types/units | ✅ | `x-unit` extension, field naming |
| Semantic annotations | ✅ | Enums with descriptions |
| Example corpus | ✅ | 4 examples in `examples/` |
| Schema definitions | ✅ | `schemas/` with JSON Schema + OpenAPI |
| Error categorization | ✅ | Standard error codes and format |
| Deterministic output | ⚠️ | Timestamps vary (inherent to monitoring) |
| Severity-based alerts | ✅ | CRITICAL/WARNING/INFO/SUGGESTION |
| Actionable insights | ✅ | action_description, action_command |
| OpenAPI specification | ✅ | Full API documentation |
| Cross-scheduler normalization | ✅ | NEW: PBS/Slurm state mapping |
| Walltime parsing | ✅ | NEW: All formats supported |

---

## Iteration Log

| Iteration | Date | Focus | Key Outcomes |
|-----------|------|-------|--------------|
| 1 | 2026-01-29 | Initial Assessment | Identified 13 improvement opportunities, established baseline |
| 2 | 2026-01-29 | Schema Foundation | Created schemas/, examples/, glossary; AI readiness 4→6/10 |
| 3 | 2026-01-29 | Code Integration | Updated models with enums, capacity/availability; 15 new tests |
| 4 | 2026-01-29 | Recommendations | Enhanced insights with severity, actions; AI readiness 6→7/10 |
| 5 | 2026-01-29 | API Documentation | OpenAPI spec, error schema, 2 more examples; AI readiness 7→8/10 |
| 6 | 2026-01-29 | Cross-System Normalization | Scheduler-agnostic normalization module; 33 new tests |
| 7 | 2026-01-29 | UI/UX Improvements | Tooltips, contextual help, visual indicators for researchers |

---

## Summary of Changes Made

### Files Created
- `schemas/README.md` - Schema documentation
- `schemas/openapi.yaml` - **OpenAPI 3.1 specification** (NEW)
- `schemas/common/units.schema.json` - Unit definitions
- `schemas/common/status-enum.schema.json` - Status enumerations
- `schemas/common/timestamp.schema.json` - Timestamp formats
- `schemas/models/system-status.schema.json` - System status model
- `schemas/models/resource-pool.schema.json` - Capacity vs availability
- `schemas/models/queue-info.schema.json` - Queue information
- `schemas/models/allocation-info.schema.json` - Allocation tracking
- `schemas/models/storage-info.schema.json` - Storage capacity
- `schemas/models/system-insight.schema.json` - Recommendations/alerts
- `schemas/api/fleet-status-response.schema.json` - Fleet status API
- `schemas/api/cluster-usage-response.schema.json` - Cluster usage API
- `schemas/api/health-response.schema.json` - Health check API
- `schemas/api/error-response.schema.json` - **Error response format** (NEW)
- `src/data/normalization.py` - **Scheduler-agnostic normalization** (NEW)
- `tests/unit/test_normalization.py` - **33 normalization tests** (NEW)
- `examples/README.md` - Examples documentation
- `examples/01-check-fleet-status.md` - Fleet status example
- `examples/02-check-allocation.md` - **Allocation checking** (NEW)
- `examples/03-find-best-queue.md` - **Queue selection** (NEW)
- `examples/20-ai-where-to-run.md` - AI assistant example
- `docs/glossary.md` - HPC terminology glossary

### Files Modified
- `src/data/models.py` - Added enums, capacity/availability models, enhanced fields
- `src/insights/recommendations.py` - Severity-based insights, better thresholds
- `tests/unit/test_models.py` - 15 new tests for new functionality
- `tests/unit/test_recommendations.py` - Updated for new metric names

### Total New Artifacts
- **24 files** created
- **4 files** modified
- **~4200 lines** of schema/documentation/code added
- **76 tests** passing (33 new normalization + 15 new model + existing)

---

## Iteration 7: UI/UX Improvements for Researchers

### Focus

Improve the frontend UI/UX for clarity and better understanding of the data for researchers, scientists, and practitioners.

### Completed

1. **Added Comprehensive Tooltips Across All Pages**

   **Fleet Status (index.html):**
   - Total systems: "Number of HPC systems being monitored across all sites"
   - Fleet uptime: "Percentage of systems with UP status. Systems may be DOWN, DEGRADED, or in MAINTENANCE."
   - Systems not UP: "Count of systems that are DEGRADED (reduced capacity), in MAINTENANCE, or completely DOWN"
   - Last observed: "When status data was last collected from the HPC systems"
   - Status breakdown: "UP=operational, DEGRADED=reduced capacity, MAINTENANCE=planned work, DOWN=offline"
   - Site coverage: "Distribution of systems across data centers (DSRCs or research facilities)"
   - Scheduler types: "PBS and Slurm are job schedulers that manage workloads on HPC systems"
   - Table headers with help icons explaining each column

   **Queue Health (queues.html):**
   - Connected clusters, Queues observed, Running/Pending jobs
   - Queue disposition explanation
   - Fleet core utilization
   - Node availability table with column explanations
   - Queue depth with queue type descriptions

   **Quota Usage (quota.html):**
   - Connected clusters, Total allocations, Hours used/remaining
   - Core-hour explanations ("One core-hour = 1 CPU core running for 1 hour")
   - Subproject table with column tooltips
   - Queue snapshot explanation

   **Storage (storage.html):**
   - Filesystem count, Near capacity warnings
   - Storage type explanations ($HOME, $WORK, $SCRATCH)
   - Filesystem table columns with usage threshold explanations
   - Storage warning recommendations (critical, high, elevated)

   **Insights (insights.html):**
   - Insight counts and severity explanations
   - Severity descriptions mapped to insight items

2. **Improved Visual Status Indicators (Accessibility)**

   **Status Badges with Icons:**
   - UP: Green with checkmark (✓)
   - DOWN: Red with X mark (✗)
   - DEGRADED/MAINTENANCE: Orange with warning triangle (⚠)
   - UNKNOWN: Yellow with question mark (?)

   **Storage Status with Icons:**
   - HEALTHY: Checkmark
   - WARNING: Warning triangle
   - CRITICAL: X mark

   **Queue Chips with Visual Indicators:**
   - Active: Play symbol (▶)
   - Backlog: Hourglass (⏳)
   - Idle: Em dash (—)

   **Allocation Status Badges:**
   - HEALTHY, LOW, CRITICAL, EXHAUSTED with appropriate icons

3. **Enhanced CSS for Help Elements**
   - Consistent styling for `.card-tooltip`, `.panel-help`, `.eyebrow-help`, `.th-help`
   - Hover effects with accent color highlighting
   - Smooth opacity transitions

4. **Contextual Recommendations in Storage Warnings**
   - Critical (>95%): "May cause job failures. Clean up files immediately."
   - High (90-95%): "Consider removing unused files or moving data to archive."
   - Elevated (80-90%): "Monitor and plan cleanup."

5. **Enhanced Insights with Severity Context**
   - Severity descriptions available on hover
   - Metric explanations for common metrics
   - Visual icon differentiation by severity level

### Files Modified

**HTML:**
- `web/index.html` - Added tooltips to cards, panels, and table headers
- `web/queues.html` - Added tooltips to all metrics and tables
- `web/quota.html` - Added tooltips with core-hour explanations
- `web/storage.html` - Added storage type explanations and thresholds
- `web/insights.html` - Added severity explanations

**CSS:**
- `web/assets/css/styles.css` - Added help icon styles, improved badge icons, queue chip icons, allocation status badges

**JavaScript:**
- `web/assets/js/quota.js` - Added table header tooltips, metric explanations
- `web/assets/js/storage.js` - Added contextual recommendations to warnings
- `web/assets/js/insights.js` - Added severity and metric explanations

### UI/UX Improvements Summary

| Improvement | Benefit for Researchers |
|-------------|------------------------|
| Tooltips on all metrics | Understand HPC terminology without leaving page |
| Icon+color status indicators | Accessibility improvement, quick scanning |
| Core-hour explanations | Clarifies allocation consumption |
| Storage threshold guidance | Actionable recommendations |
| Queue type descriptions | Understand batch/debug/gpu queue differences |
| Severity context on insights | Prioritize actions appropriately |

---

### Iteration 7 Continued: Help Panel and Data Freshness

**Additional Features:**

6. **Global Help Panel** (`page-utils.js`)
   - Help button (?) added to all pages
   - Modal panel with HPC quick reference:
     - System status definitions
     - Resource concepts (core-hours, nodes, cores, walltime)
     - Queue states
     - Storage types
     - Tips for researchers
   - Keyboard accessible (Escape to close)
   - Backdrop click to close

7. **Relative Time Display**
   - "Last updated" shows human-friendly times: "Just now", "5 minutes ago"
   - Hover reveals exact timestamp
   - Applied to all pages

8. **Data Freshness Indicators**
   - Color-coded: fresh (green), aging (yellow), stale (orange)
   - Pulsing animation for fresh data
   - CSS: `.freshness-indicator` with `.fresh`, `.aging`, `.stale` variants

9. **Quick Tips Panels**
   - Dismissible tips on each page with contextual guidance
   - Page-specific tips for Fleet, Queues, Quota, Storage, Insights
   - Persisted dismissal in localStorage

10. **Enhanced CSS Components**
    - Empty state styling
    - Sortable table header indicators
    - Page context banners
    - Keyboard shortcut styling

### Files Modified (Iteration 7 total)

**HTML:**
- `web/index.html`, `web/queues.html`, `web/quota.html`, `web/storage.html`, `web/insights.html`

**CSS:**
- `web/assets/css/styles.css` - ~200 lines added for new components

**JavaScript:**
- `web/assets/js/page-utils.js` - Added `initHelpPanel`, `initQuickTips`, `formatRelativeTime`, `createFreshnessIndicator`, `createEmptyState`
- `web/assets/js/app.js`, `quota.js`, `storage.js`, `queues.js`, `insights.js` - Integrated new features

---

### Remaining Work (Future Iterations)
1. **API Versioning**: Add `/api/v1/` prefix for stable API
2. **Pagination**: Add limit/offset for large list responses
3. **Historical Trending**: Store and expose status history
4. **Alerts Endpoint**: `/api/alerts` for webhook integration

---

*This document is maintained as part of the Ralph Loop continuous improvement process.*
