import { buildDataUrl, clampPercent, clusterPagesEnabled, initThemeToggle, buildApiUrl, initHelpPanel, initBrand, formatRelativeTime, initQuickTips } from "./page-utils.js";

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

/** Node types that indicate GPU/accelerator hardware */
const GPU_NODE_PATTERN = /^(gpu|mla|ai[\/. ]?ml|viz)/i;

// HPCMP rows label their node class as "GPU"/"MLA"/"Viz", so a name match
// is sufficient. NOAA's Slurm pipeline uses the partition name as node_type
// (e.g. "u1-gh"), so we also need to honour explicit GPU counts when the
// collector exposes them.
const isGpuNode = (node) => {
  if (!node) return false;
  if (toNumber(node.gpus_per_node) > 0) return true;
  if (toNumber(node.gpus_available) > 0) return true;
  if (String(node.gpu_types || "").trim()) return true;
  return GPU_NODE_PATTERN.test(String(node.node_type || "").trim());
};

const parseGpuTypesFromRow = (node) =>
  String(node?.gpu_types || "")
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);

const formatGpuTypeList = (typesSet) =>
  typesSet && typesSet.size ? [...typesSet].sort().join(", ") : "";

const computeFleetSummary = (clusters) => {
  const totals = {
    clusters: clusters.length,
    queues: 0,
    runningJobs: 0,
    pendingJobs: 0,
    runningCores: 0,
    pendingCores: 0,
    availableCores: 0,
    // Accelerator nodes (derived from node inventory)
    accelNodes: 0,
    accelCoresAvail: 0,
    accelCoresRunning: 0,
    accelCoresFree: 0,
    accelClusters: 0,
    accelGpusTotal: 0,
    accelGpuTypes: new Set(),
  };

  clusters.forEach((cluster) => {
    parseQueues(cluster).forEach((queue) => {
      totals.queues += 1;
      totals.runningJobs += toNumber(queue.jobs_running);
      totals.pendingJobs += toNumber(queue.jobs_pending);
      totals.pendingCores += toNumber(queue.cores_pending);
    });
    // Authoritative cluster-wide capacity from the collector — each physical
    // node counted once even when it sits in overlapping partitions. Falls
    // back to summing partition rows when the field is absent (older
    // HPCMP show_queues path or clusters without cluster_totals yet).
    const totals_for_cluster = cluster?.queue_data?.cluster_totals;
    if (totals_for_cluster) {
      totals.availableCores += toNumber(totals_for_cluster.cores_total);
      totals.runningCores += toNumber(totals_for_cluster.cores_running);
    } else {
      parseQueues(cluster).forEach((queue) => {
        totals.runningCores += toNumber(queue.cores_running);
      });
      parseNodes(cluster).forEach((node) => {
        totals.availableCores += toNumber(node.cores_available);
      });
    }
    let clusterHasAccel = false;
    let clusterGpusFromNodes = 0;
    parseNodes(cluster).forEach((node) => {
      const coresAvail = toNumber(node.cores_available);
      if (isGpuNode(node)) {
        clusterHasAccel = true;
        totals.accelNodes += toNumber(node.nodes_available);
        totals.accelCoresAvail += coresAvail;
        totals.accelCoresRunning += toNumber(node.cores_running);
        totals.accelCoresFree += toNumber(node.cores_free);
        clusterGpusFromNodes += toNumber(node.gpus_available);
        parseGpuTypesFromRow(node).forEach((t) => totals.accelGpuTypes.add(t));
      }
    });
    if (clusterHasAccel) {
      totals.accelClusters += 1;
      // Prefer cluster_totals.gpus_total when present (deduped across
      // overlapping partitions). Sum per-partition rows otherwise — that
      // overcounts on overlap, but it's the only signal we have for
      // legacy/HPCMP-shaped payloads.
      const gpusFromTotals = toNumber(totals_for_cluster?.gpus_total);
      totals.accelGpusTotal += gpusFromTotals > 0 ? gpusFromTotals : clusterGpusFromNodes;
    }
  });

  // Clamp utilization to 100% in case very stale data sneaks in.
  const rawUtil = totals.availableCores
    ? (totals.runningCores / totals.availableCores) * 100
    : 0;
  const utilization = Math.min(rawUtil, 100);

  return { ...totals, utilization };
};

