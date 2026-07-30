/**
 * Fleet topology graph.
 *
 * Draws the /api/topology graph as an interactive SVG: monitor → site →
 * system, with five interchangeable layouts, live polling, and click-through
 * to the per-system queue health page.
 *
 * Deliberately dependency-free — the deployments this ships to are often
 * air-gapped, so there is no D3/cytoscape here. Layout, zoom/pan, the force
 * simulation, and the animation loop are all local.
 */

import {
  buildApiUrl,
  clampPercent,
  clusterPagesEnabled,
  initThemeToggle,
  initHelpPanel,
  initBrand,
  formatRelativeTime,
  initQuickTips,
  initNav,
} from "./page-utils.js";

const DATA_URL = buildApiUrl("api/topology").toString();
const POLL_MS = 60_000;
const SVG_NS = "http://www.w3.org/2000/svg";

const LAYOUTS = new Set(["hierarchy", "radial", "force", "lanes", "geo"]);
const GROUPINGS = new Set(["site", "scheduler", "status", "connection"]);
// Control-plane round trip above which a link is called out as slow.
const SLOW_LINK_MS = 1500;
const COMPARE_LIMIT = 4;

const numberFormatter = new Intl.NumberFormat("en-US");
const fmt = (value) => numberFormatter.format(Math.round(Number(value) || 0));

const state = {
  graph: null,
  layout: "hierarchy",
  group: "site",
  filters: { search: "", status: "", connectedOnly: false },
  selectedId: null,
  hoverId: null,
  compare: new Set(), // node ids pinned into the comparison panel
  compareMode: false,
  view: { nodes: [], edges: [], groups: [] },
  positions: new Map(), // id -> {x, y} — what is on screen right now
  targets: new Map(), // id -> {x, y} — where the layout wants them
  velocities: new Map(), // id -> {vx, vy} — force layout only
  pinned: new Map(), // id -> {x, y} — nodes the user dragged
  transform: { x: 0, y: 0, k: 1 },
  elements: new Map(),
  animation: { handle: null, start: 0, duration: 620, from: new Map() },
  forceTicks: 0,
  suppressClick: false,
  loading: false,
  lastUpdated: null,
  pollHandle: null,
  clusterPages: clusterPagesEnabled(),
};

const dom = {};

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

const escapeHtml = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (ch) => {
    switch (ch) {
      case "&": return "&amp;";
      case "<": return "&lt;";
      case ">": return "&gt;";
      case '"': return "&quot;";
      default: return "&#39;";
    }
  });

