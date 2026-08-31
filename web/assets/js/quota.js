import {
  clampPercent,
  clusterPagesEnabled,
  initThemeToggle,
  buildApiUrl,
  initHelpPanel,
  initBrand,
  initQuickTips,
  initNav,
} from "./page-utils.js";

const DATA_URL = buildApiUrl("api/cluster-usage").toString();
const numberFormatter = new Intl.NumberFormat("en-US");
const compactFormatter = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

const RETRY_INTERVAL_MS = 15000;

const state = {
  clusters: [],
  loading: false,
  lastUpdated: null,
  retryHandle: null,
  features: {
    clusterPages: clusterPagesEnabled(),
  },
};

const elements = {};

const getElement = (id) => document.getElementById(id);

const toNumber = (value) => {
  if (value === null || value === undefined || value === "-") return 0;
  const numeric = Number(String(value).replace(/,/g, ""));
  return Number.isFinite(numeric) ? numeric : 0;
};

const formatInteger = (value) => numberFormatter.format(Math.round(Number(value) || 0));

const formatHours = (value, { compact = false } = {}) => {
  const numeric = Number(value) || 0;
  const formatter = compact ? compactFormatter : numberFormatter;
  return formatter.format(Math.round(numeric));
};

// Format a small NOAA-fairshare percentage compactly. Values are often
// extremely small (<0.01% for ~70-project clusters), so we keep extra
// precision below 1% and switch to single decimals above.
const formatFsPct = (pct) => {
  if (pct == null || !Number.isFinite(pct)) return "—";
  if (pct === 0) return "0%";
  if (pct < 0.01) return pct.toFixed(4) + "%";
  if (pct < 1) return pct.toFixed(3) + "%";
  if (pct < 10) return pct.toFixed(2) + "%";
  return pct.toFixed(1) + "%";
};

// Slurm walltime strings: "D-HH:MM:SS", "HH:MM:SS", "MM:SS", "UNLIMITED",
// or an empty/dash value. Returns "7d", "16h", "30m", "∞", or "".
const formatWalltime = (raw) => {
  if (!raw || raw === "-" || raw === "0:00:00") return "";
  if (/^unlimited$/i.test(raw) || /^infinite$/i.test(raw)) return "∞";
  const m = String(raw).match(/^(?:(\d+)-)?(\d+):(\d+)(?::(\d+))?$/);
  if (!m) return raw;
  const days = Number(m[1] || 0);
  const hours = Number(m[2] || 0);
  const mins = Number(m[3] || 0);
  if (days > 0) return `${days}d`;
  if (hours > 0) return `${hours}h`;
  if (mins > 0) return `${mins}m`;
  return raw;
};

const parseSystems = (cluster) => cluster?.usage_data?.systems || [];
const parseQueues = (cluster) => cluster?.queue_data?.queues || [];
const parseGpus = (cluster) => cluster?.gpu_data?.gpus || [];
const getSystemInfo = (cluster) => cluster?.system_info || {};
const hasScheduler = (cluster) => cluster?.cluster_metadata?.has_scheduler !== false &&
  (parseSystems(cluster).length > 0 || parseQueues(cluster).length > 0);

// NOAA RDHPCS-style accounting: no hour caps; usage tracked by fairshare.
// A row counts as fairshare-only when there's no allocation but we have
// either an FY usage figure or fairshare metadata to show.
const isFairshareRow = (system) =>
  (!Number(system?.hours_allocated)) && (
    Number(system?.hours_used) > 0 ||
    system?.fairshare_score !== undefined && system?.fairshare_score !== null ||
    system?.fairshare_rank ||
    (Array.isArray(system?.qoses) && system.qoses.length > 0)
  );

const clusterIsFairshare = (cluster) =>
  parseSystems(cluster).some(isFairshareRow) &&
  !parseSystems(cluster).some((s) => Number(s?.hours_allocated) > 0);

const fiscalYearStart = (cluster) =>
  cluster?.usage_data?.systems?.[0]?.fiscal_year_start ||
  (cluster?.usage_data?.fiscal_year_info || "").replace(/^FY since\s+/, "") ||
  "";

const computeSummary = () => {
  const totals = {
    allocations: 0,
    used: 0,
    remaining: 0,
    gpuCount: 0,
    gpuMemoryMib: 0,
    projects: 0,
    fairshareOnly: true,
    maxConcurrentJobs: 0,
  };
  state.clusters.forEach((cluster) => {
    parseSystems(cluster).forEach((system) => {
      totals.allocations += Number(system.hours_allocated) || 0;
      totals.used += Number(system.hours_used) || 0;
      totals.remaining += Number(system.hours_remaining) || 0;
      totals.projects += 1;
      if (Number(system.hours_allocated) > 0) {
        totals.fairshareOnly = false;
      }
      const jobs = Number(system.account_max_jobs);
      if (Number.isFinite(jobs) && jobs > totals.maxConcurrentJobs) {
        totals.maxConcurrentJobs = jobs;
      }
    });
    const gpuSummary = cluster?.gpu_data?.summary;
    if (gpuSummary) {
      totals.gpuCount += gpuSummary.gpu_count || 0;
      totals.gpuMemoryMib += gpuSummary.total_memory_mib || 0;
    }
  });
  return totals;
};

const disableRefresh = (disabled) => {
  const btn = elements.refreshBtn;
  if (!btn) return;
  btn.disabled = disabled;
  if (disabled) {
    btn.classList.add("is-loading");
  } else {
    btn.classList.remove("is-loading");
  }
};

