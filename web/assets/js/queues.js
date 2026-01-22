import { buildDataUrl, clampPercent, clusterPagesEnabled, initThemeToggle, buildApiUrl } from "./page-utils.js";

const DATA_URL = buildApiUrl("api/cluster-usage").toString();
const numberFormatter = new Intl.NumberFormat("en-US");

const RETRY_INTERVAL_MS = 15000;

const state = {
  clusters: [],
  selectedIndex: 0,
  loading: false,
  lastUpdated: null,
  retryHandle: null,
  features: {
    clusterPages: clusterPagesEnabled(),
  },
};

const elements = {};

const getElement = (id) => document.getElementById(id);

const getClusterIdentifier = (cluster) =>
  (cluster?.cluster_metadata?.uri || cluster?.cluster_metadata?.name || "").toString();

const toNumber = (value) => {
  if (value === null || value === undefined) return 0;
  const numeric = Number(String(value).replace(/,/g, ""));
  return Number.isFinite(numeric) ? numeric : 0;
};

const formatNumber = (value) => numberFormatter.format(Math.round(toNumber(value)));

const parseQueues = (cluster) => cluster?.queue_data?.queues || [];
const parseNodes = (cluster) => cluster?.queue_data?.nodes || [];
const parseGpus = (cluster) => cluster?.gpu_data?.gpus || [];
const getSystemInfo = (cluster) => cluster?.system_info || {};
const hasScheduler = (cluster) => cluster?.cluster_metadata?.has_scheduler !== false &&
  (parseQueues(cluster).length > 0 || parseNodes(cluster).length > 0);
const sanitizeNodes = (nodes) =>
  (nodes || []).filter((node) => String(node.node_type || "").toLowerCase() !== "nodes");

const computeFleetSummary = (clusters) => {
  const totals = {
    clusters: clusters.length,
    queues: 0,
    runningJobs: 0,
    pendingJobs: 0,
    runningCores: 0,
    pendingCores: 0,
    availableCores: 0,
  };

  clusters.forEach((cluster) => {
    parseQueues(cluster).forEach((queue) => {
      totals.queues += 1;
      totals.runningJobs += toNumber(queue.jobs_running);
      totals.pendingJobs += toNumber(queue.jobs_pending);
      totals.runningCores += toNumber(queue.cores_running);
      totals.pendingCores += toNumber(queue.cores_pending);
    });
    parseNodes(cluster).forEach((node) => {
      totals.availableCores += toNumber(node.cores_available);
    });
  });

  const utilization = totals.availableCores
    ? (totals.runningCores / totals.availableCores) * 100
    : 0;

  return { ...totals, utilization };
};

