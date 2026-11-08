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
    eq: color("--eq"), eqFill: color("--eq-fill"), vocal: color("--vocal"),
  };

  const state = {
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

  function loadInt(key, def) {
    try { const v = parseInt(localStorage.getItem(key), 10); return Number.isFinite(v) ? v : def; }
    catch { return def; }
  }
  function loadStr(key, def, allowed) {
    try { const v = localStorage.getItem(key); return v !== null && allowed.includes(v) ? v : def; }
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
      else if (msg.type === "tf5") onConsoleSnapshot(msg);
      else if (msg.type === "tf5-changes") onConsoleChanges(msg);
      else if (msg.type === "tf5-eq") onConsoleEq(msg);
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
      autoRange();
      wfPush(state.wfSource === "avg" ? state.avg : state.live);
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
    autoReset();
    renderVocal();
    wfReset();
    markSelected(index);
    send({ cmd: "view", channel: index });
    renderPanel();
    state.eq = null;
    state.dirty = true;
  }

  // ------------------------------------------------------------------ console panel

  // Sidebar with the TF5 channel feeding the shown input: name, on, fader,
  // pan and its sends. The desk's state arrives over /ws/audio: a snapshot on
  // connect ("tf5"), then each change as it happens on the desk ("tf5-changes");
  // nothing arrives when tf5.host isn't set, and the sidebar stays hidden.
  let tf5 = null;   // null = no TF5 module; {connected, channels, mixes} otherwise
  state.showConsole = loadInt("tf.console", 1) === 1;
  state.allSends = loadInt("tf.allSends", 0) === 1;

  function onConsoleSnapshot(msg) {
    tf5 = msg;
    panelSig = null;  // rebuild
    schedulePanel();
  }

  function onConsoleChanges(msg) {
    if (!tf5) return;
    tf5.connected = msg.connected;
    for (const [m, name] of Object.entries(msg.mixes || {})) tf5.mixes[Number(m)] = name;
    for (const c of msg.changes) {
      const ch = tf5.channels[c.i];
      if (!ch) continue;
      let p = ch.params[c.p];
      if (c.y === null) {
        ch.params[c.p] = { value: c.v, unit: c.u };
      } else {
        if (!p || !Array.isArray(p.value)) p = ch.params[c.p] = { value: [], unit: c.u };
        p.value[c.y] = c.v;
      }
    }
    schedulePanel();
  }

  // Many changes can arrive per frame while a fader moves: render once per frame.
  let panelQueued = false;
  function schedulePanel() {
    if (panelQueued) return;
    panelQueued = true;
    requestAnimationFrame(() => { panelQueued = false; renderPanel(); });
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

  // The sidebar is rebuilt only when its structure changes (another input, a
  // send appearing, ...); otherwise values are updated in place, so a fader
  // moving on the desk moves smoothly here (with a short CSS transition).
  let panelSig = null, refs = null;

  function panelStructure(ch) {
    if (!tf5.connected || !ch || !ch.source) return JSON.stringify([state.input, tf5.connected, ch && ch.source]);
    const p = ch.params, rows = p["ToMix/Level"] ? sendRows(p, tf5.mixes || []) : [];
    const shown = (state.allSends ? rows : rows.filter((r) => r.on && r.db !== null)).map((r) => r.name);
    return JSON.stringify([state.input, ch.source, !!eqAvailable, p["Label/Name"]?.value, p["Label/Color"]?.value,
                           !!p["ToStereo/Pan"], !!p["Out/Balance"], !!p["ToMix/Level"], state.allSends, shown,
                           Object.keys(p).sort()]);
  }

  function updatePanel(ch) {
    const p = ch.params, v = (k) => p[k]?.value;
    const isOn = v("Fader/On") !== 0;
    refs.on.className = `cp-on ${isOn ? "on" : "off"}`;
    refs.on.textContent = isOn ? "ON" : "OFF";
    const level = v("Fader/Level"), pos = `${faderPos(level) * 100}%`;
    refs.level.textContent = `${fmtDb(level)} dB`;
    refs.fill.style.height = pos;
    refs.cap.style.bottom = pos;
    if (refs.panKey && p[refs.panKey]) {
      const pan = v(refs.panKey);
      refs.panVal.textContent = fmtPan(pan);
      refs.panDot.style.left = `${(pan + 63) / 126 * 100}%`;
    }
    if (refs.sends.size) {
      for (const r of sendRows(p, tf5.mixes || [])) {
        const s = refs.sends.get(r.name);
        if (!s) continue;
        s.row.className = `cp-send${r.on && r.db !== null ? "" : " off"}`;
        s.fill.style.width = `${faderPos(r.db) * 100}%`;
        s.val.textContent = r.on ? fmtDb(r.db) : "off";
      }
    }
    for (const [k, dd] of refs.extra) {
      const x = p[k];
      if (x) dd.textContent = `${x.value ?? "−∞"}${x.unit ? " " + x.unit : ""}`;
    }
  }

  function renderPanel() {
    const panel = $("console-panel"), toggle = $("console-toggle");
    toggle.hidden = tf5 === null;
    panel.hidden = tf5 === null || !state.showConsole;
    if (panel.hidden) return;

    const ch = tf5.channels && tf5.channels.find((c) => c.index === state.input);
    const sig = panelStructure(ch);
    if (sig === panelSig && refs) {
      updatePanel(ch);
      return;
    }
    panelSig = sig;
    refs = { sends: new Map(), extra: new Map() };
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

      refs.on = el("div", "cp-on");
      items.push(refs.on);

      const section = (title) => {
        const sec = el("div", "cp-section");
        const hd = el("div", "cp-sechead");
        const value = el("span", "cp-value");
        hd.append(el("span", "cp-label", title), value);
        sec.append(hd);
        return [sec, value];
      };

      // EQ (only with tf5.eq): the console's curve for this channel, like the TF5's
      // channel screen; redrawn whenever the desk pushes a change.
      if (eqAvailable) {
        const [eqSec, eqVal] = section("EQ");
        refs.eqVal = eqVal;
        refs.eqCanvas = el("canvas", "cp-eq");
        refs.eqCanvas.setAttribute("aria-label", "Console EQ curve");
        refs.eqInfo = el("div", "cp-eqinfo");
        eqSec.append(refs.eqCanvas, refs.eqInfo);
        items.push(eqSec);
      }

      // Fader: heading with the level, then a tall fader (scale, track, cap).
      const [faderSec, levelEl] = section("Fader");
      refs.level = levelEl;
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
      refs.fill = el("div", "cp-fill");
      refs.cap = el("div", "cp-cap");
      track.append(refs.fill, refs.cap);
      fader.append(scale, track);
      faderSec.append(fader);
      items.push(faderSec);

      // Pan (balance for ST) below the fader: a full-width, centre-detented bar.
      const panKey = p["ToStereo/Pan"] ? "ToStereo/Pan" : "Out/Balance";
      if (p[panKey]) {
        const [panSec, panVal] = section(panKey === "Out/Balance" ? "Balance" : "Pan");
        const bar = el("div", "cp-pan");
        const dot = el("span");
        bar.append(dot);
        Object.assign(refs, { panKey, panVal, panDot: dot });
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
        cb.onchange = () => { state.allSends = cb.checked; save("tf.allSends", cb.checked ? 1 : 0); panelSig = null; renderPanel(); };
        lbl.append(cb, " all");
        hdr.append(el("h3", "", "Sends"), lbl);
        sends.append(hdr);
        if (!shown.length) sends.append(el("p", "cp-note", "No active sends"));
        for (const r of shown) {
          const row = el("div", "cp-send");
          const bar = el("div", "cp-sendbar");
          const f = el("span");
          bar.append(f);
          const val = el("span", "cp-sendval");
          row.append(el("span", "cp-sendname", r.name), bar, val);
          refs.sends.set(r.name, { row, fill: f, val });
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
        for (const [k] of extra) {
          const dd = el("dd");
          refs.extra.set(k, dd);
          dl.append(el("dt", "", k), dd);
        }
        items.push(dl);
      }
    }
    panel.replaceChildren(...items);
    if (refs.on) updatePanel(ch);
    drawPanelEq();
  }

  // ------------------------------------------------------------------ sidebar EQ

  // Same scale as the EQ on the spectrum: ±eqHalf() dB (half the View range, so it
  // follows Auto too) over the spectrum's frequency span.
  const PEQ_GRID_F = [100, 1000, 10000];
  const fmtF0 = (f) => (f >= 1000 ? `${+(f / 1000).toFixed(f >= 10000 ? 0 : 1)}k` : `${Math.round(f)}`);
  const BAND_SHAPE = { bell: "bell", low_shelf: "shelf", high_shelf: "shelf", hpf: "HPF", lpf: "LPF" };

  function drawPanelEq() {
    const cv = refs && refs.eqCanvas;
    if (!cv || !cv.isConnected) return;
    const d = state.eq && state.eq.index === state.input ? state.eq : null;
    const eq = d && d.eq;
    refs.eqVal.textContent = !d ? "…" : !eq ? "—" : !eq.on ? "OFF" : eq.one_knob ? "1-knob" : "ON";
    refs.eqVal.className = `cp-value${eq && !eq.on ? " cp-off" : ""}`;
    refs.eqInfo.textContent = !d ? "Reading console…" : !eq ? (d.source ? `No EQ for ${d.source}` : "Not on the console patch") :
      [eq.hpf && eq.hpf.on ? `HPF ${fmtF0(eq.hpf.freq)}` : "HPF off",
       ...eq.bands.filter((b) => b.label === "L" || b.label === "H")
         .map((b) => `${b.label} ${BAND_SHAPE[b.type] || b.type}${b.on === false ? " off" : ""}`)].join(" · ");

    const dpr = window.devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) return;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
      cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    }
    const g = cv.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    const lo = state.logMin ?? Math.log10(20), hi = state.logMax ?? Math.log10(20000);
    const x = (f) => (Math.log10(f) - lo) / (hi - lo) * w;
    const range = eqHalf();
    const y = (db) => h / 2 - db / range * (h / 2);

    // Grid: 0 dB, the spectrum's EQ steps (6 dB, 12 above ±40), 100 Hz / 1k / 10k.
    g.lineWidth = 1;
    g.strokeStyle = C.grid;
    g.beginPath();
    const step = range > 40 ? 12 : 6;
    for (let db = step; db < range; db += step) {
      for (const yy of [Math.round(y(db)) + 0.5, Math.round(y(-db)) + 0.5]) { g.moveTo(0, yy); g.lineTo(w, yy); }
    }
    for (const f of PEQ_GRID_F) { const xx = Math.round(x(f)) + 0.5; g.moveTo(xx, 0); g.lineTo(xx, h); }
    g.stroke();
    g.strokeStyle = C.gridStrong;
    g.beginPath(); g.moveTo(0, Math.round(y(0)) + 0.5); g.lineTo(w, Math.round(y(0)) + 0.5); g.stroke();
    g.fillStyle = C.muted;
    g.font = "9px system-ui, sans-serif";
    g.textBaseline = "bottom"; g.textAlign = "left";
    for (const f of PEQ_GRID_F) if (x(f) > 0 && x(f) < w - 14) g.fillText(fmtF0(f), x(f) + 2, h - 1);
    g.textBaseline = "top";
    g.fillText(`±${range} dB`, 2, 1);
    if (!eq || !d.freqs.length) return;

    // Curve (clipped by the canvas), shaded to 0 dB when the EQ is on, dashed when off.
    const path = () => { g.beginPath(); d.freqs.forEach((f, i) => (i ? g.lineTo : g.moveTo).call(g, x(f), y(d.curve[i]))); };
    if (eq.on) {
      path();
      g.lineTo(x(d.freqs[d.freqs.length - 1]), y(0)); g.lineTo(x(d.freqs[0]), y(0)); g.closePath();
      g.fillStyle = C.eqFill; g.fill();
    }
    g.globalAlpha = eq.on ? 1 : 0.45;
    g.strokeStyle = C.eq; g.fillStyle = C.eq;
    g.lineWidth = 1.5;
    if (!eq.on) g.setLineDash([4, 3]);
    path(); g.stroke();
    g.setLineDash([]);

    // Band handles on the curve: filled when the band does something.
    const dot = (f, active) => {
      const yy = Math.min(h - 3, Math.max(3, y(eqAt(f))));
      g.beginPath(); g.arc(x(f), yy, 2.5, 0, 2 * Math.PI);
      if (active) g.fill(); else g.stroke();
    };
    if (eq.hpf && eq.hpf.on) dot(eq.hpf.freq, true);
    for (const b of eq.bands) {
      const filter = b.type === "hpf" || b.type === "lpf";
      dot(b.freq, filter ? b.on !== false : b.gain !== 0);
    }
    g.globalAlpha = 1;
  }

  new ResizeObserver(() => drawPanelEq()).observe($("console-panel"));

  $("show-console").checked = state.showConsole;
  $("show-console").onchange = (e) => {
    state.showConsole = e.target.checked;
    save("tf.console", state.showConsole ? 1 : 0);
    renderPanel();
  };

  // View menu (a <details>): close on a click outside it or Escape.
  const viewMenu = $("view-menu");
  document.addEventListener("click", (e) => { if (viewMenu.open && !viewMenu.contains(e.target)) viewMenu.open = false; });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") viewMenu.open = false; });

  // ------------------------------------------------------------------ console EQ overlay

  // The EQ set on the console for the channel on the shown input (pushed as
  // "tf5-eq" over /ws/audio; only when tf5.eq is on). Read-only: this page
  // never changes the console. Drawn at the spectrum's own dB scale (the View
  // range), with EQ 0 dB at the middle of the plot: a 70 dB range shows ±35 dB.
  state.showEq = loadInt("tf.eq", 0) === 1;
  state.eq = null;         // last "tf5-eq" message for state.input
  let eqAvailable = null;  // null = not known yet, false = tf5.eq off (404)

  // Pushed over /ws/audio for the viewed input whenever it changes on the desk
  // (or the view changes); nothing arrives when tf5.eq is off.
  function onConsoleEq(msg) {
    const first = !eqAvailable;
    eqAvailable = true;
    if (msg.index === state.input) state.eq = msg;
    $("eq-toggle").hidden = false;
    state.dirty = true;
    if (first) { panelSig = null; schedulePanel(); } else drawPanelEq();
  }

  $("show-eq").checked = state.showEq;
  $("show-eq").onchange = (e) => {
    state.showEq = e.target.checked;
    save("tf.eq", state.showEq ? 1 : 0);
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
    return `EQ ${d.source}${d.eq.on ? "" : " · OFF"}${d.eq.one_knob ? " · 1-knob on" : ""}`;
  }

  // EQ curves (the console's and the vocal assist's) share one scale: centred,
  // following the View range, so they can be compared directly.
  const eqHalf = () => (state.dbMax - state.dbMin) / 2;     // EQ dB shown above / below 0
  const eqY = (db) => (PAD.t + H - PAD.b) / 2 - db / eqHalf() * (H - PAD.b - PAD.t) / 2;

  function drawEq() {
    const d = state.eq;
    if (!state.showEq || !eqAvailable || !d || d.index !== state.input) return;
    const top = PAD.t, bot = H - PAD.b, x0 = PAD.l, x1 = W - PAD.r;
    const half = eqHalf();
    const yEq = eqY;
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
    const step = half > 40 ? 12 : 6;
    const edge = Math.floor((half - 2) / step) * step;
    for (let db = edge; db >= -edge; db -= step) {
      ctx.fillText(`${db > 0 ? "+" : ""}${db}`, x1 - 4, yEq(db));
    }
    ctx.textAlign = "left"; ctx.textBaseline = "top";
    ctx.fillText(eqNote(d), x0 + 6, top + 4);
    ctx.globalAlpha = 1;
    const eq = d.eq;
    if (!eq || !d.freqs.length) return;

    // Shade between the curve and 0 dB, as the TF5's EQ screen does (only when on).
    if (eq.on) {
      ctx.beginPath();
      d.freqs.forEach((f, i) => { const x = xOf(f), y = yEq(d.curve[i]); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
      ctx.lineTo(xOf(d.freqs[d.freqs.length - 1]), yEq(0));
      ctx.lineTo(xOf(d.freqs[0]), yEq(0));
      ctx.closePath();
      ctx.fillStyle = C.eqFill;
      ctx.fill();
      ctx.fillStyle = C.eq;
    }

    // The response curve (clipped by the plot, not flattened onto its edge); dashed
    // and dim when the EQ is switched off on the desk.
    ctx.globalAlpha = eq.on ? 1 : 0.45;
    ctx.lineWidth = 2;
    if (!eq.on) ctx.setLineDash([6, 4]);
    ctx.beginPath();
    d.freqs.forEach((f, i) => { const x = xOf(f), y = yEq(d.curve[i]); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
    ctx.stroke();
    ctx.setLineDash([]);

    // A handle per band (filled when it does something) and the HPF, on the curve at
    // its frequency (overlapping bands add up, so that's rarely the band's own gain).
    ctx.lineWidth = 1.5;
    ctx.textAlign = "center"; ctx.textBaseline = "bottom";
    const handle = (f, label, active) => {
      const x = xOf(f), y = yEq(eqAt(f));
      ctx.beginPath(); ctx.arc(x, y, 4, 0, 2 * Math.PI);
      if (active) ctx.fill(); else ctx.stroke();
      ctx.fillText(label, x, y - 6);
    };
    if (eq.hpf && eq.hpf.on) handle(eq.hpf.freq, "HPF", true);
    for (const b of eq.bands) {
      const filter = b.type === "hpf" || b.type === "lpf";
      const on = b.on !== false;
      handle(b.freq, filter ? b.type.toUpperCase() : b.label, filter ? on : b.gain !== 0);
    }
    ctx.globalAlpha = 1; ctx.lineWidth = 1;
  }

  $("show-avg").onchange = (e) => { state.showAvg = e.target.checked; state.dirty = true; };
  $("show-peak").onchange = (e) => { state.showPeak = e.target.checked; state.dirty = true; };
  $("show-live").onchange = (e) => { state.showLive = e.target.checked; state.dirty = true; };
  // ------------------------------------------------------------------ range

  // Levels are per 1/48-octave display bin (a full-scale sine reads 0 dB): speech
  // harmonics land around -25..-40, a hot mix -10..-25, rings up to ~0, and below
  // ~-90 there's only preamp hiss and silence (-130..-140). The pre-fader inputs
  // sit wherever their head-amp gain puts them, so the default, Auto, keeps a
  // 70 dB window on whatever the shown input is doing: the top 5 dB above the
  // loudest point of the last 5 s, rounded up to 10 dB. It moves up at once and
  // back down only after 8 s quieter, so it doesn't pump between phrases.
  const RANGES = ["auto", "-90,-10", "-80,0", "-70,-20", "-130,0"];
  const AUTO_SPAN = 70, AUTO_TOP_MIN = -40, AUTO_TOP_MAX = 0, AUTO_HOLD_S = 8, AUTO_WINDOW_S = 5;
  state.range = loadStr("tf.range", "auto", RANGES);
  const auto = { maxima: [], quietSince: null, fitUntil: 0 };

  function setRange(min, max) {
    if (min === state.dbMin && max === state.dbMax) return;
    state.dbMin = min; state.dbMax = max;
    wfRecolor();
    drawPanelEq();   // the sidebar EQ uses the same scale
    state.dirty = true;
  }

  function autoLabel() {
    $("range").options[0].textContent = state.range === "auto" ? `Auto (${state.dbMin}…${state.dbMax} dB)` : "Auto";
  }

  function autoReset() {  // new input: fit straight away, both directions
    auto.maxima = []; auto.quietSince = null; auto.fitUntil = performance.now() + 1500;
  }

  function autoRange() {
    if (state.range !== "auto" || !state.meta) return;
    let m = -Infinity;
    for (let i = 0; i < state.live.length; i++) if (state.live[i] > m) m = state.live[i];
    const keep = Math.round(AUTO_WINDOW_S * state.meta.update_rate);
    auto.maxima.push(m);
    if (auto.maxima.length > keep) auto.maxima.shift();
    const loudest = Math.max(...auto.maxima);
    const top = Math.min(AUTO_TOP_MAX, Math.max(AUTO_TOP_MIN, Math.ceil((loudest + 5) / 10) * 10));
    const now = performance.now();
    if (top > state.dbMax || now < auto.fitUntil) {
      auto.quietSince = null;
      setRange(top - AUTO_SPAN, top);
    } else if (top < state.dbMax) {
      if (auto.quietSince === null) auto.quietSince = now;
      if (now - auto.quietSince >= AUTO_HOLD_S * 1000) {
        auto.quietSince = null;
        setRange(state.dbMin - 10, state.dbMax - 10);  // one step at a time
      }
    } else {
      auto.quietSince = null;
    }
    autoLabel();
  }

  $("range").value = state.range;
  $("range").onchange = (e) => {
    state.range = e.target.value;
    save("tf.range", state.range);
    if (state.range === "auto") autoReset();
    else setRange(...state.range.split(",").map(Number));
    autoLabel();
  };
  // At load there's no waterfall history to recolour (and it's defined further down).
  if (state.range !== "auto") [state.dbMin, state.dbMax] = state.range.split(",").map(Number);
  autoLabel();
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

  // ------------------------------------------------------------------ vocal EQ assist

  // Optional module (vocal.enabled): during a mic check, listen to the shown
  // input while the person talks and suggest HPF / PEQ settings. Suggestions
  // only: the desk is never changed. API: /api/vocal/*.
  let vocal = null;                 // GET /api/vocal/status; null = module off
  let vocalOpen = false;
  state.vocalOverlay = loadInt("tf.vocalOverlay", 1) === 1;

  async function pollVocal() {
    let next = 3000;
    try {
      const r = await fetch("/api/vocal/status");
      if (r.status === 404) { vocal = null; $("mic-check").hidden = true; return; }  // module off: stop
      vocal = await r.json();
      $("mic-check").hidden = false;
      const listening = Object.values(vocal.sessions).some((s) => s.state === "listening");
      if (vocalOpen || listening) next = 500;
      renderVocal();
      state.dirty = true;
    } catch { /* server restarting: try again */ }
    setTimeout(pollVocal, next);
  }

  function vocalPost(path) {
    return fetch(`/api/vocal/${path}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ channel: state.input }),
    }).then((r) => r.json()).catch(() => ({ ok: false, error: "server unreachable" }))
      .then((d) => { if (!d.ok && d.error) alert(d.error); setTimeout(pollVocal, 50); });
  }

  $("mic-check").onclick = () => {
    vocalOpen = !vocalOpen;
    $("mic-check").setAttribute("aria-expanded", String(vocalOpen));
    renderVocal();
    if (vocalOpen) pollVocal();
  };

  const fmtFreq = (f) => (f >= 1000 ? `${+(f / 1000).toFixed(2)} kHz` : `${Math.round(f)} Hz`);
  const fmtGain = (g) => `${g > 0 ? "+" : g < 0 ? "−" : ""}${Math.abs(g).toFixed(1)} dB`;

  // The band's current setting on the desk, when the console EQ is readable.
  function currentSetting(eq, band) {
    if (!eq) return null;
    if (band === "HPF") return eq.hpf && eq.hpf.on ? `HPF ${fmtFreq(eq.hpf.freq)}` : "HPF off";
    const b = eq.bands.find((x) => x.label === band);
    if (!b) return null;
    if (b.type === "hpf" || b.type === "lpf") return `${b.type.toUpperCase()} ${fmtFreq(b.freq)}`;
    return b.gain ? `${fmtGain(b.gain)} @ ${fmtFreq(b.freq)}, Q ${b.q}` : "flat";
  }

  function renderVocal() {
    const panel = $("vocal-panel");
    panel.hidden = !vocal || !vocalOpen;
    if (panel.hidden || !state.meta) return;
    const label = state.meta.channels[state.input]?.label ?? `In ${state.input + 1}`;
    const s = vocal.sessions[String(state.input)];
    const items = [];
    const head = el("div", "vp-head");
    const close = el("button", "vp-x", "×"); close.type = "button"; close.title = "Close";
    close.onclick = () => $("mic-check").click();
    head.append(el("strong", "", `Vocal EQ assist · ${label}`), close);
    items.push(head);
    const button = (text, fn, primary) => {
      const b = el("button", primary ? "vp-btn primary" : "vp-btn", text); b.type = "button"; b.onclick = fn; return b;
    };
    const row = (...els) => { const d = el("div", "vp-row"); d.append(...els); return d; };

    if (vocal.inputs && !vocal.inputs.includes(state.input)) {
      items.push(el("p", "vp-note", `${label} isn't one of the vocal mics (vocal.inputs).`));
    } else if (!s) {
      items.push(el("p", "", "Have the person talk normally into this mic, at their usual distance, for about "
                              + `${Math.round(vocal.listen_s)} s (reading a passage works well).`));
      items.push(row(button("Start listening", () => vocalPost("start"), true)));
    } else if (s.state === "listening") {
      const pct = Math.min(100, s.speech_s / s.target_s * 100);
      const bar = el("div", "vp-bar"); const fill = el("span"); fill.style.width = `${pct}%`; bar.append(fill);
      items.push(bar);
      items.push(el("p", "vp-note", `Heard ${s.speech_s.toFixed(1)} of ${Math.round(s.target_s)} s of speech`
                                     + (s.f0_hz ? ` · voice pitch ≈ ${Math.round(s.f0_hz)} Hz` : "")));
      items.push(row(button("Stop & analyze", () => vocalPost("stop"), true), button("Cancel", () => vocalPost("clear"))));
    } else {
      const r = s.result || {};
      if (!r.ok) {
        items.push(el("p", "vp-error", r.error || "No result."));
      } else {
        items.push(el("p", "vp-note", `${Math.round(r.speech_s)} s of speech · ${r.voice || "voice"}`
                                       + (r.f0_hz ? ` (≈ ${Math.round(r.f0_hz)} Hz)` : "") + ` · confidence ${r.confidence}`));
        const table = el("table", "vp-table");
        const tr = (cells, cls) => { const t = el("tr", cls || ""); cells.forEach((c) => t.append(el("td", "", c))); return t; };
        const cur = s.current_eq;
        const headRow = el("tr");
        ["Band", "Suggested", "For"].concat(cur ? ["Now"] : []).forEach((h) => headRow.append(el("th", "", h)));
        table.append(headRow);
        if (r.hpf) table.append(tr(["HPF", fmtFreq(r.hpf.freq), r.hpf.issue].concat(cur ? [currentSetting(cur, "HPF") || "–"] : [])));
        for (const b of r.bands) {
          table.append(tr([b.band, `${fmtGain(b.gain)} @ ${fmtFreq(b.freq)}, Q ${b.q}`, b.issue]
            .concat(cur ? [currentSetting(cur, b.band) || "–"] : []), b.kind));
        }
        if (!r.bands.length) table.append(tr(["", "No EQ moves needed", "close to the reference speech spectrum"].concat(cur ? [""] : [])));
        items.push(table);
        for (const n of r.notes) items.push(el("p", "vp-note", n));
        const lbl = el("label", "vp-check");
        const cb = el("input"); cb.type = "checkbox"; cb.checked = state.vocalOverlay;
        cb.onchange = () => { state.vocalOverlay = cb.checked; save("tf.vocalOverlay", cb.checked ? 1 : 0); state.dirty = true; };
        lbl.append(cb, " Show suggested EQ on spectrum");
        items.push(lbl);
        if (eqAvailable && !state.showEq) {  // desk EQ readable but its overlay is off: offer the comparison
          items.push(row(button("Compare with desk EQ", () => {
            const c = $("show-eq"); c.checked = true; c.dispatchEvent(new Event("change"));
            state.vocalOverlay = true; save("tf.vocalOverlay", 1); renderVocal();
          })));
        }
        const ap = s.apply;
        if (vocal.can_write && ap) {
          if (ap.possible) items.push(row(button("Apply to desk…", () => vocalDesk("apply", label, ap.changes), true)));
          else items.push(el("p", "vp-note", `Can't apply to the desk: ${ap.reason}.`));
        }
      }
      items.push(row(button("Listen again", () => vocalPost("start"), true), button("Clear", () => vocalPost("clear"))));
    }
    const key = String(state.input);
    const done = vocal.applied && vocal.applied[key];
    if (done) {
      const when = new Date(done.at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      const what = done.action === "undo" ? "Undone" : "Applied to the desk";
      const ok = done.confirmed === done.sent;
      items.push(el("p", ok ? "vp-note" : "vp-error", done.sent
        ? `${what} at ${when}: ${done.confirmed} of ${done.sent} change(s) confirmed by the desk`
          + (ok ? "." : ". Check the channel's EQ on the desk.")
        : `${what} at ${when}: the desk already matched.`));
      if (done.action === "apply" && done.sent) {
        items.push(el("p", "vp-note", "The analyzer listens before the EQ, so the spectrum doesn't change; "
                                     + "the green desk EQ curve shows the new setting."));
      }
    }
    if (vocal.undo && vocal.undo[key]) items.push(row(button("Undo apply", () => vocalDesk("undo", label))));
    items.push(el("p", "vp-foot", vocal.can_write
      ? "The desk is changed only when you press Apply and confirm; Undo puts back what it replaced."
      : "Suggestions only: nothing is changed on the desk."));
    panel.replaceChildren(...items);
  }

  // Changes the desk: only from a click, after the person confirms the exact changes.
  function vocalDesk(action, label, lines) {
    const msg = action === "apply"
      ? `Change ${label}'s EQ on the desk to the suggestion?\n\n${lines.join("\n")}\n\nUndo puts back the current settings.`
      : `Put ${label}'s EQ on the desk back to how it was before Apply?`;
    if (!window.confirm(msg)) return;
    fetch(`/api/vocal/${action}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channel: state.input, confirm: true }),
    }).then((r) => r.json()).catch(() => ({ ok: false, error: "server unreachable" }))
      .then((d) => { if (!d.ok) alert(d.error || "Not applied."); setTimeout(pollVocal, 50); });
  }

  // The suggested EQ (violet, dashed) and, faintly, why: the voice vs reference speech.
  function drawVocal() {
    if (!vocal || !state.vocalOverlay) return;
    const s = vocal.sessions[String(state.input)];
    const r = s && s.result;
    if (!r || !r.ok) return;
    // Same scale as the console EQ overlay (eqY), so the suggested curve lines
    // up with the desk's actual EQ curve when both are shown.
    const top = PAD.t, bot = H - PAD.b;
    const y = (db) => Math.min(bot, Math.max(top, eqY(db)));
    const line = (fs, vs, color, dash, width, alpha) => {
      ctx.beginPath();
      fs.forEach((f, i) => { const xx = xOf(f), yy = y(vs[i]); if (i) ctx.lineTo(xx, yy); else ctx.moveTo(xx, yy); });
      ctx.globalAlpha = alpha; ctx.strokeStyle = color; ctx.lineWidth = width; ctx.setLineDash(dash);
      ctx.stroke(); ctx.setLineDash([]); ctx.globalAlpha = 1;
    };
    const deskEq = state.showEq && eqAvailable && state.eq && state.eq.index === state.input;
    if (!deskEq) {  // otherwise the EQ overlay has drawn this scale already
      ctx.globalAlpha = 0.5; ctx.strokeStyle = C.vocal; ctx.setLineDash([2, 4]); ctx.lineWidth = 1;
      const y0 = Math.round(y(0)) + 0.5;
      ctx.beginPath(); ctx.moveTo(PAD.l, y0); ctx.lineTo(W - PAD.r, y0); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = C.vocal; ctx.font = "11px system-ui, sans-serif"; ctx.textAlign = "right"; ctx.textBaseline = "middle";
      const step = eqHalf() > 40 ? 12 : 6, edge = Math.floor((eqHalf() - 2) / step) * step;
      for (let db = edge; db >= -edge; db -= step) ctx.fillText(`${db > 0 ? "+" : ""}${db}`, W - PAD.r - 4, y(db));
      ctx.globalAlpha = 1;
    }
    line(r.points, r.deviation, C.vocal, [], 1, 0.45);          // why: the voice vs reference speech
    line(r.curve_freqs, r.curve, C.vocal, [7, 4], 2.5, 1);      // what: the suggested EQ
    ctx.font = "11px system-ui, sans-serif"; ctx.textAlign = "left"; ctx.textBaseline = "top";
    ctx.fillStyle = C.vocal;
    ctx.fillText(`suggested EQ (dashed)${deskEq ? " · desk EQ in green" : ""}`, PAD.l + 6, bot - 30);
    ctx.globalAlpha = 0.6;
    ctx.fillText("thin line: voice vs reference speech", PAD.l + 6, bot - 16);
    ctx.globalAlpha = 1; ctx.lineWidth = 1;
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
    drawVocal();
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
      const src = e.sources && e.sources[0] ? ` ← ${e.sources[0].label}` : "";
      alert.textContent = `${SEV[sev].label} ${fmtHz(e.freq)} · ${e.label}${src}` + (live.length > 1 ? ` +${live.length - 1}` : "");
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
      if (e.sources && e.sources.length) main.append(sourcesLine(e));
      const ign = el("button", "rl-btn", "Ignore");
      ign.type = "button";
      ign.title = "Not feedback (a steady tone): stop reporting this frequency";
      ign.onclick = () => post("/api/audio/rings/ignore", { freq: e.freq });
      row.append(dot, main, ign);
      rows.push(row);
    }
    list.replaceChildren(...rows);
  }

  // Which inputs feed a ring on the mains, strongest first: "from ROEDER · KAT −5 dB".
  // Ranked by each input's level at the ring frequency plus its fader (the USB
  // feeds are pre-fader); channels that are off are left out.
  function sourcesLine(e) {
    const line = el("div", "rl-src");
    line.title = e.sources_basis === "fader"
      ? "Inputs feeding this ring, by level at the ring frequency + fader. Channel EQ isn't included (the USB feeds are pre-EQ)."
      : "Inputs feeding this ring, by level at the ring frequency (fader levels unknown: no TF5 connection).";
    line.append("from ");
    e.sources.forEach((s, i) => {
      if (i) line.append(" · ");
      const b = el("button", i ? "rl-srcname" : "rl-srcname top", s.label);
      b.type = "button";
      b.title = `Show ${s.label}`;
      b.onclick = () => selectInput(s.channel);
      line.append(b);
      if (i) line.append(` ${s.rel_db.toFixed(0)} dB`);
    });
    return line;
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

  // ------------------------------------------------------------------ waterfall

  // Each frame of the shown input becomes one row (newest at the top). Rows live
  // in an off-screen canvas used as a circular buffer, one pixel per display bin,
  // so a frame costs one 400-pixel row; the levels are kept too, to recolour the
  // history when the range changes and for the hover readout.
  const LAYOUTS = ["spectrum", "both", "waterfall"];
  state.layout = loadStr("tf.layout", "both", LAYOUTS);
  state.wfSpan = loadInt("tf.wfSpan", 30);
  state.wfSource = loadStr("tf.wfSource", "live", ["live", "avg"]);

  const wfCanvas = $("waterfall");
  const wfCtx = wfCanvas.getContext("2d");
  const wf = {
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

  function wfReset() {
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
  function wfPush(values) {
    if (!wf.data || state.layout === "spectrum") return;
    wf.head = (wf.head - 1 + wf.rows) % wf.rows;
    wf.data.set(values, wf.head * wf.bins);
    wfColorRow(wf.head);
    wf.filled = Math.min(wf.filled + 1, wf.rows);
    wf.dirty = true;
  }

  function wfRecolor() {
    if (!wf.data) return;
    for (let r = 0; r < wf.rows; r++) wfColorRow(r);
    wf.dirty = true;
  }

  function wfResize() {
    const dpr = window.devicePixelRatio || 1;
    const r = wfCanvas.getBoundingClientRect();
    wf.W = r.width; wf.H = r.height;
    wfCanvas.width = Math.round(wf.W * dpr);
    wfCanvas.height = Math.round(wf.H * dpr);
    wfCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    wf.dirty = true;
  }
  new ResizeObserver(wfResize).observe(wfCanvas);

  // Same horizontal mapping as the spectrum, so columns line up; frequency
  // labels only when the spectrum (which has them) is hidden.
  const wfPad = () => ({ l: PAD.l, r: PAD.r, t: state.layout === "both" ? 4 : PAD.t, b: state.layout === "waterfall" ? PAD.b : 8 });
  const wfXOf = (f) => { const p = wfPad(); return p.l + (Math.log10(f) - state.logMin) / (state.logMax - state.logMin) * (wf.W - p.l - p.r); };
  const wfFOfX = (x) => { const p = wfPad(); return 10 ** (state.logMin + (x - p.l) / (wf.W - p.l - p.r) * (state.logMax - state.logMin)); };

  function drawWaterfall() {
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

  function wfHoverReadout() {
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

  // A bug in any overlay must not freeze the analyzer: log it (once a few
  // seconds) and keep animating.
  let lastDrawError = 0;
  function loop() {
    requestAnimationFrame(loop);
    try {
      if (state.dirty) { state.dirty = false; draw(); }
      if (wf.dirty && state.layout !== "spectrum") { wf.dirty = false; drawWaterfall(); wfHoverReadout(); }
    } catch (e) {
      ctx.restore();  // draw() may have stopped inside its clip
      if (performance.now() - lastDrawError > 5000) { console.error("draw failed:", e); lastDrawError = performance.now(); }
    }
  }

  setInterval(() => {
    state.fps = state.frames; state.frames = 0;
    if (ws && ws.readyState === WebSocket.OPEN) {
      // Connected to the server but no frames: the audio device is unplugged or silent.
      $("conn").textContent = state.fps ? `live · ${state.fps} fps` : "no audio (device disconnected?)";
      $("conn").classList.toggle("ok", state.fps > 0);
    }
  }, 1000);

  resize();
  connect();
  pollVocal();
  requestAnimationFrame(loop);
})();
