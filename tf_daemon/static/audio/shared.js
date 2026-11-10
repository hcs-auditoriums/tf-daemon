// What every part of the audio page shares: the spectrum canvas, colours, the
// view state, local-storage helpers.

import { $ } from "../common.js";

export { $, el, save } from "../common.js";
export const canvas = $("spectrum");
export const ctx = canvas.getContext("2d");
const css = getComputedStyle(document.documentElement);
export const color = (name) => css.getPropertyValue(name).trim();
export const C = {
  grid: color("--grid"), gridStrong: color("--grid-strong"), muted: color("--muted"),
  avg: color("--avg"), avgFill: color("--avg-fill"), peak: color("--peak"), rta: color("--rta"),
  panel: color("--panel"), border: color("--border"), text: color("--text"),
  eq: color("--eq"), eqFill: color("--eq-fill"), eqBand: color("--eq-band"), eqBandFill: color("--eq-band-fill"), vocal: color("--vocal"),
};

export const state = {
  meta: null,
  input: loadInt("tf.input", -1),  // device input index being shown
  showAvg: true,
  showPeak: true,
  showLive: true,
  dbMin: -90,   // see "range" below; Auto fits these to the signal
  dbMax: -20,
  avg: null,   // Float32Array for the shown input
  peak: null,
  live: null,  // real-time: this hop, unaveraged
  dirty: true,
  frames: 0,
  fps: 0,
  lastSeq: 0,
  hover: null, // {x, y} in CSS px
};

// A frequency for people: "250 Hz", "2.5 kHz".
export const fmtHz = (f) => (f >= 1000 ? `${+(f / 1000).toFixed(2)} kHz` : `${Math.round(f)} Hz`);

export function loadInt(key, def) {
  try { const v = parseInt(localStorage.getItem(key), 10); return Number.isFinite(v) ? v : def; }
  catch { return def; }
}
export function loadStr(key, def, allowed) {
  try { const v = localStorage.getItem(key); return v !== null && allowed.includes(v) ? v : def; }
  catch { return def; }
}

// Panels that poll (the vocal assist, the ring-out popup) re-render with quietRender: only when
// what they show changed, and never while someone is using them. Replacing the elements under
// a press loses the click (mousedown and mouseup land on different buttons) and closes an open
// dropdown; a render that comes then waits until the press ends or the field loses focus.
const quiet = new WeakMap();

function quietBusy(node, q) {
  const a = document.activeElement;
  return q.down || (a !== null && node.contains(a) && (a.tagName === "SELECT" || a.tagName === "TEXTAREA"
    || (a.tagName === "INPUT" && a.type !== "checkbox")));
}

function quietFlush(node, q) {
  if (!q.pending || quietBusy(node, q)) return;
  const { key, build } = q.pending;
  q.pending = null;
  if (key === q.key) return;
  q.key = key;
  node.replaceChildren(...build());
}

export function quietRender(node, key, build) {
  let q = quiet.get(node);
  if (!q) {
    q = { key: null, pending: null, down: false };
    quiet.set(node, q);
    const qq = q;
    node.addEventListener("pointerdown", () => { qq.down = true; });
    // After the click that ends the press has been handled (it fires right after pointerup).
    const up = () => { if (qq.down) { qq.down = false; setTimeout(() => quietFlush(node, qq), 0); } };
    document.addEventListener("pointerup", up, true);
    document.addEventListener("pointercancel", up, true);
    node.addEventListener("focusout", () => setTimeout(() => quietFlush(node, qq), 0));
  }
  if (key === q.key && !q.pending) return;
  q.pending = { key, build };
  quietFlush(node, q);
}

// Forget what a quietRender'd node shows (something else replaced its content).
export function quietReset(node) {
  const q = quiet.get(node);
  if (q) { q.key = null; q.pending = null; }
}

