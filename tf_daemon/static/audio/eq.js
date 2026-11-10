// The console's EQ for the shown input, overlaid on the spectrum (editing: eqedit.js).

import { drawPanelEq, rebuildPanel, schedulePanel } from "./console.js";
import { bandCurve, eqEditReset } from "./eqedit.js";
import { H, PAD, W, xOf } from "./plot.js";
import { $, C, ctx, fmtHz, loadInt, save, state } from "./shared.js";

// The EQ set on the console for the channel on the shown input (pushed as
// "tf5-eq" over /ws/audio; only when tf5.eq is on). Drawn at the spectrum's own
// dB scale (the View range), with EQ 0 dB at the middle of the plot: a 70 dB
// range shows ±35 dB. With tf5.eq_write its handles can be dragged (eqedit.js).
state.showEq = loadInt("tf.eq", 0) === 1;
state.eq = null;         // last "tf5-eq" message for state.input
export let eqAvailable = null;  // null = not known yet, false = tf5.eq off (404)
// What a person has locked per band ("<input>:<key>" -> {freq, gain, q}): a drag leaves those
// alone, their fields can't be typed in. This page's own, not the desk's.
state.eqLocks = {};
export const eqLocks = (key) => (state.eqLocks[`${state.input}:${key}`] ||= {});
export const eqLocked = (key) => Object.values(eqLocks(key)).some(Boolean);

// Pushed over /ws/audio for the viewed input whenever it changes on the desk
// (or the view changes); nothing arrives when tf5.eq is off.
export function onConsoleEq(msg) {
  const first = !eqAvailable;
  eqAvailable = true;
  if (msg.index === state.input) {
    // A drag is showing its own preview (or its last writes are still coming back): apply when it's done.
    if (state.eqEditing || state.eqSettling) state.eqPushed = msg;
    else state.eq = msg;
  }
  $("eq-toggle").hidden = false;
  state.dirty = true;
  if (first) rebuildPanel(); else { drawPanelEq(); schedulePanel(); }  // (may make the fader writable)
}

$("show-eq").checked = state.showEq;
$("show-eq").onchange = (e) => {
  state.showEq = e.target.checked;
  save("tf.eq", state.showEq ? 1 : 0);
  if (!state.showEq) eqEditReset();
  state.dirty = true;
};

// EQ value (dB) at f, interpolated on the curve's log-spaced points.
export function eqAt(f) {
  const d = state.eq;
  if (!d || !d.freqs || !d.freqs.length) return null;
  const fr = d.freqs, c = d.curve;
  if (f <= fr[0]) return c[0];
  for (let i = 1; i < fr.length; i++) {
    if (f <= fr[i]) {
      const t = (Math.log(f) - Math.log(fr[i - 1])) / (Math.log(fr[i]) - Math.log(fr[i - 1]));
      return c[i - 1] + t * (c[i] - c[i - 1]);
    }
  }
  return c[c.length - 1];
}

function eqNote(d) {
  if (!d.connected) return "EQ · console not connected";
  if (!d.synced) return "EQ · reading console…";
  if (!d.source) return `EQ · USB ${d.index + 1} is not on the console patch`;
  if (!d.eq) return `EQ · not available for ${d.source}`;
  return `EQ ${d.source}${d.eq.on ? "" : " · OFF"}${d.eq.one_knob ? " · 1-knob on" : ""}`;
}

// EQ curves (the console's and the vocal assist's) share one scale: centred,
// following the View range, so they can be compared directly.
export const eqHalf = () => (state.dbMax - state.dbMin) / 2;     // EQ dB shown above / below 0
export const eqY = (db) => (PAD.t + H - PAD.b) / 2 - db / eqHalf() * (H - PAD.b - PAD.t) / 2;

// Where the handles are: a band at its own frequency and gain (bells, shelves: what you grab
// to move it), the HPF and filter bands on the curve at their frequency. ``key`` names the
// band for editing ("HPF", "L", "LM", "HM", "H").
export function eqHandles() {
  const d = state.eq, eq = d && d.eq;
  if (!eq || !d.freqs || !d.freqs.length) return [];
  const yEq = (db) => eqY(Math.max(-eqHalf(), Math.min(eqHalf(), db)));
  const out = [];
  if (eq.hpf) {
    out.push({ key: "HPF", label: "HPF", kind: "hpf", f: eq.hpf.freq, x: xOf(eq.hpf.freq), y: yEq(eqAt(eq.hpf.freq) ?? 0),
               active: eq.hpf.on });
  }
  for (const b of eq.bands) {
    const filter = b.type === "hpf" || b.type === "lpf";
    const y = filter ? yEq(eqAt(b.freq) ?? 0) : yEq(b.gain);
    out.push({ key: b.label, label: filter ? b.type.toUpperCase() : b.label, kind: filter ? "filter" : b.type,
               f: b.freq, x: xOf(b.freq), y, active: filter ? b.on !== false : b.gain !== 0 });
  }
  return out;
}

