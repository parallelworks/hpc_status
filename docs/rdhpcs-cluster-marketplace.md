# NOAA RDHPCS — Marketplace Descriptions

Short marketing copy for each cluster currently surfaced by the RDHPCS Status
Monitor. Pick the tagline + summary for cards or workflow listings, and use
"Best for" for filter chips or eligibility notes.

Each entry mirrors the `noaa_systems:` block in
[configs/config.noaa.yaml](../configs/config.noaa.yaml).

---

## Hera

**NOAA's flagship CPU workhorse for weather and climate research.**

A 3.27-petaFLOPS Intel SkyLake cluster operated at the NOAA Environmental
Security Computing Center. 1,328 compute nodes (40 cores / 96 GB-per-core)
plus 268 BigMem nodes with 384 GB RAM each, backed by 18.5 PB of scratch
storage. Slurm-scheduled.

- **Location**: NESCC, Fairmont, WV
- **Hardware**: Intel SkyLake · 53,120 cores · 40 cores/node · HDR-100 IB
- **Scheduler**: Slurm
- **Storage**: 18.5 PB scratch across `/scratch1`–`/scratch4`
- **Best for**:
  - Operational and research NWP / climate ensembles
  - Long-running CPU jobs (up to 8 h on the default partition)
  - Bigmem post-processing on the dedicated 384 GB partition

---

## Ursa

**AMD Genoa + NVIDIA / AMD GPU supercluster for next-gen AI + HPC.**

Mixed CPU + GPU system built on AMD Genoa 9654 (192 cores @ 2.4 GHz).
576 compute nodes (4.25 PFlops CPU) alongside 58 H100 nodes, 8 Grace-Hopper
GH200 nodes, and 3 MI300X nodes (3.48 PFlops GPU). Over 100 PB of NDR-200
InfiniBand-attached Lustre, shared with Hera.

- **Location**: NESCC, Fairmont, WV
- **Hardware**: AMD Genoa 9654 · 110,592 CPU cores · 148 GPUs · Rocky 9
- **GPUs**: NVIDIA H100-NVL · Grace-Hopper GH200 · AMD MI300X
- **Scheduler**: Slurm
- **Storage**: 100+ PB Lustre (shared with Hera) · >1000 GB/s aggregate
- **Best for**:
  - Foundation-model training and AI/ML pipelines (H100 / MI300X)
  - GPU-accelerated weather and ocean models (GH200)
  - Large-core CPU jobs that exceed Hera's 40-core nodes

---

## Gaea (C5)

**HPE Cray climate-modeling capability system at ORNL.**

Joint NOAA / DOE HPE Cray supercomputer operated by the National Center for
Computational Sciences for high-resolution GFDL climate modeling.
Slurm-scheduled access to thousands of compute nodes, GPFS scratch on
`/gpfs/f5`, and dedicated DTNs for off-system transfers.

- **Location**: ORNL, Oak Ridge, TN
- **Hardware**: HPE Cray · 1500+ batch compute nodes
- **Scheduler**: Slurm (default 12 h walltime, up to 16 h)
- **Storage**: GPFS `/gpfs/f5` + dedicated F5/F6 DTNs
- **Best for**:
  - GFDL coupled-model simulations at scale
  - Long-running capability jobs (default 12 h, up to 16 h)
  - Workflows that need to stage data between F5 and F6 filesystems

---

## PPAN

**GFDL's interactive post-processing and analysis cluster.**

130+ Dell analysis nodes spanning Intel Sandy Bridge through Ice Lake (AVX2 /
AVX-512) at the Geophysical Fluid Dynamics Laboratory in Princeton. Per-node
memory ranges from 48 GB up to 4.3 TB on the Ice Lake bigmem hosts. Direct
access to multiple petabytes of `/work*` filesystems plus ~200 PB of archive
storage.

- **Location**: GFDL, Princeton, NJ
- **Hardware**: 130+ Dell nodes · Sandy Bridge → Ice Lake · 48 GB – 4.3 TB RAM
- **Scheduler**: None (interactive analysis hosts)
- **Storage**: multi-PB `/work*` + ~200 PB archive
- **Best for**:
  - Interactive analysis of GFDL model output
  - Memory-hungry diagnostics (NAG / large-array workloads)
  - Pre-staging data for Gaea jobs without burning core-hours

---

## Mercury

**Cross-RDHPCS data transfer hub and HPSS archive gateway.**

Dedicated data-mover system shared across the RDHPCS fleet for secure transfer
of NOAA R&D datasets to/from external collaborators. Direct `hsi` / `htar`
access to the HPSS archive. No scheduler — interactive use only.

- **Location**: NESCC, Fairmont, WV
- **Role**: Data transfer + HPSS gateway (no compute)
- **Tools**: `hsi`, `htar`, standard transfer clients
- **Best for**:
  - Pushing or pulling data with non-NOAA collaborators
  - HPSS archive operations (hsi / htar)
  - Routing transfers off of the busy compute systems

---

## NOAA Cloud (v3)

**On-demand HPC bursts to AWS, GCP, and Azure via Parallel Works.**

User-provisioned cloud HPC clusters managed by the RDHPCS Cloud Platform on
Parallel Works. Pick instance types, attach Lustre and object storage, and
run the same Slurm workflows you use on-prem — pay only while the cluster is
up.

- **Location**: Parallel Works platform (AWS / GCP / Azure)
- **Hardware**: Configurable; CPU + GPU instance families
- **Scheduler**: Slurm (cloud-managed)
- **Storage**: `/home` · on-demand Lustre · object buckets
- **Best for**:
  - Elastic capacity beyond on-prem allocations
  - Right-sized GPU clusters that don't exist on-prem
  - Quick reproducibility runs against a fresh image
