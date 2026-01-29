/**
 * Storage page - Filesystem capacity monitoring
 */

import { initThemeToggle, initHelpPanel, formatRelativeTime, initQuickTips } from "./page-utils.js";

const defaultApiBase = (() => {
  const basePath = document.documentElement.dataset.basePath || "/";
  try {
    return new URL(basePath, window.location.origin).toString();
  } catch {
    return window.location.origin + "/";
  }
})();

const apiBase = (() => {
  const configuredBase = window.APP_CONFIG?.apiBase;
  if (!configuredBase) return defaultApiBase;
  try {
    return new URL(configuredBase, window.location.origin).toString();
  } catch (err) {
    console.warn("Invalid API base override:", configuredBase, err);
    return defaultApiBase;
  }
})();

const DATA_URL = new URL("api/storage", apiBase).toString();

const numberFormatter = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });

const state = {
  clusters: [],
  loading: false,
  lastUpdated: null,
  features: {
    clusterPages: window.APP_CONFIG?.clusterPagesEnabled !== false,
  },
};

const elements = {};

const cacheElements = () => {
  elements.clusterCount = document.getElementById("cluster-count");
  elements.filesystemCount = document.getElementById("filesystem-count");
  elements.warningCount = document.getElementById("warning-count");
  elements.lastUpdated = document.getElementById("last-updated");
  elements.warningsSection = document.getElementById("warnings-section");
  elements.warningsList = document.getElementById("warnings-list");
  elements.warningsCountBadge = document.getElementById("warnings-count");
  elements.storageGrid = document.getElementById("storage-grid");
  elements.storageGridNote = document.getElementById("storage-grid-note");
  elements.refreshBtn = document.getElementById("refresh-btn");
  elements.statusBanner = document.getElementById("data-status");
};

const formatSize = (value) => {
  if (!value) return "--";
  return String(value).trim();
};

const parsePercent = (value) => {
  if (value === null || value === undefined) return 0;
  const num = parseFloat(String(value).replace("%", ""));
  return Number.isFinite(num) ? num : 0;
};

const clampPercent = (value) => Math.max(0, Math.min(100, value));

const getStorageData = (cluster) => cluster?.storage_data || {};
const getClusterName = (cluster) => cluster?.cluster_metadata?.name || cluster?.cluster_metadata?.uri || "Unknown";

const computeSummary = () => {
  let totalFilesystems = 0;
  let warningCount = 0;
  const warnings = [];

  state.clusters.forEach((cluster) => {
    const storage = getStorageData(cluster);
    const name = getClusterName(cluster);

    Object.entries(storage).forEach(([fsType, fsData]) => {
      if (fsData && fsData.filesystem) {
        totalFilesystems++;
        const percent = parsePercent(fsData.percent_used);
        if (percent >= 80) {
          warningCount++;
          warnings.push({
            cluster: name,
            type: fsType,
            percent,
            filesystem: fsData.filesystem,
          });
        }
      }
    });
  });

  return { totalFilesystems, warningCount, warnings };
};

const renderSummary = () => {
  const summary = computeSummary();

  if (elements.clusterCount) {
    elements.clusterCount.textContent = state.clusters.length;
  }
  if (elements.filesystemCount) {
    elements.filesystemCount.textContent = summary.totalFilesystems;
  }
  if (elements.warningCount) {
    elements.warningCount.textContent = summary.warningCount;
  }
  if (elements.lastUpdated && state.lastUpdated) {
    elements.lastUpdated.textContent = formatRelativeTime(state.lastUpdated);
    elements.lastUpdated.title = `Collected: ${new Date(state.lastUpdated).toLocaleString()}`;
  }

  renderWarnings(summary.warnings);
};

const renderWarnings = (warnings) => {
  if (!elements.warningsSection || !elements.warningsList) return;

  if (!warnings.length) {
    elements.warningsSection.setAttribute("hidden", "hidden");
    return;
  }

  elements.warningsSection.removeAttribute("hidden");
  if (elements.warningsCountBadge) {
    elements.warningsCountBadge.textContent = warnings.length;
  }

  // Sort by percent descending
  warnings.sort((a, b) => b.percent - a.percent);

  elements.warningsList.innerHTML = warnings
    .slice(0, 10)
    .map((w) => {
      const icon = w.percent >= 90 ? "&#x26A0;&#xFE0F;" : "&#x26A0;";
      const severity = w.percent >= 90 ? "warning" : "info";
      const recommendation = w.percent >= 95
        ? "Critical: May cause job failures. Clean up files immediately."
        : w.percent >= 90
        ? "High usage: Consider removing unused files or moving data to archive."
        : "Elevated usage: Monitor and plan cleanup.";
      return `
        <li class="insight-item ${severity}" title="${recommendation}">
          <span class="insight-icon">${icon}</span>
          <div class="insight-content">
            <p><strong>${w.cluster}</strong> ${w.type}: ${w.percent}% used</p>
            <small>${w.filesystem}</small>
            <p class="metric-explanation">${recommendation}</p>
          </div>
        </li>
      `;
    })
    .join("");
};

