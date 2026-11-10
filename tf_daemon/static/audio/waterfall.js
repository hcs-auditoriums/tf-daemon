// The waterfall, and the Spectrum / Both / Waterfall layout.

import { fitCanvas } from "../common.js";
import { F_MAJOR, fmtF, PAD } from "./plot.js";
import { SEV, severity } from "./rings.js";
import { $, C, loadInt, loadStr, save, state } from "./shared.js";

// Each frame of the shown input becomes one row (newest at the top). Rows live
// in an off-screen canvas used as a circular buffer, one pixel per display bin,
// so a frame costs one 400-pixel row; the levels are kept too, to recolour the
// history when the range changes and for the hover readout.
const LAYOUTS = ["spectrum", "both", "waterfall"];
state.layout = loadStr("tf.layout", "spectrum", LAYOUTS);
state.wfSpan = loadInt("tf.wfSpan", 30);
state.wfSource = loadStr("tf.wfSource", "live", ["live", "avg"]);

const wfCanvas = $("waterfall");
const wfCtx = wfCanvas.getContext("2d");
export const wf = {
  W: 0, H: 0, rows: 0, bins: 0, head: 0, filled: 0,
  data: null, buf: document.createElement("canvas"), img: null, dirty: true, hover: null,
};
const wfBufCtx = wf.buf.getContext("2d");

// Colour map (inferno-like: black, purple, red, orange, pale yellow) as a 256-entry RGB table.
const LUT = (() => {
  const stops = [[0, 0, 4], [40, 11, 84], [101, 21, 110], [159, 42, 99], [212, 72, 66],
                 [245, 125, 21], [250, 193, 39], [252, 255, 164]];
  const lut = new Uint8ClampedArray(256 * 3);
  for (let i = 0; i < 256; i++) {
    const x = i / 255 * (stops.length - 1), k = Math.min(Math.floor(x), stops.length - 2), t = x - k;
    for (let c = 0; c < 3; c++) lut[i * 3 + c] = stops[k][c] + (stops[k + 1][c] - stops[k][c]) * t;
  }
  return lut;
})();

export function wfReset() {
  if (!state.meta) return;
  wf.bins = state.freqs.length;
  wf.rows = Math.max(10, Math.round(state.wfSpan * state.meta.update_rate));
  wf.data = new Float32Array(wf.rows * wf.bins).fill(-1e9);
  wf.buf.width = wf.bins; wf.buf.height = wf.rows;
  wfBufCtx.fillStyle = `rgb(${LUT[0]},${LUT[1]},${LUT[2]})`;
  wfBufCtx.fillRect(0, 0, wf.bins, wf.rows);
  wf.img = wfBufCtx.createImageData(wf.bins, 1);
  wf.head = 0; wf.filled = 0; wf.dirty = true;
}

function wfColorRow(row) {
  const d = wf.img.data, base = row * wf.bins, span = state.dbMax - state.dbMin;
  for (let i = 0; i < wf.bins; i++) {
    const v = wf.data[base + i];
    const k = (Math.max(0, Math.min(255, Math.round((v - state.dbMin) / span * 255)))) * 3;
    d[i * 4] = LUT[k]; d[i * 4 + 1] = LUT[k + 1]; d[i * 4 + 2] = LUT[k + 2]; d[i * 4 + 3] = 255;
  }
  wfBufCtx.putImageData(wf.img, 0, row);
}

// Rows are written going *up* the buffer (head decrements), so drawing from
// head down and wrapping shows newest first.
export function wfPush(values) {
  if (!wf.data || state.layout === "spectrum") return;
  wf.head = (wf.head - 1 + wf.rows) % wf.rows;
  wf.data.set(values, wf.head * wf.bins);
  wfColorRow(wf.head);
  wf.filled = Math.min(wf.filled + 1, wf.rows);
  wf.dirty = true;
}

export function wfRecolor() {
  if (!wf.data) return;
  for (let r = 0; r < wf.rows; r++) wfColorRow(r);
  wf.dirty = true;
}

function wfResize() {
  [wf.W, wf.H] = fitCanvas(wfCanvas, wfCtx);
  wf.dirty = true;
}
new ResizeObserver(wfResize).observe(wfCanvas);

// Same horizontal mapping as the spectrum, so columns line up; frequency
// labels only when the spectrum (which has them) is hidden.
const wfPad = () => ({ l: PAD.l, r: PAD.r, t: state.layout === "both" ? 4 : PAD.t, b: state.layout === "waterfall" ? PAD.b : 8 });
const wfXOf = (f) => { const p = wfPad(); return p.l + (Math.log10(f) - state.logMin) / (state.logMax - state.logMin) * (wf.W - p.l - p.r); };
const wfFOfX = (x) => { const p = wfPad(); return 10 ** (state.logMin + (x - p.l) / (wf.W - p.l - p.r) * (state.logMax - state.logMin)); };

