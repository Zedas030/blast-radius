/* Blast-Radius web UI */
"use strict";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const color = (t) => css(`--t-${t}`) || "#888";
const pct = (x) => `${Math.round((x || 0) * 100)} %`;
const CHEV = `<svg class="chev" viewBox="0 0 24 24"><path d="m9 6 6 6-6 6"/></svg>`;

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 200)}`);
  return r.json();
}
function store(key, val) {
  try { if (val === undefined) return localStorage.getItem(key); localStorage.setItem(key, val); } catch (e) { /* private mode */ }
  return null;
}

/* Rings from the centre outwards: a failure in the middle spreads to the outside. */
const RINGS = [
  { types: ["hardware"], name: "Hardware" },
  { types: ["network"], name: "Network" },
  { types: ["host", "cloud"], name: "Hosts & cloud" },
  { types: ["service"], name: "Services" },
  { types: ["integration"], name: "Integrations" },
  { types: ["device"], name: "Devices" },
  { types: ["entity"], name: "Entities" },
  { types: ["automation", "script", "dashboard"], name: "Automations" },
  { types: ["function"], name: "Your functions" },
];
const LAYERS = [
  { key: "function", label: "Functions", types: ["function"], on: true },
  { key: "infra", label: "Infrastructure", types: ["hardware", "network", "cloud", "host", "service"], on: true },
  { key: "integration", label: "Integrations", types: ["integration"], on: true },
  { key: "automation", label: "Automations", types: ["automation", "script", "dashboard"], on: true },
  { key: "device", label: "Devices", types: ["device"], on: false },
  { key: "entity", label: "Entities", types: ["entity"], on: false },
];
const SIZE = { function: 20, hardware: 16, network: 15, host: 16, cloud: 12, service: 11, integration: 10, device: 8,
  automation: 8, script: 8, dashboard: 9, entity: 5 };
const SHAPE = { function: "star", hardware: "round-rectangle", network: "diamond", host: "round-rectangle", dashboard: "round-rectangle" };
const ALWAYS_LABEL = new Set(["function"]);                       // labelled at every zoom
const KEY_TYPES = new Set(["function", "hardware", "network", "host"]);  // labelled when affected
const ZOOM_LABEL = new Set(["hardware", "network", "host", "cloud"]); // labelled once zoomed in

let cy = null;
let G = null;            // full graph from the API
let byId = {};
let roots = [];
let layerOn = {};

/* ------------------------------------------------------------------ graph */
function visibleTypes() {
  const s = new Set();
  LAYERS.forEach((l) => { if (layerOn[l.key]) l.types.forEach((t) => s.add(t)); });
  return s;
}

// Edges between visible nodes; hidden nodes in between are skipped, so hiding a layer keeps the structure.
function collapsedEdges(vis) {
  const deps = {};
  G.edges.forEach((e) => { (deps[e.src] = deps[e.src] || []).push(e); });
  const out = new Map();
  G.nodes.forEach((n) => {
    if (!vis.has(n.type)) return;
    const seen = new Set([n.id]);
    const stack = (deps[n.id] || []).map((e) => [e, 0]);
    while (stack.length) {
      const [e, depth] = stack.pop();
      if (seen.has(e.dst)) continue;
      seen.add(e.dst);
      const t = byId[e.dst];
      if (!t) continue;
      if (vis.has(t.type)) {
        const k = `${n.id}>${e.dst}`;
        if (!out.has(k)) out.set(k, { source: n.id, target: e.dst, kind: e.kind, soft: !!e.soft, via: depth > 0 });
      } else if (depth < 4) {
        (deps[e.dst] || []).forEach((x) => stack.push([x, depth + 1]));
      }
    }
  });
  return [...out.values()];
}

// Radial layered layout: rings by type, inside a ring nodes are ordered by the angle of what they depend on.
function radialPositions(nodes, edges) {
  const ringOf = {};
  RINGS.forEach((r, i) => r.types.forEach((t) => { ringOf[t] = i; }));
  const rings = RINGS.map(() => []);
  nodes.forEach((n) => rings[ringOf[n.type]].push(n));
  const deps = {};
  edges.forEach((e) => { (deps[e.source] = deps[e.source] || []).push(e.target); });
  const pos = {}, angle = {}, guides = [];
  let r = 0;
  rings.forEach((ring, i) => {
    if (!ring.length) return;
    const t0 = ring[0].type;
    const minGap = t0 === "entity" ? 7 : t0 === "device" ? 13 : ALWAYS_LABEL.has(t0) ? 78 : 24;
    r = Math.max(r === 0 ? 80 : r + 72, (ring.length * minGap) / (2 * Math.PI));
    if (ring.length === 1 && r === 70 && i === 0) r = 0;
    const keyed = ring.map((n) => {
      const a = (deps[n.id] || []).map((d) => angle[d]).filter((x) => x !== undefined);
      let k;
      if (a.length) {   // circular mean of the dependencies' angles
        const sx = a.reduce((s, v) => s + Math.cos(v), 0), sy = a.reduce((s, v) => s + Math.sin(v), 0);
        k = Math.atan2(sy, sx);
      } else k = Math.PI * 4 + n.label.toLowerCase().charCodeAt(0) / 50;
      return { n, k };
    });
    keyed.sort((x, y) => x.k - y.k || x.n.label.localeCompare(y.n.label));
    const offset = keyed.length && keyed[0].k < Math.PI * 3 ? keyed[0].k : -Math.PI / 2;
    keyed.forEach(({ n }, j) => {
      const a = offset + (2 * Math.PI * j) / keyed.length;
      angle[n.id] = a;
      pos[n.id] = { x: r * Math.cos(a), y: r * Math.sin(a) };
    });
    guides.push({ r, name: RINGS[i].name });
  });
  return { pos, guides };
}

function buildGraph() {
  const vis = visibleTypes();
  const nodes = G.nodes.filter((n) => vis.has(n.type));
  const edges = collapsedEdges(vis);
  const { pos, guides } = radialPositions(nodes, edges);
  const els = [];
  guides.forEach((g, i) => {
    if (g.r <= 0) return;
    els.push({ group: "nodes", data: { id: `ring${i}`, ring: 1, w: g.r * 2, name: g.name }, position: { x: 0, y: 0 },
      selectable: false, grabbable: false, classes: "ring" });
  });
  nodes.forEach((n) => els.push({ group: "nodes", data: { id: n.id, label: n.label, type: n.type }, position: pos[n.id],
    classes: ALWAYS_LABEL.has(n.type) ? "lab" : ZOOM_LABEL.has(n.type) ? "lab2" : "" }));
  edges.forEach((e, i) => els.push({ group: "edges", data: { id: `e${i}`, ...e } }));
  if (cy) cy.destroy();
  cy = cytoscape({
    container: $("#cy"), elements: els, layout: { name: "preset", fit: true, padding: 30 },
    wheelSensitivity: 0.25, minZoom: 0.08, maxZoom: 4, boxSelectionEnabled: false,
    style: [
      { selector: "node", style: {
        "background-color": (n) => color(n.data("type")), width: (n) => SIZE[n.data("type")] || 8,
        height: (n) => SIZE[n.data("type")] || 8, shape: (n) => SHAPE[n.data("type")] || "ellipse",
        "border-width": 0, label: "", color: css("--ink-2"), "font-size": 9, "text-valign": "bottom", "text-margin-y": 4,
        "text-wrap": "ellipsis", "text-max-width": 130, "min-zoomed-font-size": 6,
        "text-background-color": css("--bg"), "text-background-opacity": 0.75, "text-background-padding": 2,
        "text-background-shape": "roundrectangle", "transition-property": "opacity, background-color", "transition-duration": 180 } },
      { selector: "node.lab", style: { label: "data(label)", "min-zoomed-font-size": 0 } },
      { selector: "node.lab2", style: { label: "data(label)", "font-size": 10, "min-zoomed-font-size": 10 } },
      { selector: "node[type='function']", style: { "font-size": 10.5, "font-weight": 600, color: css("--ink") } },
      { selector: "node[type='hardware'], node[type='host'], node[type='network']", style: { "font-size": 10, color: css("--ink") } },
      { selector: "node.ring", style: { shape: "ellipse", width: "data(w)", height: "data(w)", "background-opacity": 0,
        "border-width": 1, "border-color": css("--line"), "border-style": "dashed", label: "data(name)", "font-size": 10,
        color: css("--muted"), "text-valign": "top", "text-margin-y": -2, "text-background-opacity": 0, events: "no", "text-opacity": 0.6,
        "z-index": 0, "min-zoomed-font-size": 8 } },
      { selector: "edge", style: { width: 0.7, "line-color": css("--line-2"), opacity: 0.45, "curve-style": "haystack",
        "haystack-radius": 0.2, "transition-property": "opacity, line-color", "transition-duration": 180 } },
      { selector: "edge[kind='powered_by']", style: { "line-color": "#d6a23a", opacity: 0.6 } },
      { selector: "node.hover", style: { label: "data(label)", "z-index": 30, color: css("--ink") } },
      { selector: ".dim", style: { opacity: 0.12 } },
      { selector: "node.ring.dim", style: { opacity: 0.5 } },
      { selector: "node.failed", style: { "background-color": css("--fail"), "underlay-color": css("--fail"), "underlay-padding": 5,
        "underlay-opacity": 0.22, "underlay-shape": "ellipse", color: css("--ink"), opacity: 1, "z-index": 20 } },
      { selector: "node.failed.key", style: { label: "data(label)" } },
      { selector: "node.degraded", style: { "background-color": css("--deg"), "underlay-color": css("--deg"), "underlay-padding": 4,
        "underlay-opacity": 0.2, "underlay-shape": "ellipse", opacity: 1, "z-index": 15 } },
      { selector: "node.degraded[type='function']", style: { label: "data(label)" } },
      { selector: "node.risk", style: { "border-width": 2, "border-color": css("--risk"), "border-style": "dashed", opacity: 1 } },
      { selector: "node.root", style: { "background-color": css("--fail"), width: 26, height: 26, "border-width": 3,
        "border-color": css("--bg"), "underlay-color": css("--fail"), "underlay-padding": 10, "underlay-opacity": 0.3,
        "underlay-shape": "ellipse", label: "data(label)", "font-size": 12, "font-weight": 700, color: css("--ink"), "z-index": 40 } },
      { selector: "edge.hot", style: { "line-color": css("--fail"), opacity: 0.75, width: 1.4, "curve-style": "bezier", "control-point-step-size": 20 } },
    ],
  });
  const fit = () => {   // labels keep a readable size on screen whatever the zoom
    const z = cy.zoom();
    cy.style().selector("node.lab, node.hover, node.failed.key, node.degraded[type='function']")
      .style("font-size", Math.min(40, 10.5 / z)).style("text-max-width", 150 / z)
      .selector("node.root").style("font-size", Math.min(48, 12.5 / z))
      .selector("node.ring").style("font-size", Math.min(40, 10 / z))
      .update();
  };
  cy.on("zoom", fit);
  fit();
  cy.on("tap", "node", (ev) => {
    const id = ev.target.id();
    if (ev.target.hasClass("ring")) return;
    if (ev.originalEvent && ev.originalEvent.shiftKey) roots = roots.includes(id) ? roots.filter((r) => r !== id) : [...roots, id];
    else roots = [id];
    roots.length ? simulate(roots) : resetSim();
  });
  cy.on("tap", (ev) => { if (ev.target === cy && roots.length) resetSim(); });
  cy.on("mouseover", "node", (ev) => { if (!ev.target.hasClass("ring")) ev.target.addClass("hover"); });
  cy.on("mouseout", "node", (ev) => ev.target.removeClass("hover"));
  renderLegend(vis);
  if (roots.length) simulate(roots, false);
}

async function loadGraph() {
  G = await api("/api/graph?entities=true");
  byId = {};
  G.nodes.forEach((n) => { byId[n.id] = n; });
  renderLayers();
  if (mapBuilt) buildGraph();
}

function renderLayers() {
  const counts = {};
  G.nodes.forEach((n) => { counts[n.type] = (counts[n.type] || 0) + 1; });
  $("#layers").innerHTML = LAYERS.map((l) => {
    const c = l.types.reduce((s, t) => s + (counts[t] || 0), 0);
    if (!c) return "";
    return `<button data-layer="${l.key}" class="${layerOn[l.key] ? "on" : ""}" aria-pressed="${!!layerOn[l.key]}">
      <span class="dot" style="background:${color(l.types[0])}"></span>${l.label}<span class="c">${c}</span></button>`;
  }).join("");
}

function renderLegend(vis) {
  const shown = RINGS.flatMap((r) => r.types).filter((t) => vis.has(t) && G.nodes.some((n) => n.type === t));
  $("#legend").innerHTML = shown.map((t) => `<span><i class="sw" style="background:${color(t)}"></i>${t}</span>`).join("") +
    `<span><i class="sw" style="background:${css("--fail")}"></i>fails</span>` +
    `<span><i class="sw" style="background:${css("--deg")}"></i>degraded</span>` +
    `<span><i class="sw risk"></i>lost redundancy</span>`;
}

function resetSim() {
  roots = [];
  if (cy) cy.elements().removeClass("dim failed degraded risk root hot");
  $("#canvas-hint").classList.remove("hide");
  renderIntro();
}

/* ------------------------------------------------------------------ side panel */
function typeTag(t) {
  return `<span class="type-tag"><span class="dot" style="background:${color(t)}"></span>${esc(t)}</span>`;
}

function steps(path, cls) {
  if (!path || path.length < 2) return "";
  const col = cls === "deg" ? "var(--deg)" : cls === "risk" ? "var(--risk)" : "var(--fail)";
  return `<ol class="steps" style="--step:${col}">${path.map((s, i) =>
    `<li>${i && s.edge ? `<span class="e">${esc(s.edge)}</span>` : ""}<span class="nm">${esc(s.label)}</span></li>`).join("")}</ol>`;
}

function items(list, cls) {
  return `<div class="list">${list.map((it) => `<details class="item ${cls}"><summary><span class="dot"></span>
    <span class="n">${esc(it.label)}</span>${CHEV}</summary>${steps(it.path, cls)}</details>`).join("")}</div>`;
}

async function renderIntro() {
  let spofs = [];
  try { spofs = (await api("/api/spof")).spofs.slice(0, 4); } catch (e) { /* graph not ready */ }
  const asks = [["the Pi crashes", "What happens if the Pi crashes?"], ["the router fails", "What happens if the router fails?"],
    ["Zigbee goes down", "What happens if the zigbee coordinator fails?"], ["the internet is down", "What happens if the internet fails?"],
    ["the NAS fails", "What happens if the NAS fails?"]];
  $("#impact").innerHTML = `<div class="intro"><h2>What fails if…</h2>
      <p>Pick a node on the map, search above, or start with a question.</p>
      <div class="chips">${asks.map(([l, q]) => `<button class="chip" data-ask="${esc(q)}">${esc(l)}</button>`).join("")}</div></div>
    ${spofs.length ? `<div><p class="label">Weak spots</p><div class="spof-mini">${spofs.map((s) =>
      `<button data-node="${esc(s.id)}"><span class="dot" style="background:${color(s.type)}"></span><span class="n">${esc(s.label)}</span>
       <span class="p">${pct(Math.max(s.function_share, s.entity_share))}</span></button>`).join("")}</div></div>` : ""}`;
}

async function simulate(ids, center = true) {
  const r = await api(`/api/impact?${ids.map((i) => `node=${encodeURIComponent(i)}`).join("&")}`);
  showImpact(r);
  $("#canvas-hint").classList.add("hide");
  if (!cy) return;
  const failed = new Set(r.failed_ids), degraded = new Set(r.degraded_ids), risk = new Set(r.at_risk_ids || []);
  cy.batch(() => {
    cy.elements().removeClass("dim failed degraded risk root hot key");
    cy.nodes().forEach((n) => {
      if (KEY_TYPES.has(n.data("type"))) n.addClass("key");
      const id = n.id();
      if (n.hasClass("ring")) n.addClass("dim");
      else if (ids.includes(id)) n.addClass("root");
      else if (failed.has(id)) n.addClass("failed");
      else if (degraded.has(id)) n.addClass("degraded");
      else if (risk.has(id)) n.addClass("risk");
      else n.addClass("dim");
    });
    // only the edges along the explained paths (root → … → affected node), mapped onto what is visible
    const hot = new Set();
    const allPaths = [...r.functions.failed, ...r.functions.degraded, ...r.groups.flatMap((g) => [...g.failed, ...g.degraded])];
    allPaths.forEach((it) => {
      const vis = (it.path || []).map((p) => p.id).filter((id) => cy.getElementById(id).length);
      for (let i = 1; i < vis.length; i++) hot.add(`${vis[i]}>${vis[i - 1]}`);
    });
    cy.edges().forEach((e) => e.addClass(hot.has(`${e.data("source")}>${e.data("target")}`) ? "hot" : "dim"));
  });
  if (center) {
    const hit = cy.nodes(".root, .failed, .degraded");
    if (hit.length) cy.animate({ fit: { eles: hit, padding: 50 } }, { duration: 260, easing: "ease-out-cubic" });
  }
}

function showImpact(r) {
  const fnF = r.functions.failed, fnD = r.functions.degraded, total = r.functions.total || 0;
  const ent = r.counts.entity || { failed: 0 };
  const name = r.roots.map((x) => esc(x.label)).join(" + ");
  let html = `<div class="imp-head"><div class="t"><div class="kicker">If this fails</div><h2>${name}</h2>
      <div>${[...new Set(r.roots.map((x) => x.type))].map(typeTag).join(" ")}</div></div>
      <button class="close" id="imp-close" aria-label="Clear"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg></button></div>
    <div class="stats">
      <div class="stat fail"><b>${fnF.length}<small> / ${total}</small></b><span>functions down</span>
        <div class="bar"><i style="width:${total ? (100 * fnF.length) / total : 0}%"></i></div></div>
      <div class="stat deg"><b>${fnD.length}</b><span>degraded</span></div>
      <div class="stat"><b>${ent.failed}</b><span>entities affected</span></div>
    </div>`;
  html += `<div><p class="label">Stops working for you</p>${fnF.length ? items(fnF, "fail") : `<div class="empty-state">Nothing stops completely.</div>`}</div>`;
  if (fnD.length) html += `<div><p class="label">Degraded</p>${items(fnD, "deg")}</div>`;
  if (r.at_risk && r.at_risk.length) html += `<div><p class="label">Still works, redundancy lost</p>${items(r.at_risk, "risk")}</div>`;
  const tech = r.groups.filter((g) => g.type !== "function");
  if (tech.length) {
    html += `<details class="tech"><summary><span class="n">Technical details</span><span class="num">${tech.reduce((s, g) => s + g.failed.length, 0)} nodes</span>${CHEV}</summary>
      <div class="rows">${tech.map((g) => `<details><summary><span class="dot" style="background:${color(g.type)}"></span>${esc(g.title)}
        <span class="num"><span class="f">${g.failed.length}</span>${g.degraded.length ? ` · <span class="d">${g.degraded.length}</span>` : ""}</span></summary>
        <div class="names">${g.failed.slice(0, 300).map((i) => `<div>${esc(i.label)}</div>`).join("")}${g.degraded.slice(0, 100).map((i) => `<div style="color:var(--deg)">${esc(i.label)}</div>`).join("")}</div>
      </details>`).join("")}</div></details>`;
  }
  html += `<p class="small muted">${esc(r.summary)}</p>`;
  $("#impact").innerHTML = html;
  $("#imp-close").onclick = resetSim;
}

/* ------------------------------------------------------------------ search */
let searchTimer = null, selIdx = -1;
function setupSearch() {
  const q = $("#q"), res = $("#results");
  q.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
      const v = q.value.trim();
      if (!v) { res.style.display = "none"; return; }
      const hits = await api(`/api/search?q=${encodeURIComponent(v)}&limit=12`);
      selIdx = -1;
      res.innerHTML = hits.map((h) => `<div class="row" data-id="${esc(h.id)}"><span class="dot" style="background:${color(h.type)}"></span>
        <span>${esc(h.label)}</span><span class="tag">${h.type}</span></div>`).join("")
        || `<div class="empty">No node with that name — press Enter to ask it as a question.</div>`;
      res.style.display = "block";
    }, 120);
  });
  q.addEventListener("keydown", (ev) => {
    const rows = [...res.querySelectorAll(".row")];
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      selIdx = Math.max(0, Math.min(rows.length - 1, selIdx + (ev.key === "ArrowDown" ? 1 : -1)));
      rows.forEach((r, i) => r.classList.toggle("sel", i === selIdx));
    } else if (ev.key === "Enter") {
      if (selIdx >= 0 && rows[selIdx]) pick(rows[selIdx].dataset.id); else ask(q.value);
    } else if (ev.key === "Escape") { res.style.display = "none"; q.blur(); }
  });
  res.addEventListener("click", (ev) => { const d = ev.target.closest(".row"); if (d) pick(d.dataset.id); });
  document.addEventListener("click", (ev) => { if (!ev.target.closest(".search")) res.style.display = "none"; });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "/" && document.activeElement !== q && !ev.target.closest("textarea, input")) { ev.preventDefault(); showTab("map"); q.focus(); }
  });
}

async function pick(id) {
  clearTimeout(searchTimer);
  $("#results").style.display = "none";
  const n = byId[id];
  if (n) {   // make sure its layer is visible
    const layer = LAYERS.find((l) => l.types.includes(n.type));
    if (layer && !layerOn[layer.key]) { layerOn[layer.key] = true; saveLayers(); renderLayers(); buildGraph(); }
  }
  roots = [id];
  await simulate(roots);
}

async function ask(question) {
  if (!question || !question.trim()) return;
  const r = await api(`/api/ask?q=${encodeURIComponent(question)}`);
  if (!r.matched) {
    $("#impact").innerHTML = `<div class="intro"><h2>Not sure what you mean</h2><p>${esc(r.speech)} Try a name like Pi, router or NAS.</p></div>`;
    return;
  }
  await pick(r.matched.id);
}

/* ------------------------------------------------------------------ What if (default view) */
const ICON_X = `<svg class="i" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg>`;
const ICON_OK = `<svg class="i" viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>`;
const ICON_DEG = `<svg class="i" viewBox="0 0 24 24"><path d="M6 12h12"/></svg>`;
const SCENARIOS = [
  ["Power cut", "power outage", `<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>`],
  ["Internet down", "internet", `<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>`],
  ["Router fails", "router", `<rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 16.5h.01M11 16.5h.01M8 9a6 6 0 0 1 8 0M5.5 6.5a9.5 9.5 0 0 1 13 0"/>`],
  ["Pi crashes", "raspberry pi", `<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M9 9h6v6H9z"/>`],
  ["Zigbee stick fails", "zigbee coordinator", `<path d="M5 6h14L5 18h14"/>`],
  ["NAS fails", "nas", `<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 7h8M8 11h8M8 17h.01"/>`],
];
let CAT = null, wiRoots = [], wiShowAll = false, wiLast = null, wiSeq = 0;

