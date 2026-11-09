// Editing the console's EQ on the spectrum (when the desk allows it: tf5.eq_write).
//
//   drag a handle       frequency (left / right) and gain (up / down); the HPF and filter
//                       bands move sideways only
//   scroll on a handle  Q (bells)
//   click a handle      its settings: type, frequency, gain, Q (HPF: on, frequency), to type in
//
// Every change is a person's: each drag / entry is sent with "confirm" (PUT /api/tf5/eq/<usb>).
// While dragging, the curve is redrawn here with the desk's own filter maths (a copy of
// tf_daemon/dsp/eq.py) and the desk's echo is applied when the drag ends; writes are
// throttled to one in flight, always the latest position.

import { eqHalf, eqHandles, eqY } from "./eq.js";
import { fOfX } from "./plot.js";
import { $, canvas, el, state } from "./shared.js";

const PATH = { L: "PEQ/Bank/BandL", LM: "PEQ/Bank/Band:0", HM: "PEQ/Bank/Band:1", H: "PEQ/Bank/BandH" };
const HIT_PX = 9;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

// ------------------------------------------------------------------ the desk's filter maths (dsp/eq.py)

const FS = 48000, BUTTERWORTH_Q = 0.707106;
function biquad(kind, freq, gain, q) {
  const fs = Math.max(FS, 44100);
  q = Math.max(q, 0.1);
  const k = Math.tan(Math.PI * Math.max(freq, 16) / fs), kk = k * k, v = 10 ** (Math.abs(gain) / 20);
  if (kind === "bell") {
    const [num, den] = gain > 0 ? [v * k / q, k / q] : [k / q, v * k / q];
    return [[1 + kk + num, 2 * (kk - 1), 1 + kk - num], [1 + kk + den, 2 * (kk - 1), 1 + kk - den]];
  }
  if (kind === "low_shelf" || kind === "high_shelf") {
    const sv = Math.sqrt(v), plain = [1 + k, k - 1];
    const shelf = kind === "low_shelf" ? [1 + k * sv, k * sv - 1] : [k + sv, k - sv];
    const [top, bottom] = gain > 0 ? [shelf, plain] : [plain, shelf];
    const sq = (a) => [a[0] * a[0], 2 * a[0] * a[1], a[1] * a[1]];
    return [sq(top), sq(bottom)];
  }
  if (kind === "hpf" || kind === "lpf") {
    const d = [1 + k / q + kk, 2 * (kk - 1), 1 - k / q + kk];
    return [kind === "lpf" ? [kk, 2 * kk, kk] : [1, -2, 1], d];
  }
  return [kind === "lpf1" ? [k, k, 0] : [1, -1, 0], [1 + k, k - 1, 0]];   // hpf1 / lpf1
}
function sectionDb(kind, freq, gain, q, f) {
  const [b, a] = biquad(kind, freq, gain, q), w = -2 * Math.PI * f / Math.max(FS, 44100);
  const mag = (c) => { const re = c[0] + c[1] * Math.cos(w) + c[2] * Math.cos(2 * w);
    const im = c[1] * Math.sin(w) + c[2] * Math.sin(2 * w); return Math.hypot(re, im); };
  return 20 * Math.log10(Math.max(mag(b) / mag(a), 1e-12));
}
export function eqCurve(eq, freqs) {
  return freqs.map((f) => {
    if (!eq || !eq.on) return 0;
    let db = 0;
    if (eq.hpf && eq.hpf.on) db += sectionDb(eq.hpf.slope === 6 ? "hpf1" : "hpf", eq.hpf.freq, 0, BUTTERWORTH_Q, f);
    for (const b of eq.bands) {
      if (b.type === "hpf" || b.type === "lpf") { if (b.on !== false) db += sectionDb(b.type, b.freq, 0, BUTTERWORTH_Q, f); }
      else if (b.gain !== 0) db += sectionDb(b.type, b.freq, b.gain, b.q, f);
    }
    return db;
  });
}

// ------------------------------------------------------------------ writes (throttled)

let inflight = false;
let pending = null;   // {usb, changes: {path: raw}} waiting to go out

function send(usb, changes) {
  if (pending && pending.usb === usb) Object.assign(pending.changes, changes);
  else pending = { usb, changes: { ...changes } };
  if (!inflight) flush();
}
function flush() {
  if (!pending) return;
  const { usb, changes } = pending;
  pending = null;
  inflight = true;
  fetch(`/api/tf5/eq/${usb}`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ changes: Object.entries(changes), confirm: true }),
  }).then((r) => r.json()).catch(() => ({ ok: false, error: "server unreachable" }))
    .then((d) => { inflight = false; if (!d.ok) alert(d.error || "The desk didn't take that."); flush(); settled(); });
}

