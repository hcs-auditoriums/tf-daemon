// tf-daemon RF (tinySA) view. Native ES module, no build step.
// Protocol: see tf_daemon/rf/engine.py (JSON state snapshots over /ws/rf) and module.py.

import { $, connectWs, fitCanvas, loadItem, postJson, save } from "./common.js";
const canvas = $("spectrum");
const ctx = canvas.getContext("2d");
const wfCanvas = $("waterfall");
const wfCtx = wfCanvas.getContext("2d");
const css = getComputedStyle(document.documentElement);
const color = (name) => css.getPropertyValue(name).trim();
const C = {
  grid: color("--grid"), gridStrong: color("--grid-strong"), muted: color("--muted"),
  live: color("--live"), liveFill: color("--live-fill"), peak: color("--peak"), rx: color("--rx"),
};

const WF_ROWS = 200;

const state = {
  snap: null,        // latest server state
  sweep: null,       // {start_hz, stop_hz, points}
  live: null,        // Float32Array
  peak: null,
  lastSweeps: -1,
  showPeak: true,
  showRx: loadBool("rf.showRx", true),
  receivers: [],     // QLX-D receivers: [{key, name, device_id, frequency_mhz, online}]
  showWf: true,
  dbMin: -100,
  dbMax: -10,
  wfRows: [],        // newest first, Float32Array each
  inputsLoaded: false,
  settingsKey: "",   // last server settings shown in the inputs
  dirty: true,
  hover: null,
};

function loadBool(key, def) { const v = loadItem(key, null); return v === null ? def : v === "1"; }
const saveBool = (key, v) => save(key, v ? "1" : "0");

// ------------------------------------------------------------------ QLX-D receivers
// Integration with the Wireless page: /api/qlxd/frequencies lists every known
// receiver (online or not) with the frequency it reported. Absent (404) when
// the QLX-D module is disabled; the controls then stay hidden.

const RX_REFRESH_MS = 10000;

async function fetchReceivers() {
  try {
    const res = await fetch("/api/qlxd/frequencies");
    if (res.status === 404) { $("rx-controls").hidden = true; return; }
    const data = await res.json();
    state.receivers = (data.receivers || []).filter((r) => r.frequency_mhz != null);
    $("rx-controls").hidden = false;
    $("fit-rx").disabled = !state.receivers.length;
    state.dirty = true;
  } catch { /* server unreachable: keep the last list */ }
  setTimeout(fetchReceivers, RX_REFRESH_MS);
}

$("show-rx").checked = state.showRx;
$("show-rx").onchange = (e) => { state.showRx = e.target.checked; saveBool("rf.showRx", state.showRx); state.dirty = true; };
$("fit-rx").onclick = () => {
  const f = state.receivers.map((r) => r.frequency_mhz);
  if (!f.length) return;
  // Same span as the server's auto range (rf/engine.py fit_span): ±2 MHz, whole MHz, ≥ 10 MHz.
  let lo = Math.floor(Math.min(...f) - 2), hi = Math.ceil(Math.max(...f) + 2);
  if (hi - lo < 10) { const mid = (Math.min(...f) + Math.max(...f)) / 2; lo = Math.floor(mid - 5); hi = Math.ceil(mid + 5); }
  $("start").value = Math.max(0, lo);
  $("stop").value = hi;
  if (state.snap && state.snap.scanning) post("/api/rf/scan/start", readInputs());  // apply right away
};

// Receivers inside the current sweep, with their x position.
function visibleReceivers() {
  if (!state.showRx || !state.sweep) return [];
  const { start_hz: f0, stop_hz: f1 } = state.sweep;
  return state.receivers
    .map((r) => ({ ...r, hz: r.frequency_mhz * 1e6 }))
    .filter((r) => r.hz >= f0 && r.hz <= f1)
    .map((r) => ({ ...r, x: Math.round(xOf(r.hz)) + 0.5 }));
}

// ------------------------------------------------------------------ websocket

let wsOpen = false;

function connect() {
  connectWs("/ws/rf", {
    onOpen: () => { wsOpen = true; renderStatus(); },
    onClose: () => { wsOpen = false; renderStatus(); },
    onMessage: (data) => {
      const msg = JSON.parse(data);
      if (msg.type === "state") onState(msg);
    },
  });
}

