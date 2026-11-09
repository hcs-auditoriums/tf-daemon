// Ring / feedback events: the header alert, the list, and markers on the plot.

import { selectInput } from "./controls.js";
import { H, PAD, xOf } from "./plot.js";
import { $, ctx, el, loadInt, save, state } from "./shared.js";

// Ring events from the detector (tf_daemon/dsp/ring.py), for all analyzed inputs.
// Severity: a growing ring is feedback; a sustained ring, or a tail that keeps
// coming back at the same frequency, is a ring; a single tail is only possible
// (a slowly decaying instrument note can look like one) and raises no alert.
state.rings = [];
state.showRings = loadInt("tf.rings", 1) === 1;
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
    alert.onclick = () => { if (e.channel !== state.input) selectInput(e.channel); };
  }

  const list = $("ring-list");
  list.hidden = !state.showRings || !state.rings.length;
  if (list.hidden) return;
  const head = el("div", "rl-head");
  const clear = el("button", "rl-btn", "Clear");
  clear.type = "button";
  clear.title = "Forget past rings (live ones stay)";
  clear.onclick = () => post("/api/audio/rings/clear");
  head.append(el("span", "", "Rings"), clear);
  const rows = [head];
  for (const e of state.rings.slice(0, 8)) {
    const sev = severity(e);
    const row = el("div", `rl-row${e.active ? " live" : ""}`);
    const dot = el("span", "rl-dot"); dot.style.background = SEV[sev].color;
    const main = el("div", "rl-main");
    const top = el("div", "rl-top");
    const freq = el("button", "rl-freq", fmtHz(e.freq));
    freq.type = "button";
    freq.title = `Show ${e.label}`;
    freq.onclick = () => { if (e.channel !== state.input) selectInput(e.channel); };
    top.append(freq, el("span", "rl-sev", SEV[sev].label));
    if (e.count > 1) top.append(el("span", "rl-count", `×${e.count}`));
    const sub = el("div", "rl-sub",
      `${e.label} · GEQ ${fmtBand(e.geq)} · ${e.active ? "now" : ago(e.last_seen)}`);
    main.append(top, sub);
    if (e.sources && e.sources.length) main.append(sourcesLine(e));
    const ign = el("button", "rl-btn", "Ignore");
    ign.type = "button";
    ign.title = "Not feedback (a steady tone): stop reporting this frequency";
    ign.onclick = () => post("/api/audio/rings/ignore", { freq: e.freq });
    row.append(dot, main, ign);
    rows.push(row);
  }
  list.replaceChildren(...rows);
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
    const b = el("button", i ? "rl-srcname" : "rl-srcname top", s.label);
    b.type = "button";
    b.title = `Show ${s.label}`;
    b.onclick = () => selectInput(s.channel);
    line.append(b);
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
  renderRings();
  state.dirty = true;
};
setInterval(() => { if (state.rings.length) renderRings(); }, 5000);  // keep "x s ago" current
