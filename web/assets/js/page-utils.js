const THEME_STORAGE_KEY = "hpc-status-theme";

const deriveBasePath = (pathname = "/") => {
  const path = pathname || "/";
  if (path.endsWith("/")) {
    return path;
  }
  const lastSlash = path.lastIndexOf("/");
  const segment = lastSlash >= 0 ? path.slice(lastSlash + 1) : path;
  const prefix = lastSlash >= 0 ? path.slice(0, lastSlash + 1) : "/";
  return segment.includes(".") ? prefix || "/" : `${path}/`;
};

const safeGetStoredTheme = () => {
  try {
    return window.localStorage.getItem(THEME_STORAGE_KEY);
  } catch (err) {
    console.warn("Unable to read stored theme", err);
    return null;
  }
};

const safeSetStoredTheme = (value) => {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, value);
  } catch (err) {
    console.warn("Unable to persist theme", err);
  }
};

const resolveDefaultTheme = () =>
  (window.APP_CONFIG && window.APP_CONFIG.defaultTheme) ||
  document.documentElement.dataset.theme ||
  "dark";

const updateThemeToggleIndicator = (theme) => {
  const toggle = document.getElementById("theme-toggle");
  if (!toggle) return;
  const label = toggle.querySelector(".theme-label");
  const icon = toggle.querySelector(".theme-icon");
  if (label) {
    label.textContent = theme === "dark" ? "Dark" : "Light";
  }
  if (icon) {
    icon.textContent = theme === "dark" ? "🌙" : "☀️";
  }
  toggle.setAttribute("data-theme", theme);
};

const applyTheme = (theme, { persist = true } = {}) => {
  const normalized = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = normalized;
  if (document.body) {
    document.body.dataset.theme = normalized;
  }
  if (persist) {
    safeSetStoredTheme(normalized);
  }
  updateThemeToggleIndicator(normalized);
  return normalized;
};

export const initThemeToggle = () => {
  const initial = safeGetStoredTheme() || resolveDefaultTheme();
  const current = applyTheme(initial, { persist: false });
  const toggle = document.getElementById("theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      applyTheme(next);
    });
  }
  return current;
};

export const getBaseUrl = () => {
  const dataBasePath = document.documentElement.dataset.basePath || "";
  const path = dataBasePath || deriveBasePath(window.location.pathname || "/");
  return new URL(path || "/", window.location.origin);
};

export const buildDataUrl = (relativePath) => {
  const sanitized = (relativePath || "").replace(/^\/+/, "");
  return new URL(sanitized, getBaseUrl());
};

export const buildApiUrl = (relativePath) => {
  const sanitized = (relativePath || "").replace(/^\/+/, "");
  return new URL(sanitized, getBaseUrl());
};

export const clusterPagesEnabled = () => {
  const config = window.APP_CONFIG || {};
  return config.clusterPagesEnabled !== false;
};

export const clampPercent = (value) => {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return 0;
  }
  return Math.min(100, Math.max(0, numeric));
};

// Help panel content - HPC concepts quick reference for researchers
const HELP_CONTENT = {
  general: {
    title: "HPC Status Monitor - Quick Reference",
    sections: [
      {
        heading: "System Status",
        items: [
          { term: "UP", desc: "System is fully operational and accepting jobs" },
          { term: "DEGRADED", desc: "System operational but with reduced capacity or performance" },
          { term: "MAINTENANCE", desc: "Planned maintenance in progress - jobs may be suspended" },
          { term: "DOWN", desc: "System is offline - no access available" },
        ],
      },
      {
        heading: "Resource Concepts",
        items: [
          { term: "Core-hours", desc: "Allocation unit: 1 CPU core running for 1 hour. A 100-core job running 2 hours uses 200 core-hours." },
          { term: "Nodes", desc: "Individual compute servers. Each node has multiple CPU cores." },
          { term: "Cores", desc: "CPU processors. Jobs request cores; more cores = more parallelism." },
          { term: "Walltime", desc: "Maximum time your job can run. Exceeding it terminates the job." },
        ],
      },
      {
        heading: "Queue States",
        items: [
          { term: "Active", desc: "Queue accepting and scheduling jobs normally" },
          { term: "Draining", desc: "Finishing current jobs but not starting new ones" },
          { term: "Backlog", desc: "Jobs waiting in queue for resources" },
        ],
      },
      {
        heading: "Storage Types",
        items: [
          { term: "$HOME", desc: "Your home directory - small quota, backed up, for code and scripts" },
          { term: "$WORK", desc: "Working directory - larger quota for active project data" },
          { term: "$SCRATCH", desc: "High-speed temp storage - NOT backed up, files may be purged" },
        ],
      },
    ],
  },
};