const setBanner = (message, variant = "info") => {
  const banner = elements.statusBanner;
  if (!banner) return;
  banner.textContent = message || "";
  if (message) {
    banner.dataset.variant = variant;
    banner.hidden = false;
  } else {
    banner.hidden = true;
    delete banner.dataset.variant;
  }
};

const cacheElements = () => {
  elements.clusterCount = getElement("cluster-count");
  elements.totalAllocations = getElement("total-allocations");
  elements.totalUsed = getElement("total-used");
  elements.totalRemaining = getElement("total-remaining");
  elements.refreshBtn = getElement("refresh-btn");
  elements.statusBanner = getElement("data-status");
  elements.clusterGrid = getElement("cluster-grid");
  elements.clusterGridNote = getElement("cluster-grid-note");
  elements.fleetUsageDonut = getElement("fleet-usage-donut");
  elements.fleetQueueTags = getElement("fleet-queue-tags");
};

const formatProgressMessage = (progress, isFirstSweep) => {
  if (!progress || !progress.total) {
    return isFirstSweep
      ? "First-time setup: collecting quota data from your clusters…"
      : "Refreshing quota data…";
  }
  const { collected = 0, total = 0, current_cluster: current } = progress;
  const lead = isFirstSweep ? "Collecting quota data" : "Refreshing quota data";
  const where = current ? ` (currently ${current})` : "";
  return `${lead} from ${total} clusters — ${collected}/${total} ready${where}.`;
};

const formatProgressStatus = (progress, isFirstSweep) => {
  if (!progress || !progress.total) {
    return isFirstSweep
      ? "Connecting to clusters for the first time — this can take a couple of minutes."
      : "Refreshing…";
  }
  const { collected = 0, total = 0 } = progress;
  return isFirstSweep
    ? `First sweep in progress: ${collected} of ${total} clusters ready.`
    : `Refresh in progress: ${collected} of ${total} clusters updated.`;
};

const showGeneratingPlaceholder = (message = "Cluster monitor is generating quota data…") => {
  if (!elements.clusterGrid) return;
  elements.clusterGrid.innerHTML = `
    <article class="loading-panel">
      <strong>${message}</strong>
      <span>This may take a few moments the first time.</span>
    </article>
  `;
  if (elements.clusterGridNote) {
    elements.clusterGridNote.textContent = "Waiting for cluster monitor output…";
  }
};

const clusterTotals = (cluster) => {
  const summary = {
    allocations: 0,
    used: 0,
    remaining: 0,
  };
  parseSystems(cluster).forEach((system) => {
    summary.allocations += Number(system.hours_allocated) || 0;
    summary.used += Number(system.hours_used) || 0;
    summary.remaining += Number(system.hours_remaining) || 0;
  });
  return summary;
};

const scheduleRetry = () => {
  if (state.retryHandle || !state.features.clusterPages) return;
  state.retryHandle = setTimeout(() => {
    state.retryHandle = null;
    loadData({ silent: true });
  }, RETRY_INTERVAL_MS);
};

const clearRetry = () => {
  if (state.retryHandle) {
    clearTimeout(state.retryHandle);
    state.retryHandle = null;
  }
};

const aggregateQueueSnapshot = () => {
  const snapshot = { active: 0, backlog: 0, idle: 0 };
  state.clusters.forEach((cluster) => {
    parseQueues(cluster).forEach((queue) => {
      const running = toNumber(queue.jobs_running);
      const pending = toNumber(queue.jobs_pending);
      if (pending > 0) snapshot.backlog += 1;
      if (running > 0) snapshot.active += 1;
      if (running === 0 && pending === 0) snapshot.idle += 1;
    });
  });
  return snapshot;
};

const renderFleetUsageDonut = (summary) => {
  if (!elements.fleetUsageDonut) return;
  if (!summary.allocations && !summary.used) {
    elements.fleetUsageDonut.innerHTML = '<div class="placeholder">No usage data yet.</div>';
    return;
  }
  if (summary.fairshareOnly) {
    // NOAA fairshare model — show FY core-hours used across the fleet
    elements.fleetUsageDonut.innerHTML = `
      <div class="donut" style="--donut-value:100">
        <strong>${formatHours(summary.used, { compact: true })}</strong>
        <span>FY hrs</span>
      </div>
      <small>${summary.projects} project${summary.projects === 1 ? "" : "s"} across the fleet</small>
    `;
    return;
  }
  const percentRemaining = clampPercent((summary.remaining / summary.allocations) * 100);
  elements.fleetUsageDonut.innerHTML = `
    <div class="donut" style="--donut-value:${percentRemaining}">
      <strong>${Math.round(percentRemaining)}%</strong>
      <span>Hours remaining</span>
    </div>
    <small>${formatHours(summary.remaining, { compact: true })} hrs left</small>
  `;
};

const renderFleetQueueSnapshot = () => {
  if (!elements.fleetQueueTags) return;
  const snapshot = aggregateQueueSnapshot();
  const total = snapshot.active + snapshot.backlog + snapshot.idle;
  if (!total) {
    elements.fleetQueueTags.textContent = "No queue data yet.";
    return;
  }
  elements.fleetQueueTags.innerHTML = `
    <span class="queue-chip is-active">Active <small>${snapshot.active}</small></span>
    <span class="queue-chip is-backlog">Backlog <small>${snapshot.backlog}</small></span>
    <span class="queue-chip is-idle">Idle <small>${snapshot.idle}</small></span>
  `;
};