async function loadCatalog() {
  CAT = await api("/api/catalog");
  renderPicker();
  renderWhatIf(null);
}

function impBadge(it, total) {
  if (it.functions_failed) return `<span class="imp ${it.functions_failed / total > 0.3 ? "bad" : "warn"}">${it.functions_failed} of ${total} stop</span>`;
  if (it.functions_degraded) return `<span class="imp warn">${it.functions_degraded} limited</span>`;
  return `<span class="imp">no effect</span>`;
}

function renderPicker() {
  if (!CAT) return;
  const f = normalizeQ($("#wi-q").value);
  const total = CAT.functions.length;
  $("#wi-list").innerHTML = CAT.groups.map((g) => {
    let items = g.items.filter((i) => !f || normalizeQ(i.label).includes(f));
    if (!items.length) return "";
    const cut = !f && !wiShowAll && g.key === "services" && items.length > 8;
    const shown = cut ? items.slice(0, 8) : items;
    return `<div class="wi-gh">${esc(g.title)}<span class="n">${items.length}</span></div>` +
      shown.map((i) => `<div class="wi-row ${wiRoots.includes(i.id) ? "sel" : ""}" data-pick="${esc(i.id)}" role="button" tabindex="0">
        <span class="nm" title="${esc(i.label)}">${esc(i.label)}${i.planned ? "<small>planned</small>" : ""}</span>${impBadge(i, total)}
        <button class="wi-add" data-add="${esc(i.id)}" title="${wiRoots.includes(i.id) ? "Remove" : "Also fails (combine)"}"
          aria-label="${wiRoots.includes(i.id) ? "Remove" : "Add"} ${esc(i.label)}">${wiRoots.includes(i.id) ? "−" : "+"}</button></div>`).join("") +
      (cut ? `<button class="btn sm ghost wi-more" data-more="1">Show all ${items.length} services</button>` : "");
  }).join("") || `<div class="empty-state" style="padding:10px">Nothing in the list matches. Press Enter to search everything (devices, integrations …).</div>`;
}
function normalizeQ(s) { return String(s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").trim(); }

async function setRoots(ids) {
  wiRoots = ids;
  renderPicker();
  const seq = ++wiSeq;   // a slower older answer must not overwrite a newer pick
  if (!ids.length) { renderWhatIf(null); return; }
  const r = await api(`/api/impact?${ids.map((i) => `node=${encodeURIComponent(i)}`).join("&")}`);
  if (seq === wiSeq) renderWhatIf(r);
}

function renderWhatIf(r) {
  wiLast = r;
  const fns = (CAT && CAT.functions) || [];
  const total = fns.length;
  const st = {};
  if (r) {
    r.functions.failed.forEach((x) => { st[x.id] = ["fail", x]; });
    r.functions.degraded.forEach((x) => { st[x.id] = ["deg", x]; });
  }
  const nF = r ? r.functions.failed.length : 0, nD = r ? r.functions.degraded.length : 0;
  const who = r ? r.roots.map((x) => x.label).join(" + ") : "";
  $("#wi-sel").innerHTML = r ? r.roots.map((x) => `<span class="root-chip"><span class="dot" style="background:var(--bad)"></span>${esc(x.label)}
      <button data-unroot="${esc(x.id)}" aria-label="Remove ${esc(x.label)}">×</button></span>`).join("") +
    `<button class="btn sm" data-showmap="1">Show on map</button><button class="btn sm ghost" data-clear="1">Clear</button>` : "";
  const tone = !r ? "ok" : nF ? "bad" : nD ? "warn" : "ok";
  const head = !r ? "Everything works right now."
    : nF ? `If ${who} fails, ${nF} of ${total} things stop working.`
    : nD ? `If ${who} fails, nothing stops completely, but ${nD} only work partly.`
    : `If ${who} fails, none of your functions is affected.`;
  const sub = !r ? "Pick something on the left, or try one of the scenarios above."
    : `${(r.counts.entity || { failed: 0 }).failed} Home Assistant entities would be unavailable. Click a red tile to see why.`;
  $("#wi-hero").innerHTML = `<div class="card hero ${tone}"><div class="h-ico">${tone === "ok" ? ICON_OK : tone === "warn" ? ICON_DEG : ICON_X}</div>
    <div><h2>${esc(head)}</h2><p>${esc(sub)}</p></div>
    ${r ? `<div class="big">${nF}<span style="font-size:.5em;color:var(--muted)"> / ${total}</span><small>functions down</small></div>` : ""}</div>`;
  const order = { fail: 0, deg: 1, ok: 2 };
  const list = fns.map((f) => ({ f, s: st[f.id] ? st[f.id][0] : "ok", it: st[f.id] ? st[f.id][1] : null }))
    .sort((a, b) => order[a.s] - order[b.s] || a.f.label.localeCompare(b.f.label));
  $("#wi-count").textContent = r ? `${nF} stop · ${nD} limited · ${total - nF - nD} keep working` : `${total} functions, all working`;
  $("#wi-fns").innerHTML = list.map(({ f, s, it }) => {
    const cause = it && it.path && it.path.length > 1 ? it.path[it.path.length - 2] : null;
    return `<div class="fn s-${s}" ${s !== "ok" ? `role="button" tabindex="0" data-fn="1"` : ""}>
      <div class="row1"><span class="st">${s === "fail" ? ICON_X : s === "deg" ? ICON_DEG : ICON_OK}</span><span class="lb">${esc(f.label)}</span></div>
      ${s === "ok" ? "" : `<div class="why">${s === "fail" ? "Stops" : "Works only partly"}${cause ? ` · via ${esc(cause.label)}` : ""}</div>
        <span class="more">Why? Tap to see the chain</span>${steps(it.path, s)}`}</div>`;
  }).join("");
  let extra = "";
  if (r && r.at_risk && r.at_risk.length) {
    extra += `<div class="note warn"><b>Still works, but without backup:</b> ${r.at_risk.map((x) => esc(x.label)).join(", ")}.
      One more failure there and it stops too.</div>`;
  }
  const tech = r ? r.groups.filter((g) => g.type !== "function") : [];
  if (tech.length) {
    extra += `<details class="tech"><summary><span class="n">Technical details: what else goes down</span><span class="num">${tech.reduce((s, g) => s + g.failed.length, 0)} items</span></summary>
      <div class="rows">${tech.map((g) => `<details><summary><span class="dot" style="background:${color(g.type)}"></span>${esc(g.title)}
        <span class="num"><span class="f">${g.failed.length}</span>${g.degraded.length ? ` · <span class="d">${g.degraded.length}</span>` : ""}</span></summary>
        <div class="names">${g.failed.slice(0, 300).map((i) => `<div>${esc(i.label)}</div>`).join("")}${g.degraded.slice(0, 100).map((i) => `<div style="color:var(--warn)">${esc(i.label)}</div>`).join("")}</div>
      </details>`).join("")}</div></details>`;
  }
  $("#wi-extra").innerHTML = extra;
}

async function resolveAsk(text) {
  const r = await api(`/api/ask?q=${encodeURIComponent(text)}`);
  return r.matched ? r.matched.id : null;
}

function setupWhatIf() {
  $("#scen").innerHTML = `<span class="lbl">Try</span>` + SCENARIOS.map(([l, q, ic]) =>
    `<button class="chip" data-scen="${esc(q)}"><svg class="i" viewBox="0 0 24 24">${ic}</svg>${esc(l)}</button>`).join("");
  const q = $("#wi-q");
  q.addEventListener("input", () => { wiShowAll = false; renderPicker(); });
  q.addEventListener("keydown", async (ev) => {
    if (ev.key === "Escape") { q.value = ""; renderPicker(); return; }
    if (ev.key !== "Enter") return;
    const first = $("#wi-list [data-pick]");
    if (first) { setRoots([first.dataset.pick]); return; }
    const hit = (await api(`/api/search?q=${encodeURIComponent(q.value)}&limit=1`))[0];
    const id = hit ? hit.id : await resolveAsk(q.value);
    if (id) setRoots([id]);
  });
  $("#tab-whatif").addEventListener("click", async (ev) => {
    const add = ev.target.closest("[data-add]");
    if (add) { const id = add.dataset.add; setRoots(wiRoots.includes(id) ? wiRoots.filter((x) => x !== id) : [...wiRoots, id]); return; }
    const row = ev.target.closest("[data-pick]");
    if (row) { setRoots(wiRoots.length === 1 && wiRoots[0] === row.dataset.pick ? [] : [row.dataset.pick]); return; }
    if (ev.target.closest("[data-more]")) { wiShowAll = true; renderPicker(); return; }
    const un = ev.target.closest("[data-unroot]");
    if (un) { setRoots(wiRoots.filter((x) => x !== un.dataset.unroot)); return; }
    if (ev.target.closest("[data-clear]")) { setRoots([]); return; }
    if (ev.target.closest("[data-showmap]")) { const ids = [...wiRoots]; showTab("map"); roots = ids; await simulate(ids); return; }
    const sc = ev.target.closest("[data-scen]");
    if (sc) { const id = await resolveAsk(sc.dataset.scen); if (id) setRoots([id]); return; }
    const fn = ev.target.closest("[data-fn]");
    if (fn) fn.classList.toggle("open");
  });
  $("#tab-whatif").addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    const t = ev.target.closest("[data-pick], [data-fn]");
    if (t && !ev.target.closest("button, input")) { ev.preventDefault(); t.click(); }
  });
}