// Create and inject help panel into page
export const initHelpPanel = () => {
  // Create help button in hero actions if not present
  const heroActions = document.querySelector(".hero-actions");
  if (heroActions && !document.getElementById("help-btn")) {
    const helpBtn = document.createElement("button");
    helpBtn.id = "help-btn";
    helpBtn.className = "ghost-btn help-btn";
    helpBtn.setAttribute("aria-label", "Show help and quick reference");
    helpBtn.innerHTML = '<span class="btn-icon" aria-hidden="true">?</span><span>Help</span>';
    heroActions.insertBefore(helpBtn, heroActions.firstChild);
  }

  // Create help panel overlay
  if (!document.getElementById("help-panel")) {
    const panel = document.createElement("div");
    panel.id = "help-panel";
    panel.className = "help-panel-overlay";
    panel.setAttribute("hidden", "hidden");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-labelledby", "help-panel-title");

    const content = HELP_CONTENT.general;
    const sectionsHtml = content.sections.map(section => `
      <div class="help-section">
        <h4>${section.heading}</h4>
        <dl class="help-definitions">
          ${section.items.map(item => `
            <dt>${item.term}</dt>
            <dd>${item.desc}</dd>
          `).join("")}
        </dl>
      </div>
    `).join("");

    panel.innerHTML = `
      <div class="help-panel-content">
        <div class="help-panel-header">
          <h3 id="help-panel-title">${content.title}</h3>
          <button id="help-close" class="help-close-btn" aria-label="Close help">&times;</button>
        </div>
        <div class="help-panel-body">
          ${sectionsHtml}
          <div class="help-section help-tips">
            <h4>Tips for Researchers</h4>
            <ul>
              <li>Check <strong>Queue Health</strong> before submitting large jobs to find queues with shorter wait times</li>
              <li>Monitor <strong>Quota Usage</strong> to avoid job rejections when allocations run low</li>
              <li>Keep <strong>$SCRATCH</strong> usage low - files are automatically deleted after 30-60 days</li>
              <li>Use <strong>debug queues</strong> for testing - they have shorter wait times but limited walltime</li>
            </ul>
          </div>
        </div>
      </div>
    `;

    document.body.appendChild(panel);
  }

  // Bind events
  const helpBtn = document.getElementById("help-btn");
  const helpPanel = document.getElementById("help-panel");
  const closeBtn = document.getElementById("help-close");

  if (helpBtn && helpPanel) {
    helpBtn.addEventListener("click", () => {
      helpPanel.removeAttribute("hidden");
      helpPanel.focus();
    });
  }

  if (closeBtn && helpPanel) {
    closeBtn.addEventListener("click", () => {
      helpPanel.setAttribute("hidden", "hidden");
    });
  }

  // Close on escape key
  if (helpPanel) {
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !helpPanel.hasAttribute("hidden")) {
        helpPanel.setAttribute("hidden", "hidden");
      }
    });

    // Close on backdrop click
    helpPanel.addEventListener("click", (e) => {
      if (e.target === helpPanel) {
        helpPanel.setAttribute("hidden", "hidden");
      }
    });
  }
};

// Format relative time for data freshness indicators
export const formatRelativeTime = (timestamp) => {
  if (!timestamp) return "Unknown";

  const now = Date.now();
  const then = new Date(timestamp).getTime();
  if (isNaN(then)) return "Unknown";

  const diffSeconds = Math.floor((now - then) / 1000);

  if (diffSeconds < 60) return "Just now";
  if (diffSeconds < 120) return "1 minute ago";
  if (diffSeconds < 3600) return `${Math.floor(diffSeconds / 60)} minutes ago`;
  if (diffSeconds < 7200) return "1 hour ago";
  if (diffSeconds < 86400) return `${Math.floor(diffSeconds / 3600)} hours ago`;
  return `${Math.floor(diffSeconds / 86400)} days ago`;
};

// Create a freshness indicator element
export const createFreshnessIndicator = (timestamp, elementId = "freshness-indicator") => {
  let indicator = document.getElementById(elementId);
  if (!indicator) {
    indicator = document.createElement("span");
    indicator.id = elementId;
    indicator.className = "freshness-indicator";
  }

  const relativeTime = formatRelativeTime(timestamp);
  const then = new Date(timestamp).getTime();
  const diffMinutes = (Date.now() - then) / 60000;

  // Color code based on freshness
  let freshnessClass = "fresh";
  if (diffMinutes > 30) freshnessClass = "stale";
  else if (diffMinutes > 10) freshnessClass = "aging";

  indicator.className = `freshness-indicator ${freshnessClass}`;
  indicator.textContent = relativeTime;
  indicator.title = `Data collected: ${new Date(timestamp).toLocaleString()}`;

  return indicator;
};