const renderSummary = () => {
  const summary = computeSummary();
  if (elements.clusterCount) {
    elements.clusterCount.textContent = formatInteger(state.clusters.length);
  }

  // In NOAA fairshare mode, there's no concept of an hour cap. Repurpose the
  // top cards: "Projects tracked" instead of "Total allocations", and hide
  // the meaningless "Remaining" tile.
  const allocationsCard = elements.totalAllocations?.parentElement;
  const allocationsLabel = allocationsCard?.querySelector("p");
  const allocationsTip = allocationsCard?.querySelector(".card-tooltip");
  const remainingCard = elements.totalRemaining?.parentElement;
  const remainingLabel = remainingCard?.querySelector("p");
  const remainingTip = remainingCard?.querySelector(".card-tooltip");
  const usedCard = elements.totalUsed?.parentElement;
  const usedLabel = usedCard?.querySelector("p");
  const usedTip = usedCard?.querySelector(".card-tooltip");

  if (summary.fairshareOnly && summary.projects > 0) {
    if (allocationsLabel) allocationsLabel.textContent = "Projects tracked";
    if (allocationsTip) {
      allocationsTip.title = "Project accounts you have access to across the fleet.";
    }
    if (elements.totalAllocations) {
      elements.totalAllocations.textContent = formatInteger(summary.projects);
    }
    if (usedLabel) usedLabel.textContent = "FY core-hours used";
    if (usedTip) {
      usedTip.title =
        "Core-hours consumed since the start of the current NOAA fiscal year (Oct 1).";
    }
    if (elements.totalUsed) {
      elements.totalUsed.textContent = summary.used
        ? `${formatHours(summary.used, { compact: true })} hrs`
        : "0 hrs";
    }
    if (remainingLabel) remainingLabel.textContent = "Max concurrent jobs";
    if (remainingTip) {
      remainingTip.title =
        "Highest per-association MaxJobs cap across your projects (sacctmgr show association). " +
        "This is the hardest limit the scheduler enforces on you.";
    }
    if (elements.totalRemaining) {
      elements.totalRemaining.textContent = summary.maxConcurrentJobs
        ? formatInteger(summary.maxConcurrentJobs)
        : "--";
    }
  } else {
    if (allocationsLabel) allocationsLabel.textContent = "Total allocations";
    if (elements.totalAllocations) {
      elements.totalAllocations.textContent = summary.allocations
        ? `${formatHours(summary.allocations, { compact: true })} hrs`
        : "--";
    }
    if (usedLabel) usedLabel.textContent = "Hours used";
    if (elements.totalUsed) {
      elements.totalUsed.textContent = summary.used
        ? `${formatHours(summary.used, { compact: true })} hrs`
        : "--";
    }
    if (remainingLabel) remainingLabel.textContent = "Hours remaining";
    if (elements.totalRemaining) {
      elements.totalRemaining.textContent = summary.remaining
        ? `${formatHours(summary.remaining, { compact: true })} hrs`
        : "--";
    }
  }
  renderFleetUsageDonut(summary);
  renderFleetQueueSnapshot();
};

const queueState = (queue) => {
  const running = toNumber(queue.jobs_running);
  const pending = toNumber(queue.jobs_pending);
  if (pending > 0) return "backlog";
  if (running > 0) return "active";
  return "idle";
};

const buildQueueChips = (queues) => {
  if (!queues.length) {
    return '<span class="queue-chip is-idle">No queue data</span>';
  }
  const sorted = [...queues].sort((a, b) => {
    const aLoad = toNumber(a.cores_running) + toNumber(a.cores_pending);
    const bLoad = toNumber(b.cores_running) + toNumber(b.cores_pending);
    return bLoad - aLoad;
  });
  return sorted.slice(0, 8).map((queue) => {
    const stateClass = queueState(queue);
    const running = formatInteger(queue.jobs_running || 0);
    const pending = formatInteger(queue.jobs_pending || 0);
    const maxWall = queue.max_walltime && queue.max_walltime !== "-" ? queue.max_walltime : "--";
    return `
      <span class="queue-chip is-${stateClass}" title="Max wall ${maxWall}">
        ${queue.queue_name || "queue"}
        <small>${running} run / ${pending} pend</small>
      </span>
    `;
  }).join("");
};

