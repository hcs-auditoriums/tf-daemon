// The console's EQ for the shown input, overlaid on the spectrum (editing: eqedit.js).

import { drawPanelEq, rebuildPanel, schedulePanel } from "./console.js";
import { eqEditReset } from "./eqedit.js";
import { H, PAD, W, xOf } from "./plot.js";
import { $, C, ctx, loadInt, save, state } from "./shared.js";

// The EQ set on the console for the channel on the shown input (pushed as
// "tf5-eq" over /ws/audio; only when tf5.eq is on). Drawn at the spectrum's own
// dB scale (the View range), with EQ 0 dB at the middle of the plot: a 70 dB
// range shows ±35 dB. With tf5.eq_write its handles can be dragged (eqedit.js).
state.showEq = loadInt("tf.eq", 0) === 1;
state.eq = null;         // last "tf5-eq" message for state.input
export let eqAvailable = null;  // null = not known yet, false = tf5.eq off (404)

// Pushed over /ws/audio for the viewed input whenever it changes on the desk
// (or the view changes); nothing arrives when tf5.eq is off.
export function onConsoleEq(msg) {
  const first = !eqAvailable;
  eqAvailable = true;
  if (msg.index === state.input) {
    if (state.eqEditing) state.eqPushed = msg;   // a drag is showing its own preview: apply when it ends
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

  // The response curve (clipped by the plot, not flattened onto its edge); dashed
  // and dim when the EQ is switched off on the desk.
  ctx.globalAlpha = eq.on ? 1 : 0.45;
  ctx.lineWidth = 2;
  if (!eq.on) ctx.setLineDash([6, 4]);
  ctx.beginPath();
  d.freqs.forEach((f, i) => { const x = xOf(f), y = yEq(d.curve[i]); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
  ctx.stroke();
  ctx.setLineDash([]);

  // A handle per band (filled when it does something) and the HPF (eqHandles).
  ctx.lineWidth = 1.5;
  ctx.textAlign = "center"; ctx.textBaseline = "bottom";
  for (const h of eqHandles()) {
    const big = h.key === state.eqSelected;
    ctx.beginPath(); ctx.arc(h.x, h.y, big ? 6 : 4, 0, 2 * Math.PI);
    if (h.active) ctx.fill(); else ctx.stroke();
    ctx.fillText(h.label, h.x, h.y - (big ? 8 : 6));
  }
  ctx.globalAlpha = 1; ctx.lineWidth = 1;
}
