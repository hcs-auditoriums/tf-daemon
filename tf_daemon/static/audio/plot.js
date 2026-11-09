// The spectrum plot: geometry (frequency / level to pixels), grid, traces, hover readout.

import { eqAt } from "./eq.js";
import { $, C, canvas, ctx, state } from "./shared.js";
import { wf } from "./waterfall.js";

canvas.addEventListener("pointermove", (e) => {
  const r = canvas.getBoundingClientRect();
  state.hover = { x: e.clientX - r.left, y: e.clientY - r.top };
  state.dirty = true;
});
canvas.addEventListener("pointerleave", () => { state.hover = null; $("readout").style.display = "none"; state.dirty = true; });

// ------------------------------------------------------------------ drawing

export const PAD = { l: 44, r: 12, t: 12, b: 26 };
export let W = 0, H = 0;

export function resize() {
  const dpr = window.devicePixelRatio || 1;
  const r = canvas.getBoundingClientRect();
  W = r.width; H = r.height;
  canvas.width = Math.round(W * dpr);
  canvas.height = Math.round(H * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  state.dirty = true;
}
new ResizeObserver(resize).observe(canvas);

export const xOf = (f) => PAD.l + (Math.log10(f) - state.logMin) / (state.logMax - state.logMin) * (W - PAD.l - PAD.r);
export const fOfX = (x) => 10 ** (state.logMin + (x - PAD.l) / (W - PAD.l - PAD.r) * (state.logMax - state.logMin));
const yOf = (db) => PAD.t + (state.dbMax - db) / (state.dbMax - state.dbMin) * (H - PAD.t - PAD.b);
const dbOfY = (y) => state.dbMax - (y - PAD.t) / (H - PAD.t - PAD.b) * (state.dbMax - state.dbMin);

export const F_MAJOR = [50, 100, 200, 500, 1000, 2000, 5000, 10000];
const F_MINOR = [60, 70, 80, 90, 300, 400, 600, 700, 800, 900, 3000, 4000, 6000, 7000, 8000, 9000];
export const fmtF = (f) => (f >= 1000 ? `${+(f / 1000).toFixed(f % 1000 ? 1 : 0)}k` : `${Math.round(f)}`);

export function drawGrid() {
  ctx.font = "11px system-ui, sans-serif";
  ctx.lineWidth = 1;
  ctx.fillStyle = C.muted;

  ctx.strokeStyle = C.grid;
  ctx.beginPath();
  for (const f of F_MINOR) { const x = Math.round(xOf(f)) + 0.5; ctx.moveTo(x, PAD.t); ctx.lineTo(x, H - PAD.b); }
  ctx.stroke();

  ctx.strokeStyle = C.gridStrong;
  ctx.beginPath();
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  for (const f of F_MAJOR) {
    const x = Math.round(xOf(f)) + 0.5;
    ctx.moveTo(x, PAD.t); ctx.lineTo(x, H - PAD.b);
    ctx.fillText(fmtF(f), x, H - PAD.b + 6);
  }
  ctx.textAlign = "right"; ctx.textBaseline = "middle";
  for (let db = Math.ceil(state.dbMin / 10) * 10; db <= state.dbMax; db += 10) {
    const y = Math.round(yOf(db)) + 0.5;
    ctx.moveTo(PAD.l, y); ctx.lineTo(W - PAD.r, y);
    ctx.fillText(String(db), PAD.l - 6, y);
  }
  ctx.stroke();
}

export function trace(values, stroke, fill, width = 1.5) {
  const freqs = state.freqs, n = freqs.length;
  const yBottom = H - PAD.b;
  ctx.beginPath();
  for (let i = 0; i < n; i++) {
    const x = xOf(freqs[i]);
    const y = Math.min(yBottom, Math.max(PAD.t, yOf(values[i])));
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  if (fill) {
    ctx.save();
    ctx.lineTo(xOf(freqs[n - 1]), yBottom);
    ctx.lineTo(xOf(freqs[0]), yBottom);
    ctx.closePath();
    ctx.fillStyle = fill;
    ctx.fill();
    ctx.restore();
    // Re-stroke without the closing edges.
    ctx.beginPath();
    for (let i = 0; i < n; i++) {
      const x = xOf(freqs[i]);
      const y = Math.min(yBottom, Math.max(PAD.t, yOf(values[i])));
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
  }
  ctx.strokeStyle = stroke;
  ctx.lineWidth = width;
  ctx.stroke();
}

export function drawHover() {
  const h = state.hover, ro = $("readout");
  if (!h || h.x < PAD.l || h.x > W - PAD.r || h.y < PAD.t || h.y > H - PAD.b) {
    if (!wf.hover) ro.style.display = "none";
    return;
  }
  ctx.strokeStyle = C.muted;
  ctx.setLineDash([3, 3]);
  ctx.beginPath();
  ctx.moveTo(h.x + 0.5, PAD.t); ctx.lineTo(h.x + 0.5, H - PAD.b);
  ctx.stroke();
  ctx.setLineDash([]);

  const f = fOfX(h.x);
  let i = 0, best = Infinity;  // nearest display bin
  for (let k = 0; k < state.freqs.length; k++) {
    const d = Math.abs(state.freqs[k] - f);
    if (d < best) { best = d; i = k; }
  }
  const fBin = state.freqs[i];
  const a = state.avg[i], p = state.peak[i], l = state.live[i];
  ro.textContent = `${fBin >= 1000 ? (fBin / 1000).toFixed(2) + " kHz" : fBin.toFixed(1) + " Hz"}` +
    `  now ${l.toFixed(1)} dB  avg ${a.toFixed(1)} dB  peak ${p.toFixed(1)} dB  cursor ${dbOfY(h.y).toFixed(1)} dB`;
  const eqDb = state.showEq && state.eq && state.eq.eq ? eqAt(f) : null;
  if (eqDb !== null) ro.textContent += `  EQ ${eqDb > 0 ? "+" : ""}${eqDb.toFixed(1)} dB`;
  ro.style.display = "block";
}
