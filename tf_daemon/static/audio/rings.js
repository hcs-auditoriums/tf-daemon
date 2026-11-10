// Ring / feedback events: the header alert, the list, and markers on the plot.

import { channelTile } from "./console.js";
import { paintChannelTiles, selectInput } from "./controls.js";
import { H, PAD, W, xOf } from "./plot.js";
import { $, ctx, el, fmtHz, loadInt, save, state } from "./shared.js";

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
const fmtBand = (b) => (b >= 1000 ? `${+(b / 1000).toFixed(2)}k` : `${b}`);
function ago(t) {
  const s = Math.max(0, Date.now() / 1000 - t);
  return s < 60 ? `${Math.round(s)} s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
}

export function onRings(msg) {
  state.rings = msg.events || [];
  state.ringingInputs = ringingInputs(state.rings);
  paintChannelTiles();
  renderRings();
  state.dirty = true;
}

// The input a ring comes from, where its fader and EQ are: the strongest source of a ring on
// the mains, or the input it was heard on if not a bus (null: a bus ring with no known source).
// Never a channel that is off on the desk: it feeds nothing to the PA, so it can't be the
// cause, though its mic still hears the ring in the room (the USB feeds are pre-fader).
const isOff = (ch) => channelTile(ch).on === false;
function feedingInput(e) {
  if (e.sources && e.sources.length) return e.sources.find((s) => !isOff(s.channel))?.channel ?? null;
  return isBus(e.channel) || isOff(e.channel) ? null : e.channel;
}
const showFeeding = (e) => { const ch = feedingInput(e) ?? e.channel; if (ch !== state.input) selectInput(ch); };
const feedingLabel = (e) => {
  const ch = feedingInput(e);
  return ch === null ? e.label : e.sources?.find((s) => s.channel === ch)?.label ?? e.label;
};

// The inputs live rings come from (input -> worst severity), for the dots on the channel tiles.
function ringingInputs(events) {
  const out = new Map();
  for (const e of events) {
    const sev = severity(e);
    if (!e.active || sev === "possible") continue;
    const ch = feedingInput(e);
    if (ch !== null && (!out.has(ch) || SEV[sev].rank > SEV[out.get(ch)].rank)) out.set(ch, sev);
  }
  return out;
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
    const src = feedingInput(e) !== null && feedingInput(e) !== e.channel ? ` ← ${feedingLabel(e)}` : "";
    alert.textContent = `${SEV[sev].label} ${fmtHz(e.freq)} · ${e.label}${src}` + (live.length > 1 ? ` +${live.length - 1}` : "");
    alert.title = `Show ${feedingLabel(e)}`;
    alert.onclick = () => {
      showFeeding(e);
      if (!state.showRingList) setShowRingList(true);
      if (!ringsOpen) setRingsOpen(true);
      $("ring-list").scrollIntoView({ block: "nearest" });   // a phone: the list is under the plot
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
let ringsOpen = loadInt("tf.ringsOpen", 1) === 1;
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

// Mains (buses) hear every ring: a row names the inputs it came from, not them.
const isBus = (ch) => !!state.meta?.channels[ch]?.bus;
const ringKey = (g) => Math.round(Math.log2(g.top.freq) * 24);  // a quarter-tone bucket: stays put as events come and go
const expanded = new Set();   // rows opened for their details (ringKey)

// One line per ring: severity, frequency, how often, the GEQ band, the input it came from, when.
// A tap opens the details: every input that heard it and the ranked sources.
function ringRow(g) {
  const e = g.top, sev = severity(e), active = g.events.some((x) => x.active), key = ringKey(g);
  const open = expanded.has(key);
  const row = el("div", `rl-row${active ? " live" : ""}${open ? " open" : ""}`);
  const toggle = () => { if (open) expanded.delete(key); else expanded.add(key); renderRings(); };
  row.onclick = toggle;
  const line = el("div", "rl-line");
  const dot = el("span", "rl-dot"); dot.style.background = SEV[sev].color;
  dot.title = SEV[sev].label;
  const freq = el("button", "rl-freq", fmtHz(e.freq));
  freq.type = "button";
  const fedBy = g.events.find((x) => x.sources && x.sources.length) || g.events.find((x) => !isBus(x.channel)) || e;
  freq.title = `Show ${feedingLabel(fedBy)}`;
  freq.onclick = (ev) => { ev.stopPropagation(); showFeeding(fedBy); };
  line.append(dot, freq);
  if (sev === "feedback") line.append(el("span", "rl-sev", "FEEDBACK"));
  const count = Math.max(...g.events.map((x) => x.count));
  if (count > 1) line.append(el("span", "rl-count", `×${count}`));
  line.append(el("span", "rl-geq", `GEQ ${fmtBand(e.geq)}`));
  const fed = g.events.find((x) => x.sources && x.sources.length);
  const from = fed ? fed.sources[0] : g.events.find((x) => !isBus(x.channel));
  if (from) {
    const link = inputLink(from.label, from.channel, true);
    link.classList.add("rl-from");
    line.append(link);
  }
  line.append(el("span", "rl-when", active ? "now" : ago(Math.max(...g.events.map((x) => x.last_seen)))));
  const more = el("button", "rl-more", open ? "▾" : "▸");
  more.type = "button";
  more.setAttribute("aria-expanded", String(open));
  more.setAttribute("aria-label", open ? "Hide details" : "Details");
  more.onclick = (ev) => { ev.stopPropagation(); toggle(); };
  const ign = el("button", "rl-btn", "Ignore");
  ign.type = "button";
  ign.title = "Not feedback (a steady tone): stop reporting this frequency";
  ign.onclick = (ev) => { ev.stopPropagation(); post("/api/audio/rings/ignore", { freq: e.freq }); };
  line.append(more, ign);
  row.append(line);
  if (open) {
    const where = el("div", "rl-sub");
    const heard = [...new Map(g.events.map((x) => [x.channel, x])).values()];
    const mics = heard.filter((x) => !isBus(x.channel)), buses = heard.length - mics.length;
    where.append("Heard on ");
    (mics.length ? mics : heard).forEach((x, i) => { if (i) where.append(", "); where.append(inputLink(x.label, x.channel, false)); });
    if (mics.length && buses) where.append(" and the mains");
    row.append(where);
    if (fed) row.append(sourcesLine(fed));
  }
  return row;
}

function inputLink(label, channel, top) {
  const b = el("button", top ? "rl-srcname top" : "rl-srcname", label);
  b.type = "button";
  b.title = `Show ${label}`;
  b.onclick = (ev) => { ev.stopPropagation(); selectInput(channel); };
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

// While a band is being dragged or its settings are open, the list folds to its title bar.
export function syncRingList() {
  $("ring-list").classList.toggle("eq-busy", !!state.eqEditing || !!document.querySelector(".eq-pop"));
}

// Markers on the spectrum for the input being shown (rings heard on it, or on the mains it
// feeds): live rings solid, recent dashed.
// Labels that would overlap share one ("3 rings 275–353 Hz", in the worst one's colour).
const LABEL_RESERVE_R = 32;  // the console button's corner
function shownRings() {   // this input's live rings and those of the last five minutes
  const now = Date.now() / 1000;
  const mine = state.rings.filter((e) => (e.channel === state.input || feedingInput(e) === state.input)
    && (e.active || now - e.last_seen <= 300));
  return groupRings(mine).map((g) => g.top);   // one marker per ring, not one per input that heard it
}
export function drawRingMarkers() {
  if (!state.showRings) return;
  const shown = shownRings();
  for (const e of shown) {
    const x = Math.round(xOf(e.freq)) + 0.5;
    ctx.strokeStyle = SEV[severity(e)].color;
    ctx.globalAlpha = e.active ? 1 : 0.45;
    ctx.lineWidth = e.active ? 2 : 1;
    ctx.setLineDash(e.active ? [] : [4, 4]);
    ctx.beginPath(); ctx.moveTo(x, PAD.t); ctx.lineTo(x, H - PAD.b); ctx.stroke();
  }
  ctx.setLineDash([]); ctx.globalAlpha = 1; ctx.lineWidth = 1;
  ctx.font = "11px system-ui, sans-serif";
  ctx.textAlign = "left"; ctx.textBaseline = "top";
  for (const l of labelsFor(shown)) {
    ctx.globalAlpha = l.active ? 1 : 0.6;
    ctx.fillStyle = "rgba(13, 16, 20, 0.8)";
    ctx.fillRect(l.x - 3, PAD.t + 1, l.w + 6, 15);
    ctx.fillStyle = l.color;
    ctx.fillText(l.text, l.x, PAD.t + 2);
  }
  ctx.globalAlpha = 1;
}

// Where each label goes: neighbours whose labels would touch merge until none do.
function ringLabels(events, measure, left, right) {
  const GAP = 6;
  let groups = [...events].sort((a, b) => a.freq - b.freq).map((e) => ({ events: [e] }));
  const place = (g) => {
    const fs = g.events.map((e) => e.freq), lo = Math.min(...fs), hi = Math.max(...fs);
    g.text = g.events.length === 1 ? fmtHz(lo) : `${g.events.length} rings ${fmtRange(lo, hi)}`;
    g.w = measure(g.text);
    const mid = (xOf(lo) + xOf(hi)) / 2;
    g.x = Math.max(left, Math.min(right - g.w, mid - g.w / 2));
  };
  groups.forEach(place);
  for (let merged = true; merged && groups.length > 1;) {
    merged = false;
    for (let i = 0; i + 1 < groups.length; i++) {
      const a = groups[i], b = groups[i + 1];
      if (a.x + a.w + GAP > b.x) {
        groups.splice(i, 2, { events: [...a.events, ...b.events] });
        place(groups[i]);
        merged = true;
        break;
      }
    }
  }
  return groups.map((g) => {
    const worst = g.events.reduce((w, e) => (SEV[severity(e)].rank > SEV[severity(w)].rank ? e : w));
    return { text: g.text, x: g.x, w: g.w, color: SEV[severity(worst)].color, active: g.events.some((e) => e.active) };
  });
}
function fmtRange(lo, hi) {
  if (hi < 1000) return `${Math.round(lo)}–${Math.round(hi)} Hz`;
  if (lo >= 1000) return `${+(lo / 1000).toFixed(2)}–${+(hi / 1000).toFixed(2)} kHz`;
  return `${fmtHz(lo)}–${fmtHz(hi)}`;
}
const labelsFor = (shown) => ringLabels(shown, (t) => ctx.measureText(t).width, PAD.l + 2, W - PAD.r - LABEL_RESERVE_R);
// For the browser tests: the labels as drawn (canvas CSS px), or as they would be for ``events``.
window.tfRingLabels = (events) => {
  ctx.font = "11px system-ui, sans-serif";
  return labelsFor(events || (state.showRings ? shownRings() : []));
};

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