const buildSubprojectRows = (systems, { fairshareMode = false } = {}) => {
  if (!systems.length) {
    return '<tr><td colspan="4" class="placeholder">No subprojects reported.</td></tr>';
  }
  if (fairshareMode) {
    const sorted = [...systems].sort(
      (a, b) => (Number(b.hours_used) || 0) - (Number(a.hours_used) || 0),
    );
    const limited = sorted.slice(0, 5);
    const remainder = sorted.length - limited.length;
    const rows = limited
      .map((system) => {
        const fy = system.hours_used != null ? formatHours(system.hours_used) : "--";
        const maxJobs =
          system.account_max_jobs != null && system.account_max_jobs !== ""
            ? formatInteger(system.account_max_jobs)
            : null;

        // ----- Allocation cell ------------------------------------------
        // NOAA RDHPCS hands out allocations as a fairshare ratio: NormShares
        // is the project's slice of the cluster, EffUsage is the decay-
        // adjusted share consumed. EffUsage / NormShares > 1 means the
        // project has burned more than its allocation and the scheduler is
        // deprioritizing it (fairshare drops).
        // See https://docs.rdhpcs.noaa.gov/slurm/overview.html#priority-and-fairshare
        const sharePct =
          typeof system.norm_shares === "number"
            ? system.norm_shares * 100
            : null;
        const usagePct =
          typeof system.effective_usage === "number"
            ? system.effective_usage * 100
            : null;

        let allocationCell;
        if (sharePct === null && usagePct === null) {
          allocationCell = '<span class="muted-text">--</span>';
        } else {
          const shareLabel =
            sharePct !== null ? `${formatFsPct(sharePct)} share` : "no share";
          let usageRatio = null;
          if (sharePct && sharePct > 0 && usagePct !== null) {
            usageRatio = usagePct / sharePct;
          }
          let statusTag = "is-balanced";
          let statusText = "";
          if (usageRatio !== null) {
            if (usageRatio >= 1.5) {
              statusTag = "is-over";
              statusText = `${usageRatio.toFixed(1)}× over share`;
            } else if (usageRatio >= 1.0) {
              statusTag = "is-warn";
              statusText = "at share";
            } else if (usageRatio > 0) {
              statusTag = "is-under";
              statusText = `${(usageRatio * 100).toFixed(0)}% of share used`;
            }
          } else if (usagePct === 0) {
            statusText = "no usage yet";
          }
          const usageLabel =
            usagePct !== null ? `${formatFsPct(usagePct)} used` : "";

          // Fairshare priority signal: rank tells you scheduling position
          // (lower = higher priority); score is the [0,1] decayed ratio
          // Slurm hands to the priority plugin.
          const fsParts = [];
          if (system.fairshare_rank) {
            fsParts.push(`rank ${system.fairshare_rank}`);
          }
          if (typeof system.fairshare_score === "number") {
            fsParts.push(`FS ${system.fairshare_score.toFixed(3)}`);
          }
          const fsLine = fsParts.length
            ? `<small class="fs-priority muted-text" title="Slurm fairshare: rank is this project's scheduling position among all projects on the cluster (lower = higher priority). FS is the decay-adjusted ratio Slurm uses to rank jobs.">${fsParts.join(" · ")}</small>`
            : "";

          const title = `NormShares ${
            sharePct !== null ? sharePct.toFixed(4) + "%" : "—"
          }, EffUsage ${
            usagePct !== null ? usagePct.toFixed(4) + "%" : "—"
          }`;
          allocationCell = `
            <div class="fs-cell" title="${title}">
              <strong>${shareLabel}</strong>
              <small>${usageLabel}${statusText ? " · " : ""}<span class="fs-status ${statusTag}">${statusText}</span></small>
              ${fsLine}
            </div>
          `;
        }

        // ----- QoS chips (compact, sorted by walltime desc) -------------
        const qosLimits = system.qos_limits || {};
        const qosList = Array.isArray(system.qoses) ? system.qoses : [];

        const walltimeSeconds = (raw) => {
          if (!raw || /^unlimited$/i.test(raw)) return Infinity;
          const m = String(raw).match(/^(?:(\d+)-)?(\d+):(\d+)(?::(\d+))?$/);
          if (!m) return 0;
          const d = Number(m[1] || 0);
          const h = Number(m[2] || 0);
          const min = Number(m[3] || 0);
          const sec = Number(m[4] || 0);
          return ((d * 24 + h) * 60 + min) * 60 + sec;
        };

        const enrichedQos = qosList.map((q) => {
          const lim = qosLimits[q] || {};
          const wall = formatWalltime(lim.max_wall);
          const tres = lim.max_tres || "";
          const nodeMatch = tres.match(/node=(\d+)/);
          const nodes = nodeMatch ? `${nodeMatch[1]}n` : "";
          const annot = [wall, nodes].filter(Boolean).join(" · ");
          return {
            name: q,
            annot,
            wall_seconds: walltimeSeconds(lim.max_wall),
            label: annot ? `${q} (${annot})` : q,
          };
        });
        enrichedQos.sort((a, b) => b.wall_seconds - a.wall_seconds);

        const QOS_VISIBLE = 4;
        const visible = enrichedQos.slice(0, QOS_VISIBLE);
        const hidden = enrichedQos.slice(QOS_VISIBLE);
        const visibleHtml = visible
          .map((q) => {
            const title = q.annot
              ? `${q.name}: max ${q.annot}`
              : `${q.name} (no published limits)`;
            return `<span class="queue-chip is-idle qos-chip" title="${title}">${q.name}${
              q.annot ? `<small>·${q.annot}</small>` : ""
            }</span>`;
          })
          .join(" ");
        const hiddenHtml = hidden.length
          ? `<span class="qos-more muted-text" title="${hidden
              .map((q) => q.label)
              .join(", ")}">+${hidden.length} more</span>`
          : "";
        const qosChips = qosList.length
          ? `<div class="qos-chip-row">${visibleHtml}${hiddenHtml}</div>`
          : '<span class="muted-text">--</span>';

        // Max-jobs chip lives under the subproject code so we keep the row
        // narrow but still surface the operational ceiling.
        const maxJobsChip = maxJobs
          ? `<small class="muted-text">Max jobs: ${maxJobs}</small>`
          : "";

        return `
          <tr>
            <td>${system.system || "--"}</td>
            <td>
              <code>${system.subproject || "--"}</code>
              ${maxJobsChip ? `<br>${maxJobsChip}` : ""}
            </td>
            <td>${allocationCell}</td>
            <td title="Core-hours used since NOAA fiscal year start (Oct 1)">${fy}</td>
            <td>${qosChips}</td>
          </tr>
        `;
      })
      .join("");
    if (remainder > 0) {
      return `${rows}<tr><td colspan="5" class="placeholder">+${remainder} additional subprojects</td></tr>`;
    }
    return rows;
  }
  // Legacy HPCMP-style layout (allocated/availability)
  const sorted = [...systems].sort((a, b) => (Number(b.hours_allocated) || 0) - (Number(a.hours_allocated) || 0));
  const limited = sorted.slice(0, 5);
  const remainder = sorted.length - limited.length;
  const rows = limited
    .map((system) => {
      const percentRemaining = clampPercent(system.percent_remaining);
      return `
        <tr>
          <td>${system.system || "--"}</td>
          <td><code>${system.subproject || "--"}</code></td>
          <td>${formatHours(system.hours_allocated)}</td>
          <td>
            <div class="usage-progress compact">
              <div class="progress-track">
                <div class="progress-value" style="width:${percentRemaining}%"></div>
              </div>
              <span>${percentRemaining.toFixed(1)}% remaining</span>
            </div>
          </td>
        </tr>
      `;
    })
    .join("");
  if (remainder > 0) {
    return `${rows}<tr><td colspan="4" class="placeholder">+${remainder} additional subprojects</td></tr>`;
  }
  return rows;
};