export function drawEq() {
  const d = state.eq;
  if (!state.showEq || !eqAvailable || !d || d.index !== state.input) return;
  const top = PAD.t, bot = H - PAD.b, x0 = PAD.l, x1 = W - PAD.r;
  const half = eqHalf();
  const yEq = eqY;
  ctx.font = "11px system-ui, sans-serif";
  ctx.lineWidth = 1;
  ctx.strokeStyle = C.eq; ctx.fillStyle = C.eq;

  // Its scale: 0 dB line plus labels at the right edge.
  ctx.globalAlpha = 0.4;
  ctx.setLineDash([2, 4]);
  const y0 = Math.round(yEq(0)) + 0.5;
  ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y0); ctx.stroke();
  ctx.setLineDash([]);
  ctx.globalAlpha = 0.85;
  ctx.textAlign = "right"; ctx.textBaseline = "middle";
  const step = half > 40 ? 12 : 6;
  const edge = Math.floor((half - 2) / step) * step;
  for (let db = edge; db >= -edge; db -= step) {
    ctx.fillText(`${db > 0 ? "+" : ""}${db}`, x1 - 4, yEq(db));
  }
  ctx.textAlign = "left"; ctx.textBaseline = "top";
  ctx.fillText(eqNote(d), x0 + 6, top + 4);
  ctx.globalAlpha = 1;
  const eq = d.eq;
  if (!eq || !d.freqs.length) return;

  // Shade between the curve and 0 dB, as the TF5's EQ screen does (only when on).
  if (eq.on) {
    ctx.beginPath();
    d.freqs.forEach((f, i) => { const x = xOf(f), y = yEq(d.curve[i]); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
    ctx.lineTo(xOf(d.freqs[d.freqs.length - 1]), yEq(0));
    ctx.lineTo(xOf(d.freqs[0]), yEq(0));
    ctx.closePath();
    ctx.fillStyle = C.eqFill;
    ctx.fill();
    ctx.fillStyle = C.eq;
  }

  // The selected (or dragged) band's own part of the curve, under the total: what it adds.
  drawBand(d);

  // The response curve (clipped by the plot, not flattened onto its edge); dashed
  // and dim when the EQ is switched off on the desk.
  ctx.globalAlpha = eq.on ? 1 : 0.45;
  ctx.lineWidth = 2;
  if (!eq.on) ctx.setLineDash([6, 4]);
  ctx.beginPath();
  d.freqs.forEach((f, i) => { const x = xOf(f), y = yEq(d.curve[i]); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
  ctx.stroke();
  ctx.setLineDash([]);

  // A handle per band (filled when it does something) and the HPF (eqHandles), with its
  // lock beside the label when the EQ can be edited.
  ctx.lineWidth = 1.5;
  for (const h of eqHandles()) {
    const big = h.key === state.eqSelected;
    ctx.beginPath(); ctx.arc(h.x, h.y, big ? 6 : 4, 0, 2 * Math.PI);
    if (h.active) ctx.fill(); else ctx.stroke();
    ctx.textAlign = "center"; ctx.textBaseline = "bottom";
    ctx.fillText(h.label, h.x, h.y - (big ? 8 : 6));
    if (d.writable) drawLock(lockSpot(h), eqLocked(h.key));
  }
  ctx.globalAlpha = 1; ctx.lineWidth = 1;
  drawReadout(d);
}

// Where a handle's lock is: just right of its label (canvas CSS px).
export function lockSpot(h) {
  ctx.font = "11px system-ui, sans-serif";
  const big = h.key === state.eqSelected;
  return { x: h.x + ctx.measureText(h.label).width / 2 + 7, y: h.y - (big ? 8 : 6) - 6 };
}
// A small padlock centred on p: closed and solid when locked, open and faint when not.
function drawLock(p, locked) {
  const a = ctx.globalAlpha;
  ctx.globalAlpha = a * (locked ? 1 : 0.4);
  ctx.lineWidth = 1.2;
  ctx.beginPath();
  if (locked) ctx.arc(p.x, p.y - 1, 2.3, Math.PI, 0);
  else { ctx.arc(p.x + 3, p.y - 2.5, 2.3, Math.PI, 0); }
  ctx.stroke();
  ctx.beginPath(); ctx.rect(p.x - 3.5, p.y - 1, 7, 5.5);
  if (locked) ctx.fill(); else ctx.stroke();
  ctx.globalAlpha = a; ctx.lineWidth = 1.5;
}

// While a band is dragged: its values in a small tag beside the handle (Hz; dB and Q where
// the band has them), kept inside the plot.
let readoutShown = null;   // the text last drawn, for the browser tests
function readoutText(eq, key) {   // [[text, lock name or null], ...]
  if (key === "HPF") return [[fmtHz(eq.hpf.freq), "freq"], [eq.hpf.on ? `${eq.hpf.slope || 12} dB/oct` : "off", null]];
  const b = eq.bands.find((x) => x.label === key);
  if (!b) return null;
  if (b.type === "hpf" || b.type === "lpf") return [[fmtHz(b.freq), "freq"]];
  const out = [[fmtHz(b.freq), "freq"], [`${b.gain > 0 ? "+" : ""}${b.gain.toFixed(1)} dB`, "gain"]];
  if (b.type === "bell") out.push([`Q ${+b.q.toFixed(2)}`, "q"]);
  return out;
}
function drawReadout(d) {
  readoutShown = null;
  const h = state.eqEditing && eqHandles().find((x) => x.key === state.eqSelected);
  const parts = h && readoutText(d.eq, h.key);
  if (!parts) return;
  const locks = eqLocks(h.key), sep = "  ·  ", LOCK_W = 11;
  ctx.font = "600 12px system-ui, sans-serif";
  const widths = parts.map(([t, k]) => ctx.measureText(t).width + (locks[k] ? LOCK_W : 0));
  const w = widths.reduce((s, x) => s + x, 0) + ctx.measureText(sep).width * (parts.length - 1) + 16;
  const ht = 22, gap = 12;
  // Above-right of the handle; flipped left / below when that would leave the plot.
  let x = h.x + gap, y = h.y - gap - ht;
  if (x + w > W - PAD.r) x = h.x - gap - w;
  if (y < PAD.t) y = h.y + gap;
  x = Math.max(PAD.l, x); y = Math.min(H - PAD.b - ht, y);
  ctx.beginPath(); ctx.roundRect(x, y, w, ht, 6);
  ctx.globalAlpha = 0.92; ctx.fillStyle = C.panel; ctx.fill();
  ctx.globalAlpha = 1; ctx.strokeStyle = C.eq; ctx.lineWidth = 1; ctx.stroke();
  // Each value in turn; a locked one muted, behind a lock.
  ctx.textAlign = "left"; ctx.textBaseline = "middle";
  let cx = x + 8;
  parts.forEach(([t, k], i) => {
    if (i) { ctx.fillStyle = C.muted; ctx.fillText(sep, cx, y + ht / 2 + 0.5); cx += ctx.measureText(sep).width; }
    if (locks[k]) { ctx.fillStyle = ctx.strokeStyle = C.muted; drawLock({ x: cx + 3.5, y: y + ht / 2 - 1 }, true); cx += LOCK_W; }
    ctx.fillStyle = locks[k] ? C.muted : C.text;
    ctx.fillText(t, cx, y + ht / 2 + 0.5);
    cx += ctx.measureText(t).width;
  });
  ctx.font = "11px system-ui, sans-serif";
  readoutShown = parts.map(([t, k]) => (locks[k] ? `[locked] ${t}` : t)).join(sep);
}
window.tfEqReadout = () => readoutShown;

// The selected band's contribution: its own curve (thin, light) with its area to 0 dB tinted,
// so it reads apart from the total drawn over it. Skipped when the band is off or flat.
let bandShown = null;   // {key, peak} last drawn, for the browser tests
function drawBand(d) {
  bandShown = null;
  const key = state.eqSelected;
  if (!key) return;
  const curve = bandCurve(d.eq, key, d.freqs);
  const peak = Math.max(...curve.map(Math.abs));
  if (peak < 0.05) return;
  const path = () => {
    ctx.beginPath();
    d.freqs.forEach((f, i) => { const x = xOf(f), y = eqY(curve[i]); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
  };
  path();
  ctx.lineTo(xOf(d.freqs[d.freqs.length - 1]), eqY(0));
  ctx.lineTo(xOf(d.freqs[0]), eqY(0));
  ctx.closePath();
  ctx.globalAlpha = 1;
  ctx.fillStyle = C.eqBandFill;
  ctx.fill();
  path();
  ctx.globalAlpha = d.eq.on ? 0.9 : 0.45;
  ctx.lineWidth = 1.5;
  ctx.strokeStyle = C.eqBand;
  ctx.stroke();
  ctx.globalAlpha = 1; ctx.lineWidth = 1;
  ctx.strokeStyle = C.eq; ctx.fillStyle = C.eq;
  bandShown = { key, peak };
}
window.tfEqBandShown = () => bandShown;
