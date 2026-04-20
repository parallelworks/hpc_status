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

**Queue health.** For each system, live queue depth, node availability,
and core demand — so you can pick the queue that isn't backed up.

**Quota usage.** Your allocations in core-hours, how fast you're burning
them, and warnings before you hit the limit. Broken down by subproject
where relevant.

**Storage.** Capacity and usage for `$HOME`, `$WORK`, and `$SCRATCH` on
every system, with purge-window reminders for scratch.

**Insights.** Automatic recommendations — "this queue is draining, try
that one", "you're at 92% of your allocation", "scratch is filling up".

## Quick start

```bash
./scripts/run.sh
```

Then open [http://localhost:8080](http://localhost:8080).

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
- **REST API:** [docs/api.md](docs/api.md)
- **HPC glossary:** [docs/glossary.md](docs/glossary.md)

## License

See [LICENSE](LICENSE).