const buildGpuRows = (gpus) => {
  if (!gpus.length) {
    return '<tr><td colspan="5" class="placeholder">No GPUs detected.</td></tr>';
  }
  return gpus.map((gpu) => {
    const memPercent = gpu.memory_total_mib
      ? clampPercent((gpu.memory_used_mib / gpu.memory_total_mib) * 100)
      : 0;
    const memFree = gpu.memory_total_mib - gpu.memory_used_mib;
    return `
      <tr>
        <td>${gpu.index}</td>
        <td>${gpu.name || "--"}</td>
        <td>${formatInteger(gpu.memory_total_mib)} MiB</td>
        <td>
          <div class="usage-progress compact">
            <div class="progress-track">
              <div class="progress-value" style="width:${gpu.utilization_percent || 0}%"></div>
            </div>
            <span>${gpu.utilization_percent || 0}%</span>
          </div>
        </td>
        <td>
          <div class="usage-progress compact">
            <div class="progress-track">
              <div class="progress-value" style="width:${memPercent}%"></div>
            </div>
            <span>${formatInteger(memFree)} MiB free</span>
          </div>
        </td>
      </tr>
    `;
  }).join("");
};

/**
 * Links from a cluster card to that cluster's own pages.
 *
 * The quota page is one long column of cards, and finding a system's
 * queues meant scrolling back to the nav, opening Queue health, and
 * picking the cluster again. Both pages already deep-link by slug.
 */
const escapeHtml = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]
  );

const clusterLinks = (metadata) => {
  if (!clusterPagesEnabled()) return "";
  const name = metadata?.name || String(metadata?.uri || "").split("/").pop() || "";
  const slug = String(name).toLowerCase().replace(/[^a-z0-9]/g, "");
  if (!slug) return "";
  const to = encodeURIComponent(slug);
  return `
    <nav class="cluster-card-links" aria-label="${escapeHtml(name)} detail pages">
      <a href="queues.html?cluster=${to}">Queues</a>
      <a href="storage.html?cluster=${to}">Storage</a>
    </nav>`;
};

const buildGpuClusterCard = (cluster) => {
  const metadata = cluster?.cluster_metadata || {};
  const gpus = parseGpus(cluster);
  const sysInfo = getSystemInfo(cluster);
  const gpuSummary = cluster?.gpu_data?.summary || {};

  const metaParts = [];
  if (metadata.status) metaParts.push(String(metadata.status).toUpperCase());
  if (metadata.type) metaParts.push(metadata.type);
  if (sysInfo.hostname && sysInfo.hostname !== "unknown") metaParts.push(sysInfo.hostname);
  if (metadata.timestamp) metaParts.push(new Date(metadata.timestamp).toLocaleString());

  const totalMem = gpuSummary.total_memory_mib || 0;
  const freeMem = gpuSummary.free_memory_mib || 0;
  const percentFree = totalMem ? clampPercent((freeMem / totalMem) * 100) : 0;

  const memoryDetail = totalMem
    ? `${formatInteger(freeMem)} of ${formatInteger(totalMem)} MiB`
    : "No GPU memory data";

  return `
    <article class="cluster-card">
      <header>
        <div>
          <p class="eyebrow">GPU Server</p>
          <h4>${metadata.name || metadata.uri || "Cluster"}</h4>
          <p class="muted-text">${metaParts.join(" • ")}</p>
        </div>
        ${clusterLinks(metadata)}
      </header>
      <div class="cluster-card-body">
        <div class="cluster-card-summary">
          <div class="donut-chart" aria-label="${metadata.name || "Cluster"} GPU memory">
            <div class="donut" style="--donut-value:${percentFree}">
              <strong>${Math.round(percentFree)}%</strong>
              <span>Free</span>
            </div>
            <small>${memoryDetail}</small>
          </div>
          <ul class="cluster-metrics">
            <li><span>GPUs</span><strong>${gpuSummary.gpu_count || 0}</strong></li>
            <li><span>Avg Util</span><strong>${gpuSummary.avg_utilization_percent || 0}%</strong></li>
            <li><span>CPUs</span><strong>${sysInfo.cpu_count || "--"}</strong></li>
            <li><span>Load (1m)</span><strong>${sysInfo.load_1m?.toFixed(2) || "--"}</strong></li>
          </ul>
        </div>
        <div class="cluster-subprojects">
          <div class="table-head compact">
            <h5>GPU Status</h5>
            <span>${gpus.length} GPUs</span>
          </div>
          <div class="table-scroll mini">
            <table class="quota-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>GPU</th>
                  <th>Memory</th>
                  <th>Utilization</th>
                  <th>Memory Usage</th>
                </tr>
              </thead>
              <tbody>
                ${buildGpuRows(gpus)}
              </tbody>
            </table>
          </div>
        </div>
        <div class="cluster-queues">
          <div class="cluster-queues-head">
            <h5>System Resources</h5>
            <span>${sysInfo.hostname || "Unknown host"}</span>
          </div>
          <div class="queue-chip-collection">
            <span class="queue-chip is-active">RAM <small>${formatInteger(sysInfo.memory_total_mb || 0)} MB</small></span>
            <span class="queue-chip ${sysInfo.load_1m > (sysInfo.cpu_count || 1) ? "is-backlog" : "is-idle"}">Load <small>${sysInfo.load_1m?.toFixed(2) || "0"} / ${sysInfo.load_5m?.toFixed(2) || "0"} / ${sysInfo.load_15m?.toFixed(2) || "0"}</small></span>
          </div>
        </div>
      </div>
    </article>
  `;
};

