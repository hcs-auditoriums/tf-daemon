// tf-daemon spectrum view. Plain JS, no build step.
// Protocol: see tf_daemon/audio/engine.py (binary "TFS3" frames) and module.py.
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
    avg: color("--avg"), avgFill: color("--avg-fill"), peak: color("--peak"), rta: color("--rta"),
    eq: color("--eq"),
  };

  const state = {
    meta: null,
    input: loadInt("tf.input", -1),  // device input index being shown
    showAvg: true,
    showPeak: true,
    showLive: true,
    dbMin: -100,
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
      if (typeof ev.data !== "string") { onFrame(ev.data); return; }
      const msg = JSON.parse(ev.data);
      if (msg.type === "rings") onRings(msg);
      else onMeta(msg);
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
    state.live = new Float32Array(b);
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
    if (magic !== "TFS3") return;
    state.lastSeq = dv.getUint32(4, true);
    const nRec = dv.getUint16(8, true), nBins = dv.getUint16(10, true);
    if (nBins !== state.freqs.length) return;
    const recSize = 4 + 6 * nBins;  // index, reserved, int16[3][bins]
    for (let r = 0; r < nRec; r++) {
      const off = 12 + r * recSize;
      if (dv.getUint16(off, true) !== state.input) continue;
      const data = new Int16Array(buf, off + 4, 3 * nBins);
      for (let i = 0; i < nBins; i++) {
        state.avg[i] = data[i] / 100;
        state.peak[i] = data[nBins + i] / 100;
        state.live[i] = data[2 * nBins + i] / 100;
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
    state.live.fill(state.meta.db_floor);
    markSelected(index);
    send({ cmd: "view", channel: index });
    renderPanel();
    state.eq = null;
    if (state.showEq) fetchEq();
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

      // Fader: heading with the level, then a tall fader (scale, track, cap).
      const section = (title, value) => {
        const sec = el("div", "cp-section");
        const hd = el("div", "cp-sechead");
        hd.append(el("span", "cp-label", title), el("span", "cp-value", value));
        sec.append(hd);
        return sec;
      };
      const level = v("Fader/Level");
      const faderSec = section("Fader", `${fmtDb(level)} dB`);
      const fader = el("div", "cp-fader");
      const scale = el("div", "cp-scale");
      const track = el("div", "cp-track");
      for (const d of [10, 5, 0, -5, -10, -20, -30, -40, -60, null]) {
        const pos = `${faderPos(d) * 100}%`;
        const labelled = d === null || [10, 0, -10, -20, -30, -40, -60].includes(d);
        if (labelled) {
          const t = el("span", d === 0 ? "zero" : "", d === null ? "−∞" : d > 0 ? `+${d}` : String(d));
          t.style.bottom = pos;
          scale.append(t);
        }
        const tick = el("i", d === 0 ? "zero" : labelled ? "" : "minor");
        tick.style.bottom = pos;
        track.append(tick);
      }
      const fill = el("div", "cp-fill"); fill.style.height = `${faderPos(level) * 100}%`;
      const cap = el("div", "cp-cap"); cap.style.bottom = `${faderPos(level) * 100}%`;
      track.append(fill, cap);
      fader.append(scale, track);
      faderSec.append(fader);
      items.push(faderSec);

      // Pan (balance for ST) below the fader: a full-width, centre-detented bar.
      const panKey = p["ToStereo/Pan"] ? "ToStereo/Pan" : "Out/Balance";
      if (p[panKey]) {
        const pan = v(panKey);
        const panSec = section(panKey === "Out/Balance" ? "Balance" : "Pan", fmtPan(pan));
        const bar = el("div", "cp-pan");
        const dot = el("span"); dot.style.left = `${(pan + 63) / 126 * 100}%`;
        bar.append(dot);
        const marks = el("div", "cp-panmarks");
        marks.append(el("span", "", "L"), el("span", "", "C"), el("span", "", "R"));
        panSec.append(bar, marks);
        items.push(panSec);
      }

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

  // ------------------------------------------------------------------ console EQ overlay

  // The EQ set on the console for the channel on the shown input
  // (GET /api/tf5/eq/<usb>; only when tf5.eq is on). Read-only: this page
  // never changes the console. Drawn on its own ±EQ_RANGE dB scale, centred.
  const EQ_RANGE = 18;
  state.showEq = loadInt("tf.eq", 0) === 1;
  state.eq = null;         // last /api/tf5/eq response for state.input
  let eqAvailable = null;  // null = not known yet, false = tf5.eq off (404)

  async function fetchEq() {
    const want = state.input;
    try {
      const r = await fetch(`/api/tf5/eq/${want}`);
      if (r.status === 404) { eqAvailable = false; state.eq = null; }
      else if (r.ok) {
        eqAvailable = true;
        const d = await r.json();
        if (want === state.input) state.eq = d;
      }
    } catch {
      if (state.eq) state.eq = { ...state.eq, connected: false };
    }
    $("eq-toggle").hidden = !eqAvailable;
    state.dirty = true;
  }

  async function pollEq() {
    if (eqAvailable === null || (eqAvailable && state.showEq)) await fetchEq();
    if (eqAvailable !== false) setTimeout(pollEq, 1000);
  }

  $("show-eq").checked = state.showEq;
  $("show-eq").onchange = (e) => {
    state.showEq = e.target.checked;
    save("tf.eq", state.showEq ? 1 : 0);
    if (state.showEq) fetchEq();
    state.dirty = true;
  };

  // EQ value (dB) at f, interpolated on the curve's log-spaced points.
  function eqAt(f) {
    const d = state.eq;
    if (!d || !d.freqs || !d.freqs.length) return null;
    const fr = d.freqs, c = d.curve;
    if (f <= fr[0]) return c[0];
    for (let i = 1; i < fr.length; i++) {
      if (f <= fr[i]) {
        const t = (Math.log(f) - Math.log(fr[i - 1])) / (Math.log(fr[i]) - Math.log(fr[i - 1]));
        return c[i - 1] + t * (c[i] - c[i - 1]);
      }
    }
    return c[c.length - 1];
  }

  function eqNote(d) {
    if (!d.connected) return "EQ · console not connected";
    if (!d.synced) return "EQ · reading console…";
    if (!d.source) return `EQ · USB ${d.index + 1} is not on the console patch`;
    if (!d.eq) return `EQ · not available for ${d.source}`;
    return `EQ ${d.source}${d.eq.on ? "" : " · OFF"}${d.eq.one_knob ? " · 1-knob on" : ""} · approx.`;
  }

  function drawEq() {
    const d = state.eq;
    if (!state.showEq || !eqAvailable || !d || d.index !== state.input) return;
    const top = PAD.t, bot = H - PAD.b, x0 = PAD.l, x1 = W - PAD.r;
    const yEq = (db) => (top + bot) / 2 - db / EQ_RANGE * (bot - top) / 2;
    const yc = (db) => Math.min(bot, Math.max(top, yEq(db)));
    ctx.font = "11px system-ui, sans-serif";
    ctx.lineWidth = 1;
    ctx.strokeStyle = C.eq; ctx.fillStyle = C.eq;

    // Its scale: 0 dB line plus labels at the right edge.
    ctx.globalAlpha = 0.4;
    ctx.setLineDash([2, 4]);
    const y0 = Math.round(yEq(0)) + 0.5;
    ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y0); ctx.stroke();
    ctx.setLineDash([]);
    ctx.globalAlpha = 0.85;
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    for (const db of [12, 6, 0, -6, -12]) ctx.fillText(`${db > 0 ? "+" : ""}${db}`, x1 - 4, yEq(db));
    ctx.textAlign = "left"; ctx.textBaseline = "top";
    ctx.fillText(eqNote(d), x0 + 6, top + 4);
    ctx.globalAlpha = 1;
    const eq = d.eq;
    if (!eq || !d.freqs.length) return;

    // The response curve; dashed and dim when the EQ is switched off on the desk.
    ctx.globalAlpha = eq.on ? 1 : 0.45;
    ctx.lineWidth = 2;
    if (!eq.on) ctx.setLineDash([6, 4]);
    ctx.beginPath();
    d.freqs.forEach((f, i) => { const x = xOf(f), y = yc(d.curve[i]); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
    ctx.stroke();
    ctx.setLineDash([]);

    // A handle per band (filled when it does something) and the HPF.
    ctx.lineWidth = 1.5;
    ctx.textAlign = "center"; ctx.textBaseline = "bottom";
    const handle = (f, db, label, active) => {
      const x = xOf(f), y = yc(db);
      ctx.beginPath(); ctx.arc(x, y, 4, 0, 2 * Math.PI);
      if (active) ctx.fill(); else ctx.stroke();
      ctx.fillText(label, x, y - 6);
    };
    if (eq.hpf && eq.hpf.on) handle(eq.hpf.freq, 0, "HPF", true);
    for (const b of eq.bands) {
      const filter = b.type === "hpf" || b.type === "lpf";
      handle(b.freq, filter ? 0 : b.gain, filter ? b.type.toUpperCase() : b.label, filter || b.gain !== 0);
    }
    ctx.globalAlpha = 1; ctx.lineWidth = 1;
  }

  $("show-avg").onchange = (e) => { state.showAvg = e.target.checked; state.dirty = true; };
  $("show-peak").onchange = (e) => { state.showPeak = e.target.checked; state.dirty = true; };
  $("show-live").onchange = (e) => { state.showLive = e.target.checked; state.dirty = true; };
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

  function trace(values, stroke, fill, width = 1.5) {
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
    const a = state.avg[i], p = state.peak[i], l = state.live[i];
    ro.textContent = `${fBin >= 1000 ? (fBin / 1000).toFixed(2) + " kHz" : fBin.toFixed(1) + " Hz"}` +
      `  now ${l.toFixed(1)} dB  avg ${a.toFixed(1)} dB  peak ${p.toFixed(1)} dB  cursor ${dbOfY(h.y).toFixed(1)} dB`;
    const eqDb = state.showEq && state.eq && state.eq.eq ? eqAt(f) : null;
    if (eqDb !== null) ro.textContent += `  EQ ${eqDb > 0 ? "+" : ""}${eqDb.toFixed(1)} dB`;
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
    if (state.showLive) trace(state.live, C.rta, null, 1);
    drawRingMarkers();
    drawEq();
    ctx.restore();
    drawHover();
  }

  // ------------------------------------------------------------------ rings

  // Ring events from the detector (tf_daemon/dsp/ring.py), for all analyzed inputs.
  // Severity: a growing ring is feedback; a sustained ring, or a tail that keeps
  // coming back at the same frequency, is a ring; a single tail is only possible
  // (a slowly decaying instrument note can look like one) and raises no alert.
  state.rings = [];
  state.showRings = loadInt("tf.rings", 1) === 1;
  const SEV = {
    feedback: { label: "FEEDBACK", color: "#ff5d5d", rank: 3 },
    ring: { label: "RING", color: "#ff9f43", rank: 2 },
    possible: { label: "possible", color: "#f2d14e", rank: 1 },
  };
  const severity = (e) => (e.kind === "growing" ? "feedback"
    : e.kind === "sustained" || e.count >= 2 ? "ring" : "possible");
  const fmtHz = (f) => (f >= 1000 ? `${(f / 1000).toFixed(f >= 10000 ? 1 : 2)} kHz` : `${Math.round(f)} Hz`);
  const fmtBand = (b) => (b >= 1000 ? `${+(b / 1000).toFixed(2)}k` : `${b}`);
  function ago(t) {
    const s = Math.max(0, Date.now() / 1000 - t);
    return s < 60 ? `${Math.round(s)} s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
  }

  function onRings(msg) {
    state.rings = msg.events || [];
    renderRings();
    state.dirty = true;
  }

  function post(path, body) {
    fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })
      .catch(() => {});
  }

  function renderRings() {
    // Header alert: the worst live, confirmed ring on any input.
    const live = state.rings.filter((e) => e.active && severity(e) !== "possible")
      .sort((a, b) => SEV[severity(b)].rank - SEV[severity(a)].rank || b.prominence - a.prominence);
    const alert = $("ring-alert");
    alert.hidden = !live.length;
    if (live.length) {
      const e = live[0], sev = severity(e);
      alert.className = `ring-alert ${sev}`;
      alert.textContent = `${SEV[sev].label} ${fmtHz(e.freq)} · ${e.label}` + (live.length > 1 ? ` +${live.length - 1}` : "");
      alert.title = `Show ${e.label}`;
      alert.onclick = () => { if (e.channel !== state.input) selectInput(e.channel); };
    }

    const list = $("ring-list");
    list.hidden = !state.showRings || !state.rings.length;
    if (list.hidden) return;
    const head = el("div", "rl-head");
    const clear = el("button", "rl-btn", "Clear");
    clear.type = "button";
    clear.title = "Forget past rings (live ones stay)";
    clear.onclick = () => post("/api/audio/rings/clear");
    head.append(el("span", "", "Rings"), clear);
    const rows = [head];
    for (const e of state.rings.slice(0, 8)) {
      const sev = severity(e);
      const row = el("div", `rl-row${e.active ? " live" : ""}`);
      const dot = el("span", "rl-dot"); dot.style.background = SEV[sev].color;
      const main = el("div", "rl-main");
      const top = el("div", "rl-top");
      const freq = el("button", "rl-freq", fmtHz(e.freq));
      freq.type = "button";
      freq.title = `Show ${e.label}`;
      freq.onclick = () => { if (e.channel !== state.input) selectInput(e.channel); };
      top.append(freq, el("span", "rl-sev", SEV[sev].label));
      if (e.count > 1) top.append(el("span", "rl-count", `×${e.count}`));
      const sub = el("div", "rl-sub",
        `${e.label} · GEQ ${fmtBand(e.geq)} · ${e.active ? "now" : ago(e.last_seen)}`);
      main.append(top, sub);
      const ign = el("button", "rl-btn", "Ignore");
      ign.type = "button";
      ign.title = "Not feedback (a steady tone): stop reporting this frequency";
      ign.onclick = () => post("/api/audio/rings/ignore", { freq: e.freq });
      row.append(dot, main, ign);
      rows.push(row);
    }
    list.replaceChildren(...rows);
  }

  // Markers on the spectrum for the input being shown: live rings solid, recent dashed.
  function drawRingMarkers() {
    if (!state.showRings) return;
    const now = Date.now() / 1000;
    ctx.font = "11px system-ui, sans-serif";
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    for (const e of state.rings) {
      if (e.channel !== state.input || (!e.active && now - e.last_seen > 300)) continue;
      const x = Math.round(xOf(e.freq)) + 0.5, color = SEV[severity(e)].color;
      ctx.strokeStyle = color; ctx.fillStyle = color;
      ctx.globalAlpha = e.active ? 1 : 0.45;
      ctx.lineWidth = e.active ? 2 : 1;
      ctx.setLineDash(e.active ? [] : [4, 4]);
      ctx.beginPath(); ctx.moveTo(x, PAD.t); ctx.lineTo(x, H - PAD.b); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillText(fmtHz(e.freq), x, PAD.t + 2);
    }
    ctx.globalAlpha = 1; ctx.lineWidth = 1;
  }

  $("show-rings").checked = state.showRings;
  $("show-rings").onchange = (e) => {
    state.showRings = e.target.checked;
    save("tf.rings", state.showRings ? 1 : 0);
    renderRings();
    state.dirty = true;
  };
  setInterval(() => { if (state.rings.length) renderRings(); }, 5000);  // keep "x s ago" current

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
  pollEq();
  requestAnimationFrame(loop);
})();
