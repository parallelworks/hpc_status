import { clusterPagesEnabled } from "./page-utils.js";

const featureFlags = {
  clusterPages: clusterPagesEnabled(),
};

const navElement = document.querySelector("[data-cluster-nav]");
if (navElement && !featureFlags.clusterPages) {
  navElement.remove();
}

function deriveBasePath(pathname) {
  const path = pathname || "/";
  if (path.endsWith("/")) {
    return path;
  }
  const lastSlash = path.lastIndexOf("/");
  const segment = lastSlash >= 0 ? path.slice(lastSlash + 1) : path;
  const prefix = lastSlash >= 0 ? path.slice(0, lastSlash + 1) : "/";
  if (segment.includes(".")) {
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

const STATUS_URL = new URL("api/status", apiBase).toString();
const REFRESH_URL = new URL("api/refresh", apiBase).toString();
const STATIC_DATA_URL = new URL("data/status.json", defaultApiBase).toString();

const THEME_STORAGE_KEY = "hpc-status-theme";
const detailCache = new Map();
const navigationState = {
  pendingSlug: getSlugFromLocation(),
};

const state = {
  systems: [],
  summary: {},
  meta: {},
  loading: false,
  usingApi: false,
  retryHandle: null,
  activeSlug: null,
};

const elements = {
  totalSystems: document.getElementById("total-systems"),
  fleetUptime: document.getElementById("fleet-uptime"),
  degradedCount: document.getElementById("degraded-count"),
  lastUpdated: document.getElementById("last-updated"),
  statusLegend: document.getElementById("status-breakdown"),
  dsrcLegend: document.getElementById("dsrc-breakdown"),
  schedulerLegend: document.getElementById("scheduler-breakdown"),
  tableBody: document.getElementById("systems-body"),
  tableCount: document.getElementById("table-count"),
  searchInput: document.getElementById("search-input"),
  statusFilter: document.getElementById("status-filter"),
  dsrcFilter: document.getElementById("dsrc-filter"),
  refreshBtn: document.getElementById("refresh-btn"),
  themeToggle: document.getElementById("theme-toggle"),
  themeLabel: document.querySelector("#theme-toggle .theme-label"),
  themeIcon: document.querySelector("#theme-toggle .theme-icon"),
  overview: document.getElementById("overview-content"),
  detailPanel: document.getElementById("system-detail"),
  detailBack: document.getElementById("detail-back"),
  detailTitle: document.getElementById("detail-title"),
  detailHeading: document.getElementById("detail-heading"),
  detailStatus: document.getElementById("detail-status"),
  detailObserved: document.getElementById("detail-observed"),
  detailDsrc: document.getElementById("detail-dsrc"),
  detailLogin: document.getElementById("detail-login"),
  detailScheduler: document.getElementById("detail-scheduler"),
  detailMarkdown: document.getElementById("detail-markdown"),
  detailSource: document.getElementById("detail-source"),
};

function safeGetStoredTheme() {
  try {
    return window.localStorage.getItem(THEME_STORAGE_KEY);
  } catch (err) {
    console.warn("Unable to read theme from storage", err);
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

const statusClass = (status) => {
  const normalized = (status || "UNKNOWN").toUpperCase();
  if (normalized === "UP") return "up";
  if (normalized === "DOWN") return "down";
  if (normalized === "DEGRADED" || normalized === "MAINTENANCE") return "degraded";
  return "unknown";
};

const slugifySystem = (name) => (name || "").toLowerCase().replace(/[^a-z0-9]+/g, "");

const percent = (value) => `${Math.round((value || 0) * 100)}%`;

function setTableLoading(message = "Loading latest data…") {
  state.loading = true;
  elements.tableCount.textContent = "Loading…";
  elements.tableBody.innerHTML = `<tr><td colspan="6" class="placeholder">${message}</td></tr>`;
}

function clearTableLoading() {
  state.loading = false;
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(`${url} returned ${response.status}${text ? ` - ${text}` : ""}`);
  }
  return response.json();
}

async function loadData({ silentFallback = false, showLoading = true } = {}) {
  if (showLoading) {
    setTableLoading("Loading latest data…");
  }
  try {
    const data = await fetchJson(`${STATUS_URL}?t=${Date.now()}`);
    ingestData(data);
    state.usingApi = true;
    showStatusMessage("");
    clearScheduledRetry();
  } catch (apiErr) {
    console.warn("API load failed, attempting static data.", apiErr);
    try {
      const data = await fetchJson(`${STATIC_DATA_URL}?t=${Date.now()}`);
      ingestData(data);
      state.usingApi = false;
      if (!silentFallback) {
        showStatusMessage("Loaded cached status snapshot. API unavailable.");
      }
      scheduleApiRetry();
    } catch (staticErr) {
      console.error(staticErr);
      setTablePlaceholder(`Unable to load data (${staticErr.message}).`, { countLabel: "--" });
      scheduleApiRetry();
    }
  } finally {
    clearTableLoading();
  }
}

function ingestData(data) {
  const systems = data.systems || [];
  state.systems = systems.map((row, idx) => ({
    ...row,
    slug: slugifySystem(row.system) || `system${idx + 1}`,
    __index: idx,
  }));
  state.summary = data.summary || {};
  state.meta = data.meta || {};
  updateSummary();
  populateFilters();
  renderTable();
  updateDetailViewState();
  invalidateMarkdownCache();
}

function updateSummary() {
  const { summary, meta, systems } = state;
  elements.totalSystems.textContent = summary.total_systems ?? systems.length;
  elements.fleetUptime.textContent = summary.uptime_ratio !== undefined ? percent(summary.uptime_ratio) : "--";
  const nonUp = (summary.status_counts && Object.entries(summary.status_counts)
    .filter(([key]) => key !== "UP")
    .reduce((sum, [, val]) => sum + val, 0)) || 0;
  elements.degradedCount.textContent = nonUp;
  elements.lastUpdated.textContent = meta.generated_at || "--";

  buildLegend(elements.statusLegend, summary.status_counts);
  buildLegend(elements.dsrcLegend, summary.dsrc_counts, true);
  buildLegend(elements.schedulerLegend, summary.scheduler_counts, true);
}

function buildLegend(container, counts = {}, uppercase = false) {
  if (!container) {
    return;
  }
  const entries = Object.entries(counts || {}).sort((a, b) => b[1] - a[1]);
  container.innerHTML = "";
  if (!entries.length) {
    container.innerHTML = "<li>No data</li>";
    return;
  }
  for (const [label, value] of entries) {
    const item = document.createElement("li");
    const name = uppercase ? label.toUpperCase() : label;
    item.innerHTML = `<span>${name}</span>${value}`;
    container.appendChild(item);
  }
}

function populateFilters() {
  const statuses = Array.from(new Set(state.systems.map((r) => (r.status || "UNKNOWN").toUpperCase()))).sort();
  const dsrcs = Array.from(new Set(state.systems.map((r) => (r.dsrc || "UNKNOWN").toUpperCase()))).sort();
  setOptions(elements.statusFilter, statuses);
  setOptions(elements.dsrcFilter, dsrcs);
}

function setOptions(select, values) {
  const current = select.value;
  select.innerHTML = `<option value="">All</option>`;
  values.forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    select.appendChild(option);
  });
  if (values.includes(current)) {
    select.value = current;
  }
}

function renderTable() {
  const rows = filteredRows();
  elements.tableCount.textContent = `${rows.length} result${rows.length === 1 ? "" : "s"}`;
  if (!rows.length) {
    elements.tableBody.innerHTML = `<tr><td colspan="6" class="placeholder">No systems match the filters.</td></tr>`;
    return;
  }
  elements.tableBody.innerHTML = rows
    .map((row) => {
      const statusText = row.status || "UNKNOWN";
      const slugAttr = row.slug ? ` data-slug="${row.slug}"` : "";
      return `<tr${slugAttr} tabindex="0" role="button">
        <td>
          <div class="system-name">${row.system || "(unnamed)"}</div>
          <div class="system-meta">${row.raw_alt || ""}</div>
        </td>
        <td><span class="badge ${statusClass(statusText)}">${statusText}</span></td>
        <td>${row.dsrc || "—"}</td>
        <td>${row.login || "—"}</td>
        <td>${(row.scheduler || "—").toUpperCase()}</td>
        <td>${row.observed_at || "—"}</td>
      </tr>`;
    })
    .join("");
}

function filteredRows() {
  const query = (elements.searchInput.value || "").toLowerCase();
  const statusFilter = (elements.statusFilter.value || "").toUpperCase();
  const dsrcFilter = (elements.dsrcFilter.value || "").toUpperCase();

  return state.systems.filter((row) => {
    const matchesSearch =
      !query ||
      (row.system && row.system.toLowerCase().includes(query)) ||
      (row.login && row.login.toLowerCase().includes(query));

    const matchesStatus = !statusFilter || (row.status || "").toUpperCase() === statusFilter;
    const matchesDsrc = !dsrcFilter || (row.dsrc || "").toUpperCase() === dsrcFilter;
    return matchesSearch && matchesStatus && matchesDsrc;
  });
}

function setTablePlaceholder(message, { countLabel = "0 results" } = {}) {
  elements.tableCount.textContent = countLabel;
  elements.tableBody.innerHTML = `<tr><td colspan="6" class="placeholder">${message}</td></tr>`;
}

function showStatusMessage(text) {
  const note = document.querySelector(".data-status-msg");
  if (!note && !text) return;
  let el = note;
  if (!el) {
    el = document.createElement("p");
    el.className = "data-status-msg";
    el.style.margin = "0";
    el.style.color = "var(--muted)";
    const parent = document.querySelector(".table-head");
    parent?.appendChild(el);
  }
  el.textContent = text;
  el.style.display = text ? "block" : "none";
}

function scheduleApiRetry() {
  if (state.retryHandle) {
    return;
  }
  state.retryHandle = setTimeout(() => {
    state.retryHandle = null;
    loadData({ showLoading: false, silentFallback: true });
  }, 60000);
}

function clearScheduledRetry() {
  if (state.retryHandle) {
    clearTimeout(state.retryHandle);
    state.retryHandle = null;
  }
}

async function triggerRefresh() {
  const btn = elements.refreshBtn;
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Refreshing…";
  btn.removeAttribute("title");
  setTableLoading("Refreshing from source…");
  try {
    const response = await fetch(REFRESH_URL, { method: "POST" });
    const info = await response.json().catch(() => ({}));
    if (!response.ok || info.ok === false) {
      throw new Error(info.detail || "Refresh failed");
    }
    await loadData({ showLoading: false });
    showStatusMessage("Updated via manual refresh.");
  } catch (err) {
    console.error(err);
    btn.title = err.message;
    showStatusMessage(err.message);
    await loadData({ silentFallback: true, showLoading: false });
  } finally {
    clearTableLoading();
    btn.disabled = false;
    btn.textContent = original;
  }
}

function registerEvents() {
  elements.searchInput.addEventListener("input", debounce(renderTable, 150));
  elements.statusFilter.addEventListener("change", renderTable);
  elements.dsrcFilter.addEventListener("change", renderTable);
  elements.refreshBtn.addEventListener("click", () => triggerRefresh());
  elements.themeToggle?.addEventListener("click", () => {
    const current = document.documentElement.dataset.theme || resolveDefaultTheme();
    const next = current === "dark" ? "light" : "dark";
    applyTheme(next);
  });
  elements.tableBody.addEventListener("click", onTableRowClick);
  elements.tableBody.addEventListener("keydown", onTableRowKeydown);
  elements.detailBack?.addEventListener("click", () => closeDetail());
  window.addEventListener("popstate", () => syncViewWithLocation());
}

function onTableRowClick(event) {
  const row = event.target.closest("tr[data-slug]");
  if (!row) {
    return;
  }
  const slug = row.getAttribute("data-slug");
  if (slug) {
    showSystemDetail(slug);
  }
}

function onTableRowKeydown(event) {
  if (event.key !== "Enter" && event.key !== " ") {
    return;
  }
  const row = event.target.closest("tr[data-slug]");
  if (!row) {
    return;
  }
  event.preventDefault();
  const slug = row.getAttribute("data-slug");
  if (slug) {
    showSystemDetail(slug);
  }
}

function debounce(fn, delay = 200) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}