const formatDuration = (seconds) => {
  const total = Number(seconds);
  if (!Number.isFinite(total) || total < 0) return null;
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m`;
  return "just now";
};

const formatPercent = (value, digits = 0) =>
  Number.isFinite(Number(value)) ? `${Number(value).toFixed(digits)}%` : "—";

const statusKey = (status) => {
  const normalized = String(status || "UNKNOWN").toUpperCase();
  if (normalized === "UP") return "up";
  if (normalized === "DOWN") return "down";
  if (normalized === "DEGRADED" || normalized === "MAINTENANCE") return "degraded";
  return "unknown";
};

// ---------------------------------------------------------------------------
// View model — filtering and grouping
// ---------------------------------------------------------------------------

const systemNodes = () =>
  (state.graph?.nodes || []).filter((node) => node.kind === "system");

const matchesFilters = (node) => {
  const { search, status, connectedOnly } = state.filters;
  if (status && String(node.status).toUpperCase() !== status) return false;
  if (connectedOnly && !node.connected) return false;
  if (search) {
    const haystack = [
      node.label,
      node.slug,
      node.login,
      node.address,
      node.hostname,
      node.scheduler,
      node.site_label,
      node.site,
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    if (!haystack.includes(search.toLowerCase())) return false;
  }
  return true;
};

const groupDescriptor = (node) => {
  switch (state.group) {
    case "scheduler":
      return { id: `sched:${node.scheduler || "unknown"}`, label: node.scheduler || "No scheduler" };
    case "status":
      return { id: `status:${node.status}`, label: node.status };
    case "connection":
      return node.connected
        ? { id: "conn:live", label: "Live telemetry" }
        : { id: "conn:reported", label: "Status page only" };
    case "site":
    default:
      return { id: `site:${node.site}`, label: node.site_label || node.site };
  }
};

/** Collapse the raw graph into what is actually drawn for the current filters. */
const buildView = () => {
  const graph = state.graph;
  if (!graph) return { nodes: [], edges: [], groups: [] };

  const systems = systemNodes().filter(matchesFilters);
  const siteById = new Map((graph.sites || []).map((site) => [site.id, site]));
  const groups = new Map();

  systems.forEach((node) => {
    const descriptor = groupDescriptor(node);
    if (!groups.has(descriptor.id)) {
      const site = state.group === "site" ? siteById.get(node.site) : null;
      groups.set(descriptor.id, {
        id: descriptor.id,
        kind: "group",
        label: descriptor.label,
        site: site || null,
        location: site?.location || null,
        organization: site?.organization || null,
        lat: site?.lat ?? null,
        lon: site?.lon ?? null,
        members: [],
      });
    }
    groups.get(descriptor.id).members.push(node);
  });

  const groupList = [...groups.values()];
  groupList.forEach((group) => {
    const statuses = group.members.map((m) => m.status);
    group.status = statuses.every((s) => s === "UP")
      ? "UP"
      : statuses.some((s) => s === "DOWN")
        ? "DEGRADED"
        : statuses.some((s) => s === "DEGRADED" || s === "MAINTENANCE")
          ? "DEGRADED"
          : "UNKNOWN";
    group.connected = group.members.filter((m) => m.connected).length;
    group.alerts = group.members.filter((m) => m.alert === "critical" || m.alert === "warning").length;
    group.alert = group.members.some((m) => m.alert === "critical")
      ? "critical"
      : group.alerts
        ? "warning"
        : null;
    group.capacity = group.members.reduce(
      (acc, m) => {
        acc.cores_total += Number(m.capacity?.cores_total) || 0;
        acc.cores_running += Number(m.capacity?.cores_running) || 0;
        acc.gpus_total += Number(m.capacity?.gpus_total) || 0;
        return acc;
      },
      { cores_total: 0, cores_running: 0, gpus_total: 0 }
    );
  });

  // Keep site order stable and meaningful: catalog order from the API.
  const order = new Map((graph.sites || []).map((site, idx) => [`site:${site.id}`, idx]));
  groupList.sort((a, b) => {
    const ai = order.has(a.id) ? order.get(a.id) : Number.MAX_SAFE_INTEGER;
    const bi = order.has(b.id) ? order.get(b.id) : Number.MAX_SAFE_INTEGER;
    if (ai !== bi) return ai - bi;
    return String(a.label).localeCompare(String(b.label));
  });

  const monitor = graph.nodes.find((n) => n.kind === "monitor") || {
    id: "monitor",
    kind: "monitor",
    label: "Status Monitor",
  };

  const nodes = [monitor, ...groupList, ...systems];
  const edges = [];
  groupList.forEach((group) => {
    edges.push({
      id: `${monitor.id}->${group.id}`,
      source: monitor.id,
      target: group.id,
      kind: "group",
      connected: group.connected > 0,
    });
    group.members.forEach((member) => {
      edges.push({
        id: `${group.id}->${member.id}`,
        source: group.id,
        target: member.id,
        kind: "member",
        connected: Boolean(member.connected),
        status: member.status,
        latency_ms: member.connection?.latency_ms ?? null,
      });
    });
  });

  return { nodes, edges, groups: groupList, monitor };
};

// ---------------------------------------------------------------------------
// Node sizing
// ---------------------------------------------------------------------------

const nodeRadius = (node) => {
  if (node.kind === "monitor") return 34;
  if (node.kind === "group") return Math.min(46, 24 + Math.sqrt(node.members.length) * 6);
  const cores = Number(node.capacity?.cores_total) || 0;
  if (!cores) return 15;
  // sqrt scale so a 200k-core machine is legible next to a 64-core box
  return Math.max(15, Math.min(34, 12 + Math.sqrt(cores) / 12));
};

// ---------------------------------------------------------------------------
// Layouts — each returns a Map of id -> {x, y} in world coordinates
// ---------------------------------------------------------------------------

const canvasAspect = () => {
  const rect = dom.canvas?.getBoundingClientRect?.();
  return rect && rect.width && rect.height
    ? { width: rect.width, height: rect.height }
    : { width: 1280, height: 760 };
};

const layoutHierarchy = (view) => {
  const positions = new Map();
  const COLUMN = 168;
  const ROW = 104;
  const GROUP_GAP = 96;
  const BAND_GAP = 96;
  const HEAD = 190; // site marker + labels above the first member row

  const blocks = view.groups.map((group) => {
    const count = group.members.length;
    const perRow = count <= 3 ? Math.max(1, count) : Math.min(4, Math.ceil(Math.sqrt(count)));
    const rows = Math.ceil(count / perRow) || 1;
    return {
      group,
      perRow,
      width: perRow * COLUMN,
      height: HEAD + rows * ROW,
    };
  });

  // A single band of sites reads best, but with many sites it makes a very
  // wide, very short drawing that fit-to-view then shrinks until the labels
  // are unreadable. Pick the number of site columns that fills the canvas
  // best instead of always using one row.
  const canvas = canvasAspect();
  const measure = (cols) => {
    let width = 0;
    let height = 0;
    for (let i = 0; i < blocks.length; i += cols) {
      const row = blocks.slice(i, i + cols);
      width = Math.max(width, row.reduce((sum, b) => sum + b.width, 0) + GROUP_GAP * (row.length - 1));
      height += Math.max(...row.map((b) => b.height)) + BAND_GAP;
    }
    return { width, height: height + HEAD };
  };
  let best = { cols: blocks.length || 1, scale: -1 };
  for (let cols = 1; cols <= Math.max(1, blocks.length); cols += 1) {
    const { width, height } = measure(cols);
    const scale = Math.min(canvas.width / width, canvas.height / height);
    if (scale > best.scale) best = { cols, scale };
  }

  let y = 0;
  for (let i = 0; i < blocks.length; i += best.cols) {
    const row = blocks.slice(i, i + best.cols);
    const rowWidth = row.reduce((sum, b) => sum + b.width, 0) + GROUP_GAP * (row.length - 1);
    let cursor = -rowWidth / 2;
    row.forEach((block) => {
      const centerX = cursor + block.width / 2;
      positions.set(block.group.id, { x: centerX, y });
      block.group.members.forEach((member, idx) => {
        const memberRow = Math.floor(idx / block.perRow);
        const inRow = Math.min(block.perRow, block.group.members.length - memberRow * block.perRow);
        const offset = (idx % block.perRow) - (inRow - 1) / 2;
        positions.set(member.id, {
          x: centerX + offset * COLUMN,
          y: y + HEAD + memberRow * ROW,
        });
      });
      cursor += block.width + GROUP_GAP;
    });
    y += Math.max(...row.map((b) => b.height)) + BAND_GAP;
  }

  positions.set(view.monitor.id, { x: 0, y: -HEAD });
  return positions;
};

const layoutRadial = (view) => {
  const positions = new Map();
  positions.set(view.monitor.id, { x: 0, y: 0 });

  const total = view.groups.reduce((sum, g) => sum + Math.max(1, g.members.length), 0) || 1;
  const innerRadius = 230;
  let angle = -Math.PI / 2;

  view.groups.forEach((group) => {
    const share = (Math.max(1, group.members.length) / total) * Math.PI * 2;
    const mid = angle + share / 2;
    positions.set(group.id, {
      x: Math.cos(mid) * innerRadius,
      y: Math.sin(mid) * innerRadius,
    });

    const count = group.members.length;
    const perRing = Math.max(1, Math.ceil(count / Math.ceil(count / 8)));
    group.members.forEach((member, idx) => {
      const ring = Math.floor(idx / perRing);
      const inRing = Math.min(perRing, count - ring * perRing);
      const spread = Math.min(share * 0.92, 0.34 * inRing);
      const step = inRing > 1 ? spread / (inRing - 1) : 0;
      const memberAngle = mid - spread / 2 + (idx % perRing) * step;
      const radius = 430 + ring * 96;
      positions.set(member.id, {
        x: Math.cos(memberAngle) * radius,
        y: Math.sin(memberAngle) * radius,
      });
    });
    angle += share;
  });
  return positions;
};

const layoutLanes = (view) => {
  const positions = new Map();
  const laneWidth = 300;
  const laneGap = 40;
  const perRow = 3;

  view.groups.forEach((group, index) => {
    const laneX = index * (laneWidth + laneGap);
    positions.set(group.id, { x: laneX + laneWidth / 2, y: 40 });
    group.members.forEach((member, idx) => {
      const row = Math.floor(idx / perRow);
      const inRow = Math.min(perRow, group.members.length - row * perRow);
      const cell = laneWidth / perRow;
      const offset = (idx % perRow) - (inRow - 1) / 2;
      positions.set(member.id, {
        x: laneX + laneWidth / 2 + offset * cell,
        y: 190 + row * 96,
      });
    });
  });

  const width = view.groups.length * (laneWidth + laneGap) - laneGap;
  positions.set(view.monitor.id, { x: width / 2 - laneWidth / 2, y: -150 });
  return positions;
};

// Equirectangular projection. The latitude factor is deliberately below 1:
// sites in this domain spread much further in longitude than latitude, and
// squaring the aspect up keeps the whole map legible in a wide panel.
const GEO_SCALE = 66; // pixels per degree of longitude
const GEO_LAT_FACTOR = 0.95;
const projectGeo = (lat, lon) => ({
  x: lon * GEO_SCALE,
  y: -lat * GEO_SCALE * GEO_LAT_FACTOR,
});

const layoutGeo = (view) => {
  const positions = new Map();
  const placed = view.groups.filter(
    (g) => Number.isFinite(g.lat) && Number.isFinite(g.lon)
  );
  const unplaced = view.groups.filter(
    (g) => !Number.isFinite(g.lat) || !Number.isFinite(g.lon)
  );

  // Project, then relax overlapping site clusters apart (Dorling-style):
  // ERDC and Navy DSRC are 100 miles apart, which at any scale that keeps
  // the whole map on screen would draw them on top of each other.
  // Members sit in a small grid below their site marker (a ring collides
  // with the site's own label at any radius that keeps the map compact).
  const COL = 96;
  const ROW = 74;
  const HEAD = 112; // clearance for the site marker + its two label lines
  const gridOf = (group) => {
    const count = group.members.length;
    const perRow = count <= 3 ? Math.max(1, count) : Math.min(4, Math.ceil(Math.sqrt(count)));
    return { perRow, rows: Math.ceil(count / perRow) };
  };
  const clusterRadius = (group) => {
    const { perRow, rows } = gridOf(group);
    return Math.max((perRow * COL) / 2, (HEAD + rows * ROW) / 2) + 18;
  };
  const points = placed.map((group) => ({ group, ...projectGeo(group.lat, group.lon) }));
  for (let pass = 0; pass < 90; pass += 1) {
    let moved = false;
    for (let i = 0; i < points.length; i += 1) {
      for (let j = i + 1; j < points.length; j += 1) {
        const a = points[i];
        const b = points[j];
        const minDist = clusterRadius(a.group) + clusterRadius(b.group);
        let dx = b.x - a.x;
        let dy = b.y - a.y;
        let dist = Math.hypot(dx, dy);
        if (dist >= minDist) continue;
        if (dist < 0.001) {
          dx = 1;
          dy = 0;
          dist = 1;
        }
        const shift = (minDist - dist) / 2;
        a.x -= (dx / dist) * shift;
        a.y -= (dy / dist) * shift;
        b.x += (dx / dist) * shift;
        b.y += (dy / dist) * shift;
        moved = true;
      }
    }
    if (!moved) break;
  }

  points.forEach(({ group, x, y }) => {
    positions.set(group.id, { x, y });
    const { perRow } = gridOf(group);
    group.members.forEach((member, idx) => {
      const row = Math.floor(idx / perRow);
      const inRow = Math.min(perRow, group.members.length - row * perRow);
      const offset = (idx % perRow) - (inRow - 1) / 2;
      positions.set(member.id, { x: x + offset * COL, y: y + HEAD + row * ROW });
    });
  });

  // Sites with no coordinates get a tray below the map rather than being
  // silently dropped — an unlocated cluster is still part of the fleet.
  const drawn = [...positions.values()];
  const anchorY = drawn.length ? Math.max(...drawn.map((p) => p.y)) + 150 : 0;
  let trayX = drawn.length ? Math.min(...drawn.map((p) => p.x)) : 0;
  unplaced.forEach((group) => {
    const { perRow } = gridOf(group);
    positions.set(group.id, { x: trayX, y: anchorY });
    group.members.forEach((member, idx) => {
      const row = Math.floor(idx / perRow);
      const inRow = Math.min(perRow, group.members.length - row * perRow);
      const offset = (idx % perRow) - (inRow - 1) / 2;
      positions.set(member.id, { x: trayX + offset * COL, y: anchorY + HEAD + row * ROW });
    });
    trayX += clusterRadius(group) * 2 + 60;
  });

  const xs = [...positions.values()].map((p) => p.x);
  const ys = [...positions.values()].map((p) => p.y);
  positions.set(view.monitor.id, {
    x: xs.length ? (Math.min(...xs) + Math.max(...xs)) / 2 : 0,
    y: ys.length ? Math.min(...ys) - 150 : 0,
  });
  return positions;
};

/**
 * Force layout: repulsion between every pair, springs along edges, a weak
 * pull toward the centre, and a group cohesion term. n is small (tens of
 * nodes) so the naive O(n²) pass is cheaper than a quadtree.
 */
const forceTick = (view) => {
  const nodes = view.nodes;
  const alpha = Math.max(0.02, 0.9 * Math.pow(0.97, state.forceTicks));
  const positionOf = (id) => state.targets.get(id) || { x: 0, y: 0 };

  nodes.forEach((node) => {
    if (!state.velocities.has(node.id)) state.velocities.set(node.id, { vx: 0, vy: 0 });
  });

  // Repulsion
  for (let i = 0; i < nodes.length; i += 1) {
    for (let j = i + 1; j < nodes.length; j += 1) {
      const a = positionOf(nodes[i].id);
      const b = positionOf(nodes[j].id);
      let dx = b.x - a.x;
      let dy = b.y - a.y;
      let distSq = dx * dx + dy * dy;
      if (distSq < 1) {
        dx = (Math.random() - 0.5) * 2;
        dy = (Math.random() - 0.5) * 2;
        distSq = dx * dx + dy * dy;
      }
      // The +58 is label clearance: nodes carry a name and an address line
      // under them, so touching circles still read as overlapping text.
      const minDist = nodeRadius(nodes[i]) + nodeRadius(nodes[j]) + 58;
      const dist = Math.sqrt(distSq);
      const strength = (6000 + (dist < minDist ? 11000 : 0)) / distSq;
      const fx = (dx / dist) * strength;
      const fy = (dy / dist) * strength;
      const va = state.velocities.get(nodes[i].id);
      const vb = state.velocities.get(nodes[j].id);
      va.vx -= fx;
      va.vy -= fy;
      vb.vx += fx;
      vb.vy += fy;
    }
  }

  // Springs
  view.edges.forEach((edge) => {
    const a = positionOf(edge.source);
    const b = positionOf(edge.target);
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const dist = Math.hypot(dx, dy) || 1;
    const rest = edge.kind === "group" ? 300 : 170;
    const force = (dist - rest) * 0.045;
    const fx = (dx / dist) * force;
    const fy = (dy / dist) * force;
    const va = state.velocities.get(edge.source);
    const vb = state.velocities.get(edge.target);
    if (va) { va.vx += fx; va.vy += fy; }
    if (vb) { vb.vx -= fx; vb.vy -= fy; }
  });

  // Gravity toward origin keeps disconnected pieces from drifting away
  nodes.forEach((node) => {
    const p = positionOf(node.id);
    const v = state.velocities.get(node.id);
    v.vx -= p.x * 0.006;
    v.vy -= p.y * 0.006;
  });

  let motion = 0;
  nodes.forEach((node) => {
    const pinned = state.pinned.get(node.id);
    const v = state.velocities.get(node.id);
    const p = positionOf(node.id);
    if (pinned) {
      state.targets.set(node.id, { x: pinned.x, y: pinned.y });
      v.vx = 0;
      v.vy = 0;
      return;
    }
    v.vx *= 0.82;
    v.vy *= 0.82;
    const nx = p.x + v.vx * alpha;
    const ny = p.y + v.vy * alpha;
    motion += Math.abs(nx - p.x) + Math.abs(ny - p.y);
    state.targets.set(node.id, { x: nx, y: ny });
  });

  state.forceTicks += 1;
  return motion;
};

const computeLayout = (view) => {
  switch (state.layout) {
    case "radial": return layoutRadial(view);
    case "lanes": return layoutLanes(view);
    case "geo": return layoutGeo(view);
    case "force": return null; // handled by the simulation loop
    case "hierarchy":
    default: return layoutHierarchy(view);
  }
};

// ---------------------------------------------------------------------------
// SVG element construction
// ---------------------------------------------------------------------------

const svgEl = (name, attrs = {}) => {
  const element = document.createElementNS(SVG_NS, name);
  Object.entries(attrs).forEach(([key, value]) => {
    if (value !== null && value !== undefined) element.setAttribute(key, value);
  });
  return element;
};

const ensureScaffold = () => {
  const svg = dom.svg;
  svg.innerHTML = "";
  const root = svgEl("g", { class: "topo-root" });
  dom.laneLayer = svgEl("g", { class: "topo-layer topo-layer--lanes" });
  dom.edgeLayer = svgEl("g", { class: "topo-layer topo-layer--edges" });
  dom.nodeLayer = svgEl("g", { class: "topo-layer topo-layer--nodes" });
  root.append(dom.laneLayer, dom.edgeLayer, dom.nodeLayer);
  svg.append(root);
  dom.root = root;
  state.elements.clear();
};

const createNodeElement = (node) => {
  const group = svgEl("g", {
    class: "topo-node",
    "data-id": node.id,
    "data-kind": node.kind,
    tabindex: "0",
    role: "button",
  });

  const halo = svgEl("circle", { class: "topo-node-halo", r: 0 });
  const core = svgEl("circle", { class: "topo-node-core", r: 0 });
  const util = svgEl("circle", { class: "topo-node-util", r: 0 });
  const glyph = svgEl("text", { class: "topo-node-glyph", "text-anchor": "middle", dy: "0.34em" });
  const label = svgEl("text", { class: "topo-node-label", "text-anchor": "middle" });
  const sublabel = svgEl("text", { class: "topo-node-sublabel", "text-anchor": "middle" });

  // Alert badge — hidden unless the node has an insight against it.
  const badge = svgEl("g", { class: "topo-badge" });
  const badgeDot = svgEl("circle", { class: "topo-badge-dot", r: 8 });
  const badgeText = svgEl("text", { class: "topo-badge-text", "text-anchor": "middle", dy: "0.35em" });
  badge.append(badgeDot, badgeText);

  group.append(halo, util, core, glyph, label, sublabel, badge);
  group.__refs = { halo, core, util, glyph, label, sublabel, badge, badgeText };
  return group;
};

const updateNodeElement = (node, element) => {
  const refs = element.__refs;
  const radius = nodeRadius(node);
  const status = statusKey(node.status);

  element.setAttribute(
    "class",
    [
      "topo-node",
      `topo-node--${node.kind}`,
      `topo-node--${status}`,
      node.connected ? "is-live" : "is-reported",
      state.selectedId === node.id ? "is-selected" : "",
      state.hoverId === node.id ? "is-hover" : "",
      state.compare.has(node.id) ? "is-compared" : "",
    ]
      .filter(Boolean)
      .join(" ")
  );

  refs.core.setAttribute("r", radius);
  refs.halo.setAttribute("r", radius + 9);
  refs.util.setAttribute("r", radius + 5);

  // Utilization ring: a dashed circle whose filled arc is the busy share.
  const utilization = node.kind === "system"
    ? Number(node.capacity?.utilization_percent)
    : node.capacity?.cores_total
      ? (node.capacity.cores_running / node.capacity.cores_total) * 100
      : NaN;
  if (Number.isFinite(utilization) && utilization > 0) {
    const circumference = 2 * Math.PI * (radius + 5);
    const filled = (clampPercent(utilization) / 100) * circumference;
    refs.util.setAttribute("stroke-dasharray", `${filled} ${circumference - filled}`);
    refs.util.setAttribute("stroke-dashoffset", circumference / 4);
    refs.util.removeAttribute("hidden");
    refs.util.style.display = "";
  } else {
    refs.util.style.display = "none";
  }

  if (node.kind === "system") {
    refs.glyph.textContent = (node.scheduler || "").slice(0, 1).toUpperCase();
  } else if (node.kind === "group") {
    refs.glyph.textContent = String(node.members.length);
  } else {
    refs.glyph.textContent = String(state.graph?.summary?.systems ?? "");
  }

  refs.label.textContent = node.label || "";
  refs.label.setAttribute("y", radius + 20);

  const sub =
    node.kind === "system"
      ? node.address || node.hostname || node.login || ""
      : node.kind === "group"
        ? node.location || `${node.members?.length || 0} systems`
        : node.platform
          ? String(node.platform).toUpperCase()
          : "";
  refs.sublabel.textContent = sub.length > 34 ? `${sub.slice(0, 33)}…` : sub;
  refs.sublabel.setAttribute("y", radius + 36);

  // Alert badge, top-right of the node.
  const alert = node.alert || null;
  if (alert) {
    const offset = radius * 0.72;
    refs.badge.setAttribute("transform", `translate(${offset},${-offset})`);
    refs.badge.setAttribute("class", `topo-badge topo-badge--${alert}`);
    refs.badgeText.textContent =
      node.kind === "group" ? String(node.alerts || "!") : "!";
    refs.badge.style.display = "";
  } else {
    refs.badge.style.display = "none";
  }

  element.setAttribute("aria-label", describeNode(node));
};

const describeNode = (node) => {
  if (node.kind === "system") {
    const bits = [`${node.label}, ${node.status}`];
    if (node.site_label) bits.push(`at ${node.site_label}`);
    if (node.scheduler) bits.push(`${node.scheduler} scheduler`);
    bits.push(node.connected ? "live telemetry" : "status page only");
    if (node.insights?.length) {
      bits.push(`${node.insights.length} open insight${node.insights.length === 1 ? "" : "s"}`);
    }
    return bits.join(", ");
  }
  if (node.kind === "group") {
    return `${node.label}, ${node.members.length} systems, ${node.connected} connected`;
  }
  return `${node.label}, fleet monitor`;
};

const createEdgeElement = (edge) => {
  const path = svgEl("path", {
    class: "topo-edge",
    "data-id": edge.id,
    fill: "none",
  });
  return path;
};

const edgePath = (edge) => {
  const a = state.positions.get(edge.source);
  const b = state.positions.get(edge.target);
  if (!a || !b) return "";
  if (state.layout === "hierarchy" || state.layout === "lanes") {
    const midY = (a.y + b.y) / 2;
    return `M${a.x},${a.y} C${a.x},${midY} ${b.x},${midY} ${b.x},${b.y}`;
  }
  return `M${a.x},${a.y} L${b.x},${b.y}`;
};

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

const renderGraph = ({ animate = true, fit = false } = {}) => {
  const view = buildView();
  state.view = view;

  // A lone monitor node is not a graph — either nothing has been collected
  // yet, or the filters excluded everything.
  const hasSystems = view.nodes.some((node) => node.kind === "system");
  if (!hasSystems) {
    const filtered = Boolean(
      state.filters.search || state.filters.status || state.filters.connectedOnly
    );
    dom.empty.hidden = false;
    dom.empty.innerHTML = filtered
      ? `<div class="empty-state">
          <div class="empty-state-icon">🔍</div>
          <h3 class="empty-state-title">No systems match</h3>
          <p class="empty-state-message">Loosen the filters or clear the search box.</p>
        </div>`
      : `<div class="empty-state">
          <div class="empty-state-icon">🛰️</div>
          <h3 class="empty-state-title">Waiting for the first collection</h3>
          <p class="empty-state-message">
            The monitor has not reported any systems yet. This page refreshes itself every minute.
          </p>
        </div>`;
    dom.nodeLayer.innerHTML = "";
    dom.edgeLayer.innerHTML = "";
    dom.laneLayer.innerHTML = "";
    state.elements.clear();
    return;
  }
  dom.empty.hidden = true;

  // Sync SVG elements with the view model.
  const seen = new Set();
  view.nodes.forEach((node) => {
    seen.add(node.id);
    let element = state.elements.get(node.id);
    if (!element) {
      element = createNodeElement(node);
      dom.nodeLayer.append(element);
      state.elements.set(node.id, element);
    }
    updateNodeElement(node, element);
  });

  const edgeIds = new Set();
  view.edges.forEach((edge) => {
    edgeIds.add(edge.id);
    let element = state.elements.get(edge.id);
    if (!element) {
      element = createEdgeElement(edge);
      dom.edgeLayer.append(element);
      state.elements.set(edge.id, element);
    }
    const latency = Number(edge.latency_ms);
    element.setAttribute(
      "class",
      [
        "topo-edge",
        `topo-edge--${edge.kind}`,
        edge.connected ? "is-live" : "is-reported",
        Number.isFinite(latency) && latency > SLOW_LINK_MS ? "is-slow" : "",
        state.layout === "lanes" ? "is-muted" : "",
      ]
        .filter(Boolean)
        .join(" ")
    );
    // Dash animation speed carries the round trip: a sluggish control-plane
    // link visibly crawls. Width would have been ambiguous (thick = fast or
    // busy?), speed is not.
    if (edge.connected && Number.isFinite(latency)) {
      const seconds = Math.min(6, Math.max(0.8, latency / 400));
      element.style.animationDuration = `${seconds.toFixed(2)}s`;
      element.setAttribute("data-latency-ms", String(Math.round(latency)));
    } else {
      element.style.animationDuration = "";
      element.removeAttribute("data-latency-ms");
    }
  });

  [...state.elements.keys()].forEach((id) => {
    if (seen.has(id) || edgeIds.has(id)) return;
    state.elements.get(id).remove();
    state.elements.delete(id);
    state.positions.delete(id);
    state.targets.delete(id);
  });

  // Seed positions for new nodes near their group so they fly in sensibly.
  view.nodes.forEach((node) => {
    if (state.positions.has(node.id)) return;
    const parentEdge = view.edges.find((e) => e.target === node.id);
    const anchor = parentEdge ? state.positions.get(parentEdge.source) : null;
    state.positions.set(node.id, anchor ? { ...anchor } : { x: 0, y: 0 });
  });

  if (state.layout === "force") {
    startForce(view);
  } else {
    stopForce();
    state.targets = computeLayout(view);
    // Fit only once the nodes have arrived: fitting mid-flight measures a
    // half-collapsed graph and zooms in far too tight.
    animateTo(animate, fit ? () => fitToView({ animate: true }) : undefined);
  }
  renderDecorations(view);
  drawFrame();
};

/** Background decoration for the layouts that need it: lane frames, graticule. */
const renderDecorations = (view) => {
  dom.laneLayer.innerHTML = "";
  if (state.layout === "geo") {
    renderGraticule(view);
    return;
  }
  if (state.layout !== "lanes") return;
  view.groups.forEach((group) => {
    const memberPositions = group.members
      .map((m) => state.targets.get(m.id))
      .filter(Boolean);
    const groupPos = state.targets.get(group.id);
    if (!groupPos) return;
    const xs = memberPositions.map((p) => p.x);
    const ys = memberPositions.map((p) => p.y);
    // Never narrower than the site's own label, which is wider than a
    // single member column on one-system sites.
    const halfWidth = Math.max(62, (xs.length ? Math.max(...xs) - Math.min(...xs) : 0) / 2 + 62, 110);
    const left = groupPos.x - halfWidth;
    const right = groupPos.x + halfWidth;
    const top = groupPos.y - 62;
    const bottom = (ys.length ? Math.max(...ys) : groupPos.y) + 74;
    dom.laneLayer.append(
      svgEl("rect", {
        class: "topo-lane",
        x: left,
        y: top,
        width: right - left,
        height: bottom - top,
        rx: 20,
      })
    );
  });
};

/** Lat/lon grid behind the geographic layout, labelled every 5 degrees. */
const renderGraticule = (view) => {
  const located = view.groups.filter((g) => Number.isFinite(g.lat) && Number.isFinite(g.lon));
  if (!located.length) return;
  const lats = located.map((g) => g.lat);
  const lons = located.map((g) => g.lon);
  const pad = 4;
  const minLat = Math.floor((Math.min(...lats) - pad) / 5) * 5;
  const maxLat = Math.ceil((Math.max(...lats) + pad) / 5) * 5;
  const minLon = Math.floor((Math.min(...lons) - pad) / 5) * 5;
  const maxLon = Math.ceil((Math.max(...lons) + pad) / 5) * 5;

  for (let lat = minLat; lat <= maxLat; lat += 5) {
    const a = projectGeo(lat, minLon);
    const b = projectGeo(lat, maxLon);
    dom.laneLayer.append(
      svgEl("line", { class: "topo-graticule", x1: a.x, y1: a.y, x2: b.x, y2: b.y })
    );
    const label = svgEl("text", { class: "topo-graticule-label", x: a.x - 8, y: a.y, "text-anchor": "end", dy: "0.32em" });
    label.textContent = `${lat}°`;
    dom.laneLayer.append(label);
  }
  for (let lon = minLon; lon <= maxLon; lon += 5) {
    const a = projectGeo(minLat, lon);
    const b = projectGeo(maxLat, lon);
    dom.laneLayer.append(
      svgEl("line", { class: "topo-graticule", x1: a.x, y1: a.y, x2: b.x, y2: b.y })
    );
    const label = svgEl("text", { class: "topo-graticule-label", x: a.x, y: a.y + 18, "text-anchor": "middle" });
    label.textContent = `${lon}°`;
    dom.laneLayer.append(label);
  }
};

/** Interpolate current positions toward the layout targets. */
const animateTo = (animate, onComplete) => {
  cancelAnimationFrame(state.animation.handle);
  if (!animate) {
    state.targets.forEach((point, id) => state.positions.set(id, { ...point }));
    drawFrame();
    fitToView({ animate: false });
    onComplete?.();
    return;
  }
  state.animation.from = new Map(
    [...state.positions.entries()].map(([id, point]) => [id, { ...point }])
  );
  state.animation.start = performance.now();
  const step = (now) => {
    const elapsed = now - state.animation.start;
    const t = Math.min(1, elapsed / state.animation.duration);
    const eased = 1 - Math.pow(1 - t, 3);
    state.targets.forEach((target, id) => {
      const from = state.animation.from.get(id) || target;
      state.positions.set(id, {
        x: from.x + (target.x - from.x) * eased,
        y: from.y + (target.y - from.y) * eased,
      });
    });
    drawFrame();
    if (t < 1) {
      state.animation.handle = requestAnimationFrame(step);
    } else {
      onComplete?.();
    }
  };
  state.animation.handle = requestAnimationFrame(step);
};

let forceHandle = null;

const startForce = (view) => {
  // Seed the simulation from the current on-screen positions, or from the
  // hierarchy layout on a cold start, so it never explodes from (0,0).
  if (!state.targets.size || state.forceTicks === 0) {
    const seed = layoutHierarchy(view);
    state.targets = new Map(
      view.nodes.map((n) => [n.id, { ...(state.positions.get(n.id) || seed.get(n.id) || { x: 0, y: 0 }) }])
    );
  }
  state.velocities.clear();
  state.forceTicks = 0;
  cancelAnimationFrame(forceHandle);
  const step = () => {
    const motion = forceTick(view);
    state.targets.forEach((point, id) => state.positions.set(id, { ...point }));
    drawFrame();
    if (state.layout !== "force") return;
    if (state.forceTicks < 400 && motion > 0.4) {
      forceHandle = requestAnimationFrame(step);
    } else {
      // Settled: frame the final arrangement (the early fit below only
      // frames the opening seconds of the simulation).
      forceHandle = null;
      fitToView({ animate: true });
    }
  };
  forceHandle = requestAnimationFrame(step);
  // Fit once the graph has roughly settled.
  setTimeout(() => {
    if (state.layout === "force") fitToView({ animate: true });
  }, 900);
};

const stopForce = () => {
  cancelAnimationFrame(forceHandle);
  forceHandle = null;
  state.forceTicks = 0;
};

const reheatForce = () => {
  if (state.layout !== "force") return;
  state.forceTicks = Math.min(state.forceTicks, 120);
  if (!forceHandle) startForce(state.view);
};

/** Paint one frame: node transforms, edge geometry, viewport transform. */
const drawFrame = () => {
  state.view.nodes.forEach((node) => {
    const element = state.elements.get(node.id);
    const point = state.positions.get(node.id);
    if (!element || !point) return;
    element.setAttribute("transform", `translate(${point.x.toFixed(2)},${point.y.toFixed(2)})`);
  });
  state.view.edges.forEach((edge) => {
    const element = state.elements.get(edge.id);
    if (element) element.setAttribute("d", edgePath(edge));
  });
  const { x, y, k } = state.transform;
  dom.root.setAttribute("transform", `translate(${x.toFixed(2)},${y.toFixed(2)}) scale(${k.toFixed(4)})`);
};

// ---------------------------------------------------------------------------
// Zoom / pan
// ---------------------------------------------------------------------------

const graphBounds = () => {
  const points = [...state.positions.entries()];
  if (!points.length) return null;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  points.forEach(([id, point]) => {
    const node = state.view.nodes.find((n) => n.id === id);
    // Lanes draw a frame ~62px beyond the outermost member, so they need
    // more slack than the label band the other layouts pad for.
    const labelPad = state.layout === "lanes" ? 76 : 46;
    const pad = node ? nodeRadius(node) + labelPad : 40;
    minX = Math.min(minX, point.x - pad);
    minY = Math.min(minY, point.y - pad);
    maxX = Math.max(maxX, point.x + pad);
    maxY = Math.max(maxY, point.y + pad);
  });
  return { minX, minY, maxX, maxY, width: maxX - minX, height: maxY - minY };
};

const fitToView = ({ animate = true } = {}) => {
  const bounds = graphBounds();
  const rect = dom.canvas.getBoundingClientRect();
  if (!bounds || !rect.width) return;
  const k = Math.max(0.25, Math.min(1.6, Math.min(rect.width / bounds.width, rect.height / bounds.height)));
  const target = {
    k,
    x: rect.width / 2 - ((bounds.minX + bounds.maxX) / 2) * k,
    y: rect.height / 2 - ((bounds.minY + bounds.maxY) / 2) * k,
  };
  if (!animate) {
    state.transform = target;
    drawFrame();
    return;
  }
  const from = { ...state.transform };
  const start = performance.now();
  const step = (now) => {
    const t = Math.min(1, (now - start) / 420);
    const eased = 1 - Math.pow(1 - t, 3);
    state.transform = {
      x: from.x + (target.x - from.x) * eased,
      y: from.y + (target.y - from.y) * eased,
      k: from.k + (target.k - from.k) * eased,
    };
    drawFrame();
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
};

const zoomBy = (factor, origin) => {
  const rect = dom.canvas.getBoundingClientRect();
  const cx = origin ? origin.x - rect.left : rect.width / 2;
  const cy = origin ? origin.y - rect.top : rect.height / 2;
  const { x, y, k } = state.transform;
  const nextK = Math.max(0.2, Math.min(3.5, k * factor));
  state.transform = {
    k: nextK,
    x: cx - ((cx - x) / k) * nextK,
    y: cy - ((cy - y) / k) * nextK,
  };
  drawFrame();
};

// ---------------------------------------------------------------------------
// Interaction
// ---------------------------------------------------------------------------

const nodeById = (id) => state.view.nodes.find((n) => n.id === id);

const openQueueHealth = (node) => {
  if (!node || node.kind !== "system") return;
  if (!state.clusterPages) return;
  window.location.href = `queues.html?cluster=${encodeURIComponent(node.slug)}`;
};

const selectNode = (id, { navigateIfSelected = false } = {}) => {
  const node = nodeById(id);
  if (!node) return;
  if (navigateIfSelected && state.selectedId === id && node.kind === "system") {
    openQueueHealth(node);
    return;
  }
  state.selectedId = id;
  state.view.nodes.forEach((n) => {
    const element = state.elements.get(n.id);
    if (element) element.classList.toggle("is-selected", n.id === id);
  });
  renderPanel();
  syncLocation();
};

const clearSelection = () => {
  state.selectedId = null;
  state.elements.forEach((el) => el.classList?.remove("is-selected"));
  renderPanel();
  syncLocation();
};

const setHover = (id) => {
  if (state.hoverId === id) return;
  state.hoverId = id;
  state.elements.forEach((el, key) => {
    if (!el.classList) return;
    el.classList.toggle("is-hover", key === id);
  });
  // Dim everything not attached to the hovered node.
  const connected = new Set();
  if (id) {
    connected.add(id);
    state.view.edges.forEach((edge) => {
      if (edge.source === id) connected.add(edge.target);
      if (edge.target === id) connected.add(edge.source);
    });
  }
  dom.svg.classList.toggle("is-hovering", Boolean(id));
  state.view.nodes.forEach((node) => {
    const element = state.elements.get(node.id);
    if (element) element.classList.toggle("is-dimmed", Boolean(id) && !connected.has(node.id));
  });
  state.view.edges.forEach((edge) => {
    const element = state.elements.get(edge.id);
    if (element) {
      element.classList.toggle(
        "is-active",
        Boolean(id) && (edge.source === id || edge.target === id)
      );
    }
  });
};

const showTooltip = (node, event) => {
  const rect = dom.canvas.getBoundingClientRect();
  const rows = [];
  if (node.kind === "system") {
    rows.push(["Status", node.status]);
    if (node.site_label) rows.push(["Site", node.site_label]);
    if (node.scheduler) rows.push(["Scheduler", node.scheduler]);
    if (node.login) rows.push(["Login", node.login]);
    if (node.address) rows.push(["Address", node.address]);
    if (node.capacity?.cores_total) {
      rows.push(["Cores", `${fmt(node.capacity.cores_running)} / ${fmt(node.capacity.cores_total)} busy`]);
    }
    const connectedFor = formatDuration(node.connection?.connected_for_seconds);
    if (connectedFor) rows.push(["Connected", connectedFor]);
    if (Number.isFinite(Number(node.connection?.latency_ms))) {
      rows.push(["Round trip", `${fmt(node.connection.latency_ms)} ms`]);
    }
  } else if (node.kind === "group") {
    rows.push(["Systems", `${node.members.length}`]);
    rows.push(["Live", `${node.connected}`]);
    if (node.location) rows.push(["Location", node.location]);
  } else {
    rows.push(["Nodes", `${state.view.nodes.length - 1}`]);
  }

  const topInsight = node.insights?.[0];
  dom.tooltip.innerHTML = `
    <p class="topo-tooltip-title">${escapeHtml(node.label)}</p>
    <dl>${rows.map(([k, v]) => `<div><dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd></div>`).join("")}</dl>
    ${topInsight
      ? `<p class="topo-tooltip-alert topo-tooltip-alert--${escapeHtml(node.alert || "info")}">
           ${escapeHtml(topInsight.message)}
           ${node.insights.length > 1 ? `<span>+${node.insights.length - 1} more</span>` : ""}
         </p>`
      : ""}
    ${node.kind === "system" && state.clusterPages
      ? `<p class="topo-tooltip-hint">${
          state.compareMode
            ? "Click to add to the comparison"
            : "Click to inspect · click again for queue health · shift-click to compare"
        }</p>`
      : ""}
  `;
  dom.tooltip.hidden = false;
  const x = Math.min(event.clientX - rect.left + 16, rect.width - dom.tooltip.offsetWidth - 12);
  const y = Math.min(event.clientY - rect.top + 16, rect.height - dom.tooltip.offsetHeight - 12);
  dom.tooltip.style.transform = `translate(${Math.max(8, x)}px, ${Math.max(8, y)}px)`;
};

const hideTooltip = () => {
  dom.tooltip.hidden = true;
};

// ---------------------------------------------------------------------------
// Inspector
// ---------------------------------------------------------------------------

const metaRow = (label, value, title) =>
  value === null || value === undefined || value === ""
    ? ""
    : `<div class="topo-meta-row"${title ? ` title="${escapeHtml(title)}"` : ""}>
         <dt>${escapeHtml(label)}</dt><dd>${value}</dd>
       </div>`;

const bar = (percent, tone = "accent") =>
  `<div class="topo-bar"><span class="topo-bar-fill topo-bar-fill--${tone}" style="width:${clampPercent(percent)}%"></span></div>`;

/**
 * Status timeline: one flex segment per recorded status span, widths
 * proportional to time. Reads like a Gantt strip of the uptime window.
 */
const statusTimeline = (connection) => {
  const spans = connection?.spans || [];
  if (!spans.length) return "";
  const total = spans.reduce((sum, span) => sum + (Number(span.seconds) || 0), 0);
  if (total <= 0) return "";
  const window = connection.uptime_window_hours || 24;
  const uptime = Number.isFinite(Number(connection.uptime_ratio))
    ? formatPercent(Number(connection.uptime_ratio) * 100, 1)
    : "—";
  const segments = spans
    .map((span) => {
      const width = ((Number(span.seconds) || 0) / total) * 100;
      const label = `${span.status} for ${formatDuration(span.seconds) || "a moment"}`;
      return `<span class="topo-timeline-seg topo-timeline-seg--${statusKey(span.status)}"
                style="width:${width.toFixed(3)}%" title="${escapeHtml(label)}"></span>`;
    })
    .join("");
  return `
    <h4 class="topo-inspector-subhead">Last ${escapeHtml(window)}h</h4>
    <div class="topo-timeline" role="img" aria-label="Status over the last ${escapeHtml(window)} hours">${segments}</div>
    <div class="topo-timeline-axis">
      <span>${escapeHtml(window)}h ago</span>
      <span>${escapeHtml(uptime)} up</span>
      <span>now</span>
    </div>`;
};

const insightList = (insights) => {
  if (!insights?.length) return "";
  return `
    <h4 class="topo-inspector-subhead">Open insights</h4>
    <ul class="topo-insight-list">
      ${insights
        .slice(0, 6)
        .map((insight) => {
          const tone = (insight.priority || 0) >= 5
            ? "critical"
            : (insight.priority || 0) >= 3
              ? "warning"
              : "info";
          return `<li class="topo-insight topo-insight--${tone}">
            <span class="topo-dot topo-dot--${tone === "critical" ? "down" : tone === "warning" ? "degraded" : "up"}"></span>
            <div>
              <p>${escapeHtml(insight.message)}</p>
              ${insight.action_description ? `<small>${escapeHtml(insight.action_description)}</small>` : ""}
            </div>
          </li>`;
        })
        .join("")}
    </ul>`;
};

const renderInspector = (node) => {
  dom.inspectorEmpty.hidden = true;
  dom.inspectorBody.hidden = false;

  if (node.kind === "group") {
    const capacity = node.capacity || {};
    dom.inspectorBody.innerHTML = `
      <header class="topo-inspector-header">
        <p class="eyebrow">${escapeHtml(state.graph?.meta?.site_label || "Site")}</p>
        <h3>${escapeHtml(node.label)}</h3>
        ${node.organization ? `<p class="muted-text">${escapeHtml(node.organization)}</p>` : ""}
      </header>
      <dl class="topo-meta">
        ${metaRow("Location", node.location ? escapeHtml(node.location) : null)}
        ${metaRow("Systems", node.members.length)}
        ${metaRow("Live connections", `${node.connected} of ${node.members.length}`)}
        ${metaRow("Cores", capacity.cores_total ? `${fmt(capacity.cores_running)} / ${fmt(capacity.cores_total)}` : null)}
        ${metaRow("GPUs", capacity.gpus_total || null)}
      </dl>
      <h4 class="topo-inspector-subhead">Systems</h4>
      <ul class="topo-member-list">
        ${node.members
          .map(
            (member) => `<li>
              <button type="button" data-select="${escapeHtml(member.id)}">
                <span class="topo-dot topo-dot--${statusKey(member.status)}"></span>
                <span class="topo-member-name">${escapeHtml(member.label)}</span>
                <span class="topo-member-meta">${escapeHtml(member.scheduler || "—")}</span>
              </button>
            </li>`
          )
          .join("")}
      </ul>`;
    return;
  }

  if (node.kind === "monitor") {
    const summary = state.graph?.summary || {};
    dom.inspectorBody.innerHTML = `
      <header class="topo-inspector-header">
        <p class="eyebrow">Monitor</p>
        <h3>${escapeHtml(node.label)}</h3>
        <p class="muted-text">Collecting from ${summary.sites || 0} sites</p>
      </header>
      <dl class="topo-meta">
        ${metaRow("Systems", summary.systems)}
        ${metaRow("Live connections", summary.connected)}
        ${metaRow("Fleet uptime", formatPercent((summary.uptime_ratio || 0) * 100))}
        ${metaRow("Queues observed", summary.queues)}
        ${metaRow("Last collection", state.graph?.meta?.fleet_observed_at
          ? escapeHtml(formatRelativeTime(state.graph.meta.fleet_observed_at))
          : null)}
      </dl>`;
    return;
  }

  const connection = node.connection || {};
  const capacity = node.capacity || {};
  const queues = node.queues || {};
  const allocation = node.allocation;
  const connectedFor = formatDuration(connection.connected_for_seconds);
  const uptime = Number.isFinite(Number(connection.uptime_ratio))
    ? formatPercent(Number(connection.uptime_ratio) * 100, 1)
    : null;

  dom.inspectorBody.innerHTML = `
    <header class="topo-inspector-header">
      <div class="topo-inspector-title">
        <h3>${escapeHtml(node.label)}</h3>
        <span class="badge ${statusKey(node.status)}">${escapeHtml(node.status)}</span>
      </div>
      <p class="muted-text">${escapeHtml(node.site_label || node.site || "")}${
        node.note ? ` · ${escapeHtml(node.note)}` : ""
      }</p>
    </header>

    <div class="topo-actions">
      ${state.clusterPages
        ? `<a class="topo-action topo-action--primary" href="queues.html?cluster=${encodeURIComponent(node.slug)}">Queue health →</a>`
        : ""}
      <button type="button" class="topo-action" data-compare="${escapeHtml(node.id)}">
        ${state.compare.has(node.id) ? "Remove from compare" : "Compare"}
      </button>
      <a class="topo-action" href="index.html?system=${encodeURIComponent(node.slug)}">Briefing</a>
      ${state.clusterPages ? `<a class="topo-action" href="quota.html?cluster=${encodeURIComponent(node.slug)}">Quota</a>` : ""}
      ${state.clusterPages ? `<a class="topo-action" href="storage.html?cluster=${encodeURIComponent(node.slug)}">Storage</a>` : ""}
    </div>

    <dl class="topo-meta">
      ${metaRow("Login node", node.login ? `<code>${escapeHtml(node.login)}</code>` : null)}
      ${metaRow("Address", node.address ? `<code>${escapeHtml(node.address)}</code>` : null,
        "Resolved from the login hostname by the monitor")}
      ${metaRow("Scheduler", node.scheduler ? escapeHtml(node.scheduler) : null)}
      ${metaRow("Data source", node.connected ? "Live session + status page" : "Status page only")}
      ${metaRow("Connected for", connectedFor ? escapeHtml(connectedFor) : null,
        connection.connected_since ? `Since ${connection.connected_since}` : "")}
      ${metaRow(
        "Round trip",
        Number.isFinite(Number(connection.latency_ms))
          ? `${fmt(connection.latency_ms)} ms${Number(connection.latency_ms) > SLOW_LINK_MS ? " <small>slow</small>" : ""}`
          : null,
        "Time for one no-op command over the control plane (PW CLI + auth + SSH), not a network ping"
      )}
      ${metaRow("Uptime", uptime ? `${uptime} <small>(${connection.uptime_window_hours || 24}h)</small>` : null)}
      ${metaRow("Status changes", connection.transitions ?? null,
        "Status transitions recorded inside the uptime window")}
      ${metaRow("First seen", connection.first_seen ? escapeHtml(formatRelativeTime(connection.first_seen)) : null)}
      ${metaRow("Last observed", node.observed_at ? escapeHtml(formatRelativeTime(node.observed_at)) : null)}
    </dl>

    ${insightList(node.insights)}
    ${statusTimeline(connection)}

    ${capacity.cores_total
      ? `<h4 class="topo-inspector-subhead">Capacity</h4>
         <div class="topo-capacity">
           <p><strong>${fmt(capacity.cores_running)}</strong> of ${fmt(capacity.cores_total)} cores busy
             <span class="muted-text">(${formatPercent(capacity.utilization_percent)})</span></p>
           ${bar(capacity.utilization_percent, "accent")}
           <ul class="topo-chiplist">
             ${capacity.nodes_total ? `<li>${fmt(capacity.nodes_total)} nodes</li>` : ""}
             ${capacity.gpus_total ? `<li>${fmt(capacity.gpus_total)} GPUs</li>` : ""}
             ${queues.count ? `<li>${fmt(queues.count)} queues</li>` : ""}
           </ul>
         </div>`
      : `<p class="topo-hint">No live telemetry for this system — the monitor has no session to it, so
          capacity and queue depth come from whatever the site publishes.</p>`}

    ${queues.count
      ? `<h4 class="topo-inspector-subhead">Queues</h4>
         <dl class="topo-meta">
           ${metaRow("Running jobs", fmt(queues.running_jobs))}
           ${metaRow("Pending jobs", fmt(queues.pending_jobs))}
           ${metaRow("Cores waiting", fmt(queues.pending_cores))}
         </dl>
         ${queues.names?.length
            ? `<ul class="topo-chiplist">${queues.names.map((n) => `<li>${escapeHtml(n)}</li>`).join("")}</ul>`
            : ""}`
      : ""}

    ${allocation
      ? `<h4 class="topo-inspector-subhead">Allocation</h4>
         <p><strong>${fmt(allocation.hours_remaining)}</strong> of ${fmt(allocation.hours_allocated)} hours left
           <span class="muted-text">(${formatPercent(allocation.percent_remaining)})</span></p>
         ${bar(allocation.percent_remaining, allocation.percent_remaining < 15 ? "danger" : "success")}`
      : ""}

    ${connection.capabilities?.length
      ? `<h4 class="topo-inspector-subhead">Collected via</h4>
         <ul class="topo-chiplist">${connection.capabilities.map((c) => `<li>${escapeHtml(c)}</li>`).join("")}</ul>`
      : ""}
  `;
};

// ---------------------------------------------------------------------------
// Compare panel
// ---------------------------------------------------------------------------

const COMPARE_ROWS = [
  { label: "Status", value: (n) => `<span class="badge ${statusKey(n.status)}">${escapeHtml(n.status)}</span>` },
  { label: "Site", value: (n) => escapeHtml(n.site_label || n.site || "—") },
  { label: "Scheduler", value: (n) => escapeHtml(n.scheduler || "—") },
  { label: "Login node", value: (n) => (n.login ? `<code>${escapeHtml(n.login)}</code>` : "—") },
  {
    label: "Cores free",
    value: (n) => (n.capacity?.cores_total ? fmt(n.capacity.cores_free) : "—"),
    best: (n) => Number(n.capacity?.cores_free) || 0,
    prefer: "max",
  },
  {
    label: "Busy",
    value: (n) => formatPercent(n.capacity?.utilization_percent),
    best: (n) => (Number.isFinite(Number(n.capacity?.utilization_percent)) ? Number(n.capacity.utilization_percent) : null),
    prefer: "min",
  },
  {
    label: "Pending jobs",
    value: (n) => (n.queues?.count ? fmt(n.queues.pending_jobs) : "—"),
    best: (n) => (n.queues?.count ? Number(n.queues.pending_jobs) || 0 : null),
    prefer: "min",
  },
  {
    label: "Cores waiting",
    value: (n) => (n.queues?.count ? fmt(n.queues.pending_cores) : "—"),
    best: (n) => (n.queues?.count ? Number(n.queues.pending_cores) || 0 : null),
    prefer: "min",
  },
  {
    label: "Allocation left",
    value: (n) => (n.allocation ? formatPercent(n.allocation.percent_remaining) : "—"),
    best: (n) => (n.allocation ? Number(n.allocation.percent_remaining) : null),
    prefer: "max",
  },
  {
    label: "Round trip",
    value: (n) => (Number.isFinite(Number(n.connection?.latency_ms)) ? `${fmt(n.connection.latency_ms)} ms` : "—"),
    best: (n) => (Number.isFinite(Number(n.connection?.latency_ms)) ? Number(n.connection.latency_ms) : null),
    prefer: "min",
  },
  {
    label: "Uptime",
    value: (n) =>
      Number.isFinite(Number(n.connection?.uptime_ratio))
        ? formatPercent(Number(n.connection.uptime_ratio) * 100, 1)
        : "—",
    best: (n) => (Number.isFinite(Number(n.connection?.uptime_ratio)) ? Number(n.connection.uptime_ratio) : null),
    prefer: "max",
  },
  { label: "Open insights", value: (n) => String(n.insights?.length || 0) },
];

const renderComparePanel = () => {
  const nodes = [...state.compare]
    .map((id) => nodeById(id) || (state.graph?.nodes || []).find((n) => n.id === id))
    .filter(Boolean);

  if (!nodes.length) return false;

  dom.inspectorEmpty.hidden = true;
  dom.inspectorBody.hidden = false;

  const header = nodes
    .map(
      (node) => `<th scope="col">
        <span class="topo-compare-name">${escapeHtml(node.label)}</span>
        <button type="button" class="topo-compare-remove" data-compare="${escapeHtml(node.id)}"
          aria-label="Remove ${escapeHtml(node.label)} from the comparison">×</button>
      </th>`
    )
    .join("");

  const body = COMPARE_ROWS.map((row) => {
    // Highlight the winner per row, but only when the values are comparable.
    let winners = new Set();
    if (row.best) {
      const scored = nodes
        .map((node) => ({ id: node.id, score: row.best(node) }))
        .filter((entry) => Number.isFinite(entry.score));
      if (scored.length > 1) {
        const target =
          row.prefer === "min"
            ? Math.min(...scored.map((s) => s.score))
            : Math.max(...scored.map((s) => s.score));
        winners = new Set(scored.filter((s) => s.score === target).map((s) => s.id));
      }
    }
    const cells = nodes
      .map(
        (node) =>
          `<td class="${winners.has(node.id) ? "is-best" : ""}">${row.value(node)}</td>`
      )
      .join("");
    return `<tr><th scope="row">${escapeHtml(row.label)}</th>${cells}</tr>`;
  }).join("");

  const links = state.clusterPages
    ? `<tr><th scope="row"></th>${nodes
        .map(
          (node) =>
            `<td><a class="topo-action topo-action--primary" href="queues.html?cluster=${encodeURIComponent(node.slug)}">Queues →</a></td>`
        )
        .join("")}</tr>`
    : "";

  dom.inspectorBody.innerHTML = `
    <header class="topo-inspector-header">
      <div class="topo-inspector-title">
        <h3>Comparing ${nodes.length}</h3>
        <button type="button" class="topo-action" id="topo-compare-clear">Clear</button>
      </div>
      <p class="muted-text">Best value in each row is highlighted. Add up to ${COMPARE_LIMIT}.</p>
    </header>
    <div class="topo-compare-scroll">
      <table class="topo-compare">
        <thead><tr><th scope="col"></th>${header}</tr></thead>
        <tbody>${body}${links}</tbody>
      </table>
    </div>`;
  return true;
};

/** Decide what the right-hand panel shows: comparison, one node, or nothing. */
const renderPanel = () => {
  if (state.compare.size && renderComparePanel()) return;
  const node = state.selectedId ? nodeById(state.selectedId) : null;
  if (node) {
    renderInspector(node);
    return;
  }
  dom.inspectorBody.hidden = true;
  dom.inspectorEmpty.hidden = false;
};

const toggleCompare = (id) => {
  if (!id) return;
  const node = nodeById(id);
  if (!node || node.kind !== "system") return;
  if (state.compare.has(id)) {
    state.compare.delete(id);
  } else {
    if (state.compare.size >= COMPARE_LIMIT) {
      setStatus(`Comparison holds ${COMPARE_LIMIT} systems — remove one first.`, "info");
      return;
    }
    state.compare.add(id);
  }
  state.view.nodes.forEach((n) => {
    const element = state.elements.get(n.id);
    if (element) element.classList.toggle("is-compared", state.compare.has(n.id));
  });
  renderPanel();
  syncLocation();
};

const clearCompare = () => {
  state.compare.clear();
  state.elements.forEach((el) => el.classList?.remove("is-compared"));
  renderPanel();
  syncLocation();
};

// ---------------------------------------------------------------------------
// Summary cards
// ---------------------------------------------------------------------------

const renderSummary = () => {
  const summary = state.graph?.summary || {};
  const capacity = summary.capacity || {};
  dom.sites.textContent = summary.sites ?? "--";
  dom.systems.textContent = summary.systems ?? "--";
  dom.connected.textContent =
    summary.connected !== undefined ? `${summary.connected} / ${summary.systems}` : "--";
  dom.cores.textContent = capacity.cores_total ? fmt(capacity.cores_total) : "—";
  dom.cores.title = capacity.cores_total
    ? `${fmt(capacity.cores_running)} cores busy (${formatPercent(capacity.utilization_percent)})`
    : "No connected system is reporting node inventory yet";
  dom.siteLabel.textContent = state.graph?.meta?.site_label
    ? `${state.graph.meta.site_label}s`
    : "Sites";

  const statuses = Object.keys(summary.status_counts || {});
  const current = dom.statusFilter.value;
  dom.statusFilter.innerHTML =
    '<option value="">All</option>' +
    statuses.map((s) => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join("");
  if (statuses.includes(current)) dom.statusFilter.value = current;

  const observed = state.graph?.meta?.fleet_observed_at;
  dom.footer.textContent = observed
    ? `Topology assembled ${formatRelativeTime(state.graph.meta.generated_at)} · fleet observed ${formatRelativeTime(observed)}`
    : "Topology assembled from fleet status and live cluster telemetry";
};

// ---------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------

const setStatus = (message, tone = "info") => {
  if (!dom.status) return;
  if (!message) {
    dom.status.hidden = true;
    dom.status.textContent = "";
    return;
  }
  dom.status.hidden = false;
  dom.status.textContent = message;
  dom.status.dataset.variant = tone;
};

const loadData = async ({ silent = false } = {}) => {
  if (state.loading) return;
  state.loading = true;
  if (!silent) setStatus("Loading topology…");
  try {
    const response = await fetch(`${DATA_URL}?t=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const graph = await response.json();
    state.graph = graph;
    state.lastUpdated = Date.now();

    const progress = graph?.meta?.collection_progress;
    if (progress && progress.phase === "warming_up" && !progress.first_sweep_complete) {
      setStatus(
        `Collecting telemetry — ${progress.collected || 0} of ${progress.total || "?"} clusters swept.`,
        "info"
      );
    } else if (!graph.nodes?.length) {
      setStatus("No systems reported yet. The first collection sweep may still be running.", "info");
    } else {
      setStatus("");
    }

    renderSummary();
    const firstRender = !state.positions.size;
    renderGraph({ animate: !firstRender });
    if (firstRender) requestAnimationFrame(() => fitToView({ animate: false }));

    // Re-render the panel so live values (queue depth, uptime) refresh.
    if (state.selectedId && !nodeById(state.selectedId)) {
      clearSelection();
    } else {
      renderPanel();
    }
  } catch (err) {
    console.error("Unable to load topology", err);
    setStatus(`Unable to load topology (${err.message}).`, "error");
  } finally {
    state.loading = false;
  }
};

