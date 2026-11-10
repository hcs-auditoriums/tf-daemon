// Editing the console's EQ on the spectrum (when the desk allows it: tf5.eq_write).
//
//   drag a handle       frequency (left / right) and gain (up / down); the HPF and filter
//                       bands move sideways only
//   scroll on a handle  Q (bells)
//   click a handle      its settings: type, frequency, gain, Q (HPF: on, frequency), to type in;
//                       clicking it again closes them
//   its settings' locks keep frequency, gain or Q as they are (a drag then changes only the
//                       rest); a locked band shows a padlock by its label. No lock to tap on the
//                       plot itself: too small to see and too easy to hit on a touchscreen.
//   double-tap a handle its gain back to 0 dB (as in TF StageMix)
//   pinch               Q of the band dragged / selected, else the bell nearest the fingers
//
// Every change is undoable (undoEq / redoEq, per input); applyEq sets a whole EQ at once (paste,
// compare, undo). The toolbar over the plot is eqbar.js.
//
// Every change is a person's: each drag / entry is sent with "confirm" (PUT /api/tf5/eq/<usb>).
// While dragging, the curve is redrawn here with the desk's own filter maths (a copy of
// tf_daemon/dsp/eq.py) and the desk's echo is applied when the drag ends; writes are
// throttled to one in flight, always the latest position.

import { eqHalf, eqHandles, eqLocked, eqLocks, eqY } from "./eq.js";
import { fOfX, PAD, W } from "./plot.js";
import { $, canvas, el, state } from "./shared.js";

const PATH = { L: "PEQ/Bank/BandL", LM: "PEQ/Bank/Band:0", HM: "PEQ/Bank/Band:1", H: "PEQ/Bank/BandH" };
// Fingers need more room than a mouse: handles are easier to hit on touch.
const COARSE = matchMedia("(pointer: coarse)").matches;
const HIT_PX = COARSE ? 18 : 9;
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
// What one band ("HPF", "L", "LM", "HM", "H") adds at f (dB): nothing when it's off or flat.
function bandDb(eq, key, f) {
  if (key === "HPF") {
    const h = eq.hpf;
    return h && h.on ? sectionDb(h.slope === 6 ? "hpf1" : "hpf", h.freq, 0, BUTTERWORTH_Q, f) : 0;
  }
  const b = eq.bands.find((x) => x.label === key);
  if (!b) return 0;
  if (b.type === "hpf" || b.type === "lpf") return b.on !== false ? sectionDb(b.type, b.freq, 0, BUTTERWORTH_Q, f) : 0;
  return b.gain !== 0 ? sectionDb(b.type, b.freq, b.gain, b.q, f) : 0;
}
const KEYS = (eq) => ["HPF", ...eq.bands.map((b) => b.label)];
export function eqCurve(eq, freqs) {
  return freqs.map((f) => (!eq || !eq.on ? 0 : KEYS(eq).reduce((db, k) => db + bandDb(eq, k, f), 0)));
}
// One band's own part of the curve (drawn under it while that band is selected / dragged).
export const bandCurve = (eq, key, freqs) => freqs.map((f) => bandDb(eq, key, f));

// ------------------------------------------------------------------ writes (throttled)

let inflight = false;
let pending = null;   // {usb, changes: {path: raw}} waiting to go out