function showSystemDetail(slug, { pushState = true } = {}) {
  if (!slug) {
    return;
  }
  navigationState.pendingSlug = null;
  const system = state.systems.find((row) => row.slug === slug);
  if (!system) {
    showStatusMessage("Selected system is not available in the latest snapshot.");
    closeDetail({ pushState: false });
    if (pushState) {
      updateLocationForSlug(null, { replace: true });
    }
    return;
  }
  state.activeSlug = slug;
  populateDetailHeader(system);
  toggleDetailView(true);
  setDetailLoading();
  loadSystemMarkdown(slug);
  if (pushState) {
    const currentSlug = getSlugFromLocation();
    updateLocationForSlug(slug, { replace: currentSlug === slug });
  }
}

function closeDetail({ pushState = true } = {}) {
  toggleDetailView(false);
  state.activeSlug = null;
  if (pushState) {
    updateLocationForSlug(null, { replace: true });
  }
  resetDetailPanel();
}

function toggleDetailView(showDetail) {
  if (!elements.detailPanel || !elements.overview) {
    return;
  }
  if (showDetail) {
    elements.overview.setAttribute("hidden", "hidden");
    elements.detailPanel.removeAttribute("hidden");
  } else {
    elements.detailPanel.setAttribute("hidden", "hidden");
    elements.overview.removeAttribute("hidden");
  }
}