const buildFilesystemRow = (fsType, fsData) => {
  if (!fsData || !fsData.filesystem) {
    return "";
  }

  const percent = parsePercent(fsData.percent_used);
  const barClass = percent >= 90 ? "is-critical" : percent >= 80 ? "is-warning" : "";

  return `
    <tr>
      <td><strong>${fsType}</strong></td>
      <td>${formatSize(fsData.size)}</td>
      <td>${formatSize(fsData.used)}</td>
      <td>${formatSize(fsData.available)}</td>
      <td>
        <div class="usage-progress compact">
          <div class="progress-track">
            <div class="progress-value ${barClass}" style="width:${clampPercent(percent)}%"></div>
          </div>
          <span>${percent}%</span>
        </div>
      </td>
    </tr>
  `;
};

const buildStorageCard = (cluster) => {
  const metadata = cluster?.cluster_metadata || {};
  const storage = getStorageData(cluster);
  const name = getClusterName(cluster);

  const metaParts = [];
  if (metadata.status) metaParts.push(String(metadata.status).toUpperCase());
  if (metadata.type) metaParts.push(metadata.type);
  if (metadata.timestamp) metaParts.push(new Date(metadata.timestamp).toLocaleString());

  const filesystems = Object.entries(storage).filter(([, data]) => data && data.filesystem);

  if (!filesystems.length) {
    return `
      <article class="cluster-card">
        <header>
          <div>
            <p class="eyebrow">Storage</p>
            <h4>${name}</h4>
            <p class="muted-text">${metaParts.join(" &bull; ")}</p>
          </div>
        </header>
        <div class="cluster-card-body">
          <p class="placeholder">No storage data available for this cluster.</p>
        </div>
      </article>
    `;
  }

  // Calculate overall usage for donut
  let totalSize = 0;
  let totalUsed = 0;
  filesystems.forEach(([, data]) => {
    // Parse sizes - handle human-readable formats like "1.5T", "500G"
    const sizeNum = parseHumanSize(data.size);
    const usedNum = parseHumanSize(data.used);
    totalSize += sizeNum;
    totalUsed += usedNum;
  });
  const overallPercent = totalSize > 0 ? clampPercent((totalUsed / totalSize) * 100) : 0;
  const freePercent = 100 - overallPercent;

  const rows = filesystems.map(([fsType, fsData]) => buildFilesystemRow(fsType, fsData)).join("");

  return `
    <article class="cluster-card">
      <header>
        <div>
          <p class="eyebrow">Storage</p>
          <h4>${name}</h4>
          <p class="muted-text">${metaParts.join(" &bull; ")}</p>
        </div>
      </header>
      <div class="cluster-card-body">
        <div class="cluster-card-summary">
          <div class="donut-chart" aria-label="${name} storage">
            <div class="donut" style="--donut-value:${freePercent};--donut-primary:${freePercent < 20 ? 'var(--danger)' : freePercent < 40 ? 'var(--warning)' : 'var(--success)'}">
              <strong>${Math.round(freePercent)}%</strong>
              <span>Free</span>
            </div>
            <small>${filesystems.length} filesystem${filesystems.length !== 1 ? "s" : ""}</small>
          </div>
          <ul class="cluster-metrics">
            <li><span>Filesystems</span><strong>${filesystems.length}</strong></li>
            <li><span>Used</span><strong>${Math.round(overallPercent)}%</strong></li>
          </ul>
        </div>
        <div class="cluster-subprojects">
          <div class="table-head compact">
            <h5>Filesystem Details <span class="th-help" title="$HOME: backed up, small quota. $WORK: larger quota for projects. $SCRATCH: fast temp storage, may be purged.">ⓘ</span></h5>
          </div>
          <div class="table-scroll mini">
            <table class="quota-table">
              <thead>
                <tr>
                  <th>Type <span class="th-help" title="Filesystem type: home, work, scratch, etc.">ⓘ</span></th>
                  <th>Size <span class="th-help" title="Total capacity of this filesystem">ⓘ</span></th>
                  <th>Used <span class="th-help" title="Storage currently in use">ⓘ</span></th>
                  <th>Available <span class="th-help" title="Free space remaining">ⓘ</span></th>
                  <th>Usage <span class="th-help" title="Percent used. Warning at 80%, critical at 95%">ⓘ</span></th>
                </tr>
              </thead>
              <tbody>
                ${rows}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </article>
  `;
};