// After a drag the desk echoes each value the drag sent, in order, for a while after the release:
// applied as they came, the handle would jump back and creep forward again. So the preview stays
// until the last write is answered (and its echo has had a moment to arrive), then the desk's
// newest state is taken once.
const SETTLE_MS = 250;
let settleTimer = null;
function settle() {
  state.eqSettling = true;
  settled();
}
function settled() {
  if (!state.eqSettling || inflight || pending) return;
  clearTimeout(settleTimer);
  settleTimer = setTimeout(() => {
    if (inflight || pending) return;  // another edit went out meanwhile: wait for that one
    state.eqSettling = false;
    if (state.eqPushed) { state.eq = state.eqPushed; state.eqPushed = null; state.dirty = true; }
  }, SETTLE_MS);
}

// The desk changes for a band's new settings (raw values: Hz x10, dB x100, Q x1000).
function rawFor(key, s) {
  if (key === "HPF") {
    const out = {};
    if (s.freq !== undefined) out["PEQ/Bank/HPF/Frequency"] = Math.round(clamp(s.freq, 20, 600) * 10);
    if (s.on !== undefined) out["PEQ/Bank/HPF/On"] = s.on ? 1 : 0;
    return out;
  }
  const p = PATH[key], out = {};
  if (!p) return out;
  if (s.freq !== undefined) out[`${p}/Frequency`] = Math.round(clamp(s.freq, 20, 20000) * 10);
  if (s.gain !== undefined) out[`${p}/Gain`] = Math.round(clamp(s.gain, -18, 18) * 10) * 10;
  if (s.q !== undefined) out[`${p}/Q`] = Math.round(clamp(s.q, 0.1, 10) * 1000);
  if (s.type !== undefined) out[`${p}/Type`] = s.type === "bell" ? 0 : 1;
  return out;
}

// Change a band here (preview) and on the desk.
function edit(key, s) {
  const d = state.eq, eq = d && d.eq;
  if (!eq) return;
  if (key === "HPF") Object.assign(eq.hpf, s);
  else {
    const b = eq.bands.find((x) => x.label === key);
    if (!b) return;
    const kind = s.type === undefined ? undefined
      : s.type === "bell" ? "bell" : key === "L" ? "low_shelf" : "high_shelf";
    Object.assign(b, s, kind ? { type: kind } : {});
  }
  d.curve = eqCurve(eq, d.freqs);
  state.dirty = true;
  send(d.index, rawFor(key, s));
}

// ------------------------------------------------------------------ pointer: hit, drag, wheel