function resetDetailPanel(message = "Select a system to load its markdown briefing.") {
  if (!elements.detailPanel) return;
  if (elements.detailTitle) {
    elements.detailTitle.textContent = "Select a system";
  }
  if (elements.detailHeading) {
    elements.detailHeading.textContent = "";
  }
  if (elements.detailStatus) {
    elements.detailStatus.className = "badge";
    elements.detailStatus.textContent = "--";
  }
  if (elements.detailObserved) {
    elements.detailObserved.textContent = "Observed --";
  }
  if (elements.detailDsrc) {
    elements.detailDsrc.textContent = "--";
  }
  if (elements.detailLogin) {
    elements.detailLogin.textContent = "--";
  }
  if (elements.detailScheduler) {
    elements.detailScheduler.textContent = "--";
  }
  if (elements.detailSource) {
    elements.detailSource.setAttribute("hidden", "hidden");
    elements.detailSource.removeAttribute("href");
  }
  if (elements.detailMarkdown) {
    elements.detailMarkdown.classList.add("placeholder");
    elements.detailMarkdown.textContent = message;
  }
}

function setDetailLoading(message = "Loading system briefing…") {
  if (elements.detailMarkdown) {
    elements.detailMarkdown.classList.add("placeholder");
    elements.detailMarkdown.textContent = message;
  }
}