const buildSystemOnlyCard = (cluster) => {
  const metadata = cluster?.cluster_metadata || {};
  const sysInfo = getSystemInfo(cluster);

  const metaParts = [];
  if (metadata.status) metaParts.push(String(metadata.status).toUpperCase());
  if (metadata.type) metaParts.push(metadata.type);
  if (sysInfo.hostname && sysInfo.hostname !== "unknown") metaParts.push(sysInfo.hostname);
  if (metadata.timestamp) metaParts.push(new Date(metadata.timestamp).toLocaleString());

  const memTotal = sysInfo.memory_total_mb || 0;
  const memUsed = sysInfo.memory_used_mb || 0;
  const memFree = memTotal - memUsed;
  const percentFree = memTotal ? clampPercent((memFree / memTotal) * 100) : 0;

  return `
    <article class="cluster-card">
      <header>
        <div>
          <p class="eyebrow">Compute Server</p>
          <h4>${metadata.name || metadata.uri || "Cluster"}</h4>
          <p class="muted-text">${metaParts.join(" • ")}</p>
        </div>
        ${clusterLinks(metadata)}
      </header>
      <div class="cluster-card-body">
        <div class="cluster-card-summary">
          <div class="donut-chart" aria-label="${metadata.name || "Cluster"} memory">
            <div class="donut" style="--donut-value:${percentFree}">
              <strong>${Math.round(percentFree)}%</strong>
              <span>Free</span>
            </div>
            <small>${formatInteger(memFree)} of ${formatInteger(memTotal)} MB</small>
          </div>
          <ul class="cluster-metrics">
            <li><span>CPUs</span><strong>${sysInfo.cpu_count || "--"}</strong></li>
            <li><span>RAM</span><strong>${formatInteger(memTotal)} MB</strong></li>
            <li><span>Load (1m)</span><strong>${sysInfo.load_1m?.toFixed(2) || "--"}</strong></li>
            <li><span>Load (5m)</span><strong>${sysInfo.load_5m?.toFixed(2) || "--"}</strong></li>
          </ul>
        </div>
        <div class="cluster-subprojects">
          <div class="table-head compact">
            <h5>System Info</h5>
            <span>${sysInfo.hostname || "Unknown host"}</span>
          </div>
          <div class="table-scroll mini">
            <table class="quota-table">
              <thead>
                <tr>
                  <th>Property</th>
                  <th>Value</th>
                </tr>
              </thead>
              <tbody>
                <tr><td>Hostname</td><td>${sysInfo.hostname || "--"}</td></tr>
                <tr><td>CPU Count</td><td>${sysInfo.cpu_count || "--"}</td></tr>
                <tr><td>Memory Total</td><td>${formatInteger(memTotal)} MB</td></tr>
                <tr><td>Memory Used</td><td>${formatInteger(memUsed)} MB</td></tr>
                <tr><td>Load Average</td><td>${sysInfo.load_1m?.toFixed(2) || "0"} / ${sysInfo.load_5m?.toFixed(2) || "0"} / ${sysInfo.load_15m?.toFixed(2) || "0"}</td></tr>
              </tbody>
            </table>
          </div>
        </div>
        <div class="cluster-queues">
          <div class="cluster-queues-head">
            <h5>Status</h5>
            <span>No scheduler detected</span>
          </div>
          <div class="queue-chip-collection">
            <span class="queue-chip is-active">Online</span>
            <span class="queue-chip is-idle">No HPC Queue</span>
          </div>
        </div>
      </div>
    </article>
  `;
};

