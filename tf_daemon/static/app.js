// tf-daemon spectrum view. Plain JS, no build step.
// Protocol: see tf_daemon/audio/engine.py (binary "TFS1" frames) and module.py.
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
    channel: loadInt("tf.channel", 0),
    showAvg: true,
    showPeak: true,
    dbMin: -100,
    dbMax: -20,
    avg: null,   // Float32Array per channel
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
    state.meta = meta;
    state.freqs = Float32Array.from(meta.freqs);
    state.logMin = Math.log10(meta.freqs[0]);
    state.logMax = Math.log10(meta.freqs[meta.freqs.length - 1]);
    const n = meta.channels.length, b = meta.freqs.length;
    state.avg = Array.from({ length: n }, () => new Float32Array(b).fill(meta.db_floor));
    state.peak = Array.from({ length: n }, () => new Float32Array(b).fill(meta.db_floor));
    if (state.channel >= n) state.channel = 0;
    buildChannelButtons();
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
    if (magic !== "TFS1") return;
    state.lastSeq = dv.getUint32(4, true);
    const nCh = dv.getUint16(8, true), nBins = dv.getUint16(10, true);
    if (nCh !== state.avg.length || nBins !== state.freqs.length) return;
    const data = new Int16Array(buf, 12, nCh * 2 * nBins);
    for (let c = 0; c < nCh; c++) {
      const a = state.avg[c], p = state.peak[c];
      const base = c * 2 * nBins;
      for (let i = 0; i < nBins; i++) {
        a[i] = data[base + i] / 100;
        p[i] = data[base + nBins + i] / 100;
      }
    }
    state.frames++;
    state.dirty = true;
  }

  // ------------------------------------------------------------------ controls

  function buildChannelButtons() {
    const nav = $("channels");
    nav.replaceChildren();
    state.meta.channels.forEach((ch, i) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = ch.label;
      b.title = `Input ${ch.index + 1}`;
      b.setAttribute("aria-pressed", String(i === state.channel));
      b.onclick = () => {
        state.channel = i;
        save("tf.channel", i);
        [...nav.children].forEach((el, j) => el.setAttribute("aria-pressed", String(j === i)));
        state.dirty = true;
      };
      nav.appendChild(b);
    });
  }

  $("show-avg").onchange = (e) => { state.showAvg = e.target.checked; state.dirty = true; };
  $("show-peak").onchange = (e) => { state.showPeak = e.target.checked; state.dirty = true; };
  $("range").onchange = (e) => {
    [state.dbMin, state.dbMax] = e.target.value.split(",").map(Number);
    state.dirty = true;
  };
  $("reset-peak").onclick = () => send({ cmd: "reset_peak", channel: state.channel });

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
    const a = state.avg[state.channel][i], p = state.peak[state.channel][i];
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
    const ch = state.channel;
    if (state.showPeak) trace(state.peak[ch], C.peak, null);
    if (state.showAvg) trace(state.avg[ch], C.avg, C.avgFill);
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
  requestAnimationFrame(loop);
})();