const editable = () => state.showEq && state.eq && state.eq.writable && state.eq.index === state.input && state.eq.eq;
function hit(x, y) {
  let best = null, dist = HIT_PX;
  for (const h of eqHandles()) {
    const dd = Math.hypot(h.x - x, h.y - y);
    if (dd <= dist) { best = h; dist = dd; }
  }
  return best;
}
const local = (e) => { const r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
const gainAt = (y) => { const mid = eqY(0), unit = eqY(1) - mid; return clamp((y - mid) / unit, -eqHalf(), eqHalf()); };

let drag = null;   // {key, kind, moved, x0, y0}

canvas.addEventListener("pointerdown", (e) => {
  if (!editable() || e.button !== 0) return;
  const [x, y] = local(e), h = hit(x, y);
  if (!h) { closePop(); return; }
  e.preventDefault();
  canvas.setPointerCapture(e.pointerId);
  drag = { key: h.key, kind: h.kind, moved: false, x0: x, y0: y };
  state.eqSelected = h.key;
  state.eqEditing = true;
  state.dirty = true;
});

canvas.addEventListener("pointermove", (e) => {
  const [x, y] = local(e);
  if (!drag) {
    canvas.style.cursor = editable() && hit(x, y) ? "grab" : "";
    return;
  }
  if (!drag.moved && Math.hypot(x - drag.x0, y - drag.y0) < 3) return;
  drag.moved = true;
  canvas.style.cursor = "grabbing";
  const freq = Math.round(fOfX(x) * 10) / 10;
  const sideways = drag.kind === "hpf" || drag.kind === "filter";
  edit(drag.key, sideways ? { freq } : { freq, gain: Math.round(gainAt(y) * 10) / 10 });
  if (pop) showPop(drag.key);
});

function endDrag() {
  if (!drag) return;
  const { key, moved } = drag;
  drag = null;
  state.eqEditing = false;
  canvas.style.cursor = "";
  if (moved) settle();  // the desk's echoes of the drag are still on their way
  else if (state.eqPushed && !state.eqSettling) { state.eq = state.eqPushed; state.eqPushed = null; state.dirty = true; }
  if (!moved) showPop(key);   // a click: its settings
}
canvas.addEventListener("pointerup", endDrag);
canvas.addEventListener("pointercancel", endDrag);

canvas.addEventListener("wheel", (e) => {
  if (!editable()) return;
  const [x, y] = local(e), h = hit(x, y);
  const b = h && state.eq.eq.bands.find((x2) => x2.label === h.key);
  if (!b || h.kind !== "bell") return;
  e.preventDefault();
  edit(h.key, { q: Math.round(clamp(b.q * (e.deltaY < 0 ? 1.12 : 1 / 1.12), 0.1, 10) * 100) / 100 });
  if (pop) showPop(h.key);
}, { passive: false });

// ------------------------------------------------------------------ the settings popover

let pop = null;

function closePop() {
  if (pop) pop.remove();
  pop = null;
  if (state.eqSelected) { state.eqSelected = null; state.dirty = true; }
}

function field(label, value, step, unit, onSet) {
  const lbl = el("label", "eqp-row");
  const inp = el("input");
  inp.type = "number"; inp.step = String(step); inp.value = String(value);
  inp.setAttribute("aria-label", label);
  inp.onchange = () => { const v = Number(inp.value); if (Number.isFinite(v)) onSet(v); };
  lbl.append(el("span", "", label), inp, el("span", "eqp-unit", unit));
  return lbl;
}

export function showPop(key) {
  const d = state.eq, eq = d && d.eq;
  if (!eq || !editable()) return;
  const h = eqHandles().find((x) => x.key === key);
  if (!h) return;
  if (pop) pop.remove();
  pop = el("div", "eq-pop");
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-label", `EQ ${key}`);
  const head = el("div", "eqp-head");
  const close = el("button", "eqp-x", "×"); close.type = "button"; close.title = "Close"; close.onclick = closePop;
  head.append(el("strong", "", key === "HPF" ? "High-pass" : `Band ${key}`), close);
  pop.append(head);
  if (key === "HPF") {
    const on = el("label", "eqp-row"), cb = el("input");
    cb.type = "checkbox"; cb.checked = eq.hpf.on; cb.onchange = () => edit("HPF", { on: cb.checked });
    on.append(cb, el("span", "", "On"));
    pop.append(on, field("Frequency", eq.hpf.freq, 1, "Hz", (v) => edit("HPF", { freq: v })));
  } else {
    const b = eq.bands.find((x) => x.label === key);
    if (key === "L" || key === "H") {
      if (b.type === "hpf" || b.type === "lpf") {
        pop.append(el("p", "eqp-note", `Used as a ${b.type.toUpperCase()} on the desk.`));
      } else {
        const row = el("label", "eqp-row"), sel = el("select");
        sel.setAttribute("aria-label", "Type");
        sel.append(new Option("Bell", "bell"), new Option("Shelf", "shelf"));
        sel.value = b.type === "bell" ? "bell" : "shelf";
        sel.onchange = () => edit(key, { type: sel.value });
        row.append(el("span", "", "Type"), sel);
        pop.append(row);
      }
    }
    pop.append(field("Frequency", b.freq, 1, "Hz", (v) => edit(key, { freq: v })));
    if (b.type !== "hpf" && b.type !== "lpf") {
      pop.append(field("Gain", b.gain, 0.1, "dB", (v) => edit(key, { gain: v })));
      if (b.type === "bell") pop.append(field("Q", b.q, 0.01, "", (v) => edit(key, { q: v })));
    }
  }
  $("spec-pane").append(pop);
  const pane = $("spec-pane").getBoundingClientRect();
  pop.style.left = `${clamp(h.x + 14, 4, pane.width - pop.offsetWidth - 4)}px`;
  pop.style.top = `${clamp(h.y - pop.offsetHeight / 2, 4, pane.height - pop.offsetHeight - 4)}px`;
  state.eqSelected = key;
  state.dirty = true;
}

document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePop(); });
// Another input, or the overlay hidden: nothing to edit.
export function eqEditReset() {
  closePop();
  drag = null;
  state.eqEditing = false;
  clearTimeout(settleTimer);
  state.eqSettling = false;
  if (state.eqPushed) { state.eq = state.eqPushed; state.eqPushed = null; state.dirty = true; }
}

// For the browser tests: where the handles are (canvas CSS px).
window.tfEqHandles = () => eqHandles().map(({ key, x, y }) => ({ key, x, y }));
// ... and how far this page's curve maths is from the desk's own (dB), for the EQ shown.
window.tfEqPreviewError = () => {
  const d = state.eq;
  if (!d || !d.eq || !d.curve.length) return null;
  const mine = eqCurve(d.eq, d.freqs);
  return Math.max(...mine.map((v, i) => Math.abs(v - d.curve[i])));
};