const buildClusterCard = (cluster) => {
  // Check if this is a GPU-only cluster (no scheduler)
  if (!hasScheduler(cluster) && parseGpus(cluster).length > 0) {
    return buildGpuClusterCard(cluster);
  }

  // Check if this is a non-GPU compute server (no scheduler, no GPUs)
  const sysInfo = getSystemInfo(cluster);
  if (!hasScheduler(cluster) && sysInfo.cpu_count) {
    return buildSystemOnlyCard(cluster);
  }

  const metadata = cluster?.cluster_metadata || {};
  const systems = parseSystems(cluster);
  const queues = parseQueues(cluster);
  const totals = clusterTotals(cluster);
  const fairshareMode = clusterIsFairshare(cluster);
  const fyStart = fiscalYearStart(cluster);
  const percentRemaining = totals.allocations
    ? clampPercent((totals.remaining / totals.allocations) * 100)
    : 0;
  const metaParts = [];
  if (metadata.status) metaParts.push(String(metadata.status).toUpperCase());
  if (metadata.type) metaParts.push(metadata.type);
  if (metadata.uri) metaParts.push(metadata.uri);
  if (metadata.timestamp) metaParts.push(new Date(metadata.timestamp).toLocaleString());

  let donutBlock;
  let metricsList;
  if (fairshareMode) {
    // NOAA fairshare style — what users care about: FY usage, projects, and
    // their concurrent-job ceiling (the highest cap across their projects).
    const maxJobsAcrossProjects = systems
      .map((s) => Number(s.account_max_jobs))
      .filter((n) => Number.isFinite(n) && n > 0)
      .reduce((a, b) => Math.max(a, b), 0);
    const fyDetail = fyStart ? `FY since ${fyStart}` : "Fiscal year usage";
    donutBlock = `
      <div class="donut-chart" aria-label="${metadata.name || "Cluster"} fiscal year usage">
        <div class="donut" style="--donut-value:100">
          <strong>${formatHours(totals.used, { compact: true })}</strong>
          <span>FY hrs</span>
        </div>
        <small>${fyDetail}</small>
      </div>
    `;
    metricsList = `
      <ul class="cluster-metrics">
        <li title="Total core-hours consumed this fiscal year"><span>FY Used</span><strong>${formatHours(totals.used)}</strong></li>
        <li title="Number of projects you have access to on this cluster"><span>Projects</span><strong>${systems.length}</strong></li>
        <li title="Highest concurrent-job cap across this cluster's projects (sacctmgr MaxJobs)"><span>Max jobs</span><strong>${maxJobsAcrossProjects ? formatInteger(maxJobsAcrossProjects) : "--"}</strong></li>
      </ul>
    `;
  } else {
    const donutDetail = totals.allocations
      ? `${formatHours(totals.remaining, { compact: true })} of ${formatHours(totals.allocations, { compact: true })} hrs`
      : "No allocation data";
    donutBlock = `
      <div class="donut-chart" aria-label="${metadata.name || metadata.uri || "Cluster"} hours remaining">
        <div class="donut" style="--donut-value:${percentRemaining}">
          <strong>${Math.round(percentRemaining)}%</strong>
          <span>Remaining</span>
        </div>
        <small>${donutDetail}</small>
      </div>
    `;
    metricsList = `
      <ul class="cluster-metrics">
        <li title="Total core-hours granted to this cluster"><span>Allocated</span><strong>${formatHours(totals.allocations)}</strong></li>
        <li title="Core-hours already consumed"><span>Used</span><strong>${formatHours(totals.used)}</strong></li>
        <li title="Core-hours still available"><span>Remaining</span><strong>${formatHours(totals.remaining)}</strong></li>
      </ul>
    `;
  }

  const tableHeader = fairshareMode
    ? `
      <tr>
        <th>System <span class="th-help" title="HPC system for this project">ⓘ</span></th>
        <th>Subproject <span class="th-help" title="Project code (NOAA RDHPCS account name). Max jobs is the concurrent-job cap from sacctmgr.">ⓘ</span></th>
        <th>Allocation <span class="th-help" title="NOAA expresses allocations as a Slurm fairshare ratio. Share = NormShares (your slice of the cluster). Used = EffUsage (decay-adjusted consumption). When used > share, the scheduler deprioritises new jobs from this project. See https://docs.rdhpcs.noaa.gov/slurm/overview.html#priority-and-fairshare">ⓘ</span></th>
        <th>FY Used (hrs) <span class="th-help" title="Absolute core-hours consumed since the start of the current NOAA fiscal year (Oct 1), per sreport. The fairshare ratio above is decay-adjusted and normalised across the cluster, so this number won't match a simple multiplication.">ⓘ</span></th>
        <th>QoSes &amp; walltime <span class="th-help" title="Queue-of-service classes you can submit to, each annotated with its max walltime and (where set) max node count">ⓘ</span></th>
      </tr>
    `
    : `
      <tr>
        <th>System <span class="th-help" title="HPC system for this allocation">ⓘ</span></th>
        <th>Subproject <span class="th-help" title="Project code or sub-account identifier">ⓘ</span></th>
        <th>Allocated <span class="th-help" title="Total core-hours granted">ⓘ</span></th>
        <th>Availability <span class="th-help" title="Percentage of allocation still remaining">ⓘ</span></th>
      </tr>
    `;

  return `
    <article class="cluster-card">
      <header>
        <div>
          <p class="eyebrow">${metadata.status ? metadata.status.toUpperCase() : "Cluster"}</p>
          <h4>${metadata.name || metadata.uri || "Cluster"}</h4>
          <p class="muted-text">${metaParts.join(" • ")}</p>
        </div>
        ${clusterLinks(metadata)}
      </header>
      <div class="cluster-card-body">
        <div class="cluster-card-summary">
          ${donutBlock}
          ${metricsList}
        </div>
        <div class="cluster-subprojects">
          <div class="table-head compact">
            <h5>${fairshareMode ? "Projects" : "Subprojects"} <span class="th-help" title="${fairshareMode ? "Projects you have access to on this cluster, with FY-to-date usage and fairshare context." : "Allocations broken down by project or sub-account"}">ⓘ</span></h5>
            <span>${systems.length} total</span>
          </div>
          <div class="table-scroll mini">
            <table class="quota-table">
              <thead>${tableHeader}</thead>
              <tbody>
                ${buildSubprojectRows(systems, { fairshareMode })}
              </tbody>
            </table>
          </div>
        </div>
        <div class="cluster-queues">
          <div class="cluster-queues-head">
            <h5>Queue snapshot <span class="th-help" title="Current queue activity: Active=has running jobs, Backlog=jobs waiting, Idle=no activity">ⓘ</span></h5>
            <span>${queues.length ? `${queues.length} queues` : "No queues"}</span>
          </div>
          <div class="queue-chip-collection">
            ${buildQueueChips(queues)}
          </div>
        </div>
      </div>
    </article>
  `;
};