function populateDetailHeader(system) {
  if (!system) return;
  if (elements.detailTitle) {
    elements.detailTitle.textContent = system.system || "(unnamed)";
  }
  if (elements.detailHeading) {
    elements.detailHeading.textContent = system.raw_alt || "";
  }
  if (elements.detailStatus) {
    const klass = statusClass(system.status);
    elements.detailStatus.className = `badge ${klass}`;
    elements.detailStatus.textContent = system.status || "UNKNOWN";
  }
  if (elements.detailObserved) {
    elements.detailObserved.textContent = system.observed_at ? `Observed ${system.observed_at}` : "Observed --";
  }
  if (elements.detailDsrc) {
    elements.detailDsrc.textContent = (system.dsrc || "—").toUpperCase();
  }
  if (elements.detailLogin) {
    elements.detailLogin.textContent = system.login || "—";
  }
  if (elements.detailScheduler) {
    elements.detailScheduler.textContent = (system.scheduler || "—").toUpperCase();
  }
  if (elements.detailSource) {
    const href = system.source_url || state.meta.source_url || "";
    if (href) {
      elements.detailSource.href = href;
      elements.detailSource.removeAttribute("hidden");
    } else {
      elements.detailSource.setAttribute("hidden", "hidden");
      elements.detailSource.removeAttribute("href");
    }
  }
}

