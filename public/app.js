const pageUrl = new URL(window.location.href);
const basePath = pageUrl.pathname.endsWith("/") ? pageUrl.pathname : pageUrl.pathname.replace(/[^/]+$/, "");
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

const state = {
  systems: [],
  summary: {},
  meta: {},
  loading: false,
  usingApi: false,
  retryHandle: null,
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
};

const statusClass = (status) => {
  const normalized = (status || "UNKNOWN").toUpperCase();
  if (normalized === "UP") return "up";
  if (normalized === "DOWN") return "down";
  if (normalized === "DEGRADED" || normalized === "MAINTENANCE") return "degraded";
  return "unknown";
};

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
  state.systems = data.systems || [];
  state.summary = data.summary || {};
  state.meta = data.meta || {};
  updateSummary();
  populateFilters();
  renderTable();
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
      return `<tr>
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
}

function debounce(fn, delay = 200) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}

registerEvents();
loadData();
setInterval(() => loadData({ showLoading: false, silentFallback: true }), 3 * 60 * 1000);
