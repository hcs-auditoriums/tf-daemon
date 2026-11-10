// Ring / feedback events: the header alert, the list, and markers on the plot.

import { selectInput } from "./controls.js";
import { H, PAD, xOf } from "./plot.js";
import { $, ctx, el, loadInt, save, state } from "./shared.js";

// Ring events from the detector (tf_daemon/dsp/ring.py), for all analyzed inputs.
// Severity: a growing ring is feedback; a sustained ring, or a tail that keeps
// coming back at the same frequency, is a ring; a single tail is only possible
// (a slowly decaying instrument note can look like one) and raises no alert.
state.rings = [];
state.showRings = loadInt("tf.rings", 1) === 1;       // markers on the spectrum / waterfall
state.showRingList = loadInt("tf.ringList", 1) === 1;  // the list over the plot
export const SEV = {
  feedback: { label: "FEEDBACK", color: "#ff5d5d", rank: 3 },
  ring: { label: "RING", color: "#ff9f43", rank: 2 },
  possible: { label: "possible", color: "#f2d14e", rank: 1 },
};
export const severity = (e) => (e.kind === "growing" ? "feedback"
  : e.kind === "sustained" || e.count >= 2 ? "ring" : "possible");
const fmtHz = (f) => (f >= 1000 ? `${(f / 1000).toFixed(f >= 10000 ? 1 : 2)} kHz` : `${Math.round(f)} Hz`);
const fmtBand = (b) => (b >= 1000 ? `${+(b / 1000).toFixed(2)}k` : `${b}`);
function ago(t) {
  const s = Math.max(0, Date.now() / 1000 - t);
  return s < 60 ? `${Math.round(s)} s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
}

export function onRings(msg) {
  state.rings = msg.events || [];
  renderRings();
  state.dirty = true;
}

function post(path, body) {
  fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })
    .catch(() => {});
}

function renderRings() {
  // Header alert: the worst live, confirmed ring on any input.
  const live = state.rings.filter((e) => e.active && severity(e) !== "possible")
    .sort((a, b) => SEV[severity(b)].rank - SEV[severity(a)].rank || b.prominence - a.prominence);
  const alert = $("ring-alert");
  alert.hidden = !live.length;
  if (live.length) {
    const e = live[0], sev = severity(e);
    alert.className = `ring-alert ${sev}`;
    const src = e.sources && e.sources[0] ? ` ← ${e.sources[0].label}` : "";
    alert.textContent = `${SEV[sev].label} ${fmtHz(e.freq)} · ${e.label}${src}` + (live.length > 1 ? ` +${live.length - 1}` : "");
    alert.title = `Show ${e.label}`;
    alert.onclick = () => {
      if (e.channel !== state.input) selectInput(e.channel);
      if (!state.showRingList) setShowRingList(true);
      if (!ringsOpen) setRingsOpen(true);
    };
  }

  const list = $("ring-list");
  list.hidden = !state.showRingList || !state.rings.length;
  if (list.hidden) return;
  const groups = groupRings(state.rings);
  const head = el("div", "rl-head");
  const toggle = el("button", "rl-toggle", `Rings · ${groups.length}`);
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", String(ringsOpen));
  toggle.title = ringsOpen ? "Hide the list" : "Show the list";
  toggle.onclick = () => setRingsOpen(!ringsOpen);
  const clear = el("button", "rl-btn", "Clear");
  clear.type = "button";
  clear.title = "Forget past rings (live ones stay)";
  clear.onclick = () => post("/api/audio/rings/clear");
  head.append(toggle, clear);
  list.classList.toggle("collapsed", !ringsOpen);
  const rows = [head];
  if (ringsOpen) for (const g of groups.slice(0, 8)) rows.push(ringRow(g));
  list.replaceChildren(...rows);
}

// The list folds to its title bar (remembered in this browser); the header alert unfolds it.
// Folded at first on a phone, where it would cover the plot.
let ringsOpen = loadInt("tf.ringsOpen", matchMedia("(max-width: 720px)").matches ? 0 : 1) === 1;
function setRingsOpen(open) {
  ringsOpen = open;
  save("tf.ringsOpen", open ? 1 : 0);
  renderRings();
}

// One ring heard on several inputs (the mains L and R, the mics feeding them) is one row:
// events within a quarter tone of each other. Worst first: live, then severity, then latest.
const SAME_RING = 2 ** (1 / 24);
const worse = (a, b) => (b.active - a.active) || (SEV[severity(b)].rank - SEV[severity(a)].rank) || (b.last_seen - a.last_seen);
function groupRings(events) {
  const groups = [];
  for (const e of [...events].sort(worse)) {
    const g = groups.find((g) => Math.max(g.top.freq, e.freq) / Math.min(g.top.freq, e.freq) < SAME_RING);
    if (g) g.events.push(e); else groups.push({ top: e, events: [e] });
  }
  return groups;
}

function ringRow(g) {
  const e = g.top, sev = severity(e), active = g.events.some((x) => x.active);
  const row = el("div", `rl-row${active ? " live" : ""}`);
  const dot = el("span", "rl-dot"); dot.style.background = SEV[sev].color;
  const main = el("div", "rl-main");
  const top = el("div", "rl-top");
  const freq = el("button", "rl-freq", fmtHz(e.freq));
  freq.type = "button";
  freq.title = `Show ${e.label}`;
  freq.onclick = () => { if (e.channel !== state.input) selectInput(e.channel); };
  top.append(freq, el("span", "rl-sev", SEV[sev].label));
  const count = Math.max(...g.events.map((x) => x.count));
  if (count > 1) top.append(el("span", "rl-count", `×${count}`));
  // Where it was heard, each a link to that input, then the GEQ band and when.
  const where = el("div", "rl-sub");
  const seen = new Set();
  for (const x of g.events) {
    if (seen.has(x.channel)) continue;
    if (seen.size) where.append(", ");
    seen.add(x.channel);
    where.append(inputLink(x.label, x.channel, seen.size === 1));
  }
  where.append(` · GEQ ${fmtBand(e.geq)} · ${active ? "now" : ago(Math.max(...g.events.map((x) => x.last_seen)))}`);
  main.append(top, where);
  const fed = g.events.find((x) => x.sources && x.sources.length);
  if (fed) main.append(sourcesLine(fed));
  const ign = el("button", "rl-btn", "Ignore");
  ign.type = "button";
  ign.title = "Not feedback (a steady tone): stop reporting this frequency";
  ign.onclick = () => post("/api/audio/rings/ignore", { freq: e.freq });
  row.append(dot, main, ign);
  return row;
}

function inputLink(label, channel, top) {
  const b = el("button", top ? "rl-srcname top" : "rl-srcname", label);
  b.type = "button";
  b.title = `Show ${label}`;
  b.onclick = () => selectInput(channel);
  return b;
}

// Which inputs feed a ring on the mains, strongest first: "from ROEDER · KAT −5 dB".
// Ranked by each input's level at the ring frequency plus its fader (the USB
// feeds are pre-fader); channels that are off are left out.
function sourcesLine(e) {
  const line = el("div", "rl-src");
  line.title = e.sources_basis === "fader"
    ? "Inputs feeding this ring, by level at the ring frequency + fader. Channel EQ isn't included (the USB feeds are pre-EQ)."
    : "Inputs feeding this ring, by level at the ring frequency (fader levels unknown: no TF5 connection).";
  line.append("from ");
  e.sources.forEach((s, i) => {
    if (i) line.append(" · ");
    line.append(inputLink(s.label, s.channel, !i));
    if (i) line.append(` ${s.rel_db.toFixed(0)} dB`);
  });
  return line;
}

// Markers on the spectrum for the input being shown: live rings solid, recent dashed.
export function drawRingMarkers() {
  if (!state.showRings) return;
  const now = Date.now() / 1000;
  ctx.font = "11px system-ui, sans-serif";
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  for (const e of state.rings) {
    if (e.channel !== state.input || (!e.active && now - e.last_seen > 300)) continue;
    const x = Math.round(xOf(e.freq)) + 0.5, color = SEV[severity(e)].color;
    ctx.strokeStyle = color; ctx.fillStyle = color;
    ctx.globalAlpha = e.active ? 1 : 0.45;
    ctx.lineWidth = e.active ? 2 : 1;
    ctx.setLineDash(e.active ? [] : [4, 4]);
    ctx.beginPath(); ctx.moveTo(x, PAD.t); ctx.lineTo(x, H - PAD.b); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillText(fmtHz(e.freq), x, PAD.t + 2);
  }
  ctx.globalAlpha = 1; ctx.lineWidth = 1;
}

$("show-rings").checked = state.showRings;
$("show-rings").onchange = (e) => {
  state.showRings = e.target.checked;
  save("tf.rings", state.showRings ? 1 : 0);
  state.dirty = true;
};
function setShowRingList(on) {
  state.showRingList = on;
  $("show-ring-list").checked = on;
  save("tf.ringList", on ? 1 : 0);
  renderRings();
}
$("show-ring-list").checked = state.showRingList;
$("show-ring-list").onchange = (e) => setShowRingList(e.target.checked);
setInterval(() => { if (state.rings.length) renderRings(); }, 5000);  // keep "x s ago" current