async function loadSystemMarkdown(slug) {
  try {
    const markdown = await fetchSystemMarkdown(slug);
    if (!elements.detailMarkdown) {
      return;
    }
    if (state.activeSlug !== slug) {
      return;
    }
    if (!markdown.trim()) {
      elements.detailMarkdown.classList.add("placeholder");
      elements.detailMarkdown.textContent = "No detailed notes available for this system yet.";
      return;
    }
    elements.detailMarkdown.classList.remove("placeholder");
    elements.detailMarkdown.innerHTML = renderMarkdown(markdown);
  } catch (err) {
    console.error(err);
    if (elements.detailMarkdown) {
      elements.detailMarkdown.classList.add("placeholder");
      elements.detailMarkdown.textContent = err.message || "Unable to load system briefing.";
    }
  }
}

async function fetchSystemMarkdown(slug) {
  if (detailCache.has(slug)) {
    return detailCache.get(slug);
  }
  const url = new URL(`api/system-markdown/${encodeURIComponent(slug)}`, apiBase);
  url.searchParams.set("t", Date.now());
  const response = await fetch(url.toString(), { cache: "no-store" });
  if (!response.ok) {
    const message = response.status === 404
      ? "Detailed briefing is not available for this system yet."
      : "Unable to load system briefing.";
    throw new Error(message);
  }
  const payload = await response.json();
  const content = payload.content || "";
  detailCache.set(slug, content);
  return content;
}

function updateDetailViewState() {
  if (navigationState.pendingSlug) {
    const slug = navigationState.pendingSlug;
    const next = state.systems.find((row) => row.slug === slug);
    navigationState.pendingSlug = null;
    if (next) {
      showSystemDetail(slug, { pushState: false });
    } else if (state.systems.length) {
      showStatusMessage(`System "${slug}" is not available in this snapshot.`);
      closeDetail({ pushState: false });
    }
    return;
  }
  if (state.activeSlug) {
    const current = state.systems.find((row) => row.slug === state.activeSlug);
    if (current) {
      populateDetailHeader(current);
    } else {
      showStatusMessage("Selected system is no longer present in the latest snapshot.");
      closeDetail({ pushState: true });
    }
  }
}

function invalidateMarkdownCache() {
  detailCache.clear();
  if (state.activeSlug) {
    setDetailLoading("Refreshing system briefing…");
    loadSystemMarkdown(state.activeSlug);
  }
}

function syncViewWithLocation() {
  const slug = getSlugFromLocation();
  if (!slug) {
    navigationState.pendingSlug = null;
    closeDetail({ pushState: false });
    return;
  }
  navigationState.pendingSlug = slug;
  if (state.systems.length) {
    showSystemDetail(slug, { pushState: false });
  }
}

function updateLocationForSlug(slug, { replace = false } = {}) {
  const url = new URL(window.location.href);
  if (slug) {
    url.searchParams.set("system", slug);
  } else {
    url.searchParams.delete("system");
  }
  const method = replace ? "replaceState" : "pushState";
  window.history[method]({ slug: slug || null }, "", url);
}

function getSlugFromLocation() {
  const params = new URLSearchParams(window.location.search);
  const raw = params.get("system");
  if (!raw) {
    return null;
  }
  const normalized = raw.toLowerCase().replace(/[^a-z0-9]/g, "");
  return normalized || null;
}