/**
 * Bucket every queue into one of three mutually-exclusive states based on
 * pending÷running pressure. Heavy backlog = pending exceeds running cores by
 * 2× or more (avoid scheduling here). Light backlog = some pending but less
 * than 2× running. Open = no backlog (idle or running with nothing waiting).
 */
const HEAVY_BACKLOG_RATIO = 2;
const aggregateQueueSnapshot = (clusters) => {
  const snapshot = { heavy: 0, light: 0, open: 0 };
  clusters.forEach((cluster) => {
    parseQueues(cluster).forEach((queue) => {
      const running = toNumber(queue.cores_running);
      const pending = toNumber(queue.cores_pending);
      if (pending <= 0) {
        snapshot.open += 1;
      } else if (running === 0 || pending / running >= HEAVY_BACKLOG_RATIO) {
        snapshot.heavy += 1;
      } else {
        snapshot.light += 1;
      }
    });
  });
  return snapshot;
};

/**
 * Sum capacity across node inventory. Used cores/nodes come from the running
 * column of the node table — this is what's currently allocated, regardless
 * of which queue the jobs are in. Free is what's actually schedulable.
 */
const computeClusterCapacity = (cluster) => {
  const nodes = sanitizeNodes(parseNodes(cluster));
  const clusterTotals = cluster?.queue_data?.cluster_totals;
  const totals = {
    coresTotal: 0,
    coresUsed: 0,
    coresFree: 0,
    nodesTotal: 0,
    accelNodesTotal: 0,
    accelCoresTotal: 0,
    accelCoresUsed: 0,
    accelGpusTotal: 0,
    accelGpuTypes: new Set(),
    nodeClasses: nodes.length,
  };
  // Prefer cluster_totals when present — these already de-duplicate
  // nodes across overlapping partitions.
  if (clusterTotals) {
    totals.coresTotal = toNumber(clusterTotals.cores_total);
    totals.coresUsed = toNumber(clusterTotals.cores_running);
    totals.coresFree = toNumber(clusterTotals.cores_free);
    totals.nodesTotal = toNumber(clusterTotals.nodes_total);
  }
  nodes.forEach((node) => {
    const total = toNumber(node.cores_available);
    const used = toNumber(node.cores_running);
    const free = toNumber(node.cores_free);
    const nodeCount = toNumber(node.nodes_available);
    if (!clusterTotals) {
      totals.coresTotal += total;
      totals.coresUsed += used;
      totals.coresFree += free || Math.max(total - used, 0);
      totals.nodesTotal += nodeCount;
    }
    if (isGpuNode(node)) {
      totals.accelNodesTotal += nodeCount;
      totals.accelCoresTotal += total;
      totals.accelCoresUsed += used;
      totals.accelGpusTotal += toNumber(node.gpus_available);
      parseGpuTypesFromRow(node).forEach((t) => totals.accelGpuTypes.add(t));
    }
  });
  // Prefer the cluster-wide deduped GPU count when the collector ships one.
  const gpusFromTotals = toNumber(clusterTotals?.gpus_total);
  if (gpusFromTotals > 0) {
    totals.accelGpusTotal = gpusFromTotals;
  }
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

const formatProgressMessage = (progress, isFirstSweep) => {
  if (!progress || !progress.total) {
    return isFirstSweep
      ? "First-time setup: collecting queue data from your clusters…"
      : "Refreshing queue data…";
  }
  const { collected = 0, total = 0, current_cluster: current } = progress;
  const lead = isFirstSweep ? "Collecting queue data" : "Refreshing queue data";
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

const showGeneratingPlaceholder = (message = "Cluster monitor is generating queue data…") => {
  setQueueGridPlaceholder(message);
  setNodePlaceholder(message);
  clearClusterStats();
  if (elements.queueDepthMeta) elements.queueDepthMeta.textContent = "";
  if (elements.nodeMeta) elements.nodeMeta.textContent = "";
  if (elements.capacityStrip) {
    elements.capacityStrip.innerHTML = `<div class="placeholder">${message}</div>`;
  }
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
  elements.nodeBody.innerHTML = `<tr><td colspan="7" class="placeholder">${message}</td></tr>`;
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
  elements.fleetGpuDonut = getElement("fleet-gpu-donut");
  elements.fleetQueueTags = getElement("fleet-queue-tags");
  elements.capacityStrip = getElement("cluster-capacity-strip");
  elements.availabilityRanking = getElement("availability-ranking");
};

const bindEvents = () => {
  if (elements.refreshBtn) {
    elements.refreshBtn.addEventListener("click", () => loadData({ silent: false }));
  }
  if (elements.clusterSelect) {
    elements.clusterSelect.addEventListener("click", (event) => {
      const btn = event.target.closest(".cluster-picker-btn");
      if (!btn) return;
      state.selectedIndex = Number(btn.dataset.index) || 0;
      // Update aria-selected on all buttons
      elements.clusterSelect.querySelectorAll(".cluster-picker-btn").forEach((b) => {
        b.setAttribute("aria-selected", b === btn ? "true" : "false");
      });
      renderClusterDetail();
      renderAvailabilityRanking();
    });
  }
  if (elements.availabilityRanking) {
    elements.availabilityRanking.addEventListener("click", (event) => {
      const row = event.target.closest("[data-cluster-index]");
      if (!row) return;
      const idx = Number(row.dataset.clusterIndex);
      if (!Number.isFinite(idx)) return;
      state.selectedIndex = idx;
      renderClusterOptions();
      renderClusterDetail();
      renderAvailabilityRanking();
      // Scroll to the cluster picker (not the cluster title below it) so
      // the quick-select buttons stay visible at the top of the viewport
      // — the user can jump to a different cluster without scrolling back.
      const target = elements.clusterSelect;
      if (target && typeof target.scrollIntoView === "function") {
        target.scrollIntoView({ behavior: "smooth", block: "start" });
      }
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

const renderFleetGpuDonut = (summary) => {
  if (!elements.fleetGpuDonut) return;
  if (!summary.accelNodes) {
    elements.fleetGpuDonut.innerHTML = '<div class="placeholder">No accelerator nodes detected.</div>';
    return;
  }
  const utilPercent = summary.accelCoresAvail
    ? clampPercent((summary.accelCoresRunning / summary.accelCoresAvail) * 100)
    : 0;
  // GPU count is only present from collectors that parse Slurm Gres (NOAA).
  // HPCMP rows expose nodes/cores only — fall back to the original wording.
  const gpus = toNumber(summary.accelGpusTotal);
  const types = formatGpuTypeList(summary.accelGpuTypes);
  const subtitle = gpus > 0
    ? `${formatNumber(gpus)} GPUs${types ? ` (${types})` : ""} &middot; ${formatNumber(summary.accelNodes)} nodes`
    : `${formatNumber(summary.accelNodes)} nodes &middot; ${formatNumber(summary.accelCoresFree)} of ${formatNumber(summary.accelCoresAvail)} cores free`;
  elements.fleetGpuDonut.innerHTML = `
    <div class="donut" style="--donut-value:${utilPercent};--donut-primary:var(--info);" title="Estimated from CPU load on accelerator nodes">
      <strong>${utilPercent.toFixed(0)}%</strong>
      <span>In use</span>
    </div>
    <small>${subtitle}</small>
  `;
};

/**
 * Build a "most available" ranking across the fleet. Each entry combines
 * core capacity (from node inventory) with backlog state (from queue cores)
 * so the row can show both how full a cluster is now AND whether there's
 * pending demand queued behind that.
 *
 * Sort by utilization% ascending — "most idle first" — which is what users
 * want when picking a cluster for a new job. Ties broken by absolute free
 * cores so a 50%-free 1M cluster outranks a 50%-free 1k cluster.
 *
 * Clusters with no node inventory (e.g. GPU-only servers) are returned in
 * `unranked` so the renderer can list them separately rather than scoring
 * them as "0% utilized" and shoving them to the top.
 */
const computeAvailabilityRanking = (clusters) => {
  const ranked = [];
  const unranked = [];
  clusters.forEach((cluster, index) => {
    const metadata = cluster?.cluster_metadata || {};
    const name = metadata.name || metadata.uri || `Cluster ${index + 1}`;
    const capacity = computeClusterCapacity(cluster);
    const queues = parseQueues(cluster);
    const pendingCores = queues.reduce((sum, q) => sum + toNumber(q.cores_pending), 0);
    const runningCoresFromQueues = queues.reduce((sum, q) => sum + toNumber(q.cores_running), 0);

    if (capacity.coresTotal <= 0) {
      unranked.push({ index, name, reason: "No capacity inventory reported." });
      return;
    }

    // Use queue-derived running cores when present (more accurate than node
    // table aggregation for clusters where the scheduler reports per-queue),
    // otherwise fall back to node inventory's cores_running.
    const runningCores = runningCoresFromQueues || capacity.coresUsed;
    const freeCores = Math.max(capacity.coresTotal - runningCores, 0);
    const utilization = clampPercent((runningCores / capacity.coresTotal) * 100);
    const pendingShare = clampPercent((pendingCores / capacity.coresTotal) * 100);
    let backlog;
    if (pendingCores <= 0) {
      backlog = { tier: "open", label: "Open" };
    } else if (runningCores === 0 || pendingCores / runningCores >= HEAVY_BACKLOG_RATIO) {
      backlog = { tier: "heavy", label: "Heavy backlog" };
    } else {
      backlog = { tier: "light", label: "Light backlog" };
    }

    ranked.push({
      index,
      name,
      coresTotal: capacity.coresTotal,
      runningCores,
      pendingCores,
      freeCores,
      utilization,
      pendingShare,
      backlog,
      status: metadata.status || "",
    });
  });
  ranked.sort((a, b) => {
    if (a.utilization !== b.utilization) return a.utilization - b.utilization;
    return b.freeCores - a.freeCores;
  });
  return { ranked, unranked };
};

const renderAvailabilityRanking = () => {
  if (!elements.availabilityRanking) return;
  const { ranked, unranked } = computeAvailabilityRanking(state.clusters);
  if (!ranked.length && !unranked.length) {
    elements.availabilityRanking.innerHTML = '<div class="placeholder">Awaiting capacity data…</div>';
    return;
  }
  if (!ranked.length) {
    elements.availabilityRanking.innerHTML = '<div class="placeholder">No clusters with node inventory yet — check back after the next collection.</div>';
    return;
  }

  const rows = ranked
    .map((entry, idx) => {
      // Running width is honest (running ÷ capacity). Pending fills only
      // the remaining free space — overflow ("180% pending vs capacity")
      // is signalled by the Heavy backlog badge instead of distorting the
      // bar geometry.
      const runningWidth = clampPercent((entry.runningCores / entry.coresTotal) * 100);
      const remaining = Math.max(0, 100 - runningWidth);
      const pendingDesired = clampPercent((entry.pendingCores / entry.coresTotal) * 100);
      const pendingWidth = Math.min(pendingDesired, remaining);
      const pendingTitle = entry.pendingCores
        ? `${formatNumber(entry.pendingCores)} cores waiting`
        : "No pending demand";
      const backlogChip = `<span class="backlog-badge is-${entry.backlog.tier}" title="${pendingTitle}">${entry.backlog.label}</span>`;
      const isSelected = entry.index === state.selectedIndex;
      return `
        <button type="button"
                class="availability-row${isSelected ? ' is-selected' : ''}"
                data-cluster-index="${entry.index}"
                aria-label="Select ${entry.name}">
          <span class="availability-rank">${idx + 1}</span>
          <span class="availability-name">${entry.name}</span>
          <span class="availability-bar" title="${formatNumber(entry.runningCores)} running · ${formatNumber(entry.pendingCores)} waiting · ${formatNumber(entry.freeCores)} free">
            <span class="progress-track progress-split availability-track">
              <span class="progress-value is-running" style="width:${runningWidth}%"></span>
              <span class="progress-value is-pending" style="width:${pendingWidth}%"></span>
            </span>
          </span>
          <span class="availability-free"><strong>${formatNumber(entry.freeCores)}</strong><small>cores free</small></span>
          <span class="availability-util">${entry.utilization.toFixed(0)}% used</span>
          ${backlogChip}
        </button>
      `;
    })
    .join("");

  const unrankedHtml = unranked.length
    ? `<p class="availability-note muted-text">${unranked.length} cluster${unranked.length === 1 ? "" : "s"} not ranked (no node inventory): ${unranked.map((u) => u.name).join(", ")}</p>`
    : "";

  elements.availabilityRanking.innerHTML = `
    <div class="availability-list" role="list">${rows}</div>
    ${unrankedHtml}
  `;
};

const renderFleetQueueTags = (snapshot) => {
  if (!elements.fleetQueueTags) return;
  const total = snapshot.heavy + snapshot.light + snapshot.open;
  if (!total) {
    elements.fleetQueueTags.textContent = "No queue data yet.";
    return;
  }
  elements.fleetQueueTags.innerHTML = `
    <span class="queue-chip is-heavy" title="Pending cores ≥ 2× running cores. New jobs likely face long waits.">Heavy backlog <small>${snapshot.heavy}</small></span>
    <span class="queue-chip is-light" title="Some jobs waiting but less than 2× the running load.">Light backlog <small>${snapshot.light}</small></span>
    <span class="queue-chip is-open" title="No pending jobs — running freely or idle.">Open <small>${snapshot.open}</small></span>
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
  renderFleetGpuDonut(summary);
  renderFleetQueueTags(aggregateQueueSnapshot(state.clusters));
  renderAvailabilityRanking();
};

const renderClusterOptions = () => {
  if (!elements.clusterSelect) return;
  if (!state.clusters.length) {
    elements.clusterSelect.innerHTML = '<span class="muted-text">No clusters available</span>';
    return;
  }
  // Order picker buttons to match the availability ranking — most-idle
  // clusters first, then any unranked (no node inventory) appended at the
  // end. data-index keeps the click handler decoupled from display order.
  const { ranked, unranked } = computeAvailabilityRanking(state.clusters);
  const rankedIndices = ranked.map((entry) => entry.index);
  const unrankedIndices = unranked.map((entry) => entry.index);
  const seen = new Set([...rankedIndices, ...unrankedIndices]);
  // Defensive fallback: if any cluster wasn't classified (shouldn't happen
  // but guards against drift), append it so the picker stays complete.
  const fallback = state.clusters
    .map((_, idx) => idx)
    .filter((idx) => !seen.has(idx));
  const order = [...rankedIndices, ...unrankedIndices, ...fallback];

  elements.clusterSelect.innerHTML = order
    .map((idx) => {
      const cluster = state.clusters[idx];
      const name =
        cluster?.cluster_metadata?.name || cluster?.cluster_metadata?.uri || `Cluster ${idx + 1}`;
      const selected = idx === state.selectedIndex;
      return `<button type="button" class="cluster-picker-btn" role="tab"
        data-index="${idx}" aria-selected="${selected}">
        <span class="picker-status"></span>${name}
      </button>`;
    })
    .join("");
};

/**
 * Classify a queue's current pressure for display. Same buckets as the fleet
 * snapshot: heavy / light / open. See HEAVY_BACKLOG_RATIO.
 */
const classifyQueueBacklog = (runningCores, pendingCores) => {
  if (pendingCores <= 0) {
    return { tier: "open", label: "Open", detail: "No backlog" };
  }
  if (runningCores === 0 || pendingCores / runningCores >= HEAVY_BACKLOG_RATIO) {
    const ratio = runningCores ? pendingCores / runningCores : null;
    return {
      tier: "heavy",
      label: "Heavy backlog",
      detail: ratio ? `${ratio.toFixed(1)}× pending vs. running` : "All demand pending",
    };
  }
  return {
    tier: "light",
    label: "Light backlog",
    detail: `${(pendingCores / runningCores).toFixed(1)}× pending vs. running`,
  };
};

const renderQueueGrid = (queues, clusterCoresTotal = 0) => {
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
      const backlog = classifyQueueBacklog(runningCores, pendingCores);
      // Bar widths are scaled against the cluster's total core capacity when
      // available, so the visual fill represents the queue's footprint on the
      // system rather than just its share of demand. Falls back to local
      // running+pending so single-queue clusters still render a meaningful bar.
      const denom = clusterCoresTotal > 0
        ? clusterCoresTotal
        : Math.max(runningCores + pendingCores, 1);
      const runningWidth = clampPercent((runningCores / denom) * 100);
      const pendingWidth = clampPercent((pendingCores / denom) * 100);
      const footprintLabel = clusterCoresTotal > 0
        ? `${formatNumber(runningCores + pendingCores)} of ${formatNumber(clusterCoresTotal)} cluster cores`
        : `${formatNumber(runningCores + pendingCores)} cores in flight`;
      return `
        <article class="queue-card" data-backlog="${backlog.tier}">
          <header class="queue-card-head">
            <h4>${queue.queue_name || "Queue"}</h4>
            <span class="badge backlog-badge is-${backlog.tier}" title="${backlog.detail}">${backlog.label}</span>
          </header>
          <dl class="queue-card-metrics">
            <div>
              <dt>Jobs running</dt>
              <dd>${formatNumber(runningJobs)}</dd>
            </div>
            <div>
              <dt>Jobs waiting</dt>
              <dd>${formatNumber(pendingJobs)}</dd>
            </div>
            <div>
              <dt>Cores in use</dt>
              <dd>${formatNumber(runningCores)}</dd>
            </div>
            <div>
              <dt>Cores waiting</dt>
              <dd>${formatNumber(pendingCores)}</dd>
            </div>
          </dl>
          <div class="usage-progress compact">
            <span>Cluster footprint</span>
            <div class="progress-track progress-split">
              <div class="progress-value is-running" style="width:${runningWidth}%" title="${formatNumber(runningCores)} cores running"></div>
              <div class="progress-value is-pending" style="width:${pendingWidth}%" title="${formatNumber(pendingCores)} cores waiting"></div>
            </div>
            <small>${footprintLabel}</small>
          </div>
          <p class="queue-backlog-note muted-text">${backlog.detail}</p>
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
      const gpusTotal = toNumber(node.gpus_available);
      const gpusPerNode = toNumber(node.gpus_per_node);
      const gpuTypes = (node.gpu_types || "").trim();
      let gpuCell = '<span class="muted-text">—</span>';
      if (gpusTotal > 0) {
        const tip = [
          gpuTypes ? `Type${gpuTypes.includes(",") ? "s" : ""}: ${gpuTypes}` : "",
          gpusPerNode ? `${gpusPerNode}/node` : "",
        ]
          .filter(Boolean)
          .join(" · ");
        gpuCell = `<span title="${tip}">${formatNumber(gpusTotal)}${
          gpusPerNode ? ` <small class="muted-text">(${gpusPerNode}/node)</small>` : ""
        }</span>`;
      }
      return `
        <tr>
          <td>${node.node_type || "--"}</td>
          <td>${formatNumber(node.nodes_available)}</td>
          <td>${formatNumber(node.cores_per_node)}</td>
          <td>${formatNumber(node.cores_available)}</td>
          <td>${formatNumber(node.cores_running)}</td>
          <td>${formatNumber(node.cores_free)}</td>
          <td>${gpuCell}</td>
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
  const capacity = computeClusterCapacity(cluster);
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
    // Donut now anchors to true cluster capacity (sum of cores_available from
    // node inventory). Falls back to running+pending only when node inventory
    // is missing — the old behavior — so ssh-only collectors still display.
    const denom = capacity.coresTotal > 0 ? capacity.coresTotal : runningCores + pendingCores;
    const used = capacity.coresTotal > 0 ? capacity.coresUsed : runningCores;
    const free = capacity.coresTotal > 0
      ? Math.max(capacity.coresFree, capacity.coresTotal - capacity.coresUsed)
      : Math.max(denom - used, 0);
    if (!denom) {
      elements.clusterCoreDonut.innerHTML = '<div class="placeholder">No capacity data</div>';
    } else {
      const percent = clampPercent((used / denom) * 100);
      const subtitle = capacity.coresTotal > 0
        ? `${formatNumber(used)} of ${formatNumber(denom)} cores in use · ${formatNumber(free)} free`
        : `${formatNumber(used)} of ${formatNumber(denom)} cores running`;
      elements.clusterCoreDonut.innerHTML = `
        <div class="donut" style="--donut-value:${percent};--donut-primary:var(--accent);" title="Running cores ÷ total cluster capacity">
          <strong>${percent.toFixed(1)}%</strong>
          <span>In use</span>
        </div>
        <small>${subtitle}</small>
      `;
    }
  }
};

/**
 * Render the "Capacity at a glance" strip — three tiles answering the
 * scheduler's question "where can I land a job right now?". Cores tile
 * uses node inventory totals; nodes tile shows free vs total node count;
 * GPUs tile only renders when accelerator nodes exist.
 */
const renderCapacityStrip = (cluster) => {
  if (!elements.capacityStrip) return;
  const capacity = computeClusterCapacity(cluster);
  if (!capacity.coresTotal && !capacity.nodesTotal && !capacity.accelNodesTotal) {
    elements.capacityStrip.innerHTML = '<div class="placeholder">No capacity inventory reported.</div>';
    return;
  }
  const corePct = capacity.coresTotal
    ? clampPercent((capacity.coresUsed / capacity.coresTotal) * 100)
    : 0;
  const nodesUsedEstimate = capacity.coresTotal
    ? Math.round(capacity.nodesTotal * (capacity.coresUsed / capacity.coresTotal))
    : 0;
  const nodesFreeEstimate = Math.max(capacity.nodesTotal - nodesUsedEstimate, 0);
  const accelPct = capacity.accelCoresTotal
    ? clampPercent((capacity.accelCoresUsed / capacity.accelCoresTotal) * 100)
    : 0;
  const accelFreeCores = Math.max(capacity.accelCoresTotal - capacity.accelCoresUsed, 0);

  const tiles = [];
  tiles.push(`
    <div class="capacity-tile">
      <p class="eyebrow">Cores</p>
      <strong>${formatNumber(capacity.coresFree)} free</strong>
      <small>${formatNumber(capacity.coresUsed)} in use of ${formatNumber(capacity.coresTotal)} total</small>
      <div class="progress-track"><div class="progress-value is-running" style="width:${corePct}%"></div></div>
      <small class="muted-text">${corePct.toFixed(1)}% utilized</small>
    </div>
  `);
  tiles.push(`
    <div class="capacity-tile">
      <p class="eyebrow">Nodes</p>
      <strong>~${formatNumber(nodesFreeEstimate)} free</strong>
      <small>${capacity.nodeClasses} node class${capacity.nodeClasses === 1 ? "" : "es"} · ${formatNumber(capacity.nodesTotal)} total</small>
      <small class="muted-text">Free count estimated from core utilization. See node table for exact per-class breakdown.</small>
    </div>
  `);
  if (capacity.accelNodesTotal > 0) {
    const gpus = toNumber(capacity.accelGpusTotal);
    const types = formatGpuTypeList(capacity.accelGpuTypes);
    const primary = gpus > 0
      ? `${formatNumber(gpus)} GPUs`
      : `${formatNumber(accelFreeCores)} cores free`;
    const subtitle = gpus > 0
      ? `${types ? `${types} · ` : ""}${formatNumber(capacity.accelNodesTotal)} nodes · ${formatNumber(capacity.accelCoresTotal)} CPU cores`
      : `${formatNumber(capacity.accelNodesTotal)} accelerator nodes · ${formatNumber(capacity.accelCoresTotal)} cores total`;
    const muted = gpus > 0
      ? `${accelPct.toFixed(1)}% node load (CPU proxy)`
      : `${accelPct.toFixed(1)}% utilized`;
    tiles.push(`
      <div class="capacity-tile">
        <p class="eyebrow">GPU / accelerator</p>
        <strong>${primary}</strong>
        <small>${subtitle}</small>
        <div class="progress-track"><div class="progress-value is-running" style="width:${accelPct}%"></div></div>
        <small class="muted-text">${muted}</small>
      </div>
    `);
  } else {
    tiles.push(`
      <div class="capacity-tile is-muted">
        <p class="eyebrow">GPU / accelerator</p>
        <strong>—</strong>
        <small>No accelerator nodes detected on this cluster.</small>
      </div>
    `);
  }
  elements.capacityStrip.innerHTML = tiles.join("");
};

const clearClusterStats = () => {
  if (elements.clusterRunningJobs) elements.clusterRunningJobs.textContent = "--";
  if (elements.clusterPendingJobs) elements.clusterPendingJobs.textContent = "--";
  if (elements.clusterRunningCores) elements.clusterRunningCores.textContent = "--";
  if (elements.clusterPendingCores) elements.clusterPendingCores.textContent = "--";
  if (elements.clusterCoreDonut) {
    elements.clusterCoreDonut.innerHTML = '<div class="placeholder">Select a cluster</div>';
  }
  if (elements.capacityStrip) {
    elements.capacityStrip.innerHTML = '<div class="placeholder">Select a cluster</div>';
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
    renderCapacityStrip(cluster);
    const capacity = computeClusterCapacity(cluster);
    renderQueueGrid(queues, capacity.coresTotal);
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
    // The API returns a warming/partial envelope while the first cluster
    // sweep is still in progress; render that as a progress UI rather than
    // an error. Once the sweep completes the response is a raw list again.
    const envelopeStatus =
      payload && !Array.isArray(payload) && typeof payload === "object"
        ? payload.status
        : null;
    if (envelopeStatus === "warming_up" && (!payload.clusters || !payload.clusters.length)) {
      state.clusters = [];
      state.lastUpdated = Date.now();
      renderSummary();
      renderClusterOptions();
      showGeneratingPlaceholder(formatProgressMessage(payload.progress, true));
      setStatus(formatProgressStatus(payload.progress, true), "info");
      scheduleRetry();
      return;
    }
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
      // Initial load: default to the most-available cluster (top of the
      // ranking) so the user lands on something useful instead of the
      // first cluster in arbitrary collector order.
      const { ranked } = computeAvailabilityRanking(state.clusters);
      state.selectedIndex = ranked.length ? ranked[0].index : 0;
    }
    renderSummary();
    renderClusterOptions();
    renderClusterDetail();
    if (envelopeStatus === "partial") {
      setStatus(formatProgressStatus(payload.progress, false), "info");
      scheduleRetry();
    } else {
      setStatus(silent ? "" : "Queue data updated just now.");
      if (state.clusters.length) {
        clearRetry();
      } else {
        scheduleRetry();
      }
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
  initHelpPanel();
  initQuickTips();
  applyConfigBranding();
  initBrand();
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