function onState(s) {
  state.snap = s;
  // Follow the server's settings (auto mode retargets the range as receivers change,
  // other viewers may start sweeps), except while this user is editing them.
  const key = JSON.stringify(s.settings);
  if (!state.inputsLoaded || (key !== state.settingsKey && !editingInputs())) loadInputs(s);
  state.settingsKey = key;
  $("points").max = s.max_points;  // drops to 290 once an original tinySA connects
  if (s.sweep && s.sweeps !== state.lastSweeps) {
    const sw = s.sweep;
    const geomChanged = !state.sweep || state.sweep.start_hz !== sw.start_hz ||
      state.sweep.stop_hz !== sw.stop_hz || state.sweep.points !== sw.points;
    if (geomChanged) state.wfRows = [];
    state.sweep = { start_hz: sw.start_hz, stop_hz: sw.stop_hz, points: sw.points };
    state.live = Float32Array.from(sw.live);
    state.wfRows.unshift(state.live);
    if (state.wfRows.length > WF_ROWS) state.wfRows.length = WF_ROWS;
    state.lastSweeps = s.sweeps;
    if (geomChanged) rebuildWaterfall(); else pushWaterfallRow(state.live);
  }
  if (s.sweep) state.peak = Float32Array.from(s.sweep.peak);  // also refreshes after reset-peak
  renderStatus();
  state.dirty = true;
}

// ------------------------------------------------------------------ controls

const SETTING_INPUTS = ["start", "stop", "points", "rbw", "interval"];
const editingInputs = () => SETTING_INPUTS.includes(document.activeElement && document.activeElement.id);

function loadInputs(s) {
  const rbw = $("rbw");
  if (rbw.options.length !== s.rbw_choices.length) {
    rbw.replaceChildren(...s.rbw_choices.map((v) => new Option(v === "auto" ? "auto" : `${v} kHz`, v)));
  }
  const st = s.settings;
  $("start").value = +(st.start_hz / 1e6).toFixed(6);
  $("stop").value = +(st.stop_hz / 1e6).toFixed(6);
  $("points").value = st.points;
  rbw.value = st.rbw;
  $("interval").value = st.interval_s;
  state.inputsLoaded = true;
}

function readInputs() {
  return {
    start_hz: Math.round(parseFloat($("start").value) * 1e6),
    stop_hz: Math.round(parseFloat($("stop").value) * 1e6),
    points: parseInt($("points").value, 10),
    rbw: $("rbw").value,
    interval_s: parseFloat($("interval").value),
  };
}

function renderStatus() {
  const s = state.snap;
  const el = $("conn");
  const connected = !!(s && s.connected), scanning = !!(s && s.scanning);
  el.classList.toggle("ok", wsOpen && connected);
  if (!wsOpen) el.textContent = "server disconnected";
  else if (!s) el.textContent = "connecting…";
  else if (!connected) el.textContent = s.auto && !s.paused ? `${s.device} · waiting for device…` : `${s.device} · not connected`;
  else el.textContent = `${s.device}${s.input ? ` · ${s.input.toUpperCase()} input` : ""} · ` +
    `${scanning ? "sweeping" : s.paused ? "paused" : "idle"} · ${s.sweeps} sweeps`;
  el.title = s && s.input ? `Antenna must be on the tinySA's ${s.input.toUpperCase()} connector` : !s ? "Connection" : s.auto ? (s.paused ? "Auto mode paused by Stop/Disconnect; press Start or Connect to resume"
    : "Auto mode: connects and sweeps on its own") : "Connection";

  $("connect").textContent = connected ? "Disconnect" : "Connect";
  $("connect").classList.toggle("primary", !connected);
  $("scan").disabled = !connected;
  $("scan").textContent = scanning ? "Stop sweep" : "Start sweep";
  $("scan").classList.toggle("primary", connected && !scanning);
  $("reset-peak").disabled = !state.live;
  if (s) $("error").textContent = s.error ? `⚠ ${s.error}` : "";
  $("info").textContent = s && s.info ? s.info.split(/\r?\n/)[0] : "";
}

