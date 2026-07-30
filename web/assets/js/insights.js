/**
 * Insights page functionality
 * Displays recommendations and alerts based on fleet status and cluster data
 */

import {
  initHelpPanel,
  initBrand,
  formatRelativeTime,
  initQuickTips,
  initNav,
} from "./page-utils.js";

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
  criticalCount: document.getElementById("critical-count"),
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
  critical: "Requires immediate attention — may block work",
  warning: "Should be addressed soon to prevent issues",
  info: "Informational — no action required",
  suggestion: "Optional optimization for better efficiency",
};

const SEVERITY_RANK = { critical: 0, warning: 1, info: 2, suggestion: 3 };
const SEVERITY_LABEL = {
  critical: "Action required",
  warning: "Needs attention",
  info: "Update",
  suggestion: "Tip",
};

function renderInsights() {
  const { insights } = state;

  if (!insights.length) {
    elements.insightsList.innerHTML = '<li class="placeholder">No insights available. All systems operating normally.</li>';
    elements.insightsNote.textContent = "Fleet is operating normally — nothing to act on right now.";
    return;
  }

  // Sort the rendered list with critical first so users see blockers up top.
  // Server already sorts, but guard against ordering drift if shape changes.
  const sorted = [...insights].sort((a, b) => {
    const aRank = SEVERITY_RANK[(a.type || "info").toLowerCase()] ?? 4;
    const bRank = SEVERITY_RANK[(b.type || "info").toLowerCase()] ?? 4;
    return aRank - bRank;
  });

  const critical = sorted.filter((i) => (i.type || "").toLowerCase() === "critical").length;
  const warnings = sorted.filter((i) => (i.type || "").toLowerCase() === "warning").length;
  const noteParts = [];
  if (critical) noteParts.push(`${critical} need${critical === 1 ? "s" : ""} immediate action`);
  if (warnings) noteParts.push(`${warnings} need${warnings === 1 ? "s" : ""} attention soon`);
  noteParts.push(`${sorted.length} total`);
  elements.insightsNote.textContent = noteParts.join(" · ");

  elements.insightsList.innerHTML = sorted
    .map((insight) => {
      const typeClass = (insight.type || "info").toLowerCase();
      const iconMap = {
        critical: "&#x2717;",
        warning: "&#x26A0;",
        info: "&#x2139;",
        suggestion: "&#x2713;",
      };
      const icon = iconMap[typeClass] || "&#x2139;";
      const severityLabel = SEVERITY_LABEL[typeClass] || "Update";
      const severityTip = severityDescriptions[typeClass] || "";

      // Link the cluster pill to the queue health page so a user reading the
      // insight can jump straight to the affected cluster.
      const clusterChip = insight.cluster
        ? `<a class="insight-cluster" href="queues.html" title="View queue health for ${escapeHtml(insight.cluster)}">${escapeHtml(insight.cluster)}</a>`
        : "";
      const queueChip = insight.queue
        ? `<span class="insight-chip" title="Affected queue">${escapeHtml(insight.queue)}</span>`
        : "";
      const action = insight.action_description
        ? `<p class="insight-action">${escapeHtml(insight.action_description)}</p>`
        : "";

      return `
        <li class="insight-item ${typeClass}" title="${severityTip}">
          <span class="insight-icon" aria-hidden="true">${icon}</span>
          <div class="insight-content">
            <p class="insight-severity-label">${severityLabel}</p>
            <p>${escapeHtml(insight.message)}</p>
            ${action}
            <div class="insight-meta">
              ${clusterChip}
              ${queueChip}
            </div>
          </div>
        </li>
      `;
    })
    .join("");
}