// ---------------------------------------------------------------------------
// URL state
// ---------------------------------------------------------------------------

const readLocation = () => {
  const params = new URLSearchParams(window.location.search);
  const layout = params.get("layout");
  const group = params.get("group");
  if (layout && LAYOUTS.has(layout)) state.layout = layout;
  else state.layout = window.APP_CONFIG?.topologyLayout && LAYOUTS.has(window.APP_CONFIG.topologyLayout)
    ? window.APP_CONFIG.topologyLayout
    : "hierarchy";
  if (group && GROUPINGS.has(group)) state.group = group;
  state.filters.search = params.get("q") || "";
  state.filters.status = (params.get("status") || "").toUpperCase();
  state.filters.connectedOnly = params.get("connected") === "1";
  const node = params.get("node");
  if (node) state.selectedId = node;
  const compare = params.get("compare");
  if (compare) {
    compare
      .split(",")
      .filter(Boolean)
      .slice(0, COMPARE_LIMIT)
      .forEach((id) => state.compare.add(id));
  }
};

const syncLocation = () => {
  const params = new URLSearchParams();
  params.set("layout", state.layout);
  params.set("group", state.group);
  if (state.filters.search) params.set("q", state.filters.search);
  if (state.filters.status) params.set("status", state.filters.status);
  if (state.filters.connectedOnly) params.set("connected", "1");
  if (state.selectedId) params.set("node", state.selectedId);
  if (state.compare.size) params.set("compare", [...state.compare].join(","));
  window.history.replaceState({}, "", `${window.location.pathname}?${params.toString()}`);
};

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