// Quick Tips content for each page
const QUICK_TIPS = {
  "index.html": {
    title: "Fleet Status Tips",
    tips: [
      "Click on any system row to see detailed information",
      "Systems marked <strong>DEGRADED</strong> are operational but may have reduced performance",
      "Check the <strong>Queue Health</strong> page before submitting large jobs"
    ],
  },
  "queues.html": {
    title: "Queue Health Tips",
    tips: [
      "Queues with <strong>backlog</strong> have pending jobs waiting for resources",
      "Use <strong>debug</strong> queues for quick tests - they have shorter wait times",
      "The <strong>Core demand</strong> bar shows how much of the requested resources are being satisfied"
    ],
  },
  "quota.html": {
    title: "Quota Usage Tips",
    tips: [
      "Allocations are measured in <strong>core-hours</strong> (1 core × 1 hour = 1 core-hour)",
      "Request more allocation time before your remaining hours drop below 10%",
      "Check <strong>subprojects</strong> to see how hours are distributed across sub-accounts"
    ],
  },
  "storage.html": {
    title: "Storage Tips",
    tips: [
      "<strong>$SCRATCH</strong> files are automatically deleted after 30-60 days - don't store important data there",
      "Keep <strong>$HOME</strong> under 80% - it's your only backed-up storage",
      "Move completed project data to <strong>archive</strong> storage to free up working space"
    ],
  },
  "insights.html": {
    title: "Insights Tips",
    tips: [
      "Insights are sorted by severity - address <strong>warnings</strong> first",
      "Click the system name to navigate directly to that system's details",
      "Insights refresh automatically every 2 minutes"
    ],
  },
};

const TIPS_DISMISSED_KEY = "hpc-status-tips-dismissed";

// Check if tips have been dismissed for a page
const isTipsDismissed = (pageKey) => {
  try {
    const dismissed = JSON.parse(window.localStorage.getItem(TIPS_DISMISSED_KEY) || "{}");
    return dismissed[pageKey] === true;
  } catch {
    return false;
  }
};

// Mark tips as dismissed for a page
const dismissTips = (pageKey) => {
  try {
    const dismissed = JSON.parse(window.localStorage.getItem(TIPS_DISMISSED_KEY) || "{}");
    dismissed[pageKey] = true;
    window.localStorage.setItem(TIPS_DISMISSED_KEY, JSON.stringify(dismissed));
  } catch {
    // Ignore storage errors
  }
};

// Initialize quick tips for the current page
export const initQuickTips = (insertAfterSelector = ".cards") => {
  // Determine current page
  const path = window.location.pathname;
  const pageName = path.substring(path.lastIndexOf("/") + 1) || "index.html";
  const pageKey = pageName.replace(".html", "");

  const tips = QUICK_TIPS[pageName];
  if (!tips || isTipsDismissed(pageKey)) {
    return;
  }

  const insertAfter = document.querySelector(insertAfterSelector);
  if (!insertAfter) {
    return;
  }

  // Create tips panel
  const panel = document.createElement("div");
  panel.className = "quick-tips";
  panel.id = "quick-tips";
  panel.innerHTML = `
    <div class="quick-tips-header">
      <h4>${tips.title}</h4>
      <button class="quick-tips-dismiss" aria-label="Dismiss tips" title="Don't show again">&times;</button>
    </div>
    <div class="quick-tips-content">
      ${tips.tips.map(tip => `<p>• ${tip}</p>`).join("")}
    </div>
  `;

  // Insert after the cards section
  insertAfter.parentNode.insertBefore(panel, insertAfter.nextSibling);

  // Bind dismiss button
  const dismissBtn = panel.querySelector(".quick-tips-dismiss");
  if (dismissBtn) {
    dismissBtn.addEventListener("click", () => {
      dismissTips(pageKey);
      panel.remove();
    });
  }
};

// Create an improved empty state element
export const createEmptyState = ({ icon = "📭", title, message, actionText, actionHref }) => {
  const container = document.createElement("div");
  container.className = "empty-state";

  let actionHtml = "";
  if (actionText && actionHref) {
    actionHtml = `
      <div class="empty-state-action">
        <a href="${actionHref}" class="ghost-btn">${actionText}</a>
      </div>
    `;
  }

  container.innerHTML = `
    <div class="empty-state-icon">${icon}</div>
    <h3 class="empty-state-title">${title}</h3>
    <p class="empty-state-message">${message}</p>
    ${actionHtml}
  `;

  return container;
};