const parseHumanSize = (sizeStr) => {
  if (!sizeStr) return 0;
  const str = String(sizeStr).trim().toUpperCase();
  const match = str.match(/^([\d.]+)\s*([KMGTP]?)I?B?$/i);
  if (!match) return 0;

  const num = parseFloat(match[1]);
  const unit = match[2] || "";
  const multipliers = { K: 1024, M: 1024 ** 2, G: 1024 ** 3, T: 1024 ** 4, P: 1024 ** 5 };
  return num * (multipliers[unit] || 1);
};

const renderStorageGrid = () => {
  if (!elements.storageGrid) return;

  if (!state.clusters.length) {
    elements.storageGrid.innerHTML = '<article class="card placeholder">No cluster data available.</article>';
    if (elements.storageGridNote) {
      elements.storageGridNote.textContent = "No clusters connected.";
    }
    return;
  }

  // Filter to clusters that have storage data
  const clustersWithStorage = state.clusters.filter((c) => {
    const storage = getStorageData(c);
    return Object.values(storage).some((fs) => fs && fs.filesystem);
  });

  if (!clustersWithStorage.length) {
    elements.storageGrid.innerHTML = '<article class="card placeholder">No storage data collected yet. Storage data will appear after the next collection cycle.</article>';
    if (elements.storageGridNote) {
      elements.storageGridNote.textContent = "Waiting for storage data...";
    }
    return;
  }

  elements.storageGrid.innerHTML = clustersWithStorage.map(buildStorageCard).join("");

  if (elements.storageGridNote) {
    elements.storageGridNote.textContent = `Showing ${clustersWithStorage.length} cluster${clustersWithStorage.length !== 1 ? "s" : ""} with storage data.`;
  }
};

const setBanner = (message, type = "info") => {
  if (!elements.statusBanner) return;
  elements.statusBanner.textContent = message;
  elements.statusBanner.className = `status-banner ${type}`;
  elements.statusBanner.removeAttribute("hidden");
};

const clearBanner = () => {
  if (!elements.statusBanner) return;
  elements.statusBanner.setAttribute("hidden", "hidden");
};

const disableRefresh = (disabled) => {
  if (elements.refreshBtn) {
    elements.refreshBtn.disabled = disabled;
  }
};

const showGeneratingPlaceholder = (message = "Loading storage data...") => {
  if (elements.storageGrid) {
    elements.storageGrid.innerHTML = `<article class="card placeholder">${message}</article>`;
  }
};

const loadData = async ({ silent = true } = {}) => {
  if (!state.features.clusterPages) {
    return;
  }
  if (state.loading) return;

  state.loading = true;
  disableRefresh(true);

  if (!silent) {
    showGeneratingPlaceholder("Refreshing storage data...");
  }

  try {
    const response = await fetch(`${DATA_URL}?t=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const payload = await response.json();

    if (Array.isArray(payload)) {
      state.clusters = payload;
    } else if (payload && Array.isArray(payload.clusters)) {
      state.clusters = payload.clusters;
    } else {
      state.clusters = [];
    }

    state.lastUpdated = Date.now();
    clearBanner();
    renderSummary();
    renderStorageGrid();
  } catch (err) {
    console.error("Failed to load storage data:", err);
    if (!silent) {
      setBanner(`Failed to load storage data: ${err.message}`, "error");
    }
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
  document.title = `Storage | ${title}`;
};

const bindEvents = () => {
  if (elements.refreshBtn) {
    elements.refreshBtn.addEventListener("click", () => loadData({ silent: false }));
  }
};

document.addEventListener("DOMContentLoaded", () => {
  cacheElements();
  initThemeToggle();
  initHelpPanel();
  initQuickTips();
  applyConfigBranding();

  const nav = document.querySelector("[data-cluster-nav]");
  if (!state.features.clusterPages) {
    if (nav) nav.remove();
    setBanner("Cluster pages are disabled on this server.", "error");
    showGeneratingPlaceholder("Storage monitoring disabled.");
    disableRefresh(true);
    return;
  }

  bindEvents();
  loadData();
  setInterval(() => loadData({ silent: true }), 5 * 60 * 1000);
});