function renderMarkdown(markdown) {
  const normalized = (markdown || "").replace(/\r\n/g, "\n").trim();
  if (!normalized) {
    return '<p class="placeholder">No detailed notes available for this system yet.</p>';
  }
  const lines = normalized.split("\n");
  const blocks = [];
  let paragraph = [];
  let listState = null;
  let inCode = false;
  let codeLang = "";
  let codeLines = [];

  const flushParagraph = () => {
    if (!paragraph.length) return;
    blocks.push(`<p>${inlineMarkdown(paragraph.join(" ").trim())}</p>`);
    paragraph = [];
  };

  const flushList = () => {
    if (!listState) return;
    blocks.push(`<${listState.type}>${listState.items.join("")}</${listState.type}>`);
    listState = null;
  };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const trimmed = line.trim();

    if (inCode) {
      if (trimmed.startsWith("```")) {
        blocks.push(`<pre><code${codeLang ? ` class="language-${codeLang}"` : ""}>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
        inCode = false;
        codeLines = [];
        codeLang = "";
        continue;
      }
      codeLines.push(line);
      continue;
    }

    const fenceMatch = trimmed.match(/^```(\w+)?\s*$/);
    if (fenceMatch) {
      flushParagraph();
      flushList();
      inCode = true;
      codeLang = fenceMatch[1] || "";
      codeLines = [];
      continue;
    }

    if (!trimmed) {
      flushParagraph();
      flushList();
      continue;
    }

    const headingMatch = trimmed.match(/^(#{1,6})\s+(.*)$/);
    if (headingMatch) {
      flushParagraph();
      flushList();
      const level = headingMatch[1].length;
      blocks.push(`<h${level}>${inlineMarkdown(headingMatch[2].trim())}</h${level}>`);
      continue;
    }

    const imageMatch = trimmed.match(/^!\[([^\]]*)\]\(([^)]+)\)\s*$/);
    if (imageMatch) {
      flushParagraph();
      flushList();
      const alt = escapeHtml(imageMatch[1] || "");
      const src = escapeHtml(imageMatch[2] || "");
      const altAttr = alt || "System image";
      let figure = `<figure class="markdown-figure"><img src="${src}" alt="${altAttr}">`;
      if (alt) {
        figure += `<figcaption>${alt}</figcaption>`;
      }
      figure += "</figure>";
      blocks.push(figure);
      continue;
    }

    if (trimmed.startsWith("|") && isTableDivider(lines[i + 1] || "")) {
      flushParagraph();
      flushList();
      const tableLines = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        tableLines.push(lines[i]);
        i += 1;
      }
      i -= 1;
      blocks.push(renderMarkdownTable(tableLines));
      continue;
    }

    const listMatch = trimmed.match(/^([-*+]|\d+\.)\s+(.*)$/);
    if (listMatch) {
      flushParagraph();
      const type = /^\d+\.$/.test(listMatch[1]) ? "ol" : "ul";
      if (!listState || listState.type !== type) {
        flushList();
        listState = { type, items: [] };
      }
      listState.items.push(`<li>${inlineMarkdown(listMatch[2].trim())}</li>`);
      continue;
    }

    paragraph.push(line);
  }

  flushParagraph();
  flushList();
  if (inCode) {
    blocks.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
  }
  return blocks.join("\n");
}

function inlineMarkdown(text) {
  let html = escapeHtml(text);
  html = html.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (_, alt, src) => {
    const safeAlt = alt || "System image";
    return `<img src="${src}" alt="${safeAlt}">`;
  });
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  html = html.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  html = html.replace(/_([^_]+)_/g, "<em>$1</em>");
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  return html;
}

function escapeHtml(str) {
  return (str || "").replace(/[&<>"']/g, (ch) => {
    switch (ch) {
      case "&":
        return "&amp;";
      case "<":
        return "&lt;";
      case ">":
        return "&gt;";
      case '"':
        return "&quot;";
      case "'":
        return "&#39;";
      default:
        return ch;
    }
  });
}

function renderMarkdownTable(lines) {
  if (!lines.length) {
    return "";
  }
  const headerCells = parseTableRow(lines[0]);
  const bodyRows = lines.slice(2).map((row) => parseTableRow(row));
  const header = headerCells.map((cell) => `<th>${inlineMarkdown(cell)}</th>`).join("");
  const body = bodyRows
    .map((row) => `<tr>${row.map((cell) => `<td>${inlineMarkdown(cell)}</td>`).join("")}</tr>`)
    .join("");
  return `<div class="table-scroll"><table><thead><tr>${header}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function parseTableRow(line) {
  return line
    .trim()
    .replace(/^\||\|$/g, "")
    .split("|")
    .map((cell) => cell.trim());
}

function isTableDivider(line) {
  const trimmed = line.trim();
  if (!trimmed.startsWith("|")) {
    return false;
  }
  return /^(\|\s*:?-{3,}:?\s*)+\|?$/.test(trimmed);
}

applyTheme(safeGetStoredTheme() || resolveDefaultTheme(), { persist: false });
registerEvents();
loadData();
setInterval(() => loadData({ showLoading: false, silentFallback: true }), 3 * 60 * 1000);