export function drawWaterfall() {
  const c = wfCtx, p = wfPad();
  c.clearRect(0, 0, wf.W, wf.H);
  if (!state.meta || !wf.data || wf.W < 100 || wf.H < 40) return;
  const pw = wf.W - p.l - p.r, ph = wf.H - p.t - p.b;
  // Bin centres sit at the plot edges (as on the spectrum): stretch by half a bin each side.
  const colW = pw / (wf.bins - 1), x0 = p.l - colW / 2, w = colW * wf.bins;
  const rowH = ph / wf.rows, first = wf.rows - wf.head;  // rows head..end, then 0..head
  c.save();
  c.beginPath(); c.rect(p.l, p.t, pw, ph); c.clip();
  c.imageSmoothingEnabled = false;
  c.drawImage(wf.buf, 0, wf.head, wf.bins, first, x0, p.t, w, first * rowH);
  if (wf.head) c.drawImage(wf.buf, 0, 0, wf.bins, wf.head, x0, p.t + first * rowH, w, wf.head * rowH);
  // Live rings on this input, carried down the waterfall.
  if (state.showRings) {
    c.setLineDash([2, 4]); c.lineWidth = 1;
    for (const e of state.rings) {
      if (e.channel !== state.input || !e.active) continue;
      const x = Math.round(wfXOf(e.freq)) + 0.5;
      c.strokeStyle = SEV[severity(e)].color;
      c.beginPath(); c.moveTo(x, p.t); c.lineTo(x, p.t + ph); c.stroke();
    }
    c.setLineDash([]);
  }
  if (wf.hover) {
    c.strokeStyle = "rgba(255,255,255,0.6)"; c.setLineDash([3, 3]);
    c.beginPath(); c.moveTo(wf.hover.x + 0.5, p.t); c.lineTo(wf.hover.x + 0.5, p.t + ph);
    c.moveTo(p.l, wf.hover.y + 0.5); c.lineTo(p.l + pw, wf.hover.y + 0.5); c.stroke(); c.setLineDash([]);
  }
  c.restore();

  // Time scale on the left: 0 s at the top.
  c.font = "11px system-ui, sans-serif"; c.fillStyle = C.muted;
  c.textAlign = "right"; c.textBaseline = "middle";
  const step = [1, 2, 5, 10, 15, 30, 60, 120].find((s) => state.wfSpan / s <= Math.max(2, ph / 40)) || 60;
  for (let t = 0; t <= state.wfSpan + 1e-9; t += step) {
    const y = p.t + t / state.wfSpan * ph;
    c.fillText(t === 0 ? "now" : t >= 60 ? `-${t / 60}m` : `-${t}s`, p.l - 6, Math.min(Math.max(y, p.t + 6), p.t + ph - 6));
  }
  if (state.layout === "waterfall") {
    c.textAlign = "center"; c.textBaseline = "top";
    for (const f of F_MAJOR) c.fillText(fmtF(f), wfXOf(f), p.t + ph + 6);
  }
}

export function wfHoverReadout() {
  const ro = $("readout"), h = wf.hover, p = wfPad();
  const ph = wf.H - p.t - p.b;
  if (!h || !wf.data || h.x < p.l || h.x > wf.W - p.r || h.y < p.t || h.y > p.t + ph) {
    if (!state.hover) ro.style.display = "none";
    return;
  }
  const f = wfFOfX(h.x);
  let i = 0, best = Infinity;
  for (let k = 0; k < state.freqs.length; k++) {
    const d = Math.abs(state.freqs[k] - f);
    if (d < best) { best = d; i = k; }
  }
  const back = Math.min(wf.rows - 1, Math.floor((h.y - p.t) / ph * wf.rows));
  const v = wf.data[((wf.head + back) % wf.rows) * wf.bins + i];
  const ago = back / state.meta.update_rate, fBin = state.freqs[i];
  ro.textContent = `${fBin >= 1000 ? (fBin / 1000).toFixed(2) + " kHz" : fBin.toFixed(1) + " Hz"}` +
    `  ${ago < 0.05 ? "now" : `${ago.toFixed(1)} s ago`}  ` + (v > -1e8 ? `${v.toFixed(1)} dB` : "no data");
  ro.style.display = "block";
}

wfCanvas.addEventListener("pointermove", (e) => {
  const r = wfCanvas.getBoundingClientRect();
  wf.hover = { x: e.clientX - r.left, y: e.clientY - r.top };
  wf.dirty = true;
});
wfCanvas.addEventListener("pointerleave", () => { wf.hover = null; if (!state.hover) $("readout").style.display = "none"; wf.dirty = true; });

function applyLayout() {
  $("spec-pane").hidden = state.layout === "waterfall";
  $("wf-pane").hidden = state.layout === "spectrum";
  for (const r of document.querySelectorAll('input[name="layout"]')) r.checked = r.value === state.layout;
  state.dirty = true; wf.dirty = true;
}
for (const r of document.querySelectorAll('input[name="layout"]')) {
  r.onchange = () => {
    const was = state.layout;
    state.layout = r.value;
    save("tf.layout", state.layout);
    if (was === "spectrum") wfReset();  // it wasn't recording while hidden
    applyLayout();
  };
}
$("wf-span").value = String(state.wfSpan);
if ($("wf-span").value !== String(state.wfSpan)) { state.wfSpan = 30; $("wf-span").value = "30"; }
$("wf-span").onchange = (e) => { state.wfSpan = Number(e.target.value); save("tf.wfSpan", state.wfSpan); wfReset(); };
$("wf-source").value = state.wfSource;
$("wf-source").onchange = (e) => { state.wfSource = e.target.value; save("tf.wfSource", state.wfSource); wfReset(); };
applyLayout();
