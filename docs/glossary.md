# HPC Terminology Glossary

This glossary defines terms used throughout the HPC Status Monitor to ensure consistent understanding for users, integrators, and AI assistants.

## Resource Concepts

### Capacity vs Availability

**Capacity** (Static)
: The total amount of resources that exist in a system or queue. Capacity changes only when hardware is added or removed, or when administrative policies change.

: Example: "Nautilus has a capacity of 150,000 cores"

**Availability** (Dynamic)
: The resources currently free and usable. Availability changes constantly as jobs start and complete.

: Example: "45,000 cores are currently available on Nautilus"

The distinction is critical: a system may have high capacity but low availability (busy), or the reverse during maintenance (reduced capacity but remaining resources available).

### Allocation

**Allocation**
: A grant of compute time, measured in core-hours or node-hours, assigned to a project or user. When an allocation is exhausted, jobs may be rejected or deprioritized.

: Also called: compute allocation, project hours, service units (SUs)

**Burn Rate**
: The rate at which an allocation is being consumed, typically measured in hours per day.

### Fragmentation

**Fragmentation**
: The distribution of available resources across a system. Even if 1000 cores are "available," they may be spread across many nodes in small chunks, making it impossible to run a job requiring 500 contiguous cores.

: A system with low fragmentation has large contiguous blocks available. High fragmentation means resources are scattered.

## Scheduler Concepts

### Queue (PBS) / Partition (Slurm)

**Queue**
: A logical grouping of resources with associated policies (walltime limits, core limits, priority, access controls). PBS uses "queue," Slurm uses "partition," but they serve the same purpose.

: Common queue types:
- **batch/standard**: General-purpose, longer walltime limits
- **debug**: Short jobs for testing, usually < 30 minutes
- **gpu**: Access to GPU accelerators
- **bigmem**: High-memory nodes

### Queue States

**ACTIVE**
: Queue is accepting and scheduling jobs normally.

**DRAINING**
: Queue is finishing existing jobs but not starting new ones. Often precedes maintenance.

**INACTIVE**
: Queue exists but is not accepting submissions.

**OFFLINE**
: Queue is completely disabled.

### Job States

**PENDING** (Slurm: PD, PBS: Q)
: Job is waiting in queue to be scheduled. May be waiting for resources, priority, or dependencies.

**RUNNING** (Slurm: R, PBS: R)
: Job is executing on compute nodes.

**HELD** (Slurm: PD with HoldReason, PBS: H)
: Job is in the queue but will not be scheduled until released. May be user-held or system-held.

**COMPLETED** (Slurm: CD, PBS: F)
: Job finished successfully.

**FAILED** (Slurm: F, PBS: F)
: Job terminated with an error.

### Walltime

**Walltime**
: The maximum real-world time a job is allowed to run, measured from when it starts executing. If a job exceeds its walltime, it is killed by the scheduler.

: Format varies: `HH:MM:SS`, `DD:HH:MM:SS`, or hours as a number.

### Priority / Fair Share

**Priority**
: A job's position in the scheduling order. Higher priority jobs start before lower priority ones when resources become available.

**Fair Share**
: A scheduling policy that adjusts priority based on historical usage. Users/projects that have used less than their "fair share" get boosted priority; heavy users get reduced priority.

## System Concepts

### System Status

**UP**
: System is fully operational, accepting logins and jobs.

**DEGRADED**
: System is operational but with reduced capacity or performance. May indicate partial node failures, network issues, or filesystem problems.

**MAINTENANCE**
: System is undergoing planned maintenance. Usually does not accept jobs; existing jobs may be suspended or terminated.

**DOWN**
: System is completely offline. No access available.

### Node States

**IDLE**
: Node is powered on and has no jobs running. Available for immediate allocation.

**ALLOCATED**
: Node is fully occupied by one or more jobs. No resources available.

**MIXED**
: Node has some resources allocated and some free. Occurs when jobs don't use entire nodes.

**DRAINING**
: Node is completing current jobs but won't accept new ones. Usually precedes maintenance.

**DOWN**
: Node is offline or failed. May be due to hardware failure, network issues, or administrative action.

### DSRC (DoD HPC Context)

**DSRC**
: Defense Supercomputing Resource Center. A DoD facility hosting HPC systems.

: DSRCs include:
- **AFRL**: Air Force Research Laboratory
- **ARL**: Army Research Laboratory
- **ERDC**: Engineer Research and Development Center (Army Corps)
- **NAVO**: Navy DSRC

## Storage Concepts

### Storage Types

**$HOME**
: User home directory. Usually quota-limited, backed up, and intended for code, scripts, and small datasets.

**$WORKDIR / $WORK**
: Working directory for active projects. Larger quota than home, may not be backed up.

**$SCRATCH / /scratch**
: High-speed temporary storage. Typically not backed up and subject to purge policies (files older than N days deleted).

### Storage Status

**HEALTHY**
: Storage usage is below warning threshold (typically <80%).

**WARNING**
: Storage usage is elevated (80-95%). User should consider cleanup.

**CRITICAL**
: Storage is nearly full (>95%). May cause job failures.

### Purge Policy

**Purge**
: Automatic deletion of old files from scratch storage to maintain free space. Files not accessed within the retention period (e.g., 30 days) are deleted.

## Insight / Recommendation Types

### Severity Levels

**CRITICAL**
: Requires immediate attention. May block work entirely.

: Example: "Allocation exhausted, jobs will be rejected"

**WARNING**
: Should be addressed soon to prevent problems.

: Example: "Home directory 90% full"

**INFO**
: Informational, no action required.

: Example: "9 of 10 systems operational"

**SUGGESTION**
: Optional optimization or improvement.

: Example: "Consider debug queue for jobs under 30 minutes"

### Insight Types

**RECOMMENDATION**
: Suggestion for where or how to run jobs based on current conditions.

**WARNING**
: Potential issue that may affect operations.

**ALERT**
: Active issue requiring user attention.

**INFO**
: General information about system state.

## API Concepts

### Cache / Freshness

**Cache**
: Stored copy of previously fetched data, served to reduce load on HPC systems.

**Cache Age**
: How old the cached data is. Indicated by `meta.observed_at` in API responses.

**Stale Data**
: Cached data that is older than the acceptable freshness threshold.

### Rate Limiting

**Rate Limiting**
: Restriction on how frequently requests can be made, to prevent overloading HPC systems with monitoring queries.

**Circuit Breaker**
: Automatic pause on data collection when repeated failures occur, to allow systems to recover.

## Abbreviations

| Abbreviation | Meaning |
|--------------|---------|
| API | Application Programming Interface |
| CLI | Command Line Interface |
| CPU | Central Processing Unit |
| DSRC | Defense Supercomputing Resource Center |
| GPU | Graphics Processing Unit |
| HPC | High Performance Computing |
| PBS | Portable Batch System (job scheduler) |
| PW | Parallel Works (platform) |
| RDHPCS | Research and Development HPC Systems (NOAA) |
| SSH | Secure Shell |
| SU | Service Unit (compute time unit) |
