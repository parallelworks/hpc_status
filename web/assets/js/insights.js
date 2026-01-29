/**
 * Insights page functionality
 * Displays recommendations and alerts based on fleet status and cluster data
 */

import { initHelpPanel, formatRelativeTime, initQuickTips } from "./page-utils.js";

const THEME_STORAGE_KEY = "hpc-status-theme";

function deriveBasePath(pathname) {
  const path = pathname || "/";
  if (path.endsWith("/")) {
    return path;
  }
  const lastSlash = path.lastIndexOf("/");
  const segment = lastSlash >= 0 ? path.slice(lastSlash + 1) : path;
  if (segment.includes(".")) {
    const prefix = lastSlash >= 0 ? path.slice(0, lastSlash + 1) : "/";
    return prefix || "/";
  }
  return `${path}/`;
}

const pageUrl = new URL(window.location.href);
const dataBasePath = document.documentElement.dataset.basePath || "";
const basePath = dataBasePath || deriveBasePath(pageUrl.pathname);
const defaultApiBase = new URL(basePath || "/", pageUrl.origin);
const configuredBase = window.API_BASE_URL || document.documentElement.getAttribute("data-api-base");
const apiBase = (() => {
  if (!configuredBase) return defaultApiBase;
  try {
    return new URL(configuredBase, defaultApiBase);
  } catch (err) {
    console.warn("Invalid API base override:", configuredBase, err);
    return defaultApiBase;
  }
})();

const INSIGHTS_URL = new URL("api/insights", apiBase).toString();

const state = {
  insights: [],
  loading: false,
  retryHandle: null,
};

const elements = {
  totalInsights: document.getElementById("total-insights"),
  warningCount: document.getElementById("warning-count"),
  infoCount: document.getElementById("info-count"),
  lastUpdated: document.getElementById("last-updated"),
  insightsList: document.getElementById("insights-list"),
  insightsNote: document.getElementById("insights-note"),
  refreshBtn: document.getElementById("refresh-btn"),
  themeToggle: document.getElementById("theme-toggle"),
  themeLabel: document.querySelector("#theme-toggle .theme-label"),
  themeIcon: document.querySelector("#theme-toggle .theme-icon"),
  dataStatus: document.getElementById("data-status"),
};

function safeGetStoredTheme() {
  try {
    return window.localStorage.getItem(THEME_STORAGE_KEY);
  } catch (err) {
    return null;
  }
}

function safeSetStoredTheme(value) {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, value);
  } catch (err) {
    console.warn("Unable to persist theme", err);
  }
}

function resolveDefaultTheme() {
  return (window.APP_CONFIG && window.APP_CONFIG.defaultTheme) || document.documentElement.dataset.theme || "dark";
}

function applyTheme(theme, { persist = true } = {}) {
  const normalized = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = normalized;
  document.body.dataset.theme = normalized;
  if (persist) {
    safeSetStoredTheme(normalized);
  }
  updateThemeToggle(normalized);
}

function updateThemeToggle(theme) {
  if (elements.themeLabel) {
    elements.themeLabel.textContent = theme === "dark" ? "Dark" : "Light";
  }
  if (elements.themeIcon) {
    elements.themeIcon.textContent = theme === "dark" ? "🌙" : "☀️";
  }
  if (elements.themeToggle) {
    elements.themeToggle.setAttribute("data-theme", theme);
  }
}

function escapeHtml(str) {
  return (str || "").replace(/[&<>"']/g, (ch) => {
    switch (ch) {
      case "&": return "&amp;";
      case "<": return "&lt;";
      case ">": return "&gt;";
      case '"': return "&quot;";
      case "'": return "&#39;";
      default: return ch;
    }
  });
}