function send(usb, changes) {
  state.eqSettling = true;   // the preview stays until the desk's echoes are done (below)
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

// The desk echoes each value written, one by one, for a while after: applied as they came, the
// curve would flash back to a half-changed EQ (a drag's handle jump back and creep forward; a
// whole-EQ change such as Compare or Undo show old and new bands mixed). So after any write the
// preview stays until the last write is answered (and its echo has had a moment to arrive), then
// the desk's newest state is taken once. A drag holds it until the release (settle()).
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

// The desk's ranges (Hz, dB, Q), sent with the EQ (console/module.py EQ_LIMITS, from the
// write guard's limits). Every change is held to them, the preview here as well as the write.
const lim = (name) => state.eq?.limits?.[name] ?? [-Infinity, Infinity];
function limited(key, s) {
  const out = { ...s };
  if (out.freq !== undefined) out.freq = clamp(out.freq, ...lim(key === "HPF" ? "hpf_freq" : "freq"));
  if (out.gain !== undefined) out.gain = clamp(out.gain, ...lim("gain"));
  if (out.q !== undefined) out.q = clamp(out.q, ...lim("q"));
  return out;
}

// The desk changes for a band's new settings (raw values: Hz x10, dB x100, Q x1000), already limited.
const TYPE_RAW = { bell: 0, shelf: 1, lpf: 2, hpf: 2 };
function rawFor(key, s) {
  if (key === "EQ") return s.on === undefined ? {} : { "PEQ/On": s.on ? 1 : 0 };
  if (key === "HPF") {
    const out = {};
    if (s.freq !== undefined) out["PEQ/Bank/HPF/Frequency"] = Math.round(s.freq * 10);
    if (s.on !== undefined) out["PEQ/Bank/HPF/On"] = s.on ? 1 : 0;
    return out;
  }
  const p = PATH[key], out = {};
  if (!p) return out;
  if (s.freq !== undefined) out[`${p}/Frequency`] = Math.round(s.freq * 10);
  if (s.gain !== undefined) out[`${p}/Gain`] = Math.round(s.gain * 10) * 10;
  if (s.q !== undefined) out[`${p}/Q`] = Math.round(s.q * 1000);
  if (s.type !== undefined) out[`${p}/Type`] = TYPE_RAW[s.type];
  if (s.type === "lpf") out[`${p}/LPFOn`] = 1;  // the high band as an LPF: on (else it would be a dead band)
  return out;
}

// Change a band here (preview) and on the desk.
// The band types a person can pick: the low band bell / shelf (an input channel has its own HPF,
// so not HPF, as in StageMix), the high band bell / shelf / LPF.
export const BAND_TYPES = { L: ["bell", "shelf"], H: ["bell", "shelf", "lpf"] };
const KIND = { bell: "bell", lpf: "lpf", hpf: "hpf" };
export const typeOf = (b) => (b.type === "low_shelf" || b.type === "high_shelf" ? "shelf" : b.type);

let batch = false;   // applyEq: many edits, one step in the history
function edit(key, s) {
  const d = state.eq, eq = d && d.eq;
  if (!eq) return;
  if (!batch && !drag && !pinch) remember();
  lastTap = null;   // a change between two taps: not a double-tap
  s = limited(key, s);
  if (key === "EQ") eq.on = !!s.on;
  else if (key === "HPF") Object.assign(eq.hpf, s);
  else {
    const b = eq.bands.find((x) => x.label === key);
    if (!b) return;
    const kind = s.type === undefined ? undefined
      : KIND[s.type] || (key === "L" ? "low_shelf" : "high_shelf");
    Object.assign(b, s, kind ? { type: kind } : {}, s.type === "lpf" ? { on: true } : {});
  }
  d.curve = eqCurve(eq, d.freqs);
  state.dirty = true;
  send(d.index, rawFor(key, s));
}

// ------------------------------------------------------------------ history, whole-EQ changes

// Undo / redo per input, in this browser: the EQ as it was before each change (a drag is one).
const history = new Map();   // input -> {undo: [eq...], redo: [eq...]}
const UNDO_MAX = 50;
const hist = () => { if (!history.has(state.input)) history.set(state.input, { undo: [], redo: [] }); return history.get(state.input); };
export const eqSnapshot = () => (state.eq && state.eq.eq ? structuredClone(state.eq.eq) : null);
// The EQ just before the first change made here to each input, and when: what "Compare with
// before" goes back to. Kept for the page's life, whatever the undo limit drops; a change made
// on the desk before then is part of it, so comparing never undoes someone else's work.
const start = new Map();   // input -> {eq, at}
export const eqBefore = () => start.get(state.input) || null;
// Every change a person makes (a drag, a typed value, a toolbar button) starts here.
let onUserEdit = () => {};
export function onEqUserEdit(fn) { onUserEdit = fn; }
function remember() {
  const snap = eqSnapshot();
  if (!snap) return;
  onUserEdit();
  if (!start.has(state.input)) start.set(state.input, { eq: structuredClone(snap), at: Date.now() });
  const h = hist();
  h.undo.push(snap);
  if (h.undo.length > UNDO_MAX) h.undo.shift();
  h.redo = [];
  onHistory();
}
let onHistory = () => {};
export function onEqHistory(fn) { onHistory = fn; }
export const canUndo = () => hist().undo.length > 0;
export const canRedo = () => hist().redo.length > 0;
export function undoEq() { step(hist().undo, hist().redo); }
export function redoEq() { step(hist().redo, hist().undo); }
function step(from, to) {
  if (!from.length || !editable()) return;
  to.push(eqSnapshot());
  applyEq(from.pop(), false);
  onHistory();
}

// Set the whole EQ to ``target`` (an eq as the desk reports it): only what differs is written.
// One step in the history unless ``record`` is false (undo / redo keep their own).
export function applyEq(target, record = true) {
  const eq = state.eq && state.eq.eq;
  if (!eq || !target || !editable()) return;
  if (record) remember();
  batch = true;
  try {
    if (target.on !== eq.on) edit("EQ", { on: target.on });
    if (target.hpf && eq.hpf) {
      const s = {};
      if (target.hpf.on !== eq.hpf.on) s.on = target.hpf.on;
      if (target.hpf.freq !== eq.hpf.freq) s.freq = target.hpf.freq;
      if (Object.keys(s).length) edit("HPF", s);
    }
    for (const t of target.bands || []) {
      const b = eq.bands.find((x) => x.label === t.label);
      if (!b || !PATH[t.label]) continue;
      const s = {};
      if (typeOf(t) !== typeOf(b) && (t.label === "L" || t.label === "H")) s.type = typeOf(t);
      for (const k of ["freq", "gain", "q"]) if (t[k] !== undefined && t[k] !== b[k]) s[k] = t[k];
      if (Object.keys(s).length) edit(t.label, s);
    }
  } finally {
    batch = false;
  }
  if (pop) showPop(pop.dataset.key);
}
// A single change from the toolbar (EQ / HPF on, ...): one undoable step.
export function editEq(key, s) { if (editable()) edit(key, s); }

// ------------------------------------------------------------------ pointer: hit, drag, wheel

export const editable = () => state.showEq && state.eq && state.eq.writable && state.eq.index === state.input && state.eq.eq;
function hit(x, y) {
  let best = null, dist = HIT_PX;
  for (const h of eqHandles()) {
    const dd = Math.hypot(h.x - x, h.y - y);
    if (dd <= dist) { best = h; dist = dd; }
  }
  return best;
}
// What a drag of this band may change: frequency (sideways) and gain (up / down), less its locks.
function dragAxes(key, kind) {
  const locks = eqLocks(key), gain = kind !== "hpf" && kind !== "filter";
  return { freq: !locks.freq, gain: gain && !locks.gain };
}
const local = (e) => { const r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
const gainAt = (y) => { const mid = eqY(0), unit = eqY(1) - mid; return clamp((y - mid) / unit, -eqHalf(), eqHalf()); };

let drag = null;   // {key, kind, moved, x0, y0}

// Pinch (two fingers): the Q of the band being dragged or selected when it's a bell, else of the
// bell nearest the fingers; fingers apart widen it (lower Q), together narrow it.
const touches = new Map();   // pointerId -> [x, y]
let pinch = null;            // {key, d0, q0}
const spread = () => { const [a, b] = [...touches.values()]; return Math.hypot(a[0] - b[0], a[1] - b[1]) || 1; };
function startPinch() {
  const bands = state.eq.eq.bands, bell = (k) => bands.find((b) => b.label === k && b.type === "bell");
  const mid = fOfX(([...touches.values()].reduce((s, t) => s + t[0], 0)) / 2);
  const b = bell(drag && drag.key) || bell(state.eqSelected)
    || bands.filter((x) => x.type === "bell").sort((x, y) => Math.abs(Math.log(x.freq / mid)) - Math.abs(Math.log(y.freq / mid)))[0];
  if (!b || eqLocks(b.label).q) return;
  if (!drag || !drag.moved) remember();
  drag = null;
  pinch = { key: b.label, d0: spread(), q0: b.q };
  state.eqSelected = b.label;
  state.eqEditing = true;
  state.dirty = true;
}
function endPinch() {
  pinch = null;
  state.eqEditing = false;
  settle();
}

// Double-tap a handle: its gain back to 0 dB (bells and shelves; not when the gain is locked).
let lastTap = null;   // {key, t}
function doubleTapped(key) {
  const now = performance.now(), twice = lastTap && lastTap.key === key && now - lastTap.t < 350;
  lastTap = twice ? null : { key, t: now };
  const b = twice && state.eq.eq.bands.find((x) => x.label === key);
  if (!b || b.type === "hpf" || b.type === "lpf" || eqLocks(key).gain) return twice;
  edit(key, { gain: 0 });
  return true;
}

canvas.addEventListener("pointerdown", (e) => {
  if (!editable() || (e.pointerType === "mouse" && e.button !== 0)) return;
  touches.set(e.pointerId, local(e));
  if (touches.size === 2) { e.preventDefault(); startPinch(); return; }
  if (touches.size > 2) return;
  const [x, y] = local(e);
  const h = hit(x, y);
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
  if (touches.has(e.pointerId)) touches.set(e.pointerId, [x, y]);
  if (pinch) {
    if (touches.size === 2) {
      edit(pinch.key, { q: Math.round(pinch.q0 * pinch.d0 / spread() * 100) / 100 });
      if (pop) showPop(pinch.key);
    }
    return;
  }
  if (!drag) {
    const h = editable() && hit(x, y);
    const axes = h && dragAxes(h.key, h.kind);
    canvas.style.cursor = !editable() ? ""
      : !h ? "" : axes.freq && axes.gain ? "grab" : axes.freq ? "ew-resize" : axes.gain ? "ns-resize" : "not-allowed";
    return;
  }
  if (!drag.moved && Math.hypot(x - drag.x0, y - drag.y0) < 3) return;
  const axes = dragAxes(drag.key, drag.kind), s = {};
  if (axes.freq) s.freq = Math.round(fOfX(clamp(x, PAD.l, W - PAD.r)) * 10) / 10;  // not off the plot
  if (axes.gain) s.gain = Math.round(gainAt(y) * 10) / 10;
  if (!Object.keys(s).length) return;   // all locked: nothing to drag
  if (!drag.moved) remember();          // the whole drag is one step back
  drag.moved = true;
  canvas.style.cursor = "grabbing";
  edit(drag.key, s);
  if (pop) showPop(drag.key);
});

function endDrag(e) {
  touches.delete(e.pointerId);
  if (pinch) { if (touches.size < 2) endPinch(); return; }
  if (!drag) return;
  const { key, moved } = drag;
  drag = null;
  state.eqEditing = false;
  canvas.style.cursor = "";
  if (moved) settle();  // the desk's echoes of the drag are still on their way
  else if (state.eqPushed && !state.eqSettling) { state.eq = state.eqPushed; state.eqPushed = null; state.dirty = true; }
  if (!moved && doubleTapped(key)) showPop(key);  // the second tap: gain to 0, settings stay open
  else if (!moved) {   // a click: its settings, or closes them when they are already open
    if (pop && pop.dataset.key === key) closePop(); else showPop(key);
  }
}
canvas.addEventListener("pointerup", endDrag);
canvas.addEventListener("pointercancel", endDrag);

canvas.addEventListener("wheel", (e) => {
  if (!editable()) return;
  const [x, y] = local(e), h = hit(x, y);
  const b = h && state.eq.eq.bands.find((x2) => x2.label === h.key);
  if (!b || h.kind !== "bell") return;
  e.preventDefault();
  if (eqLocks(h.key).q) return;
  edit(h.key, { q: Math.round(b.q * (e.deltaY < 0 ? 1.12 : 1 / 1.12) * 100) / 100 });
  if (pop) showPop(h.key);
}, { passive: false });

// ------------------------------------------------------------------ the settings popover

let pop = null;

function closePop() {
  if (pop) pop.remove();
  pop = null;
  if (state.eqSelected) { state.eqSelected = null; state.dirty = true; }
}

const LOCK_SVG = (locked) => `<svg viewBox="0 0 12 14" width="11" height="13" aria-hidden="true">
  <path d="${locked ? "M3 6V4a3 3 0 0 1 6 0v2" : "M3 6V4a3 3 0 0 1 6 0"}" fill="none" stroke="currentColor" stroke-width="1.5"/>
  <rect x="1.5" y="6" width="9" height="7" rx="1.2" fill="${locked ? "currentColor" : "none"}" stroke="currentColor" stroke-width="1.3"/></svg>`;

// How − / + move each setting: frequency and Q by a ratio (a semitone, about 12 %), gain in
// half dB.
const NUDGE = {
  freq: (v, dir) => Math.round(v * 2 ** (dir / 12)),
  gain: (v, dir) => Math.round((v + dir * 0.5) * 10) / 10,
  q: (v, dir) => Math.round(v * 1.12 ** dir * 100) / 100,
};

// A number to type in, between − / + buttons (no keyboard needed on a touch screen); with
// ``lock`` (band key, setting) a lock beside it that keeps drags, the wheel and the field off it.
function field(label, value, step, unit, onSet, lock) {
  const row = el("div", "eqp-row");
  const inp = el("input");
  inp.type = "number"; inp.step = String(step); inp.value = String(value);
  inp.inputMode = "decimal";
  inp.setAttribute("aria-label", label);
  inp.onchange = () => { const v = Number(inp.value); if (Number.isFinite(v)) onSet(v); };
  const locked = lock && !!eqLocks(lock[0])[lock[1]];
  const box = el("div", "eqp-num");
  for (const dir of [-1, 1]) {
    const b = el("button", "eqp-step", dir < 0 ? "−" : "+");
    b.type = "button"; b.tabIndex = -1; b.disabled = locked;
    b.setAttribute("aria-label", `${label} ${dir < 0 ? "down" : "up"}`);
    b.onclick = () => onSet(NUDGE[lock ? lock[1] : "freq"](Number(inp.value), dir));
    if (dir < 0) box.append(b, inp); else box.append(b);
  }
  row.append(el("span", "", label), box, el("span", "eqp-unit", unit));
  if (lock) {
    const [key, name] = lock, locks = eqLocks(key);
    inp.disabled = !!locks[name];
    const btn = el("button", "eqp-lock");
    btn.type = "button";
    btn.innerHTML = LOCK_SVG(!!locks[name]);
    btn.title = locks[name] ? `Unlock ${label.toLowerCase()}` : `Lock ${label.toLowerCase()}`;
    btn.setAttribute("aria-label", `Lock ${label}`);
    btn.setAttribute("aria-pressed", String(!!locks[name]));
    btn.onclick = () => { locks[name] = !locks[name]; showPop(key); state.dirty = true; };
    row.append(btn);
  }
  return row;
}

export function showPop(key) {
  const d = state.eq, eq = d && d.eq;
  if (!eq || !editable()) return;
  const h = eqHandles().find((x) => x.key === key);
  if (!h) return;
  if (pop) pop.remove();
  pop = el("div", "eq-pop");
  pop.dataset.key = key;
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
    pop.append(on, field("Frequency", eq.hpf.freq, 1, "Hz", (v) => { edit("HPF", { freq: v }); showPop("HPF"); }, ["HPF", "freq"]));
  } else {
    const b = eq.bands.find((x) => x.label === key);
    if (BAND_TYPES[key]) {
      // Bell / shelf (and LPF on the high band); a low band set to HPF on the desk can be
      // turned back into a bell or shelf here.
      const names = { bell: "Bell", shelf: "Shelf", lpf: "LPF", hpf: "HPF" };
      const row = el("label", "eqp-row"), sel = el("select");
      sel.setAttribute("aria-label", "Type");
      const now = typeOf(b), kinds = BAND_TYPES[key].includes(now) ? BAND_TYPES[key] : [...BAND_TYPES[key], now];
      for (const k of kinds) sel.append(new Option(names[k], k));
      sel.value = now;
      sel.onchange = () => { edit(key, { type: sel.value }); showPop(key); };
      row.append(el("span", "", "Type"), sel);
      pop.append(row);
    }
    pop.append(field("Frequency", b.freq, 1, "Hz", (v) => { edit(key, { freq: v }); showPop(key); }, [key, "freq"]));
    if (b.type !== "hpf" && b.type !== "lpf") {
      pop.append(field("Gain", b.gain, 0.1, "dB", (v) => { edit(key, { gain: v }); showPop(key); }, [key, "gain"]));
      if (b.type === "bell") pop.append(field("Q", b.q, 0.01, "", (v) => { edit(key, { q: v }); showPop(key); }, [key, "q"]));
    }
  }
  $("spec-pane").append(pop);
  const pane = $("spec-pane").getBoundingClientRect();
  // Beside the handle, never over it (it's clicked again to close this): right, else left.
  const right = h.x + 14, left = h.x - 14 - pop.offsetWidth;
  pop.style.left = `${right + pop.offsetWidth + 4 <= pane.width || left < 4 ? right : left}px`;
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
window.tfEqHandles = () => eqHandles().map((h) => ({ key: h.key, x: h.x, y: h.y, locked: eqLocked(h.key) }));
// ... and how far this page's curve maths is from the desk's own (dB), for the EQ shown.
window.tfEqPreviewError = () => {
  const d = state.eq;
  if (!d || !d.eq || !d.curve.length) return null;
  const mine = eqCurve(d.eq, d.freqs);
  return Math.max(...mine.map((v, i) => Math.abs(v - d.curve[i])));
};