async function post(path, body) {
  const data = await postJson(path, body);
  if (!data.ok) $("error").textContent = `⚠ ${data.error}`;
  return data;
}

$("connect").onclick = async (e) => {
  e.target.disabled = true;
  await post(state.snap && state.snap.connected ? "/api/rf/disconnect" : "/api/rf/connect");
  e.target.disabled = false;
};
$("scan").onclick = () => {
  if (state.snap && state.snap.scanning) post("/api/rf/scan/stop");
  else post("/api/rf/scan/start", readInputs());
};
$("reset-peak").onclick = () => post("/api/rf/reset-peak");
$("show-peak").onchange = (e) => { state.showPeak = e.target.checked; state.dirty = true; };
$("show-wf").onchange = (e) => { state.showWf = e.target.checked; $("wf-pane").hidden = !state.showWf; };
$("range").onchange = (e) => {
  [state.dbMin, state.dbMax] = e.target.value.split(",").map(Number);
  rebuildWaterfall();
  state.dirty = true;
};

canvas.addEventListener("pointermove", (e) => {
  const r = canvas.getBoundingClientRect();
  state.hover = { x: e.clientX - r.left, y: e.clientY - r.top };
  state.dirty = true;
});
canvas.addEventListener("pointerleave", () => { state.hover = null; $("readout").style.display = "none"; state.dirty = true; });

// ------------------------------------------------------------------ geometry

const PAD = { l: 48, r: 12, t: 12, b: 26 };
let W = 0, H = 0, WW = 0, WH = 0;

new ResizeObserver(() => { [W, H] = fitCanvas(canvas, ctx); state.dirty = true; }).observe(canvas);
new ResizeObserver(() => { [WW, WH] = fitCanvas(wfCanvas, wfCtx); state.dirty = true; }).observe(wfCanvas);

const plotW = () => W - PAD.l - PAD.r;
const xOf = (f) => PAD.l + (f - state.sweep.start_hz) / (state.sweep.stop_hz - state.sweep.start_hz) * plotW();
const fOfX = (x) => state.sweep.start_hz + (x - PAD.l) / plotW() * (state.sweep.stop_hz - state.sweep.start_hz);
const yOf = (db) => PAD.t + (state.dbMax - db) / (state.dbMax - state.dbMin) * (H - PAD.t - PAD.b);
const dbOfY = (y) => state.dbMax - (y - PAD.t) / (H - PAD.t - PAD.b) * (state.dbMax - state.dbMin);
const freqAt = (i) => state.sweep.start_hz + (state.sweep.stop_hz - state.sweep.start_hz) * i / (state.sweep.points - 1);

function fmtHz(hz, digits) {
  if (hz >= 1e9) return `${(hz / 1e9).toFixed(digits ?? 3)} GHz`;
  if (hz >= 1e6) return `${(hz / 1e6).toFixed(digits ?? 3)} MHz`;
  if (hz >= 1e3) return `${(hz / 1e3).toFixed(digits ?? 1)} kHz`;
  return `${Math.round(hz)} Hz`;
}

// A 1/2/5 x 10^k step giving roughly `target` divisions of the span.
function niceStep(span, target) {
  const raw = span / target, mag = 10 ** Math.floor(Math.log10(raw));
  const n = raw / mag;
  return (n < 1.5 ? 1 : n < 3.5 ? 2 : n < 7.5 ? 5 : 10) * mag;
}

// ------------------------------------------------------------------ spectrum

function drawGrid() {
  const { start_hz: f0, stop_hz: f1 } = state.sweep;
  ctx.font = "11px system-ui, sans-serif";
  ctx.lineWidth = 1;
  ctx.fillStyle = C.muted;
  ctx.strokeStyle = C.gridStrong;
  ctx.beginPath();

  const step = niceStep(f1 - f0, Math.max(2, Math.floor(plotW() / 110)));
  const unit = f1 >= 1e9 ? 1e9 : f1 >= 1e6 ? 1e6 : 1e3;
  const digits = Math.max(0, -Math.floor(Math.log10(step / unit) + 1e-9));
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  for (let f = Math.ceil(f0 / step) * step; f <= f1 + step * 1e-6; f += step) {
    const x = Math.round(xOf(f)) + 0.5;
    ctx.moveTo(x, PAD.t); ctx.lineTo(x, H - PAD.b);
    ctx.fillText(fmtHz(f, digits), x, H - PAD.b + 6);
  }
  ctx.textAlign = "right"; ctx.textBaseline = "middle";
  for (let db = Math.ceil(state.dbMin / 10) * 10; db <= state.dbMax; db += 10) {
    const y = Math.round(yOf(db)) + 0.5;
    ctx.moveTo(PAD.l, y); ctx.lineTo(W - PAD.r, y);
    ctx.fillText(String(db), PAD.l - 6, y);
  }
  ctx.stroke();
}