async function loadInsights({ showLoading = true } = {}) {
  if (state.loading) return;
  state.loading = true;

  if (showLoading) {
    elements.insightsList.innerHTML = '<li class="placeholder">Loading insights...</li>';
  }

  try {
    const response = await fetch(`${INSIGHTS_URL}?t=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const payload = await response.json();
    state.insights = payload.insights || [];
    renderInsights();
    updateSummary(payload.generated_at);
    clearRetry();
  } catch (err) {
    console.warn("Unable to load insights", err);
    showError("Unable to load insights. Retrying...");
    scheduleRetry();
  } finally {
    state.loading = false;
  }
}

// Map insight types to severity descriptions for researcher context
const severityDescriptions = {
  critical: "Requires immediate attention - may block work",
  warning: "Should be addressed soon to prevent issues",
  info: "Informational - no action required",
  suggestion: "Optional optimization for better efficiency",
};

// Map metric types to human-readable explanations
const metricExplanations = {
  allocation_percent_remaining: "Percentage of compute hours still available",
  storage_percent_used: "Filesystem usage level",
  queue_pending_jobs: "Jobs waiting for resources",
  queue_wait_time: "Expected queue wait time",
  system_status: "Current operational state",
};

function renderInsights() {
  const { insights } = state;

  if (!insights.length) {
    elements.insightsList.innerHTML = '<li class="placeholder">No insights available. All systems operating normally.</li>';
    elements.insightsNote.textContent = "No recommendations at this time";
    return;
  }

  elements.insightsNote.textContent = `${insights.length} recommendation${insights.length === 1 ? "" : "s"}`;

  elements.insightsList.innerHTML = insights
    .map((insight) => {
      const typeClass = insight.type || "info";
      // Enhanced icons with better visual distinction
      const iconMap = {
        critical: "&#x2717;", // X mark
        warning: "&#x26A0;",  // Warning triangle
        info: "&#x2139;",     // Info symbol
        suggestion: "&#x2713;", // Checkmark
      };
      const icon = iconMap[typeClass] || "&#x2139;";
      const cluster = insight.cluster ? `<small class="insight-cluster">${escapeHtml(insight.cluster)}</small>` : "";
      const metric = insight.metric ? `<span class="insight-metric" title="${metricExplanations[insight.metric] || 'Metric value'}">${escapeHtml(insight.metric)}</span>` : "";
      const priority = insight.priority ? `<span class="insight-priority">Priority: ${insight.priority}</span>` : "";
      const severityTip = severityDescriptions[typeClass] || "";

      return `
        <li class="insight-item ${typeClass}" title="${severityTip}">
          <span class="insight-icon">${icon}</span>
          <div class="insight-content">
            <p>${escapeHtml(insight.message)}</p>
            <div class="insight-meta">
              ${cluster}
              ${metric}
              ${priority}
            </div>
          </div>
        </li>
      `;
    })
    .join("");
}

function updateSummary(generatedAt) {
  const { insights } = state;
  const warnings = insights.filter((i) => i.type === "warning").length;
  const infos = insights.filter((i) => i.type === "info").length;

  elements.totalInsights.textContent = insights.length;
  elements.warningCount.textContent = warnings;
  elements.infoCount.textContent = infos;

  if (generatedAt) {
    try {
      elements.lastUpdated.textContent = formatRelativeTime(generatedAt);
      elements.lastUpdated.title = `Generated: ${new Date(generatedAt).toLocaleString()}`;
    } catch {
      elements.lastUpdated.textContent = "--";
    }
  }
}

function showError(message) {
  if (elements.dataStatus) {
    elements.dataStatus.textContent = message;
    elements.dataStatus.setAttribute("data-variant", "error");
    elements.dataStatus.removeAttribute("hidden");
  }
}

function clearError() {
  if (elements.dataStatus) {
    elements.dataStatus.setAttribute("hidden", "hidden");
  }
}

function scheduleRetry() {
  if (state.retryHandle) return;
  state.retryHandle = setTimeout(() => {
    state.retryHandle = null;
    loadInsights({ showLoading: false });
  }, 15000);
}

function clearRetry() {
  if (state.retryHandle) {
    clearTimeout(state.retryHandle);
    state.retryHandle = null;
  }
  clearError();
}

async function triggerRefresh() {
  const btn = elements.refreshBtn;
  const originalHTML = btn.innerHTML;
  btn.disabled = true;
  btn.classList.add("is-loading");
  btn.innerHTML = '<span class="btn-icon" aria-hidden="true">↻</span><span>Refreshing…</span>';

  try {
    await loadInsights({ showLoading: true });
  } finally {
    btn.disabled = false;
    btn.classList.remove("is-loading");
    btn.innerHTML = originalHTML;
  }
}

function registerEvents() {
  elements.refreshBtn?.addEventListener("click", () => triggerRefresh());
  elements.themeToggle?.addEventListener("click", () => {
    const current = document.documentElement.dataset.theme || resolveDefaultTheme();
    const next = current === "dark" ? "light" : "dark";
    applyTheme(next);
  });
}

// Initialize
applyTheme(safeGetStoredTheme() || resolveDefaultTheme(), { persist: false });
initHelpPanel();
initQuickTips();
registerEvents();
loadInsights();

// Auto-refresh every 2 minutes
setInterval(() => loadInsights({ showLoading: false }), 2 * 60 * 1000);