function updateSummary(generatedAt) {
  const { insights } = state;
  const critical = insights.filter((i) => (i.type || "").toLowerCase() === "critical").length;
  const warnings = insights.filter((i) => (i.type || "").toLowerCase() === "warning").length;
  // Roll suggestions in with info — both are non-actionable and that's the
  // distinction the user cares about. Splitting them was just noise.
  const infos = insights.filter((i) => {
    const type = (i.type || "").toLowerCase();
    return type === "info" || type === "suggestion";
  }).length;

  if (elements.criticalCount) elements.criticalCount.textContent = critical;
  if (elements.warningCount) elements.warningCount.textContent = warnings;
  if (elements.infoCount) elements.infoCount.textContent = infos;

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

// ---------------------------------------------------------------------------
// Job placement planner
// ---------------------------------------------------------------------------

const PLACEMENT_URL = new URL("api/placement", apiBase).toString();

const formatScore = (score) => `${Math.round(Number(score) || 0)}`;

function renderPlacement(result) {
  const container = document.getElementById("planner-results");
  if (!container) return;

  const { candidates = [], blocked = [], considered = 0, request = {} } = result || {};

  if (!candidates.length && !blocked.length) {
    container.innerHTML = `<p class="muted-text">
      No queue data yet — the cluster monitor has not reported any queues to rank.
    </p>`;
    return;
  }

  const jobLabel = `${Number(request.cores || 0).toLocaleString()} cores × ${request.hours}h`;

  const card = (entry, isBlocked) => `
    <li class="planner-card${isBlocked ? " is-blocked" : ""}">
      <div class="planner-card-head">
        <div>
          <h4>${escapeHtml(entry.cluster)} <span>/ ${escapeHtml(entry.queue || "")}</span></h4>
          <p class="muted-text">
            ${Number(entry.cores_free || 0).toLocaleString()} cores idle
            ${entry.max_walltime ? `· max ${escapeHtml(entry.max_walltime)}` : ""}
            ${entry.allocation_hours_remaining !== null && entry.allocation_hours_remaining !== undefined
              ? `· ${Number(entry.allocation_hours_remaining).toLocaleString()} hrs left`
              : ""}
          </p>
        </div>
        ${isBlocked
          ? '<span class="badge down">Cannot run</span>'
          : `<span class="planner-score" title="Capacity ${entry.components.capacity} · wait ${entry.components.wait} · backlog ${entry.components.backlog} · allocation ${entry.components.allocation}">${formatScore(entry.score)}</span>`}
      </div>
      ${entry.wait_estimate?.wait_display
        ? `<p class="planner-wait">Est. start <strong>${escapeHtml(entry.wait_estimate.wait_display)}</strong>
             <small>${escapeHtml(entry.wait_estimate.basis || "")}</small></p>`
        : ""}
      ${(isBlocked ? entry.blockers : entry.reasons)?.length
        ? `<ul class="planner-reasons">
             ${(isBlocked ? entry.blockers : entry.reasons)
               .map((reason) => `<li>${escapeHtml(reason)}</li>`)
               .join("")}
           </ul>`
        : ""}
      ${isBlocked
        ? ""
        : `<a class="planner-link" href="${entry.links.queues}">Open queue health →</a>`}
    </li>`;

  container.innerHTML = `
    <p class="planner-summary muted-text">
      Ranked ${candidates.length} of ${considered} queues for ${escapeHtml(jobLabel)}.
    </p>
    <ul class="planner-list">${candidates.map((c) => card(c, false)).join("")}</ul>
    ${blocked.length
      ? `<details class="planner-blocked">
           <summary>${blocked.length} queue${blocked.length === 1 ? "" : "s"} cannot run this job</summary>
           <ul class="planner-list">${blocked.map((b) => card(b, true)).join("")}</ul>
         </details>`
      : ""}`;
}

async function runPlanner(event) {
  event?.preventDefault();
  const container = document.getElementById("planner-results");
  const params = new URLSearchParams({
    cores: document.getElementById("planner-cores")?.value || "1",
    hours: document.getElementById("planner-hours")?.value || "1",
    gpus: document.getElementById("planner-gpus")?.value || "0",
  });
  if (container) {
    container.innerHTML = '<p class="muted-text">Ranking queues…</p>';
  }
  try {
    const response = await fetch(`${PLACEMENT_URL}?${params}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    renderPlacement(await response.json());
  } catch (err) {
    console.error("Placement request failed", err);
    if (container) {
      container.innerHTML = `<p class="muted-text">Unable to rank queues (${escapeHtml(err.message)}).</p>`;
    }
  }
}

function registerEvents() {
  document.getElementById("planner-form")?.addEventListener("submit", runPlanner);
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
initBrand();
initNav();
registerEvents();
loadInsights();

// Auto-refresh every 2 minutes
setInterval(() => loadInsights({ showLoading: false }), 2 * 60 * 1000);
