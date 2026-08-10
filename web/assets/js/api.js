/**
 * API reference page.
 *
 * The endpoint list is fetched from /api/endpoints rather than written
 * here, so this page describes the deployment it is served from. "Try it"
 * runs the real request against that deployment: a live response beats a
 * hand-written example that may not match what you actually get.
 */

import {
  buildApiUrl,
  getBaseUrl,
  initThemeToggle,
  initHelpPanel,
  initBrand,
  initNav,
  initQuickTips,
} from "./page-utils.js";

const ENDPOINTS_URL = buildApiUrl("api/endpoints").toString();

const dom = {};
const state = { endpoints: [], groups: [], baseUrl: "" };

const escapeHtml = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]
  );

const setStatus = (message, variant = "info") => {
  if (!dom.status) return;
  dom.status.hidden = !message;
  dom.status.textContent = message || "";
  dom.status.dataset.variant = variant;
};

/**
 * The absolute URL a caller outside the browser would use.
 *
 * The base is this document, so an empty relative path resolves to the
 * document itself — which is how the base-URL panel came to advertise
 * ".../api.html". "." asks for the directory it lives in instead, which
 * is what callers actually append to.
 */
const absoluteUrl = (path) =>
  new URL(path.replace(/^\//, "") || ".", getBaseUrl()).toString();

const paramRows = (params) => {
  if (!params?.length) return "";
  return `
    <table class="api-params">
      <thead>
        <tr><th>Parameter</th><th>Type</th><th>Default</th><th>Meaning</th></tr>
      </thead>
      <tbody>
        ${params
          .map(
            (param) => `<tr>
              <td><code>${escapeHtml(param.name)}</code></td>
              <td>${escapeHtml(param.type)}</td>
              <td>${param.default === undefined ? "—" : `<code>${escapeHtml(param.default)}</code>`}</td>
              <td>${escapeHtml(param.description)}</td>
            </tr>`
          )
          .join("")}
      </tbody>
    </table>`;
};

const endpointCard = (entry, index) => {
  // Path parameters need a value before the request means anything.
  const placeholder = /\{([^}]+)\}/.exec(entry.path);
  const query = (entry.params || [])
    .filter((param) => param.default !== undefined)
    .map((param) => `${param.name}=${param.default}`)
    .join("&");

  return `
    <article class="api-endpoint" id="ep-${index}">
      <header class="api-endpoint-head">
        <span class="api-method api-method--${entry.method.toLowerCase()}">${entry.method}</span>
        <code class="api-path">${escapeHtml(entry.path)}</code>
        <span class="api-summary">${escapeHtml(entry.summary)}</span>
      </header>
      <p class="api-description">${escapeHtml(entry.description)}</p>
      ${entry.notes ? `<p class="api-note">${escapeHtml(entry.notes)}</p>` : ""}
      ${paramRows(entry.params)}
      ${entry.returns?.length
        ? `<p class="api-returns"><span>${
            entry.shape === "array" ? "Array of objects with" : "Returns"
          }</span> ${entry.returns
            .map((key) => `<code>${escapeHtml(key)}</code>`)
            .join(" ")}</p>`
        : ""}
      <div class="api-actions">
        ${placeholder
          ? `<label class="api-arg">
               <span>${escapeHtml(placeholder[1])}</span>
               <input type="text" data-arg="${index}" placeholder="e.g. narwhal" />
             </label>`
          : ""}
        ${query
          ? `<label class="api-arg api-arg--wide">
               <span>query</span>
               <input type="text" data-query="${index}" value="${escapeHtml(query)}" />
             </label>`
          : ""}
        <button type="button" class="ghost-btn ghost-btn--sm" data-try="${index}"
          ${entry.method === "POST" ? 'data-confirm="1"' : ""}>
          ${entry.method === "POST" ? "Run (changes state)" : "Try it"}
        </button>
        <button type="button" class="ghost-btn ghost-btn--sm" data-curl="${index}">Copy curl</button>
      </div>
      <pre class="api-response" data-response="${index}" hidden></pre>
    </article>`;
};

const render = () => {
  dom.list.innerHTML = state.groups
    .map((group) => {
      const cards = state.endpoints
        .map((entry, index) => ({ entry, index }))
        .filter(({ entry }) => entry.group === group);
      if (!cards.length) return "";
      return `
        <section class="api-group">
          <h2>${escapeHtml(group)}</h2>
          ${cards.map(({ entry, index }) => endpointCard(entry, index)).join("")}
        </section>`;
    })
    .join("");
};

/** Build the URL for a card, substituting the path argument and query. */
const requestUrlFor = (index) => {
  const entry = state.endpoints[index];
  let path = entry.path;
  const arg = dom.list.querySelector(`[data-arg="${index}"]`);
  if (arg) {
    const value = (arg.value || "").trim();
    if (!value) return null;
    path = path.replace(/\{[^}]+\}/, encodeURIComponent(value));
  }
  const queryInput = dom.list.querySelector(`[data-query="${index}"]`);
  const query = queryInput ? (queryInput.value || "").trim() : "";
  return absoluteUrl(path) + (query ? `?${query}` : "");
};

const showResponse = (index, text, ok = true) => {
  const target = dom.list.querySelector(`[data-response="${index}"]`);
  if (!target) return;
  target.hidden = false;
  target.classList.toggle("is-error", !ok);
  target.textContent = text;
};

