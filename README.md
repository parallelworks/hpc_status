# HPC Status Monitor

A single pane of glass for your HPC fleet. At a glance: what's up, what's
slow, where your jobs will wait, and how much allocation you have left.

## What it's for

You have work to run on HPC systems, but the systems are scattered across
sites, schedulers, and login nodes. Before submitting a job you want to
know:

- Is the system even up right now?
- Which queue will get me running fastest?
- Am I close to burning through my allocation?
- Is `$SCRATCH` about to purge my files?

The Status Monitor answers those questions in one place, refreshed
continuously, so you don't have to `ssh` around and run five different
commands to decide where to submit.

## What you'll see

**Fleet status.** Every HPC system you have access to, with a status
(UP / DEGRADED / MAINTENANCE / DOWN), its login node, its scheduler, and
when it was last checked. Click a system for the full details page.

**Topology.** An interactive map of the fleet — every site, every system,
and which ones the monitor actually holds a live session to. Group by site
(DSRC), scheduler, status, or connection; switch between hierarchy, radial,
force, lane, load, and geographic layouts — the geographic one is a real map,
with each site pinned at its actual coordinates and framed insets for
anything off the mainland. Cloud clusters are placed too — an AWS GovCloud
machine pins to its region. Zoom in (or set **Map detail**) and each site
opens up into the individual systems behind it. A **Timeline** button reveals a transport that replays the last 6 hours to 3 days from the monitor's own
records — scrub or press play to watch status and load move — and systems
that change on a live refresh pulse so you can see it happen. The **Load**
layout plots each system by how busy it is, so replaying a day shows the
fleet rising and falling; systems the monitor could not measure sit in a
band below the axis rather than pretending to be idle. Node color is status, size is core
count, and the outer ring is how busy it is. Nodes with an open insight
carry a warning badge, links animate at the speed of their measured round
trip, and each system shows its status timeline for the last 24 hours.
Click a system to inspect it — click again to jump straight to its queue
health, or shift-click several to compare them side by side.

**Queue health.** For each system, live queue depth, node availability,
and core demand — so you can pick the queue that isn't backed up.

**Quota usage.** Your allocations in core-hours, how fast you're burning
them, and warnings before you hit the limit. Broken down by subproject
where relevant.

**Storage.** Capacity and usage for `$HOME`, `$WORK`, and `$SCRATCH` on
every system, with purge-window reminders for scratch.

**Insights.** Automatic recommendations — "this queue is draining, try
that one", "you're at 92% of your allocation", "scratch is filling up".

**Where should I run this?** Describe a job — cores, walltime, GPUs — and
the Insights page ranks every queue that can actually run it, using idle
cores, measured backlog, estimated time-to-start, and how much allocation
you have left. Queues that *can't* run it say why instead of ranking last.

**Wait estimates.** Queue depth is recorded over time, so the queue page
can turn a backlog into an estimated start time — derived from observed
core turnover, and labelled with the confidence behind it. A queue with no
observed turnover says so rather than inventing a number.

**API.** Everything on every page is available as JSON. The **API** tab
lists every endpoint with its parameters, runs any of them against the
running deployment, and hands you the equivalent `curl` command. The list
is served by the API itself, so it describes the version you are running.

**Alerts.** Point `alerts.webhook_url` at Slack, Teams, or your own
receiver and the monitor notifies you when a system goes down or comes
back, with a per-system cooldown so a flapping machine doesn't spam you.

## Quick start

```bash
./scripts/run.sh
```

Then open [http://localhost:8080](http://localhost:8080). If something else
already holds 8080 — on an ACTIVATE workspace that is Grafana — the script
steps to the next free port and tells you which. Set `PORT=...` to choose
one yourself.

### Sharing it

On [Parallel Works](https://parallel.works/), publish the dashboard as an
endpoint session and get a URL you can hand to someone else:

```bash
pw endpoints run --name hpc-status -- ./scripts/run.sh
```

The platform assigns a free port, tunnels to it without needing any
inbound access to your machine, and kills the dashboard when you exit.
The **HPC Status** workflow does exactly this — see
[docs/deployment.md](docs/deployment.md).

### Watching your own clusters

If you're using [Parallel Works](https://parallel.works/), authenticate
and the dashboard will pick up every cluster you have access to:

```bash
pip install pw-client
pw auth             # paste your ACTIVATE API key
./scripts/run.sh
```

It works with PBS/Slurm HPC clusters, GPU servers (via `nvidia-smi`), and
plain compute nodes.

## Help while you're using it

Every page has a **Help** button in the top-right with a quick reference
for the HPC terms you'll see (core-hours, walltime, draining queues, the
difference between `$HOME` / `$WORK` / `$SCRATCH`, and so on). Each
metric also has a `ⓘ` tooltip that explains what it means.

## Deployments

The monitor supports branded deployments. The HPCMP build, for example,
uses the HPCMP purple palette and logo mark — launch it with:

```bash
CONFIG_FILE=configs/config.hpcmp.yaml ./scripts/run.sh
```

## For operators and developers

- **Deploying it for your team:** [docs/deployment.md](docs/deployment.md)
- **Configuration options:** [docs/configuration.md](docs/configuration.md)
- **REST API:** the **API** tab in a running deployment, or
  [docs/api.md](docs/api.md) / [schemas/openapi.yaml](schemas/openapi.yaml)
  (generated by `python scripts/build_openapi.py`)
- **HPC glossary:** [docs/glossary.md](docs/glossary.md)

## License

See [LICENSE](LICENSE).