const cacheDom = () => {
  dom.svg = document.getElementById("topo-svg");
  dom.canvas = document.getElementById("topo-canvas");
  dom.tooltip = document.getElementById("topo-tooltip");
  dom.empty = document.getElementById("topo-empty");
  dom.status = document.getElementById("topo-status");
  dom.inspectorBody = document.getElementById("topo-inspector-body");
  dom.inspectorEmpty = document.getElementById("topo-inspector-empty");
  dom.sites = document.getElementById("topo-sites");
  dom.systems = document.getElementById("topo-systems");
  dom.connected = document.getElementById("topo-connected");
  dom.cores = document.getElementById("topo-cores");
  dom.siteLabel = document.getElementById("topo-site-label");
  dom.statusFilter = document.getElementById("topo-status-filter");
  dom.search = document.getElementById("topo-search");
  dom.connectedOnly = document.getElementById("topo-connected-only");
  dom.groupSelect = document.getElementById("topo-group");
  dom.layoutButtons = document.getElementById("topo-layout");
  dom.footer = document.getElementById("topo-footer");
  dom.compareToggle = document.getElementById("topo-compare");
};

const applyControlsFromState = () => {
  dom.search.value = state.filters.search;
  dom.connectedOnly.checked = state.filters.connectedOnly;
  dom.groupSelect.value = state.group;
  [...dom.layoutButtons.querySelectorAll("button")].forEach((btn) => {
    const active = btn.dataset.layout === state.layout;
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
};

const setLayout = (layout) => {
  if (!LAYOUTS.has(layout) || layout === state.layout) return;
  state.layout = layout;
  state.pinned.clear();
  applyControlsFromState();
  renderGraph({ animate: true, fit: true });
  syncLocation();
};

const bindEvents = () => {
  document.getElementById("refresh-btn")?.addEventListener("click", () => loadData());
  document.getElementById("topo-fit")?.addEventListener("click", () => fitToView());
  document.getElementById("topo-zoom-in")?.addEventListener("click", () => zoomBy(1.25));
  document.getElementById("topo-zoom-out")?.addEventListener("click", () => zoomBy(0.8));
  document.getElementById("topo-zoom-reset")?.addEventListener("click", () => fitToView());
  document.getElementById("topo-export")?.addEventListener("click", exportSvg);

  dom.layoutButtons.addEventListener("click", (event) => {
    const btn = event.target.closest("button[data-layout]");
    if (btn) setLayout(btn.dataset.layout);
  });

  dom.groupSelect.addEventListener("change", () => {
    state.group = GROUPINGS.has(dom.groupSelect.value) ? dom.groupSelect.value : "site";
    renderGraph({ animate: true, fit: true });
    syncLocation();
  });

  let searchTimer;
  dom.search.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.filters.search = dom.search.value.trim();
      renderGraph({ animate: true, fit: true });
      syncLocation();
    }, 180);
  });

  dom.statusFilter.addEventListener("change", () => {
    state.filters.status = dom.statusFilter.value.toUpperCase();
    renderGraph({ animate: true, fit: true });
    syncLocation();
  });

  dom.connectedOnly.addEventListener("change", () => {
    state.filters.connectedOnly = dom.connectedOnly.checked;
    renderGraph({ animate: true, fit: true });
    syncLocation();
  });

  // --- graph interaction
  dom.svg.addEventListener("pointerover", (event) => {
    const group = event.target.closest(".topo-node");
    if (!group) return;
    const node = nodeById(group.dataset.id);
    if (!node) return;
    setHover(node.id);
    showTooltip(node, event);
  });

  dom.svg.addEventListener("pointermove", (event) => {
    if (!state.hoverId || dom.tooltip.hidden) return;
    const node = nodeById(state.hoverId);
    if (node) showTooltip(node, event);
  });

  dom.svg.addEventListener("pointerout", (event) => {
    const group = event.target.closest(".topo-node");
    if (!group) return;
    if (group.contains(event.relatedTarget)) return;
    setHover(null);
    hideTooltip();
  });

  dom.svg.addEventListener("click", (event) => {
    // A pan that ends on empty canvas must not read as "deselect".
    if (state.suppressClick) {
      state.suppressClick = false;
      return;
    }
    const group = event.target.closest(".topo-node");
    if (!group) {
      clearSelection();
      return;
    }
    // Shift-click always pins into the comparison; in compare mode a plain
    // click does too, so you can rack up systems without a modifier.
    if (event.shiftKey || (state.compareMode && group.dataset.kind === "system")) {
      toggleCompare(group.dataset.id);
      return;
    }
    selectNode(group.dataset.id, { navigateIfSelected: true });
  });

  dom.svg.addEventListener("dblclick", (event) => {
    const group = event.target.closest(".topo-node");
    if (!group) return;
    event.preventDefault();
    openQueueHealth(nodeById(group.dataset.id));
  });

  dom.svg.addEventListener("keydown", (event) => {
    const group = event.target.closest(".topo-node");
    if (!group) return;
    if (event.key === "Enter") {
      event.preventDefault();
      const node = nodeById(group.dataset.id);
      if (node?.kind === "system") openQueueHealth(node);
      else selectNode(group.dataset.id);
    } else if (event.key === " ") {
      event.preventDefault();
      selectNode(group.dataset.id);
    }
  });

  dom.inspectorBody.addEventListener("click", (event) => {
    const select = event.target.closest("button[data-select]");
    if (select) {
      selectNode(select.dataset.select);
      return;
    }
    const compare = event.target.closest("button[data-compare]");
    if (compare) {
      toggleCompare(compare.dataset.compare);
      return;
    }
    if (event.target.closest("#topo-compare-clear")) clearCompare();
  });

  dom.compareToggle?.addEventListener("click", () => {
    state.compareMode = !state.compareMode;
    dom.compareToggle.classList.toggle("is-active", state.compareMode);
    dom.compareToggle.setAttribute("aria-pressed", state.compareMode ? "true" : "false");
    setStatus(
      state.compareMode ? "Compare mode: click systems to add them to the comparison." : ""
    );
  });

  // --- zoom & pan
  dom.canvas.addEventListener(
    "wheel",
    (event) => {
      event.preventDefault();
      zoomBy(event.deltaY < 0 ? 1.12 : 0.89, { x: event.clientX, y: event.clientY });
    },
    { passive: false }
  );

  let drag = null;
  dom.svg.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    const group = event.target.closest(".topo-node");
    if (group && state.layout === "force") {
      drag = { type: "node", id: group.dataset.id, moved: false };
    } else {
      drag = { type: "pan", x: event.clientX, y: event.clientY, origin: { ...state.transform } };
      dom.canvas.classList.add("is-panning");
    }
    dom.svg.setPointerCapture(event.pointerId);
  });

  dom.svg.addEventListener("pointermove", (event) => {
    if (!drag) return;
    if (drag.type === "pan") {
      const dx = event.clientX - drag.x;
      const dy = event.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 4) drag.moved = true;
      state.transform = { ...state.transform, x: drag.origin.x + dx, y: drag.origin.y + dy };
      drawFrame();
      return;
    }
    const rect = dom.canvas.getBoundingClientRect();
    const world = {
      x: (event.clientX - rect.left - state.transform.x) / state.transform.k,
      y: (event.clientY - rect.top - state.transform.y) / state.transform.k,
    };
    state.pinned.set(drag.id, world);
    state.targets.set(drag.id, world);
    state.positions.set(drag.id, world);
    drag.moved = true;
    reheatForce();
    drawFrame();
  });

  const endDrag = (event) => {
    if (!drag) return;
    if (drag.type === "pan") dom.canvas.classList.remove("is-panning");
    state.suppressClick = Boolean(drag.moved);
    dom.svg.releasePointerCapture?.(event.pointerId);
    drag = null;
  };
  dom.svg.addEventListener("pointerup", endDrag);
  dom.svg.addEventListener("pointercancel", endDrag);

  window.addEventListener("resize", () => fitToView({ animate: false }));

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearInterval(state.pollHandle);
      state.pollHandle = null;
    } else if (!state.pollHandle) {
      loadData({ silent: true });
      state.pollHandle = setInterval(() => loadData({ silent: true }), POLL_MS);
    }
  });
};