const aggregateQueueSnapshot = (clusters) => {
  const snapshot = { active: 0, backlog: 0, idle: 0 };
  clusters.forEach((cluster) => {
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

const disableRefresh = (disabled) => {
  const btn = elements.refreshBtn;
  if (!btn) return;
  btn.disabled = disabled;
  btn.textContent = disabled ? "Refreshing…" : "Refresh data";
};

const showGeneratingPlaceholder = (message = "Cluster monitor is generating queue data…") => {
  setQueueGridPlaceholder(message);
  setNodePlaceholder(message);
  clearClusterStats();
  if (elements.queueDepthMeta) elements.queueDepthMeta.textContent = "";
  if (elements.nodeMeta) elements.nodeMeta.textContent = "";
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

const setStatus = (message, variant = "info") => {
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

const setQueueGridPlaceholder = (message) => {
  if (!elements.queueGrid) return;
  elements.queueGrid.innerHTML = `<div class="loading-panel">${message}<small>This may take a few moments.</small></div>`;
};

const setNodePlaceholder = (message) => {
  if (!elements.nodeBody) return;
  elements.nodeBody.innerHTML = `<tr><td colspan="6" class="placeholder">${message}</td></tr>`;
};

const cacheElements = () => {
  elements.connectedCount = getElement("connected-count");
  elements.queueCount = getElement("queue-count");
  elements.runningJobs = getElement("running-jobs");
  elements.pendingJobs = getElement("pending-jobs");
  elements.refreshBtn = getElement("refresh-btn");
  elements.statusBanner = getElement("queue-status");
  elements.clusterSelect = getElement("queue-cluster-select");
  elements.clusterTitle = getElement("queue-cluster-title");
  elements.clusterMeta = getElement("queue-cluster-meta");
  elements.clusterNote = getElement("queue-cluster-note");
  elements.clusterRunningJobs = getElement("cluster-running-jobs");
  elements.clusterPendingJobs = getElement("cluster-pending-jobs");
  elements.clusterRunningCores = getElement("cluster-running-cores");
  elements.clusterPendingCores = getElement("cluster-pending-cores");
  elements.clusterCoreDonut = getElement("cluster-core-donut");
  elements.queueGrid = getElement("queue-grid");
  elements.nodeBody = getElement("node-body");
  elements.nodeMeta = getElement("node-meta");
  elements.queueDepthMeta = getElement("queue-depth-meta");
  elements.fleetCoreDonut = getElement("fleet-core-donut");
  elements.fleetQueueTags = getElement("fleet-queue-tags");
};

const bindEvents = () => {
  if (elements.refreshBtn) {
    elements.refreshBtn.addEventListener("click", () => loadData({ silent: false }));
  }
  if (elements.clusterSelect) {
    elements.clusterSelect.addEventListener("change", (event) => {
      state.selectedIndex = Number(event.target.value) || 0;
      renderClusterDetail();
    });
  }
};

const renderFleetCoreDonut = (summary) => {
  if (!elements.fleetCoreDonut) return;
  if (!summary.availableCores) {
    elements.fleetCoreDonut.innerHTML = '<div class="placeholder">No core data yet.</div>';
    return;
  }
  const percent = clampPercent(summary.utilization);
  elements.fleetCoreDonut.innerHTML = `
    <div class="donut" style="--donut-value:${percent};--donut-primary:var(--accent);">
      <strong>${percent.toFixed(1)}%</strong>
      <span>Utilized</span>
    </div>
    <small>${formatNumber(summary.runningCores)} / ${formatNumber(summary.availableCores)} cores</small>
  `;
};

const renderFleetQueueTags = (snapshot) => {
  if (!elements.fleetQueueTags) return;
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
  const summary = computeFleetSummary(state.clusters);
  if (elements.connectedCount) {
    elements.connectedCount.textContent = formatNumber(summary.clusters);
  }
  if (elements.queueCount) {
    elements.queueCount.textContent = formatNumber(summary.queues);
  }
  if (elements.runningJobs) {
    elements.runningJobs.textContent = formatNumber(summary.runningJobs);
  }
  if (elements.pendingJobs) {
    elements.pendingJobs.textContent = formatNumber(summary.pendingJobs);
  }
  renderFleetCoreDonut(summary);
  renderFleetQueueTags(aggregateQueueSnapshot(state.clusters));
};

const renderClusterOptions = () => {
  if (!elements.clusterSelect) return;
  if (!state.clusters.length) {
    elements.clusterSelect.innerHTML = "";
    elements.clusterSelect.disabled = true;
    return;
  }
  elements.clusterSelect.disabled = false;
  elements.clusterSelect.innerHTML = state.clusters
    .map((cluster, idx) => {
      const name =
        cluster?.cluster_metadata?.name || cluster?.cluster_metadata?.uri || `Cluster ${idx + 1}`;
      const selected = idx === state.selectedIndex ? "selected" : "";
      return `<option value="${idx}" ${selected}>${name}</option>`;
    })
    .join("");
};

const renderQueueGrid = (queues) => {
  if (!elements.queueGrid) return;
  if (!queues.length) {
    setQueueGridPlaceholder("No queue information available for this cluster.");
    return;
  }
  const cards = [...queues]
    .sort((a, b) => {
      const aLoad = toNumber(a.cores_running) + toNumber(a.cores_pending);
      const bLoad = toNumber(b.cores_running) + toNumber(b.cores_pending);
      return bLoad - aLoad;
    })
    .map((queue) => {
      const runningJobs = toNumber(queue.jobs_running);
      const pendingJobs = toNumber(queue.jobs_pending);
      const runningCores = toNumber(queue.cores_running);
      const pendingCores = toNumber(queue.cores_pending);
      const totalJobs = runningJobs + pendingJobs;
      const totalCores = runningCores + pendingCores;
      const jobsRatio = clampPercent(totalJobs ? (runningJobs / totalJobs) * 100 : 0);
      const pendingJobsRatio = clampPercent(totalJobs ? 100 - jobsRatio : 0);
      const coresRatio = clampPercent(totalCores ? (runningCores / totalCores) * 100 : 0);
      const pendingCoresRatio = clampPercent(totalCores ? 100 - coresRatio : 0);
      return `
        <article class="queue-card">
          <header class="queue-card-head">
            <h4>${queue.queue_name || "Queue"}</h4>
            <span class="badge">${queue.queue_type || "—"}</span>
          </header>
          <dl class="queue-card-metrics">
            <div>
              <dt>Running jobs</dt>
              <dd>${formatNumber(runningJobs)}</dd>
            </div>
            <div>
              <dt>Pending jobs</dt>
              <dd>${formatNumber(pendingJobs)}</dd>
            </div>
            <div>
              <dt>Cores running</dt>
              <dd>${formatNumber(runningCores)}</dd>
            </div>
            <div>
              <dt>Cores pending</dt>
              <dd>${formatNumber(pendingCores)}</dd>
            </div>
          </dl>
          <div class="usage-progress compact">
            <span>Job mix</span>
            <div class="progress-track progress-split">
              <div class="progress-value is-running" style="width:${jobsRatio}%"></div>
              <div class="progress-value is-pending" style="width:${pendingJobsRatio}%"></div>
            </div>
            <small>${totalJobs ? `${Math.round(jobsRatio)}% running / ${Math.round(100 - jobsRatio)}% pending` : "No jobs"}</small>
          </div>
          <div class="usage-progress compact">
            <span>Core demand</span>
            <div class="progress-track progress-split">
              <div class="progress-value is-running" style="width:${coresRatio}%"></div>
              <div class="progress-value is-pending" style="width:${pendingCoresRatio}%"></div>
            </div>
            <small>${totalCores ? `${Math.round(coresRatio)}% satisfied` : "No demand"}</small>
          </div>
        </article>`;
    })
    .join("");
  elements.queueGrid.innerHTML = cards;
};

const renderNodeTable = (nodes) => {
  if (!elements.nodeBody) return;
  const filtered = sanitizeNodes(nodes);
  if (!filtered.length) {
    setNodePlaceholder("No node inventory was returned for this cluster.");
    return;
  }
  const rows = filtered
    .map((node) => {
      return `
        <tr>
          <td>${node.node_type || "--"}</td>
          <td>${formatNumber(node.nodes_available)}</td>
          <td>${formatNumber(node.cores_per_node)}</td>
          <td>${formatNumber(node.cores_available)}</td>
          <td>${formatNumber(node.cores_running)}</td>
          <td>${formatNumber(node.cores_free)}</td>
        </tr>`;
    })
    .join("");
  elements.nodeBody.innerHTML = rows;
};

const renderGpuTable = (gpus) => {
  if (!elements.nodeBody) return;
  if (!gpus.length) {
    setNodePlaceholder("No GPUs detected on this server.");
    return;
  }
  const rows = gpus
    .map((gpu) => {
      const memFree = (gpu.memory_total_mib || 0) - (gpu.memory_used_mib || 0);
      return `
        <tr>
          <td>${gpu.name || "--"}</td>
          <td>${gpu.index}</td>
          <td>${formatNumber(gpu.memory_total_mib)} MiB</td>
          <td>${formatNumber(memFree)} MiB</td>
          <td>${gpu.utilization_percent || 0}%</td>
          <td>${gpu.temperature_c != null ? gpu.temperature_c + "°C" : "--"}</td>
        </tr>`;
    })
    .join("");
  elements.nodeBody.innerHTML = rows;
};

const renderClusterStats = (cluster) => {
  const queues = parseQueues(cluster);
  const runningJobs = queues.reduce((sum, queue) => sum + toNumber(queue.jobs_running), 0);
  const pendingJobs = queues.reduce((sum, queue) => sum + toNumber(queue.jobs_pending), 0);
  const runningCores = queues.reduce((sum, queue) => sum + toNumber(queue.cores_running), 0);
  const pendingCores = queues.reduce((sum, queue) => sum + toNumber(queue.cores_pending), 0);
  const totalCores = runningCores + pendingCores;
  if (elements.clusterRunningJobs) {
    elements.clusterRunningJobs.textContent = queues.length ? formatNumber(runningJobs) : "--";
  }
  if (elements.clusterPendingJobs) {
    elements.clusterPendingJobs.textContent = queues.length ? formatNumber(pendingJobs) : "--";
  }
  if (elements.clusterRunningCores) {
    elements.clusterRunningCores.textContent = queues.length ? formatNumber(runningCores) : "--";
  }
  if (elements.clusterPendingCores) {
    elements.clusterPendingCores.textContent = queues.length ? formatNumber(pendingCores) : "--";
  }
  if (elements.clusterCoreDonut) {
    if (!queues.length || !totalCores) {
      elements.clusterCoreDonut.innerHTML = '<div class="placeholder">No queue data</div>';
    } else {
      const percent = clampPercent((runningCores / totalCores) * 100);
      elements.clusterCoreDonut.innerHTML = `
        <div class="donut" style="--donut-value:${percent};--donut-primary:var(--success);">
          <strong>${percent.toFixed(1)}%</strong>
          <span>Satisfied</span>
        </div>
        <small>${formatNumber(runningCores)} / ${formatNumber(totalCores)} cores</small>
      `;
    }
  }
};

const clearClusterStats = () => {
  if (elements.clusterRunningJobs) elements.clusterRunningJobs.textContent = "--";
  if (elements.clusterPendingJobs) elements.clusterPendingJobs.textContent = "--";
  if (elements.clusterRunningCores) elements.clusterRunningCores.textContent = "--";
  if (elements.clusterPendingCores) elements.clusterPendingCores.textContent = "--";
  if (elements.clusterCoreDonut) {
    elements.clusterCoreDonut.innerHTML = '<div class="placeholder">Select a cluster</div>';
  }
};

const renderClusterDetail = () => {
  if (!state.clusters.length) {
    if (elements.clusterTitle) elements.clusterTitle.textContent = "No data available";
    if (elements.clusterMeta) elements.clusterMeta.textContent = "";
    if (elements.clusterNote) elements.clusterNote.textContent = "";
    showGeneratingPlaceholder("Waiting for cluster monitor data…");
    return;
  }

  const safeIndex = Math.min(state.selectedIndex, state.clusters.length - 1);
  state.selectedIndex = safeIndex;
  const cluster = state.clusters[safeIndex];
  const metadata = cluster?.cluster_metadata || {};
  const queues = parseQueues(cluster);
  const rawNodes = parseNodes(cluster);
  const nodes = sanitizeNodes(rawNodes);
  const gpus = parseGpus(cluster);
  const sysInfo = getSystemInfo(cluster);
  const isGpuCluster = !hasScheduler(cluster) && gpus.length > 0;
  const displayName = metadata.name || metadata.uri || `Cluster ${safeIndex + 1}`;

  if (elements.clusterTitle) {
    elements.clusterTitle.textContent = displayName;
  }
  if (elements.clusterMeta) {
    const parts = [];
    if (metadata.status) parts.push(String(metadata.status).toUpperCase());
    if (isGpuCluster) parts.push("GPU Server");
    else if (metadata.type) parts.push(metadata.type);
    if (sysInfo.hostname && sysInfo.hostname !== "unknown") parts.push(sysInfo.hostname);
    if (metadata.timestamp) parts.push(new Date(metadata.timestamp).toLocaleString());
    elements.clusterMeta.textContent = parts.join(" • ");
  }
  if (elements.clusterNote) {
    elements.clusterNote.textContent = metadata.timestamp
      ? `Data refreshed ${new Date(metadata.timestamp).toLocaleString()}.`
      : "Timestamp unavailable.";
  }

  const isSystemOnly = !hasScheduler(cluster) && gpus.length === 0 && sysInfo.cpu_count;

  if (isGpuCluster) {
    // GPU cluster display
    if (elements.queueDepthMeta) {
      elements.queueDepthMeta.textContent = `${gpus.length} GPUs`;
    }
    if (elements.nodeMeta) {
      elements.nodeMeta.textContent = `${sysInfo.cpu_count || 0} CPUs`;
    }
    renderGpuClusterStats(cluster);
    renderGpuQueueGrid(gpus, sysInfo);
    renderGpuTable(gpus);
  } else if (isSystemOnly) {
    // System-only cluster (no scheduler, no GPUs)
    if (elements.queueDepthMeta) {
      elements.queueDepthMeta.textContent = "No queues";
    }
    if (elements.nodeMeta) {
      elements.nodeMeta.textContent = `${sysInfo.cpu_count || 0} CPUs`;
    }
    renderSystemOnlyStats(cluster);
    renderSystemOnlyGrid(sysInfo);
    renderSystemOnlyTable(sysInfo);
  } else {
    // HPC cluster display
    if (elements.queueDepthMeta) {
      elements.queueDepthMeta.textContent = `${queues.length} queues`;
    }
    if (elements.nodeMeta) {
      elements.nodeMeta.textContent = `${nodes.length} node classes`;
    }
    renderClusterStats(cluster);
    renderQueueGrid(queues);
    renderNodeTable(nodes);
  }
};

const renderGpuClusterStats = (cluster) => {
  const gpus = parseGpus(cluster);
  const sysInfo = getSystemInfo(cluster);
  const gpuSummary = cluster?.gpu_data?.summary || {};

  if (elements.clusterRunningJobs) {
    elements.clusterRunningJobs.textContent = gpuSummary.gpu_count || 0;
  }
  if (elements.clusterPendingJobs) {
    elements.clusterPendingJobs.textContent = `${gpuSummary.avg_utilization_percent || 0}%`;
  }
  if (elements.clusterRunningCores) {
    elements.clusterRunningCores.textContent = formatNumber(gpuSummary.used_memory_mib || 0);
  }
  if (elements.clusterPendingCores) {
    elements.clusterPendingCores.textContent = formatNumber(gpuSummary.free_memory_mib || 0);
  }
  if (elements.clusterCoreDonut) {
    const totalMem = gpuSummary.total_memory_mib || 0;
    const freeMem = gpuSummary.free_memory_mib || 0;
    if (!totalMem) {
      elements.clusterCoreDonut.innerHTML = '<div class="placeholder">No GPU data</div>';
    } else {
      const percent = clampPercent((freeMem / totalMem) * 100);
      elements.clusterCoreDonut.innerHTML = `
        <div class="donut" style="--donut-value:${percent};--donut-primary:var(--success);">
          <strong>${percent.toFixed(1)}%</strong>
          <span>Free</span>
        </div>
        <small>${formatNumber(freeMem)} / ${formatNumber(totalMem)} MiB</small>
      `;
    }
  }
};

const renderSystemOnlyStats = (cluster) => {
  const sysInfo = getSystemInfo(cluster);
  const memTotal = sysInfo.memory_total_mb || 0;
  const memUsed = sysInfo.memory_used_mb || 0;
  const memFree = memTotal - memUsed;

  if (elements.clusterRunningJobs) {
    elements.clusterRunningJobs.textContent = sysInfo.cpu_count || 0;
  }
  if (elements.clusterPendingJobs) {
    elements.clusterPendingJobs.textContent = sysInfo.load_1m?.toFixed(2) || "0";
  }
  if (elements.clusterRunningCores) {
    elements.clusterRunningCores.textContent = formatNumber(memUsed);
  }
  if (elements.clusterPendingCores) {
    elements.clusterPendingCores.textContent = formatNumber(memFree);
  }
  if (elements.clusterCoreDonut) {
    if (!memTotal) {
      elements.clusterCoreDonut.innerHTML = '<div class="placeholder">No memory data</div>';
    } else {
      const percent = clampPercent((memFree / memTotal) * 100);
      elements.clusterCoreDonut.innerHTML = `
        <div class="donut" style="--donut-value:${percent};--donut-primary:var(--success);">
          <strong>${percent.toFixed(1)}%</strong>
          <span>Free</span>
        </div>
        <small>${formatNumber(memFree)} / ${formatNumber(memTotal)} MB</small>
      `;
    }
  }
};

const renderSystemOnlyGrid = (sysInfo) => {
  if (!elements.queueGrid) return;
  const memTotal = sysInfo.memory_total_mb || 0;
  const memUsed = sysInfo.memory_used_mb || 0;
  const memFree = memTotal - memUsed;
  const memPercent = memTotal ? clampPercent((memUsed / memTotal) * 100) : 0;

  const card = `
    <article class="queue-card">
      <header class="queue-card-head">
        <h4>System Resources</h4>
        <span class="badge">${sysInfo.hostname || "Server"}</span>
      </header>
      <dl class="queue-card-metrics">
        <div>
          <dt>CPUs</dt>
          <dd>${sysInfo.cpu_count || 0}</dd>
        </div>
        <div>
          <dt>Total RAM</dt>
          <dd>${formatNumber(memTotal)} MB</dd>
        </div>
        <div>
          <dt>Free RAM</dt>
          <dd>${formatNumber(memFree)} MB</dd>
        </div>
        <div>
          <dt>Load (1m)</dt>
          <dd>${sysInfo.load_1m?.toFixed(2) || "0"}</dd>
        </div>
      </dl>
      <div class="usage-progress compact">
        <span>Memory Usage</span>
        <div class="progress-track progress-split">
          <div class="progress-value is-running" style="width:${memPercent}%"></div>
        </div>
        <small>${memPercent.toFixed(1)}% used</small>
      </div>
      <div class="usage-progress compact">
        <span>Load Average</span>
        <div class="progress-track progress-split">
          <div class="progress-value is-running" style="width:${clampPercent((sysInfo.load_1m / (sysInfo.cpu_count || 1)) * 100)}%"></div>
        </div>
        <small>${sysInfo.load_1m?.toFixed(2) || "0"} / ${sysInfo.load_5m?.toFixed(2) || "0"} / ${sysInfo.load_15m?.toFixed(2) || "0"}</small>
      </div>
    </article>`;
  elements.queueGrid.innerHTML = card;
};

const renderSystemOnlyTable = (sysInfo) => {
  if (!elements.nodeBody) return;
  const rows = `
    <tr>
      <td>CPU</td>
      <td>${sysInfo.cpu_count || 0}</td>
      <td>--</td>
      <td>--</td>
      <td>--</td>
      <td>--</td>
    </tr>
    <tr>
      <td>Memory</td>
      <td>1</td>
      <td>${formatNumber(sysInfo.memory_total_mb || 0)} MB</td>
      <td>${formatNumber(sysInfo.memory_total_mb || 0)} MB</td>
      <td>${formatNumber(sysInfo.memory_used_mb || 0)} MB</td>
      <td>${formatNumber((sysInfo.memory_total_mb || 0) - (sysInfo.memory_used_mb || 0))} MB</td>
    </tr>
  `;
  elements.nodeBody.innerHTML = rows;
};

const renderGpuQueueGrid = (gpus, sysInfo) => {
  if (!elements.queueGrid) return;
  if (!gpus.length) {
    setQueueGridPlaceholder("No GPUs detected on this server.");
    return;
  }
  const cards = gpus.map((gpu) => {
    const memTotal = gpu.memory_total_mib || 0;
    const memUsed = gpu.memory_used_mib || 0;
    const memFree = memTotal - memUsed;
    const memPercent = memTotal ? clampPercent((memUsed / memTotal) * 100) : 0;
    const utilPercent = gpu.utilization_percent || 0;
    return `
      <article class="queue-card">
        <header class="queue-card-head">
          <h4>GPU ${gpu.index}: ${gpu.name || "Unknown"}</h4>
          <span class="badge">${gpu.temperature_c != null ? gpu.temperature_c + "°C" : "--"}</span>
        </header>
        <dl class="queue-card-metrics">
          <div>
            <dt>Total Memory</dt>
            <dd>${formatNumber(memTotal)} MiB</dd>
          </div>
          <div>
            <dt>Free Memory</dt>
            <dd>${formatNumber(memFree)} MiB</dd>
          </div>
          <div>
            <dt>GPU Util</dt>
            <dd>${utilPercent}%</dd>
          </div>
          <div>
            <dt>Mem Used</dt>
            <dd>${formatNumber(memUsed)} MiB</dd>
          </div>
        </dl>
        <div class="usage-progress compact">
          <span>GPU Utilization</span>
          <div class="progress-track progress-split">
            <div class="progress-value is-running" style="width:${utilPercent}%"></div>
          </div>
          <small>${utilPercent}% utilized</small>
        </div>
        <div class="usage-progress compact">
          <span>Memory Usage</span>
          <div class="progress-track progress-split">
            <div class="progress-value is-running" style="width:${memPercent}%"></div>
          </div>
          <small>${memPercent.toFixed(1)}% used</small>
        </div>
      </article>`;
  }).join("");
  elements.queueGrid.innerHTML = cards;
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
  setStatus(silent ? "" : "Refreshing queue data…", "info");
  const previousIdentifier = state.clusters.length
    ? getClusterIdentifier(state.clusters[state.selectedIndex])
    : null;
  try {
    const response = await fetch(`${DATA_URL}?t=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const payload = await response.json();
    // Handle both array format and {clusters: [...]} format
    if (Array.isArray(payload)) {
      state.clusters = payload;
    } else if (payload && Array.isArray(payload.clusters)) {
      state.clusters = payload.clusters;
    } else {
      state.clusters = [];
    }
    state.lastUpdated = Date.now();
    if (previousIdentifier) {
      const idx = state.clusters.findIndex(
        (cluster) => getClusterIdentifier(cluster) === previousIdentifier
      );
      state.selectedIndex = idx >= 0 ? idx : 0;
    } else {
      state.selectedIndex = 0;
    }
    renderSummary();
    renderClusterOptions();
    renderClusterDetail();
    setStatus(silent ? "" : "Queue data updated just now.");
    if (state.clusters.length) {
      clearRetry();
    } else {
      scheduleRetry();
    }
  } catch (err) {
    console.error("Unable to load queue data", err);
    setStatus(`Unable to load queue data (${err.message}).`, "error");
    if (!hadData) {
      showGeneratingPlaceholder("Still gathering queue metrics…");
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
  document.title = `Queue Health | ${title}`;
};

const bootstrap = () => {
  cacheElements();
  initThemeToggle();
  applyConfigBranding();
  const nav = document.querySelector("[data-cluster-nav]");
  if (!state.features.clusterPages) {
    if (nav) nav.remove();
    setStatus("Cluster pages are disabled on this server.", "error");
    disableRefresh(true);
    setQueueGridPlaceholder("Cluster pages disabled.");
    setNodePlaceholder("Cluster pages disabled.");
    clearClusterStats();
    clearRetry();
    return;
  }
  bindEvents();
  loadData();
};

document.addEventListener("DOMContentLoaded", bootstrap);