/* ------------------------------------------------------------------ pages */
function meter(x, tone) {
  return `<div class="mt"><div class="meter ${tone}"><i style="width:${pct(x)}"></i></div><b>${pct(x)}</b></div>`;
}
const pageHead = (kicker, title, text, right = "") =>
  `<div class="page-head"><div><div class="kicker">${kicker}</div><h1>${title}</h1><p>${text}</p></div>${right ? `<div class="actions">${right}</div>` : ""}</div>`;
const card = (title, body, aside = "", flush = false) =>
  `<div class="card"><div class="card-h"><h3>${title}</h3>${aside ? `<span class="aside">${aside}</span>` : ""}</div><div class="card-b${flush ? " flush" : ""}">${body}</div></div>`;

async function renderSpof() {
  const r = await api("/api/spof");
  const row = (x) => `<tr class="rowlink" data-whatif="${esc(x.id)}" title="Simulate"><td><div style="display:flex;gap:8px;align-items:center">
      <span class="dot" style="background:${color(x.type)}"></span><b>${esc(x.label)}</b></div>
      ${x.same_impact.length ? `<div class="same">same outcome: ${x.same_impact.map((m) => esc(m.label)).join(", ")}</div>` : ""}</td>
    <td>${meter(x.function_share, "bad")}</td><td>${meter(x.entity_share, "warn")}</td>
    <td class="small">${x.functions.slice(0, 6).map(esc).join(", ")}${x.functions.length > 6 ? ` +${x.functions.length - 6}` : ""}</td></tr>`;
  const table = (rows) => `<div class="tbl-wrap"><table class="t"><thead><tr><th>Single point of failure</th><th>Functions</th><th>Entities</th><th>Functions affected</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  const rest = r.top.filter((x) => x.score <= r.threshold).slice(0, 10);
  const n = r.spofs.length;
  $("#spof").innerHTML = pageHead("Weak spots", "Single points of failure",
    `Each of these takes down more than ${pct(r.threshold)} of your ${r.totals.function} functions or ${r.totals.entity} entities on its own.
     Nodes with exactly the same outcome are merged into one row. Click a row to simulate it.`) +
    `<div class="card hero ${n ? "warn" : "ok"}" style="margin-bottom:16px"><div class="h-ico">${n ? ICON_DEG : ICON_OK}</div>
      <div><h2>${n ? `${n} weak spot${n > 1 ? "s" : ""} found` : "No single point of failure above the threshold"}</h2>
      <p>${n ? "A backup or a second path for these would help most." : "Nothing alone takes down most of your home."}</p></div></div>` +
    card("Weak spots", table(r.spofs.map(row).join("") || `<tr><td colspan="4" class="empty-state">None above the threshold.</td></tr>`), "", true) +
    (rest.length ? `<div style="height:16px"></div>` + card("Next biggest risks", table(rest.map(row).join("")), "", true) : "");
}

async function renderLive() {
  const r = await api("/api/live");
  const state = !r.ok ? `<span class="c-bad">failed</span>` : r.warming_up ? "learning baseline (Home Assistant just started)" : "watching";
  $("#live").innerHTML = pageHead("Live", "Mass outages",
      "Home Assistant is polled every minute. When many entities become unavailable at once, Blast-Radius looks for the common cause and notifies you.") +
    `<div class="grid cols-2" style="margin-bottom:16px">
      <div class="card tile"><div class="tile-top">Unavailable now</div><div class="tile-val">${r.unavailable}</div><div class="tile-sub">newly unavailable: ${r.new}</div></div>
      <div class="card tile"><div class="tile-top">Monitoring</div><div class="tile-val" style="font-size:20px">${state}</div>
        <div class="tile-sub">${r.last_poll ? `last poll ${esc(r.last_poll.slice(11, 19))}` : ""} ${r.error ? esc(r.error) : ""}</div></div>
    </div>
    <div class="grid">
    ${card("Current cause", r.cause ? causeHtml(r.cause) : `<div class="empty-state">No mass outage right now.</div>`)}
    ${card("Simulate a mass outage", `<p class="small muted" style="margin-top:0">Paste entity ids, one per line. Blast-Radius finds the node
      that explains them best. Nothing is changed and no notification is sent.</p>
      <textarea id="sim-ents" placeholder="binary_sensor.kitchen_motion&#10;sensor.kitchen_temperature"></textarea>
      <div class="actions"><button class="btn primary" id="sim-run">Find cause</button>
        <span class="muted small">or fill with the entities behind</span>
        <button class="btn sm" data-fill="zigbee coordinator">Zigbee</button><button class="btn sm" data-fill="kasa hub">Thermostat hub</button>
        <button class="btn sm" data-fill="fritzbox">Router</button></div>
      <div id="sim-out" style="margin-top:12px"></div>`)}
    ${card("History", (r.history || []).map((h) => `<div class="alert warn"><time>${esc(h.at.slice(0, 16).replace("T", " "))}</time><span>${esc(h.message)}</span></div>`).join("")
      || `<div class="empty-state">No mass outages recorded.</div>`)}
    </div>`;
  $("#sim-run").onclick = async () => {
    const ents = $("#sim-ents").value.split(/[\s,]+/).filter(Boolean);
    if (!ents.length) return;
    $("#sim-out").innerHTML = causeHtml(await api("/api/cause/simulate", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ entities: ents }) }));
  };
  document.querySelectorAll("[data-fill]").forEach((b) => b.onclick = async () => {
    const hits = await api(`/api/search?q=${encodeURIComponent(b.dataset.fill)}&limit=1&entities=false`);
    if (!hits.length) return;
    const imp = await api(`/api/impact?node=${encodeURIComponent(hits[0].id)}&paths=false`);
    $("#sim-ents").value = imp.failed_ids.filter((x) => x.startsWith("entity:")).map((x) => x.slice(7)).join("\n");
  });
}

function causeHtml(c) {
  if (!c.candidates || !c.candidates.length) return `<div class="empty-state">${esc(c.message || "No candidates.")}</div>`;
  return `<div class="alert"><span><b>${esc(c.message)}</b></span></div><div class="tbl-wrap"><table class="t"><thead><tr><th>Candidate</th><th>Explains</th><th>Precision</th></tr></thead><tbody>` +
    c.candidates.map((x) => `<tr class="rowlink" data-whatif="${esc(x.id)}"><td><div style="display:flex;gap:8px;align-items:center"><span class="dot" style="background:${color(x.type)}"></span>${esc(x.label)}</div>
      ${x.same_impact.length ? `<div class="same">same outcome: ${x.same_impact.map((m) => esc(m.label)).join(", ")}</div>` : ""}</td>
      <td>${x.explains} of ${c.count}</td><td>${meter(x.precision, "")}</td></tr>`).join("") +
    `</tbody></table></div>${c.unexplained && c.unexplained.length ? `<p class="small muted">Not explained: ${c.unexplained.map(esc).join(", ")}</p>` : ""}`;
}

async function renderChanges() {
  const r = await api("/api/changes?limit=60");
  const names = (arr) => `<div class="names">${arr.map((n) => `<div>${esc(n.label || n)} ${n.type ? `<span class="muted">${n.type}</span>` : ""}</div>`).join("")}</div>`;
  $("#changes").innerHTML = pageHead("Changes", "What changed in your setup", "Every rebuild that changes the dependency graph is logged here, newest first.") +
    `<div class="card"><div class="card-b flush">${r.map((c) => `<div class="change">
      <div class="when">${esc(c.at.slice(0, 16).replace("T", " "))}</div><div class="body">
      ${c.initial ? `<b>Initial graph</b> <span class="muted">${c.nodes_added} nodes, ${c.edges_added} edges</span>`
        : `<span class="delta"><span class="p">+${c.nodes_added}</span><span class="m">−${c.nodes_removed}</span> nodes</span>
           <span class="delta" style="margin-left:10px"><span class="p">+${c.edges_added}</span><span class="m">−${c.edges_removed}</span> edges</span>`}
      ${c.added.length ? `<details><summary>Added nodes</summary>${names(c.added)}</details>` : ""}
      ${c.removed.length ? `<details><summary>Removed nodes</summary>${names(c.removed)}</details>` : ""}
      ${c.edges_added_sample.length ? `<details><summary>Added edges</summary>${names(c.edges_added_sample)}</details>` : ""}
      ${c.edges_removed_sample.length ? `<details><summary>Removed edges</summary>${names(c.edges_removed_sample)}</details>` : ""}
      </div></div>`).join("") || `<div class="empty-state" style="padding:12px 18px">No changes yet.</div>`}</div></div>`;
}

async function renderHealth() {
  const s = await api("/api/status");
  const g = s.graph || {};
  const alerts = Object.entries(s.alerts || {});
  const errs = (g.meta && g.meta.source_errors) || [];
  const counts = Object.entries(g.counts || {}).filter(([, c]) => c);
  const kv = (pairs) => `<dl class="kv">${pairs.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>`;
  $("#health").innerHTML = pageHead("Health", "Is Blast-Radius itself OK?", "Self-monitoring of discovery, the graph and the connection to Home Assistant.") +
    `<div class="card hero ${alerts.length ? "warn" : "ok"}" style="margin-bottom:16px"><div class="h-ico">${alerts.length ? ICON_DEG : ICON_OK}</div>
      <div><h2>${alerts.length ? `${alerts.length} active alert${alerts.length > 1 ? "s" : ""}` : "Everything fine"}</h2>
      <p>${alerts.length ? alerts.map(([, a]) => esc(a.message)).join(" · ") : `Graph with ${g.nodes || 0} nodes and ${g.edges || 0} edges, built ${esc(String(g.meta && g.meta.built_at).slice(0, 16).replace("T", " "))}.`}</p></div>
      ${s.pending_shrink ? `<div class="actions" style="margin:0 0 0 auto"><button class="btn primary" id="accept">Accept the smaller graph</button></div>` : ""}</div>
    <div class="grid cols-2">
      ${card("Discovery", kv([
        ...Object.entries((g.meta && g.meta.collected_at) || {}).map(([h, t]) => [esc(h.replace("host:", "")), `collected ${esc(String(t).slice(0, 16).replace("T", " "))}`]),
        ["Graph built", esc(String(g.meta && g.meta.built_at).slice(0, 16).replace("T", " "))],
        ["Nodes / edges", `${g.nodes} / ${g.edges}`],
        ["Notifications", `${esc(s.notify_service || "not configured")}${s.ha_configured ? "" : " (HA token missing)"}`]]) +
        `<div class="actions"><button class="btn" id="rebuild">Rebuild now</button><span class="small muted">uses the latest facts and the current overrides</span></div>`)}
      ${card("Nodes", kv(counts.map(([t, c]) => [`<span class="dot" style="margin-right:6px;background:${color(t)}"></span>${t}`, c])))}
    </div>
    <div class="grid" style="margin-top:16px">
    ${errs.length ? card("Source errors", errs.map((e) => `<div class="alert">${esc(e.host)} / ${esc(e.source)}: ${esc(e.error)}</div>`).join("")) : ""}
    ${card("Override warnings", (g.warnings || []).map((w) => `<div class="alert warn">${esc(w)}</div>`).join("")
      || `<div class="alert ok"><span>Every reference in overrides.yaml resolves.</span></div>`)}
    ${card("API for voice assistants", `<p class="small" style="margin:0"><code>GET /api/ask?q=What happens if the Pi crashes?</code>
      returns <code>speech</code>, <code>functions</code> and <code>matched</code>. All endpoints: <a href="/docs">/docs</a></p>`)}
    </div>`;
  const rb = $("#rebuild"); if (rb) rb.onclick = async () => { rb.disabled = true; await api("/api/rebuild", { method: "POST" }); await reloadAll(); renderHealth(); };
  const ac = $("#accept"); if (ac) ac.onclick = async () => { await api("/api/graph/accept", { method: "POST" }); await reloadAll(); renderHealth(); };
}

async function reloadAll() { await loadGraph(); await loadCatalog(); }

async function refreshStatus() {
  const pill = $("#status-pill");
  try {
    const s = await api("/api/status");
    const keys = Object.keys(s.alerts || {});
    const err = keys.some((k) => k.startsWith("facts:") || k.startsWith("build"));
    pill.className = `live ${err ? "error" : keys.length ? "warning" : "ok"}`;
    pill.querySelector("span").textContent = !s.graph ? "waiting for discovery"
      : keys.length ? `${keys.length} alert${keys.length > 1 ? "s" : ""}` : `${s.graph.nodes} nodes · healthy`;
    pill.title = keys.length ? Object.values(s.alerts).map((a) => a.message).join("\n") : "";
  } catch (e) { pill.className = "live error"; pill.querySelector("span").textContent = "offline"; }
}

async function setupHome() {
  try {
    const u = await api("/api/ui");
    if (!u.home_url) return;
    const a = document.createElement("a");
    a.className = "btn cross home-btn"; a.href = u.home_url;
    a.innerHTML = `<svg class="i" viewBox="0 0 24 24"><path d="M3 11l9-8 9 8M5 10v10h14V10"/></svg><span class="lbl">${esc(u.home_label || "Dashboard")}</span>`;
    $("#top-right").insertBefore(a, $("#theme-btn"));
  } catch (e) { /* optional */ }
}

/* ------------------------------------------------------------------ boot */
function saveLayers() { store("br-layers", JSON.stringify(layerOn)); }
let mapBuilt = false;
function showTab(t) {
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
  document.querySelectorAll(".tab").forEach((s) => s.classList.toggle("active", s.id === `tab-${t}`));
  store("br-tab", t);
  if (t === "map") { if (!mapBuilt && G) { mapBuilt = true; buildGraph(); } else if (cy) cy.resize(); return; }
  ({ spof: renderSpof, live: renderLive, changes: renderChanges, health: renderHealth }[t] || (() => {}))();
}

(async function boot() {
  let saved = {};
  try { saved = JSON.parse(store("br-layers") || "{}"); } catch (e) { saved = {}; }
  LAYERS.forEach((l) => { layerOn[l.key] = saved[l.key] ?? l.on; });
  document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  document.body.addEventListener("click", (ev) => {
    const w = ev.target.closest("[data-whatif]");
    if (w) { showTab("whatif"); setRoots([w.dataset.whatif]); return; }
    const n = ev.target.closest("[data-node]");
    if (n) { showTab("map"); pick(n.dataset.node); return; }
    const a = ev.target.closest("[data-ask]");
    if (a) { ev.preventDefault(); $("#q").value = a.dataset.ask; ask(a.dataset.ask); }
  });
  $("#layers").addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-layer]");
    if (!b) return;
    layerOn[b.dataset.layer] = !layerOn[b.dataset.layer];
    if (!Object.values(layerOn).some(Boolean)) layerOn.function = true;
    saveLayers(); renderLayers(); buildGraph();
  });
  $("#theme-btn").onclick = () => {
    const r = document.documentElement, t = r.dataset.theme === "dark" ? "light" : "dark";
    r.dataset.theme = t; store("hl-theme", t);
    if (cy) buildGraph();
  };
  setupSearch();
  setupWhatIf();
  setupHome();
  $("#btn-fit").onclick = () => cy && cy.animate({ fit: { padding: 30 } }, { duration: 220, easing: "ease-out-cubic" });
  $("#btn-reset").onclick = resetSim;
  refreshStatus(); setInterval(refreshStatus, 60000);
  renderIntro();
  try {
    G = await api("/api/graph?entities=true");
    byId = {}; G.nodes.forEach((n) => { byId[n.id] = n; });
    renderLayers();
    await loadCatalog();
  } catch (e) {
    $("#wi-hero").innerHTML = `<div class="card hero warn"><div class="h-ico">${ICON_DEG}</div><div><h2>No map yet</h2><p>${esc(e.message)}</p></div></div>`;
  }
  const u = new URLSearchParams(location.search);
  if (u.get("node")) { showTab("whatif"); setRoots([u.get("node")]); }
  else if (u.get("ask")) { const id = await resolveAsk(u.get("ask")); if (id) setRoots([id]); }
  else { const t = store("br-tab"); if (t && t !== "whatif") showTab(t); }
})();
