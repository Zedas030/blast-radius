/* Blast-Radius web UI */
"use strict";

const TYPES = ["function", "hardware", "network", "cloud", "host", "service", "integration", "device",
  "automation", "script", "dashboard", "entity"];
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const color = (t) => css(`--t-${t}`) || "#888";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (x) => `${Math.round(x * 100)} %`;

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 200)}`);
  return r.json();
}

let cy = null;
let graphData = null;
let roots = [];
let labelOf = {};
let typeOf = {};

function store(key, val) { try { if (val === undefined) return localStorage.getItem(key); localStorage.setItem(key, val); } catch (e) { return null; } }

/* ------------------------------------------------------------------ graph */
async function loadGraph() {
  const withEntities = $("#show-entities").checked;
  graphData = await api(`/api/graph?entities=${withEntities ? 1 : 0}`);
  labelOf = {}; typeOf = {};
  graphData.nodes.forEach((n) => { labelOf[n.id] = n.label; typeOf[n.id] = n.type; });
  const elements = [];
  graphData.nodes.forEach((n) => elements.push({ data: { id: n.id, label: n.label, type: n.type } }));
  graphData.edges.forEach((e, i) => elements.push({ data: { id: `e${i}`, source: e.src, target: e.dst, kind: e.kind, soft: !!e.soft } }));
  if (cy) cy.destroy();
  cy = cytoscape({
    container: $("#cy"), elements, wheelSensitivity: 0.25, minZoom: 0.05, maxZoom: 3,
    style: [
      { selector: "node", style: {
        "background-color": (n) => color(n.data("type")), label: "data(label)", "font-size": 9, color: css("--ink"),
        "text-valign": "bottom", "text-margin-y": 3, "text-wrap": "ellipsis", "text-max-width": 120,
        width: (n) => ({ function: 26, hardware: 24, network: 22, host: 24, cloud: 18, service: 18 }[n.data("type")] || 12),
        height: (n) => ({ function: 26, hardware: 24, network: 22, host: 24, cloud: 18, service: 18 }[n.data("type")] || 12),
        shape: (n) => ({ function: "star", hardware: "round-rectangle", network: "diamond", cloud: "ellipse", host: "round-rectangle" }[n.data("type")] || "ellipse"),
        "min-zoomed-font-size": 7 } },
      { selector: "node[type='entity']", style: { label: "", width: 6, height: 6 } },
      { selector: "edge", style: { width: 1, "line-color": css("--line"), "target-arrow-color": css("--line"),
        "target-arrow-shape": "triangle", "arrow-scale": 0.6, "curve-style": "haystack", opacity: 0.8 } },
      { selector: "edge[kind='powered_by']", style: { "line-color": "#ffba18", "line-style": "solid", width: 1.5 } },
      { selector: "edge[?soft]", style: { "line-style": "dashed" } },
      { selector: ".dim", style: { opacity: 0.12 } },
      { selector: "node.failed", style: { "background-color": css("--fail"), "border-width": 2, "border-color": css("--fail"), opacity: 1, "font-weight": 600 } },
      { selector: "node.degraded", style: { "background-color": css("--degraded"), opacity: 1 } },
      { selector: "node.risk", style: { "border-width": 3, "border-color": css("--risk"), "border-style": "dashed", opacity: 1 } },
      { selector: "node.root", style: { "background-color": css("--root"), "border-width": 4, "border-color": css("--fail"), width: 34, height: 34, "font-size": 12, "z-index": 10 } },
      { selector: "edge.hot", style: { "line-color": css("--fail"), "target-arrow-color": css("--fail"), opacity: 0.9, width: 1.5, "curve-style": "bezier" } },
      { selector: "node.hl", style: { "border-width": 4, "border-color": css("--accent") } },
    ],
    layout: layoutOptions(),
  });
  cy.on("tap", "node", (ev) => {
    const id = ev.target.id();
    if (ev.originalEvent && ev.originalEvent.shiftKey) {
      roots = roots.includes(id) ? roots.filter((r) => r !== id) : [...roots, id];
    } else roots = [id];
    if (roots.length) simulate(roots); else resetSim();
  });
  renderLegend();
  if (roots.length) simulate(roots);
}

function layoutOptions() {
  return { name: "cose", animate: false, nodeRepulsion: () => 9000, idealEdgeLength: () => 60, gravity: 0.6,
    numIter: 1500, randomize: true, componentSpacing: 60, nodeDimensionsIncludeLabels: false };
}

function renderLegend() {
  const counts = graphData.counts;
  $("#legend").innerHTML = TYPES.filter((t) => counts[t]).map((t) =>
    `<span><i style="background:${color(t)}"></i>${t} ${counts[t]}</span>`).join("") +
    `<span><i style="background:${css("--fail")}"></i>fails</span><span><i style="background:${css("--degraded")}"></i>degraded</span>` +
    `<span><i style="border:2px dashed ${css("--risk")};width:8px;height:8px"></i>lost redundancy</span>`;
}

function resetSim() {
  roots = [];
  if (cy) cy.elements().removeClass("dim failed degraded risk root hot hl");
  $("#impact").innerHTML = "";
}

/* ------------------------------------------------------------------ impact */
function pathHtml(path) {
  if (!path || path.length < 2) return "";
  return `<div class="path">${path.map((s, i) => (i ? `<span class="arrow">→</span>${s.edge ? `<span class="muted">(${esc(s.edge)})</span> ` : ""}` : "") +
    `<b>${esc(s.label)}</b>`).join("")}</div>`;
}

function itemList(items, cls) {
  return items.map((it) => `<details class="it"><summary><span class="badge ${cls}">${cls === "b-fail" ? "✕" : "!"}</span> ${esc(it.label)}</summary>${pathHtml(it.path)}</details>`).join("");
}

async function simulate(ids) {
  const params = ids.map((i) => `node=${encodeURIComponent(i)}`).join("&");
  const r = await api(`/api/impact?${params}`);
  showImpact(r);
  if (!cy) return;
  const failed = new Set(r.failed_ids), degraded = new Set(r.degraded_ids), risk = new Set(r.at_risk_ids || []);
  cy.batch(() => {
    cy.elements().removeClass("dim failed degraded risk root hot hl");
    cy.nodes().forEach((n) => {
      const id = n.id();
      if (ids.includes(id)) n.addClass("root");
      else if (failed.has(id)) n.addClass("failed");
      else if (degraded.has(id)) n.addClass("degraded");
      else if (risk.has(id)) n.addClass("risk");
      else n.addClass("dim");
    });
    cy.edges().forEach((e) => {
      if (failed.has(e.data("source")) && (failed.has(e.data("target")))) e.addClass("hot"); else e.addClass("dim");
    });
  });
}

function showImpact(r) {
  const rootNames = r.roots.map((x) => `<b>${esc(x.label)}</b> <span class="type" style="background:${color(x.type)}">${x.type}</span>`).join(" + ");
  const fnF = r.functions.failed, fnD = r.functions.degraded;
  let html = `<h2>If ${rootNames} fails</h2><div class="summary">${esc(r.summary)}</div>`;
  html += `<h3>For you: ${fnF.length} of ${r.functions.total} functions stop working</h3>`;
  html += fnF.length ? itemList(fnF, "b-fail") : `<div class="muted">No function stops completely.</div>`;
  if (fnD.length) html += `<h3>Degraded (${fnD.length})</h3>` + itemList(fnD, "b-deg");
  if (r.at_risk && r.at_risk.length) html += `<h3>Still working, but redundancy lost (${r.at_risk.length})</h3>` + itemList(r.at_risk, "b-risk");
  html += `<h3>Technical details</h3>`;
  r.groups.filter((g) => g.type !== "function").forEach((g) => {
    const n = g.failed.length + g.degraded.length;
    html += `<details><summary><span class="type" style="background:${color(g.type)}">${esc(g.title)}</span>
      <span class="badge b-fail">${g.failed.length}</span>${g.degraded.length ? ` <span class="badge b-deg">${g.degraded.length}</span>` : ""}</summary>
      <div class="items">${itemList(g.failed.slice(0, 400), "b-fail")}${itemList(g.degraded.slice(0, 200), "b-deg")}${n > 600 ? `<div class="muted">…</div>` : ""}</div></details>`;
  });
  $("#impact").innerHTML = html;
  $("#hint").style.display = "none";
}

/* ------------------------------------------------------------------ search */
let searchTimer = null;
let selIdx = -1;
function setupSearch() {
  const q = $("#q"), res = $("#results");
  q.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
      const v = q.value.trim();
      if (!v) { res.style.display = "none"; return; }
      const hits = await api(`/api/search?q=${encodeURIComponent(v)}&limit=15`);
      selIdx = -1;
      res.innerHTML = hits.map((h) => `<div data-id="${esc(h.id)}"><span class="type" style="background:${color(h.type)}">${h.type}</span>${esc(h.label)}</div>`).join("")
        || `<div class="muted">No match. Try a question: <span class="link" data-ask="${esc(v)}">ask “${esc(v)}”</span></div>`;
      res.style.display = "block";
    }, 150);
  });
  q.addEventListener("keydown", (ev) => {
    const rows = [...res.querySelectorAll("div[data-id]")];
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      selIdx = Math.max(0, Math.min(rows.length - 1, selIdx + (ev.key === "ArrowDown" ? 1 : -1)));
      rows.forEach((r, i) => r.classList.toggle("sel", i === selIdx));
    } else if (ev.key === "Enter") {
      if (rows[selIdx >= 0 ? selIdx : 0]) pick(rows[selIdx >= 0 ? selIdx : 0].dataset.id);
      else ask(q.value);
    } else if (ev.key === "Escape") res.style.display = "none";
  });
  res.addEventListener("click", (ev) => {
    const d = ev.target.closest("div[data-id]");
    if (d) pick(d.dataset.id);
    const a = ev.target.closest("[data-ask]");
    if (a) ask(a.dataset.ask);
  });
  document.addEventListener("click", (ev) => { if (!ev.target.closest(".search")) res.style.display = "none"; });
  $("#hint").addEventListener("click", (ev) => { const a = ev.target.closest("[data-ask]"); if (a) { ev.preventDefault(); ask(a.dataset.ask); } });
}

async function pick(id) {
  clearTimeout(searchTimer);
  $("#results").style.display = "none";
  if (!typeOf[id] && !$("#show-entities").checked && id.startsWith("entity:")) {
    $("#show-entities").checked = true;
    await loadGraph();
  }
  roots = [id];
  await simulate(roots);
  const n = cy && cy.getElementById(id);
  if (n && n.length) cy.animate({ center: { eles: n }, zoom: Math.max(cy.zoom(), 0.9) }, { duration: 250 });
}

async function ask(question) {
  const r = await api(`/api/ask?q=${encodeURIComponent(question)}`);
  if (!r.matched) { $("#impact").innerHTML = `<div class="summary">${esc(r.speech)}</div>`; return; }
  $("#q").value = question;
  await pick(r.matched.id);
}

/* ------------------------------------------------------------------ other tabs */
async function renderSpof() {
  const r = await api("/api/spof");
  const row = (x) => `<tr><td><span class="link" data-node="${esc(x.id)}">${esc(x.label)}</span> <span class="type" style="background:${color(x.type)}">${x.type}</span>
      ${x.same_impact.length ? `<div class="muted" style="font-size:12px">same impact: ${x.same_impact.map((m) => esc(m.label)).join(", ")}</div>` : ""}</td>
    <td><div class="bar"><i style="width:${pct(x.function_share)}"></i></div>${pct(x.function_share)}</td>
    <td><div class="bar"><i style="width:${pct(x.entity_share)}"></i></div>${pct(x.entity_share)}</td>
    <td style="font-size:12px">${x.functions.map(esc).join(", ") || "–"}</td></tr>`;
  $("#spof").innerHTML = `<div class="card"><h2>${r.spofs.length} single points of failure</h2>
    <div class="muted">Each of these nodes alone takes down more than ${pct(r.threshold)} of your ${r.totals.function} functions or ${r.totals.entity} entities.
    Nodes with exactly the same outcome are merged into one row.</div></div>
    <table><thead><tr><th>Node</th><th>Functions down</th><th>Entities down</th><th>Functions affected</th></tr></thead>
    <tbody>${r.spofs.map(row).join("") || `<tr><td colspan=4>None above the threshold.</td></tr>`}</tbody></table>
    <h3>Next biggest risks</h3><table><tbody>${r.top.filter((x) => x.score <= r.threshold).slice(0, 12).map(row).join("")}</tbody></table>`;
}

async function renderLive() {
  const r = await api("/api/live");
  const cause = r.cause ? causeHtml(r.cause) : `<div class="muted">No mass outage right now.</div>`;
  $("#live").innerHTML = `<div class="card"><h2>Home Assistant right now</h2>
    <div class="kv"><span>Poll</span><span>${r.ok ? "ok" : `<b style="color:var(--fail)">failed</b> ${esc(r.error || "")}`} ${r.last_poll ? `(${esc(r.last_poll)})` : ""}</span>
    <span>Unavailable entities</span><span>${r.unavailable}</span><span>Newly unavailable (window)</span><span>${r.new}</span></div>
    <h3>Cause</h3>${cause}</div>
    <div class="card"><h2>Simulate a mass outage</h2>
      <div class="muted">Paste entity ids (one per line or comma separated). Blast-Radius looks for the node that explains them best. Nothing is changed and no notification is sent.</div>
      <textarea id="sim-ents" placeholder="binary_sensor.kitchen_motion&#10;sensor.kitchen_temperature"></textarea>
      <p><button class="btn primary" id="sim-run">Find cause</button> <button class="btn" id="sim-fill">Fill with the entities behind a node…</button></p>
      <div id="sim-out"></div></div>
    <div class="card"><h2>History</h2>${(r.history || []).map((h) => `<div class="alert"><b>${esc(h.at)}</b> ${esc(h.message)}</div>`).join("") || `<div class="muted">No mass outages recorded.</div>`}</div>`;
  $("#sim-run").onclick = async () => {
    const ents = $("#sim-ents").value.split(/[\s,]+/).filter(Boolean);
    const out = await api("/api/cause/simulate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ entities: ents }) });
    $("#sim-out").innerHTML = causeHtml(out);
  };
  $("#sim-fill").onclick = async () => {
    const q = prompt("Which node? (e.g. zigbee coordinator)");
    if (!q) return;
    const hits = await api(`/api/search?q=${encodeURIComponent(q)}&limit=1&entities=false`);
    if (!hits.length) return;
    const imp = await api(`/api/impact?node=${encodeURIComponent(hits[0].id)}&paths=false`);
    $("#sim-ents").value = imp.failed_ids.filter((x) => x.startsWith("entity:")).map((x) => x.slice(7)).join("\n");
  };
}

function causeHtml(c) {
  if (!c.candidates || !c.candidates.length) return `<div class="muted">${esc(c.message || "no candidates")}</div>`;
  return `<div class="summary"><b>${esc(c.message)}</b></div><table><thead><tr><th>Candidate</th><th>Explains</th><th>Precision</th><th>Same impact</th></tr></thead><tbody>` +
    c.candidates.map((x) => `<tr><td><span class="link" data-node="${esc(x.id)}">${esc(x.label)}</span> <span class="type" style="background:${color(x.type)}">${x.type}</span></td>
      <td>${x.explains} of ${c.count}</td><td>${pct(x.precision)}</td><td style="font-size:12px">${x.same_impact.map((m) => esc(m.label)).join(", ") || "–"}</td></tr>`).join("") +
    `</tbody></table>${c.unexplained && c.unexplained.length ? `<div class="muted" style="margin-top:6px">Not explained: ${c.unexplained.map(esc).join(", ")}</div>` : ""}`;
}

async function renderChanges() {
  const r = await api("/api/changes?limit=60");
  $("#changes").innerHTML = `<div class="card"><h2>Graph changes</h2><div class="muted">Every rebuild that changes the graph is logged here (newest first).</div></div>` +
    (r.map((c) => `<div class="card"><b>${esc(c.at)}</b> ${c.initial ? `– initial graph with ${c.nodes_added} nodes and ${c.edges_added} edges` :
      `– <span class="badge b-deg">+${c.nodes_added}</span> <span class="badge b-fail">−${c.nodes_removed}</span> nodes, +${c.edges_added} / −${c.edges_removed} edges`}
      ${c.added.length ? `<details><summary>Added nodes (${c.nodes_added})</summary><div class="items">${c.added.map((n) => `<div>${esc(n.label)} <span class="muted">${n.type}</span></div>`).join("")}</div></details>` : ""}
      ${c.removed.length ? `<details><summary>Removed nodes (${c.nodes_removed})</summary><div class="items">${c.removed.map((n) => `<div>${esc(n.label)} <span class="muted">${n.type}</span></div>`).join("")}</div></details>` : ""}
      ${c.edges_added_sample.length ? `<details><summary>Added edges (${c.edges_added})</summary><div class="items">${c.edges_added_sample.map((e) => `<div>${esc(e)}</div>`).join("")}</div></details>` : ""}
      ${c.edges_removed_sample.length ? `<details><summary>Removed edges (${c.edges_removed})</summary><div class="items">${c.edges_removed_sample.map((e) => `<div>${esc(e)}</div>`).join("")}</div></details>` : ""}
      </div>`).join("") || `<div class="muted">No changes yet.</div>`);
}

async function renderHealth() {
  const s = await api("/api/status");
  const g = s.graph || {};
  const alerts = Object.entries(s.alerts || {});
  const errs = (g.meta && g.meta.source_errors) || [];
  $("#health").innerHTML = `<div class="card"><h2>Self-monitoring</h2>
    ${alerts.length ? alerts.map(([k, a]) => `<div class="alert"><b>${esc(a.since)}</b> ${esc(a.message)}</div>`).join("") : `<div style="color:var(--ok)">No active alerts.</div>`}
    ${s.pending_shrink ? `<p><button class="btn primary" id="accept">Accept the smaller graph</button></p>` : ""}
    <h3>Discovery</h3><div class="kv">${Object.entries((g.meta && g.meta.collected_at) || {}).map(([h, t]) => `<span>${esc(h)}</span><span>collected ${esc(t)}</span>`).join("")}
      <span>Graph built</span><span>${esc(g.meta && g.meta.built_at)}</span>
      <span>Nodes / edges</span><span>${g.nodes} / ${g.edges}</span>
      <span>Notifications</span><span>${esc(s.notify_service || "not configured")}${s.ha_configured ? "" : " – HA token missing"}</span></div>
    ${errs.length ? `<h3>Source errors</h3>${errs.map((e) => `<div class="alert">${esc(e.host)} / ${esc(e.source)}: ${esc(e.error)}</div>`).join("")}` : ""}
    <p><button class="btn" id="rebuild">Rebuild graph now</button> <span class="muted">uses the latest collected facts and the current overrides file</span></p></div>
    <div class="card"><h2>Override warnings (${(g.warnings || []).length})</h2>${(g.warnings || []).map((w) => `<div class="alert">${esc(w)}</div>`).join("") || `<div class="muted">None – every reference in overrides.yaml resolved.</div>`}</div>
    <div class="card"><h2>Node counts</h2><div class="kv">${Object.entries(g.counts || {}).map(([t, c]) => `<span>${t}</span><span>${c}</span>`).join("")}</div></div>
    <div class="card"><h2>API for voice assistants</h2><div class="muted"><code>GET /api/ask?q=What happens if the Pi crashes?</code> → <code>speech</code>, <code>functions</code>, <code>matched</code>.
      Docs: <a href="/docs">/docs</a></div></div>`;
  const rb = $("#rebuild"); if (rb) rb.onclick = async () => { rb.disabled = true; await api("/api/rebuild", { method: "POST" }); await loadGraph(); renderHealth(); };
  const ac = $("#accept"); if (ac) ac.onclick = async () => { await api("/api/graph/accept", { method: "POST" }); await loadGraph(); renderHealth(); };
}

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    const alerts = Object.keys(s.alerts || {}).length;
    const pill = $("#status-pill");
    const err = Object.keys(s.alerts || {}).some((k) => k.startsWith("facts:") || k.startsWith("build"));
    pill.className = `pill ${err ? "error" : alerts ? "warning" : "ok"}`;
    pill.textContent = s.graph ? `${s.graph.nodes} nodes · ${alerts ? `${alerts} alert${alerts > 1 ? "s" : ""}` : "healthy"}` : "waiting for first discovery";
  } catch (e) { $("#status-pill").textContent = "offline"; }
}

/* ------------------------------------------------------------------ boot */
function setupTabs() {
  document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  document.body.addEventListener("click", (ev) => {
    const l = ev.target.closest("[data-node]");
    if (l) { showTab("map"); pick(l.dataset.node); }
  });
}
function showTab(t) {
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === t));
  document.querySelectorAll(".tab").forEach((s) => s.classList.toggle("active", s.id === `tab-${t}`));
  store("br-tab", t);
  ({ spof: renderSpof, live: renderLive, changes: renderChanges, health: renderHealth }[t] || (() => { if (cy) cy.resize(); }))();
}

(async function boot() {
  setupTabs(); setupSearch();
  $("#show-entities").addEventListener("change", loadGraph);
  $("#btn-fit").onclick = () => cy && cy.fit(undefined, 30);
  $("#btn-reset").onclick = resetSim;
  refreshStatus(); setInterval(refreshStatus, 60000);
  try { await loadGraph(); } catch (e) { $("#impact").innerHTML = `<div class="summary">${esc(e.message)}</div>`; }
  const t = store("br-tab"); if (t && t !== "map") showTab(t);
  const u = new URLSearchParams(location.search);
  if (u.get("node")) pick(u.get("node")); else if (u.get("ask")) ask(u.get("ask"));
})();