const renderClusterGrid = () => {
  if (!elements.clusterGrid) return;
  if (!state.clusters.length) {
    showGeneratingPlaceholder("Cluster monitor is gathering usage data…");
    return;
  }
  const sorted = [...state.clusters].sort((a, b) => {
    const aTotals = clusterTotals(a);
    const bTotals = clusterTotals(b);
    // Allocation mode: sort by least-remaining ratio (most-burned first).
    // Fairshare mode (NOAA): sort by hours used descending.
    if (aTotals.allocations || bTotals.allocations) {
      const aPct = aTotals.allocations ? aTotals.remaining / aTotals.allocations : 0;
      const bPct = bTotals.allocations ? bTotals.remaining / bTotals.allocations : 0;
      return aPct - bPct;
    }
    return (bTotals.used || 0) - (aTotals.used || 0);
  });
  elements.clusterGrid.innerHTML = sorted.map((cluster) => buildClusterCard(cluster)).join("");
  if (elements.clusterGridNote) {
    const latest = sorted.reduce((acc, cluster) => {
      const ts = Date.parse(cluster?.cluster_metadata?.timestamp || "");
      return Number.isFinite(ts) ? Math.max(acc, ts) : acc;
    }, 0);
    if (latest) {
      elements.clusterGridNote.textContent = `Updated ${new Date(latest).toLocaleString()}`;
    } else if (state.lastUpdated) {
      elements.clusterGridNote.textContent = `Updated ${new Date(state.lastUpdated).toLocaleString()}`;
    } else {
      elements.clusterGridNote.textContent = "Timestamp unavailable";
    }
  }
};

const bindEvents = () => {
  if (elements.refreshBtn) {
    elements.refreshBtn.addEventListener("click", () => loadData({ silent: false }));
  }
};

const applyClusterPayload = (payload) => {
  // Handle both array format and {clusters: [...]} format
  if (Array.isArray(payload)) {
    state.clusters = payload;
  } else if (payload && Array.isArray(payload.clusters)) {
    state.clusters = payload.clusters;
  } else {
    state.clusters = [];
  }
  state.lastUpdated = Date.now();
  renderSummary();
  renderClusterGrid();
};

const loadData = async ({ silent = true } = {}) => {
  if (!state.features.clusterPages) {
    return;
  }
  if (state.loading) return;
  const hadData = state.clusters.length > 0;
  if (!hadData) {
    showGeneratingPlaceholder();
  }
  state.loading = true;
  disableRefresh(true);
  if (!silent) {
    setBanner("Refreshing quota data…");
  }
  try {
    const response = await fetch(`${DATA_URL}?t=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const payload = await response.json();
    // First-sweep envelope: show progress instead of an empty "no data" state.
    const envelopeStatus =
      payload && !Array.isArray(payload) && typeof payload === "object"
        ? payload.status
        : null;
    if (envelopeStatus === "warming_up" && (!payload.clusters || !payload.clusters.length)) {
      state.clusters = [];
      state.lastUpdated = Date.now();
      renderSummary();
      showGeneratingPlaceholder(formatProgressMessage(payload.progress, true));
      setBanner(formatProgressStatus(payload.progress, true), "info");
      scheduleRetry();
      return;
    }
    applyClusterPayload(payload);
    if (envelopeStatus === "partial") {
      setBanner(formatProgressStatus(payload.progress, false), "info");
      scheduleRetry();
    } else {
      setBanner(silent ? "" : "Quota data updated just now.");
      if (state.clusters.length) {
        clearRetry();
      } else {
        scheduleRetry();
      }
    }
  } catch (err) {
    console.error("Unable to load quota data", err);
    setBanner(`Unable to load quota data (${err.message}).`, "error");
    if (!hadData) {
      showGeneratingPlaceholder("Waiting for cluster monitor to finish…");
    }
    scheduleRetry();
  } finally {
    state.loading = false;
    disableRefresh(false);
  }
};

const applyConfigBranding = () => {
  const title = window.APP_CONFIG?.title || "HPC Status Monitor";
  const eyebrowText = window.APP_CONFIG?.eyebrow || "HPC STATUS";
  const eyebrow = document.getElementById("header-eyebrow");
  if (eyebrow) {
    eyebrow.textContent = eyebrowText;
  }
  document.title = `Quota Usage | ${title}`;
};

const bootstrap = () => {
  cacheElements();
  initThemeToggle();
  initHelpPanel();
  initQuickTips();
  applyConfigBranding();
  initBrand();
  initNav();
  const nav = document.querySelector("[data-cluster-nav]");
  if (!state.features.clusterPages) {
    if (nav) nav.remove();
    setBanner("Cluster pages are disabled on this server.", "error");
    showGeneratingPlaceholder("Cluster usage pages disabled.");
    disableRefresh(true);
    clearRetry();
    return;
  }
  bindEvents();
  loadData();
};

// Run once, whenever this module happens to execute: a module script
// normally runs before DOMContentLoaded, but waiting for an event that may
// already have fired means never booting at all.
let booted = false;
const bootOnce = () => {
  if (booted) return;
  booted = true;
  bootstrap();
};

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", bootOnce, { once: true });
} else {
  bootOnce();
}
