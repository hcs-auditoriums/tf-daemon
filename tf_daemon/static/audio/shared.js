// What every part of the audio page shares: the spectrum canvas, colours, the
// view state, local-storage helpers.

export const $ = (id) => document.getElementById(id);
export const canvas = $("spectrum");
export const ctx = canvas.getContext("2d");
const css = getComputedStyle(document.documentElement);
export const color = (name) => css.getPropertyValue(name).trim();
export const C = {
  grid: color("--grid"), gridStrong: color("--grid-strong"), muted: color("--muted"),
  avg: color("--avg"), avgFill: color("--avg-fill"), peak: color("--peak"), rta: color("--rta"),
  eq: color("--eq"), eqFill: color("--eq-fill"), vocal: color("--vocal"),
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

export function loadInt(key, def) {
  try { const v = parseInt(localStorage.getItem(key), 10); return Number.isFinite(v) ? v : def; }
  catch { return def; }
}
export function loadStr(key, def, allowed) {
  try { const v = localStorage.getItem(key); return v !== null && allowed.includes(v) ? v : def; }
  catch { return def; }
}
export function save(key, value) { try { localStorage.setItem(key, String(value)); } catch { /* ignore */ } }

export function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