const runRequest = async (index) => {
  const entry = state.endpoints[index];
  const url = requestUrlFor(index);
  if (!url) {
    showResponse(index, "Fill in the path argument first.", false);
    return;
  }
  showResponse(index, "Requesting…");
  const started = performance.now();
  try {
    const response = await fetch(url, {
      method: entry.method,
      cache: "no-store",
    });
    const elapsed = Math.round(performance.now() - started);
    const body = await response.text();
    let pretty = body;
    try {
      pretty = JSON.stringify(JSON.parse(body), null, 2);
    } catch {
      /* not JSON — app-config.js, or an error page */
    }
    // Long payloads are trimmed: this is a reference page, not a data dump.
    const trimmed =
      pretty.length > 4000
        ? `${pretty.slice(0, 4000)}\n\n… truncated (${pretty.length.toLocaleString()} characters total)`
        : pretty;
    showResponse(
      index,
      `HTTP ${response.status} · ${elapsed} ms · ${body.length.toLocaleString()} bytes\n\n${trimmed}`,
      response.ok
    );
  } catch (err) {
    showResponse(index, `Request failed: ${err.message}`, false);
  }
};

/**
 * navigator.clipboard only exists in a secure context, and these dashboards
 * are routinely served over plain HTTP inside a network, so the modern API
 * is the fast path rather than the only one.
 */
const copyText = async (text, label) => {
  try {
    if (window.isSecureContext && navigator.clipboard) {
      await navigator.clipboard.writeText(text);
    } else {
      const scratch = document.createElement("textarea");
      scratch.value = text;
      scratch.setAttribute("readonly", "");
      scratch.style.position = "fixed";
      scratch.style.opacity = "0";
      document.body.appendChild(scratch);
      scratch.select();
      const copied = document.execCommand("copy");
      scratch.remove();
      if (!copied) throw new Error("execCommand refused");
    }
    setStatus(`${label} copied to the clipboard.`);
    setTimeout(() => setStatus(""), 2500);
  } catch {
    setStatus(`Could not copy — select it by hand: ${text}`, "error");
  }
};

const bindEvents = () => {
  dom.list.addEventListener("click", (event) => {
    const tryBtn = event.target.closest("[data-try]");
    if (tryBtn) {
      const index = Number(tryBtn.dataset.try);
      // POST /api/refresh makes the server go and collect; ask first.
      if (tryBtn.dataset.confirm && !window.confirm(
        "This runs a real refresh against the collectors. Continue?"
      )) {
        return;
      }
      runRequest(index);
      return;
    }
    const curlBtn = event.target.closest("[data-curl]");
    if (curlBtn) {
      const index = Number(curlBtn.dataset.curl);
      const entry = state.endpoints[index];
      const url = requestUrlFor(index);
      if (!url) {
        showResponse(index, "Fill in the path argument first.", false);
        return;
      }
      const method = entry.method === "POST" ? "-X POST " : "";
      copyText(`curl -s ${method}"${url}"`, "curl command");
    }
  });

  dom.copyBase?.addEventListener("click", () => copyText(state.baseUrl, "Base URL"));
};

/**
 * Fill the path-argument boxes with an identifier this deployment actually
 * has, so "Try it" returns data on the first press instead of a 404. The
 * two placeholders live in different id spaces: {system} is a fleet slug,
 * {cluster} is a key of the connected-cluster map.
 */
const hydrateExamples = async () => {
  const inputs = [...dom.list.querySelectorAll("[data-arg]")];
  if (!inputs.length) return;

  const firstOf = async (path, pick) => {
    try {
      const response = await fetch(absoluteUrl(path), { cache: "no-store" });
      if (!response.ok) return "";
      return pick(await response.json()) || "";
    } catch {
      return "";
    }
  };

  const slugify = (name) => String(name || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  const [system, cluster] = await Promise.all([
    firstOf("/api/fleet/summary", (data) => slugify((data.systems || [])[0]?.system)),
    // /api/cluster-usage answers with a list, one entry per cluster.
    firstOf("/api/cluster-usage", (data) =>
      slugify((Array.isArray(data) ? data : [])[0]?.cluster_metadata?.name)
    ),
  ]);

  for (const input of inputs) {
    const name = input.closest(".api-arg")?.querySelector("span")?.textContent || "";
    const example = name === "cluster" ? cluster || system : system || cluster;
    if (example && !input.value) input.value = example;
  }
};

const load = async () => {
  try {
    const response = await fetch(`${ENDPOINTS_URL}?t=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    state.endpoints = payload.endpoints || [];
    state.groups = payload.groups || [];
    state.baseUrl = absoluteUrl("/");
    dom.baseUrl.textContent = state.baseUrl;
    render();
    setStatus("");
    hydrateExamples();
  } catch (err) {
    console.error("Unable to load the endpoint catalog", err);
    dom.list.innerHTML = "";
    setStatus(
      `Could not load the endpoint catalog (${err.message}). The API reference is ` +
        "also in docs/api.md.",
      "error"
    );
  }
};

const bootstrap = () => {
  dom.list = document.getElementById("api-list");
  dom.status = document.getElementById("api-status");
  dom.baseUrl = document.getElementById("api-base-url");
  dom.copyBase = document.getElementById("api-copy-base");
  initThemeToggle();
  initHelpPanel();
  initBrand();
  initNav();
  initQuickTips(".panel");
  const title = window.APP_CONFIG?.title || "HPC Status Monitor";
  document.title = `API | ${title}`;
  const eyebrow = document.getElementById("header-eyebrow");
  if (eyebrow) eyebrow.textContent = window.APP_CONFIG?.eyebrow || "HPC STATUS";
  bindEvents();
  load();
};

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
