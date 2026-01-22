import { buildDataUrl, clampPercent, clusterPagesEnabled, initThemeToggle, buildApiUrl } from "./page-utils.js";

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

const parseSystems = (cluster) => cluster?.usage_data?.systems || [];
const parseQueues = (cluster) => cluster?.queue_data?.queues || [];
const parseGpus = (cluster) => cluster?.gpu_data?.gpus || [];
const getSystemInfo = (cluster) => cluster?.system_info || {};
const hasScheduler = (cluster) => cluster?.cluster_metadata?.has_scheduler !== false &&
  (parseSystems(cluster).length > 0 || parseQueues(cluster).length > 0);

const computeSummary = () => {
  const totals = { allocations: 0, used: 0, remaining: 0, gpuCount: 0, gpuMemoryMib: 0 };
  state.clusters.forEach((cluster) => {
    parseSystems(cluster).forEach((system) => {
      totals.allocations += Number(system.hours_allocated) || 0;
      totals.used += Number(system.hours_used) || 0;
      totals.remaining += Number(system.hours_remaining) || 0;
    });
    // Also count GPUs
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
  btn.textContent = disabled ? "Refreshing…" : "Refresh data";
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
  if (!summary.allocations) {
    elements.fleetUsageDonut.innerHTML = '<div class="placeholder">No usage data yet.</div>';
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
  if (elements.totalAllocations) {
    elements.totalAllocations.textContent = summary.allocations
      ? `${formatHours(summary.allocations, { compact: true })} hrs`
      : "--";
  }
  if (elements.totalUsed) {
    elements.totalUsed.textContent = summary.used
      ? `${formatHours(summary.used, { compact: true })} hrs`
      : "--";
  }
  if (elements.totalRemaining) {
    elements.totalRemaining.textContent = summary.remaining
      ? `${formatHours(summary.remaining, { compact: true })} hrs`
      : "--";
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

const buildSubprojectRows = (systems) => {
  if (!systems.length) {
    return '<tr><td colspan="4" class="placeholder">No subprojects reported.</td></tr>';
  }
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
  const percentRemaining = totals.allocations
    ? clampPercent((totals.remaining / totals.allocations) * 100)
    : 0;
  const metaParts = [];
  if (metadata.status) metaParts.push(String(metadata.status).toUpperCase());
  if (metadata.type) metaParts.push(metadata.type);
  if (metadata.uri) metaParts.push(metadata.uri);
  if (metadata.timestamp) metaParts.push(new Date(metadata.timestamp).toLocaleString());
  const donutDetail = totals.allocations
    ? `${formatHours(totals.remaining, { compact: true })} of ${formatHours(totals.allocations, { compact: true })} hrs`
    : "No allocation data";
  return `
    <article class="cluster-card">
      <header>
        <div>
          <p class="eyebrow">${metadata.status ? metadata.status.toUpperCase() : "Cluster"}</p>
          <h4>${metadata.name || metadata.uri || "Cluster"}</h4>
          <p class="muted-text">${metaParts.join(" • ")}</p>
        </div>
      </header>
      <div class="cluster-card-body">
        <div class="cluster-card-summary">
          <div class="donut-chart" aria-label="${metadata.name || metadata.uri || "Cluster"} hours remaining">
            <div class="donut" style="--donut-value:${percentRemaining}">
              <strong>${Math.round(percentRemaining)}%</strong>
              <span>Remaining</span>
            </div>
            <small>${donutDetail}</small>
          </div>
          <ul class="cluster-metrics">
            <li><span>Allocated</span><strong>${formatHours(totals.allocations)}</strong></li>
            <li><span>Used</span><strong>${formatHours(totals.used)}</strong></li>
            <li><span>Remaining</span><strong>${formatHours(totals.remaining)}</strong></li>
          </ul>
        </div>
        <div class="cluster-subprojects">
          <div class="table-head compact">
            <h5>Subprojects</h5>
            <span>${systems.length} total</span>
          </div>
          <div class="table-scroll mini">
            <table class="quota-table">
              <thead>
                <tr>
                  <th>System</th>
                  <th>Subproject</th>
                  <th>Allocated</th>
                  <th>Availability</th>
                </tr>
              </thead>
              <tbody>
                ${buildSubprojectRows(systems)}
              </tbody>
            </table>
          </div>
        </div>
        <div class="cluster-queues">
          <div class="cluster-queues-head">
            <h5>Queue snapshot</h5>
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
    const aPct = aTotals.allocations ? aTotals.remaining / aTotals.allocations : 0;
    const bPct = bTotals.allocations ? bTotals.remaining / bTotals.allocations : 0;
    return aPct - bPct;
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
    applyClusterPayload(payload);
    setBanner(silent ? "" : "Quota data updated just now.");
    if (state.clusters.length) {
      clearRetry();
    } else {
      scheduleRetry();
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
  const eyebrow = document.getElementById("header-eyebrow");
  if (eyebrow) {
    eyebrow.textContent = "HPC STATUS";
  }
  document.title = `Quota Usage | ${title}`;
};

document.addEventListener("DOMContentLoaded", () => {
  cacheElements();
  initThemeToggle();
  applyConfigBranding();
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
});