const exportSvg = () => {
  const clone = dom.svg.cloneNode(true);
  const rect = dom.canvas.getBoundingClientRect();
  clone.setAttribute("xmlns", SVG_NS);
  clone.setAttribute("width", Math.round(rect.width));
  clone.setAttribute("height", Math.round(rect.height));
  // Inline the computed palette so the exported file is readable outside
  // the app, where the theme's CSS variables do not exist.
  const styles = getComputedStyle(document.documentElement);
  const vars = ["--panel", "--panel-alt", "--text", "--muted", "--border", "--accent", "--success", "--alert", "--danger", "--bg"];
  const style = document.createElementNS(SVG_NS, "style");
  style.textContent = `:root{${vars.map((v) => `${v}:${styles.getPropertyValue(v).trim()}`).join(";")}}
    .topo-node-label{font:600 12px "Plus Jakarta Sans",sans-serif;fill:var(--text)}
    .topo-node-sublabel{font:500 10px "Plus Jakarta Sans",sans-serif;fill:var(--muted)}
    .topo-node-glyph{font:700 11px "Plus Jakarta Sans",sans-serif;fill:var(--bg)}
    .topo-edge{stroke:var(--border);stroke-width:1.5;fill:none}
    .topo-node--up .topo-node-core{fill:var(--success)}
    .topo-node--degraded .topo-node-core{fill:var(--alert)}
    .topo-node--down .topo-node-core{fill:var(--danger)}
    .topo-node--unknown .topo-node-core{fill:var(--muted)}
    .topo-node--group .topo-node-core{fill:var(--panel-alt);stroke:var(--border);stroke-width:2}
    .topo-node--monitor .topo-node-core{fill:var(--accent)}
    .topo-lane{fill:var(--panel-alt);opacity:.35}`;
  clone.prepend(style);

  const blob = new Blob([new XMLSerializer().serializeToString(clone)], {
    type: "image/svg+xml;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `fleet-topology-${state.layout}.svg`;
  link.click();
  URL.revokeObjectURL(url);
};

const applyConfigBranding = () => {
  const title = window.APP_CONFIG?.title || "HPC Status Monitor";
  const eyebrow = document.getElementById("header-eyebrow");
  if (eyebrow) eyebrow.textContent = window.APP_CONFIG?.eyebrow || "HPC STATUS";
  document.title = `Topology | ${title}`;
};

const bootstrap = async () => {
  cacheDom();
  initThemeToggle();
  initHelpPanel();
  initQuickTips();
  initBrand();
  initNav();
  applyConfigBranding();
  readLocation();
  applyControlsFromState();
  ensureScaffold();
  bindEvents();

  const pendingSelection = state.selectedId;
  state.selectedId = null;
  await loadData();
  if (pendingSelection) selectNode(pendingSelection);
  else if (state.compare.size) renderPanel();

  state.pollHandle = setInterval(() => loadData({ silent: true }), POLL_MS);
};

// Run once, whenever this module happens to execute. Guarding on a flag
// (rather than trusting a single DOMContentLoaded) keeps every listener
// registered exactly once — double-registering would make one click toggle
// a node into the comparison and straight back out again.
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
