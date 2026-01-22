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
