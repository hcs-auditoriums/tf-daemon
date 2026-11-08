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
    renderPanel();
    state.dirty = true;
  }

  // ------------------------------------------------------------------ console panel

  // Sidebar with the TF5 channel feeding the shown input: name, on, fader,
  // pan and its sends (GET /api/tf5/channels; only when tf5.host is set).
  let tf5 = null;   // null = no TF5 module; {connected, channels, mixes} otherwise
  state.showConsole = loadInt("tf.console", 1) === 1;
  state.allSends = loadInt("tf.allSends", 0) === 1;

  async function pollConsole() {
    try {
      const r = await fetch("/api/tf5/channels");
      if (r.status === 404) { tf5 = null; renderPanel(); return; }  // TF5 not configured: stop polling
      tf5 = await r.json();
    } catch {
      if (tf5) tf5.connected = false;
    }
    renderPanel();
    setTimeout(pollConsole, 1000);
  }

  const TF_COLORS = {
    blue: "#3f7fff", skyblue: "#4fc3f7", green: "#4cd07d", yellow: "#f2d14e", orange: "#ff9f43",
    red: "#ff5d5d", pink: "#ff7ac6", purple: "#a77bff", white: "#e6e6e6",
  };

  // Fader position 0..1 for a level in dB (null = -inf), roughly following a
  // console fader's scale so the cap sits where it does on the desk.
  const LAW = [[10, 1], [5, 0.92], [0, 0.8], [-5, 0.69], [-10, 0.58], [-20, 0.43], [-30, 0.31],
               [-40, 0.21], [-50, 0.13], [-60, 0.07], [-138, 0]];
  function faderPos(db) {
    if (db === null || db === undefined) return 0;
    if (db >= LAW[0][0]) return 1;
    for (let i = 1; i < LAW.length; i++) {
      const [d1, p1] = LAW[i - 1], [d0, p0] = LAW[i];
      if (db >= d0) return p0 + (db - d0) / (d1 - d0) * (p1 - p0);
    }
    return 0;
  }
  const fmtDb = (db) => (db === null || db === undefined ? "−∞" : `${db > 0 ? "+" : ""}${db.toFixed(1)}`);
  const fmtPan = (v) => (v === 0 ? "C" : v < 0 ? `L${-v}` : `R${v}`);

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  // Sends to the mixes (consecutive mixes with the same name are a stereo pair
  // and shown once), the two FX and mono.
  function sendRows(p, mixes) {
    const rows = [];
    const lv = p["ToMix/Level"]?.value || [], on = p["ToMix/On"]?.value || [];
    for (let m = 0; m < lv.length; m++) {
      const name = mixes[m] || `Mix ${m + 1}`;
      const pair = m + 1 < lv.length && mixes[m] && mixes[m + 1] === mixes[m];
      rows.push({ name, db: lv[m], on: on[m] !== 0 });
      if (pair) m++;
    }
    const fl = p["ToFx/Level"]?.value || [], fo = p["ToFx/On"]?.value || [];
    fl.forEach((db, i) => rows.push({ name: `FX ${i + 1}`, db, on: fo[i] !== 0 }));
    if (p["ToMono/Level"]) rows.push({ name: "Mono", db: p["ToMono/Level"].value, on: p["ToMono/On"]?.value !== 0 });
    return rows;
  }

  function renderPanel() {
    const panel = $("console-panel"), toggle = $("console-toggle");
    toggle.hidden = tf5 === null;
    panel.hidden = tf5 === null || !state.showConsole;
    if (panel.hidden) return;

    const ch = tf5.channels && tf5.channels.find((c) => c.index === state.input);
    const items = [];
    if (!tf5.connected) {
      items.push(el("p", "cp-note", "TF5 not connected"));
    } else if (!ch || !ch.source) {
      items.push(el("p", "cp-note", `USB ${state.input + 1} is not on the console patch (tf5.usb_patch)`));
    } else {
      const p = ch.params, v = (k) => p[k]?.value;
      const head = el("div", "cp-head");
      const sw = el("span", "cp-swatch");
      sw.style.background = TF_COLORS[String(v("Label/Color") || "").toLowerCase()] || "var(--border)";
      const title = el("div");
      title.append(el("div", "cp-name", v("Label/Name") || ch.source), el("div", "cp-src", `${ch.source} · USB ${ch.index + 1}`));
      head.append(sw, title);
      items.push(head);

      const isOn = v("Fader/On") !== 0;
      items.push(el("div", `cp-on ${isOn ? "on" : "off"}`, isOn ? "ON" : "OFF"));

      // Fader: scale, track and cap, with the value underneath.
      const fader = el("div", "cp-fader");
      const scale = el("div", "cp-scale");
      for (const d of [10, 0, -10, -20, -30, -40, null]) {
        const t = el("span", d === 0 ? "zero" : "", d === null ? "−∞" : d > 0 ? `+${d}` : String(d));
        t.style.bottom = `${faderPos(d) * 100}%`;
        scale.append(t);
      }
      const track = el("div", "cp-track");
      const fill = el("div", "cp-fill"); fill.style.height = `${faderPos(v("Fader/Level")) * 100}%`;
      const cap = el("div", "cp-cap"); cap.style.bottom = `${faderPos(v("Fader/Level")) * 100}%`;
      track.append(fill, cap);
      fader.append(scale, track);
      const faderBox = el("div", "cp-faderbox");
      faderBox.append(fader, el("div", "cp-value", `${fmtDb(v("Fader/Level"))} dB`));

      // Pan (balance for ST) as a centre-detented bar.
      const panKey = p["ToStereo/Pan"] ? "ToStereo/Pan" : "Out/Balance";
      const side = el("div", "cp-side");
      if (p[panKey]) {
        const pan = v(panKey);
        const bar = el("div", "cp-pan");
        const dot = el("span"); dot.style.left = `${(pan + 63) / 126 * 100}%`;
        bar.append(dot);
        side.append(el("div", "cp-label", panKey === "Out/Balance" ? "Balance" : "Pan"), bar, el("div", "cp-panval", fmtPan(pan)));
      }
      const top = el("div", "cp-top");
      top.append(faderBox, side);
      items.push(top);

      // Sends: by default only those that are on and above -inf.
      if (p["ToMix/Level"]) {
        const rows = sendRows(p, tf5.mixes || []);
        const shown = state.allSends ? rows : rows.filter((r) => r.on && r.db !== null);
        const sends = el("div", "cp-sends");
        const hdr = el("div", "cp-sendhdr");
        const lbl = el("label");
        const cb = el("input"); cb.type = "checkbox"; cb.checked = state.allSends;
        cb.onchange = () => { state.allSends = cb.checked; save("tf.allSends", cb.checked ? 1 : 0); renderPanel(); };
        lbl.append(cb, " all");
        hdr.append(el("h3", "", "Sends"), lbl);
        sends.append(hdr);
        if (!shown.length) sends.append(el("p", "cp-note", "No active sends"));
        for (const r of shown) {
          const row = el("div", `cp-send${r.on && r.db !== null ? "" : " off"}`);
          const bar = el("div", "cp-sendbar");
          const f = el("span"); f.style.width = `${faderPos(r.db) * 100}%`;
          bar.append(f);
          row.append(el("span", "cp-sendname", r.name), bar, el("span", "cp-sendval", r.on ? fmtDb(r.db) : "off"));
          sends.append(row);
        }
        items.push(sends);
      }
      // Anything else the console offers (gain, EQ, ... if its firmware lists them).
      const known = new Set(["Label/Name", "Label/Color", "Fader/Level", "Fader/On", "ToStereo/Pan", "Out/Balance",
                             "ToMix/Level", "ToMix/On", "ToFx/Level", "ToFx/On", "ToMono/Level", "ToMono/On"]);
      const extra = Object.entries(p).filter(([k]) => !known.has(k));
      if (extra.length) {
        const dl = el("dl", "cp-extra");
        for (const [k, x] of extra) dl.append(el("dt", "", k), el("dd", "", `${x.value ?? "−∞"}${x.unit ? " " + x.unit : ""}`));
        items.push(dl);
      }
    }
    panel.replaceChildren(...items);
  }

  $("show-console").checked = state.showConsole;
  $("show-console").onchange = (e) => {
    state.showConsole = e.target.checked;
    save("tf.console", state.showConsole ? 1 : 0);
    renderPanel();
  };

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