function tracePath(values) {
  const yBottom = H - PAD.b;
  ctx.beginPath();
  for (let i = 0; i < values.length; i++) {
    const x = xOf(freqAt(i));
    const y = Math.min(yBottom, Math.max(PAD.t, yOf(values[i])));
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
}

function trace(values, stroke, fill) {
  if (fill) {
    tracePath(values);
    ctx.lineTo(xOf(freqAt(values.length - 1)), H - PAD.b);
    ctx.lineTo(xOf(freqAt(0)), H - PAD.b);
    ctx.closePath();
    ctx.fillStyle = fill;
    ctx.fill();
  }
  tracePath(values);
  ctx.strokeStyle = stroke;
  ctx.lineWidth = 1.5;
  ctx.stroke();
}

function drawReceivers() {
  const rx = visibleReceivers();
  if (!rx.length) return;
  ctx.save();
  ctx.lineWidth = 1;
  ctx.font = "600 12px system-ui, sans-serif";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  let lastLabelX = -Infinity;
  for (const r of rx) {
    ctx.globalAlpha = r.online ? 0.9 : 0.4;
    ctx.strokeStyle = C.rx;
    ctx.setLineDash(r.online ? [] : [4, 4]);
    ctx.beginPath();
    ctx.moveTo(r.x, PAD.t); ctx.lineTo(r.x, H - PAD.b);
    ctx.stroke();
    // Vertical label at the top; skip it if it would overlap the previous one.
    if (r.x - lastLabelX >= 16) {
      const text = r.name || r.device_id || r.key;
      ctx.save();
      ctx.translate(r.x + 8, PAD.t + 4);
      ctx.rotate(-Math.PI / 2);
      const w = ctx.measureText(text).width;
      ctx.fillStyle = "rgba(13, 16, 20, 0.85)";  // backing so labels read over the grid and traces
      ctx.fillRect(-w - 4, -8, w + 8, 16);
      ctx.fillStyle = C.rx;
      ctx.fillText(text, 0, 0);
      ctx.restore();
      lastLabelX = r.x;
    }
  }
  ctx.restore();
}

function nearestReceiver(x, maxPx) {
  let best = null;
  for (const r of visibleReceivers()) if (!best || Math.abs(r.x - x) < Math.abs(best.x - x)) best = r;
  return best && Math.abs(best.x - x) <= maxPx ? best : null;
}

function drawHover() {
  const h = state.hover, ro = $("readout");
  if (!h || h.x < PAD.l || h.x > W - PAD.r || h.y < PAD.t || h.y > H - PAD.b) { ro.style.display = "none"; return; }
  ctx.strokeStyle = C.muted;
  ctx.setLineDash([3, 3]);
  ctx.beginPath();
  ctx.moveTo(h.x + 0.5, PAD.t); ctx.lineTo(h.x + 0.5, H - PAD.b);
  ctx.stroke();
  ctx.setLineDash([]);

  const n = state.sweep.points;
  const span = state.sweep.stop_hz - state.sweep.start_hz;
  const i = Math.max(0, Math.min(n - 1, Math.round((fOfX(h.x) - state.sweep.start_hz) / span * (n - 1))));
  ro.textContent = `${fmtHz(freqAt(i))}  live ${state.live[i].toFixed(1)} dBm` +
    (state.peak ? `  peak ${state.peak[i].toFixed(1)} dBm` : "") +
    `  cursor ${dbOfY(h.y).toFixed(1)} dBm`;
  const r = nearestReceiver(h.x, 6);
  if (r) {
    ro.textContent += `  ·  ${r.name || "?"}${r.device_id ? ` (${r.device_id})` : ""} ` +
      `${r.frequency_mhz.toFixed(3)} MHz${r.online ? "" : " · offline"}`;
  }
  ro.style.display = "block";
}

function drawSpectrum() {
  ctx.clearRect(0, 0, W, H);
  if (!state.sweep || !state.live || W < 100 || H < 80) return;
  drawGrid();
  ctx.save();
  ctx.beginPath();
  ctx.rect(PAD.l, PAD.t, plotW(), H - PAD.t - PAD.b);
  ctx.clip();
  if (state.showPeak && state.peak) trace(state.peak, C.peak, null);
  trace(state.live, C.live, C.liveFill);
  drawReceivers();
  ctx.restore();
  drawHover();
}

// ------------------------------------------------------------------ waterfall
// Rows live in an offscreen canvas (one pixel per sweep point, one row per
// sweep, newest at the top) that is stretched over the plot area.

const wfOff = document.createElement("canvas");
const wfOffCtx = wfOff.getContext("2d");

// Cold blue -> hot red, 256 steps.
const LUT = (() => {
  const lut = new Uint8ClampedArray(256 * 3);
  for (let k = 0; k < 256; k++) {
    const t = k / 255, h = (1 - t) * 240, l = 0.2 + t * 0.45;
    const a = Math.min(l, 1 - l);
    const ch = (n) => { const m = (n + h / 30) % 12; return l - a * Math.max(-1, Math.min(m - 3, 9 - m, 1)); };
    lut[k * 3] = ch(0) * 255; lut[k * 3 + 1] = ch(8) * 255; lut[k * 3 + 2] = ch(4) * 255;
  }
  return lut;
})();

function rowImage(values) {
  const img = wfOffCtx.createImageData(values.length, 1), d = img.data;
  const span = state.dbMax - state.dbMin;
  for (let i = 0; i < values.length; i++) {
    const k = Math.max(0, Math.min(255, Math.round((values[i] - state.dbMin) / span * 255))) * 3;
    d[i * 4] = LUT[k]; d[i * 4 + 1] = LUT[k + 1]; d[i * 4 + 2] = LUT[k + 2]; d[i * 4 + 3] = 255;
  }
  return img;
}

function rebuildWaterfall() {
  if (!state.sweep) return;
  wfOff.width = state.sweep.points;
  wfOff.height = WF_ROWS;
  state.wfRows.forEach((row, r) => wfOffCtx.putImageData(rowImage(row), 0, r));
  state.dirty = true;
}

function pushWaterfallRow(values) {
  wfOffCtx.drawImage(wfOff, 0, 1);  // scroll down one row
  wfOffCtx.putImageData(rowImage(values), 0, 0);
}

function drawWaterfall() {
  wfCtx.clearRect(0, 0, WW, WH);
  if (!state.showWf || !state.sweep || !state.wfRows.length || WW < 100) return;
  // Match the spectrum's horizontal plot area; point i sits at the center of its column.
  const colW = plotW() / (state.sweep.points - 1);
  wfCtx.imageSmoothingEnabled = false;
  wfCtx.save();
  wfCtx.beginPath();
  wfCtx.rect(PAD.l, 0, plotW(), WH);
  wfCtx.clip();
  wfCtx.drawImage(wfOff, 0, 0, wfOff.width, WF_ROWS, PAD.l - colW / 2, 0, plotW() + colW, WH);
  wfCtx.strokeStyle = C.rx;
  wfCtx.lineWidth = 1;
  for (const r of visibleReceivers()) {
    wfCtx.globalAlpha = r.online ? 0.55 : 0.25;
    wfCtx.setLineDash(r.online ? [] : [4, 4]);
    wfCtx.beginPath();
    wfCtx.moveTo(r.x, 0); wfCtx.lineTo(r.x, WH);
    wfCtx.stroke();
  }
  wfCtx.restore();
}

// ------------------------------------------------------------------ loop

function loop() {
  if (state.dirty) { state.dirty = false; drawSpectrum(); drawWaterfall(); }
  requestAnimationFrame(loop);
}

renderStatus();
connect();
fetchReceivers();
requestAnimationFrame(loop);
