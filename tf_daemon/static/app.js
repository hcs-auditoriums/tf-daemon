// tf-daemon spectrum view. Plain JS, no build step.
// Protocol: see tf_daemon/audio/engine.py (binary "TFS2" frames) and module.py.
// The server analyzes an input only while some client views it, so we tell it
// which one we show ({"cmd": "view"}) and pick that input's record out of each frame.
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const canvas = $("spectrum");
  const ctx = canvas.getContext("2d");
  const css = getComputedStyle(document.documentElement);
  const color = (name) => css.getPropertyValue(name).trim();
  const C = {
    grid: color("--grid"), gridStrong: color("--grid-strong"), muted: color("--muted"),
    avg: color("--avg"), avgFill: color("--avg-fill"), peak: color("--peak"),
  };

  const state = {
    meta: null,
    input: loadInt("tf.input", -1),  // device input index being shown
    showAvg: true,
    showPeak: true,
    dbMin: -100,
    dbMax: -20,
    avg: null,   // Float32Array for the shown input
    peak: null,
    dirty: true,
    frames: 0,
    fps: 0,
    lastSeq: 0,
    hover: null, // {x, y} in CSS px
  };

  function loadInt(key, def) {
    try { const v = parseInt(localStorage.getItem(key), 10); return Number.isFinite(v) ? v : def; }
    catch { return def; }
  }
  function save(key, value) { try { localStorage.setItem(key, String(value)); } catch { /* ignore */ } }

  // ------------------------------------------------------------------ websocket

  let ws = null;
  let retryMs = 500;

  function connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}/ws/audio`);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => { retryMs = 500; setConn(true); };
    ws.onclose = () => {
      setConn(false);
      setTimeout(connect, retryMs);
      retryMs = Math.min(retryMs * 2, 5000);
    };
    ws.onerror = () => ws.close();
    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") onMeta(JSON.parse(ev.data));
      else onFrame(ev.data);
    };
  }

  function send(obj) { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj)); }

  function setConn(ok) {
    const el = $("conn");
    el.classList.toggle("ok", ok);
    el.textContent = ok ? "live" : "disconnected";
  }

  function onMeta(meta) {
    if (meta.type !== "meta") return;
    // A later meta on the same connection only renames inputs (names read from
    // the console): relabel without dropping the trace or re-sending "view".
    const relabel = state.meta && ws && state.metaWs === ws && meta.freqs.length === state.freqs.length;
    state.meta = meta;
    state.metaWs = ws;
    if (relabel) {
      buildChannelPicker();
      markSelected(state.input);
      return;
    }
    state.freqs = Float32Array.from(meta.freqs);
    state.logMin = Math.log10(meta.freqs[0]);
    state.logMax = Math.log10(meta.freqs[meta.freqs.length - 1]);
    const b = meta.freqs.length;
    state.avg = new Float32Array(b);
    state.peak = new Float32Array(b);
    if (!meta.channels.some((c) => c.index === state.input)) {
      state.input = (meta.channels.find((c) => c.pinned) || meta.channels[0]).index;
    }
    buildChannelPicker();
    selectInput(state.input);
    $("info").textContent =
      `${meta.source} · ${meta.samplerate} Hz · FFT ${meta.fft_size} ` +
      `(${(meta.samplerate / meta.fft_size).toFixed(2)} Hz/bin) · ` +
      `${meta.update_rate.toFixed(1)} upd/s · avg τ ${meta.average_tau} s`;
    state.dirty = true;
  }

  function onFrame(buf) {
    if (!state.meta) return;
    const dv = new DataView(buf);
    const magic = String.fromCharCode(dv.getUint8(0), dv.getUint8(1), dv.getUint8(2), dv.getUint8(3));
    if (magic !== "TFS2") return;
    state.lastSeq = dv.getUint32(4, true);
    const nRec = dv.getUint16(8, true), nBins = dv.getUint16(10, true);
    if (nBins !== state.freqs.length) return;
    const recSize = 4 + 4 * nBins;
    for (let r = 0; r < nRec; r++) {
      const off = 12 + r * recSize;
      if (dv.getUint16(off, true) !== state.input) continue;
      const data = new Int16Array(buf, off + 4, 2 * nBins);
      for (let i = 0; i < nBins; i++) {
        state.avg[i] = data[i] / 100;
        state.peak[i] = data[nBins + i] / 100;
      }
      state.frames++;
      state.dirty = true;
      return;
    }
  }

  // ------------------------------------------------------------------ controls

  // Buttons for the inputs named in config.toml, plus a menu with every input.
  function buildChannelPicker() {
    const nav = $("channels");
    nav.replaceChildren();
    for (const ch of state.meta.channels.filter((c) => c.pinned)) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = ch.label;
      b.title = `Input ${ch.index + 1}`;
      b.dataset.input = ch.index;
      b.onclick = () => selectInput(ch.index);
      nav.appendChild(b);
    }
    const sel = document.createElement("select");
    sel.id = "input";
    sel.setAttribute("aria-label", "Input");
    for (const ch of state.meta.channels) {
      const o = document.createElement("option");
      o.value = ch.index;
      const n = `In ${ch.index + 1}`;
      o.textContent = ch.label === `Ch ${ch.index + 1}` ? n : `${n} · ${ch.label}`;
      sel.appendChild(o);
    }
    sel.onchange = () => selectInput(Number(sel.value));
    nav.appendChild(sel);
  }

  function markSelected(index) {
    for (const b of $("channels").querySelectorAll("button")) {
      b.setAttribute("aria-pressed", String(Number(b.dataset.input) === index));
    }
    $("input").value = index;
  }

  function selectInput(index) {
    state.input = index;
    save("tf.input", index);
    state.avg.fill(state.meta.db_floor);
    state.peak.fill(state.meta.db_floor);
    markSelected(index);
    send({ cmd: "view", channel: index });
    renderStrip();
    state.dirty = true;
  }

  // ------------------------------------------------------------------ console strip

  // Settings of the TF5 channel feeding the shown input (GET /api/tf5/channels,
  // only present when tf5.host is configured).
  let consoleChannels = null;   // null = no TF5 module; [] = none yet

  async function pollConsole() {
    try {
      const r = await fetch("/api/tf5/channels");
      if (r.status === 404) { consoleChannels = null; renderStrip(); return; }  // not configured
      const data = await r.json();
      consoleChannels = data.connected ? data.channels : [];
    } catch {
      consoleChannels = [];
    }
    renderStrip();
    setTimeout(pollConsole, 2000);
  }

  const PARAM_LABELS = {
    "Fader/Level": "Fader", "Fader/On": "On", "ToStereo/Pan": "Pan", "Out/Balance": "Balance",
  };
  const SKIP = new Set(["Label/Name", "Label/Color"]);

  function fmtParam(path, p) {
    const v = p.value;
    if (path.endsWith("/On")) return v ? "on" : "off";
    if (path === "ToStereo/Pan" || path === "Out/Balance") {
      return v === 0 ? "C" : v < 0 ? `L${-v}` : `R${v}`;
    }
    if (v === null) return "−∞ dB";
    if (typeof v === "number") return `${+v.toFixed(2)}${p.unit ? " " + p.unit : ""}`;
    return String(v);
  }

  function renderStrip() {
    const el = $("console-strip");
    if (consoleChannels === null) { el.hidden = true; return; }
    el.hidden = false;
    const ch = consoleChannels.find((c) => c.index === state.input);
    if (!ch) {
      el.replaceChildren(Object.assign(document.createElement("span"), {
        className: "muted", textContent: consoleChannels.length ? "not on the console patch" : "TF5 not connected",
      }));
      return;
    }
    const items = [];
    const name = ch.params["Label/Name"];
    const head = document.createElement("strong");
    head.textContent = `${ch.source}${name ? " · " + name.value : ""}`;
    const color = ch.params["Label/Color"];
    if (color && color.value) head.style.borderLeftColor = String(color.value).toLowerCase();
    items.push(head);
    for (const [path, p] of Object.entries(ch.params)) {
      if (SKIP.has(path)) continue;
      const item = document.createElement("span");
      item.className = path === "Fader/On" && !p.value ? "off" : "";
      item.textContent = `${PARAM_LABELS[path] || path} ${fmtParam(path, p)}`;
      items.push(item);
    }
    el.replaceChildren(...items);
  }

  $("show-avg").onchange = (e) => { state.showAvg = e.target.checked; state.dirty = true; };
  $("show-peak").onchange = (e) => { state.showPeak = e.target.checked; state.dirty = true; };
  $("range").onchange = (e) => {
    [state.dbMin, state.dbMax] = e.target.value.split(",").map(Number);
    state.dirty = true;
  };
  $("reset-peak").onclick = () => send({ cmd: "reset_peak", channel: state.input });

  canvas.addEventListener("pointermove", (e) => {
    const r = canvas.getBoundingClientRect();
    state.hover = { x: e.clientX - r.left, y: e.clientY - r.top };
    state.dirty = true;
  });
  canvas.addEventListener("pointerleave", () => { state.hover = null; $("readout").style.display = "none"; state.dirty = true; });

  // ------------------------------------------------------------------ drawing

  const PAD = { l: 44, r: 12, t: 12, b: 26 };
  let W = 0, H = 0;

  function resize() {
    const dpr = window.devicePixelRatio || 1;
    const r = canvas.getBoundingClientRect();
    W = r.width; H = r.height;
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    state.dirty = true;
  }
  new ResizeObserver(resize).observe(canvas);

  const xOf = (f) => PAD.l + (Math.log10(f) - state.logMin) / (state.logMax - state.logMin) * (W - PAD.l - PAD.r);
  const fOfX = (x) => 10 ** (state.logMin + (x - PAD.l) / (W - PAD.l - PAD.r) * (state.logMax - state.logMin));
  const yOf = (db) => PAD.t + (state.dbMax - db) / (state.dbMax - state.dbMin) * (H - PAD.t - PAD.b);
  const dbOfY = (y) => state.dbMax - (y - PAD.t) / (H - PAD.t - PAD.b) * (state.dbMax - state.dbMin);

  const F_MAJOR = [50, 100, 200, 500, 1000, 2000, 5000, 10000];
  const F_MINOR = [60, 70, 80, 90, 300, 400, 600, 700, 800, 900, 3000, 4000, 6000, 7000, 8000, 9000];
  const fmtF = (f) => (f >= 1000 ? `${+(f / 1000).toFixed(f % 1000 ? 1 : 0)}k` : `${Math.round(f)}`);

  function drawGrid() {
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

  function trace(values, stroke, fill) {
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
    ctx.lineWidth = 1.5;
    ctx.stroke();
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

    const f = fOfX(h.x);
    let i = 0, best = Infinity;  // nearest display bin
    for (let k = 0; k < state.freqs.length; k++) {
      const d = Math.abs(state.freqs[k] - f);
      if (d < best) { best = d; i = k; }
    }
    const fBin = state.freqs[i];
    const a = state.avg[i], p = state.peak[i];
    ro.textContent = `${fBin >= 1000 ? (fBin / 1000).toFixed(2) + " kHz" : fBin.toFixed(1) + " Hz"}` +
      `  avg ${a.toFixed(1)} dB  peak ${p.toFixed(1)} dB  cursor ${dbOfY(h.y).toFixed(1)} dB`;
    ro.style.display = "block";
  }

  function draw() {
    ctx.clearRect(0, 0, W, H);
    if (!state.meta || W < 100 || H < 80) return;
    drawGrid();
    ctx.save();
    ctx.beginPath();
    ctx.rect(PAD.l, PAD.t, W - PAD.l - PAD.r, H - PAD.t - PAD.b);
    ctx.clip();
    if (state.showPeak) trace(state.peak, C.peak, null);
    if (state.showAvg) trace(state.avg, C.avg, C.avgFill);
    ctx.restore();
    drawHover();
  }

  function loop() {
    if (state.dirty) { state.dirty = false; draw(); }
    requestAnimationFrame(loop);
  }

  setInterval(() => {
    state.fps = state.frames; state.frames = 0;
    if (ws && ws.readyState === WebSocket.OPEN) $("conn").textContent = `live · ${state.fps} fps`;
  }, 1000);

  resize();
  connect();
  pollConsole();
  requestAnimationFrame(loop);
})();
