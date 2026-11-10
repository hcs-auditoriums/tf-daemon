// The console sidebar: the TF5 channel on the shown input, and its EQ graph.

import { eqAt, eqAvailable, eqHalf } from "./eq.js";
import { paintChannelColors } from "./controls.js";
import { profileBar } from "./profiles.js";
import { paintTools, stripTools } from "./strip.js";
import { $, C, el, loadInt, save, state } from "./shared.js";

// Sidebar with the TF5 channel feeding the shown input: name, on, fader,
// pan (the sends are in strip.js). With tf5.fader_write, an input channel's ON button and
// fader work: a click / drag here sets them on the desk (PUT /api/tf5/fader). The desk's state arrives over /ws/audio: a snapshot on
// connect ("tf5"), then each change as it happens on the desk ("tf5-changes");
// nothing arrives when tf5.host isn't set, and the sidebar stays hidden.
let tf5 = null;   // null = no TF5 module; {connected, channels, mixes} otherwise
state.showConsole = loadInt("tf.console", 1) === 1;

export function onConsoleSnapshot(msg) {
  tf5 = msg;
  panelSig = null;  // rebuild
  schedulePanel();
  paintChannelColors();
}

// The console's colour for a USB input's channel (CSS), or null when unknown.
export function channelColor(usb) {
  const ch = tf5?.channels?.find((c) => c.index === usb);
  return TF_COLORS[String(ch?.params["Label/Color"]?.value || "").toLowerCase()] || null;
}

export function onConsoleChanges(msg) {
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
  if (msg.changes.some((c) => c.p === "Label/Color")) paintChannelColors();
  schedulePanel();
}

// Many changes can arrive per frame while a fader moves: render once per frame.
let panelQueued = false;
export function schedulePanel() {
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
function posToDb(pos) {  // the inverse: null below the bottom
  if (pos <= 0.004) return null;
  if (pos >= 1) return LAW[0][0];
  for (let i = 1; i < LAW.length; i++) {
    const [d1, p1] = LAW[i - 1], [d0, p0] = LAW[i];
    if (pos >= p0) return d0 + (pos - p0) / (p1 - p0) * (d1 - d0);
  }
  return null;
}
const fmtDb = (db) => (db === null || db === undefined ? "−∞" : `${db > 0 ? "+" : ""}${db.toFixed(1)}`);
const fmtPan = (v) => (v === 0 ? "C" : v < 0 ? `L${-v}` : `R${v}`);




// A short fader (a tablet) has no room for every label: keep those at least a line apart,
// the most useful first, and hide the rest (their ticks stay).
const SCALE_KEEP = ["0", "-inf", "10", "-10", "-20", "-40", "-30", "-60"], SCALE_GAP = 12;
const scaleFit = new ResizeObserver(([entry]) => {
  const h = entry.contentRect.height, kept = [];
  const labels = [...entry.target.parentElement.querySelectorAll(".cp-scale span")];
  for (const key of SCALE_KEEP) {
    const t = labels.find((s) => s.dataset.db === key);
    if (!t) continue;
    const y = faderPos(key === "-inf" ? null : Number(key)) * h;
    t.hidden = kept.some((k) => Math.abs(k - y) < SCALE_GAP);
    if (!t.hidden) kept.push(y);
  }
});

// ------------------------------------------------------------------ fader / ON from the page

// Writable when the desk lets faders be written and this input carries an input channel
// (told with the channel's EQ, "tf5-eq").
function faderWritable() {
  return !!(state.eq && state.eq.index === state.input && state.eq.fader_writable);
}

// What a person just set here wins over the desk's (older) values while they drag, and for a
// moment after, so the cap doesn't jump back while the desk catches up.
let local = null;   // {usb, level?, on?, until}
let dragging = null;  // {startY, startPos, height, fine}
const HOLD_MS = 800;

function localFor(usb) {
  if (!local || local.usb !== usb) return null;
  if (!dragging && performance.now() > local.until) { local = null; return null; }
  return local;
}

function setLocal(usb, values) {
  local = { ...(local && local.usb === usb ? local : { usb }), ...values, until: performance.now() + HOLD_MS };
}

// One request at a time; while one is out, the newest value waits (a drag sends many).
let faderOut = null, faderBusy = false;
function sendFader(usb, body) {
  faderOut = { usb, body: { ...(faderOut && faderOut.usb === usb ? faderOut.body : {}), ...body } };
  if (!faderBusy) flushFader();
}

async function flushFader() {
  if (!faderOut) return;
  const { usb, body } = faderOut;
  faderOut = null;
  faderBusy = true;
  let error = null;
  try {
    const r = await fetch(`/api/tf5/fader/${usb}`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...body, confirm: true }),
    });
    const d = await r.json();
    if (!d.ok) error = d.error || "not set";
    else if (d.results.some((x) => !x.confirmed)) error = "the desk didn't confirm it";
  } catch { error = "server unreachable"; }
  faderBusy = false;
  if (refs && refs.faderErr) {
    refs.faderErr.textContent = error ? `Not set: ${error}` : "";
    refs.faderErr.hidden = !error;
  }
  if (error) local = null;  // show the desk's value again
  flushFader();
}

const dbToRaw = (db) => (db === null || db < -138 ? -32768 : Math.round(Math.min(db, 10) * 10) * 10);

function setLevel(ch, db) {
  const raw = dbToRaw(db);
  setLocal(ch.index, { level: raw <= -32768 ? null : raw / 100 });
  updatePanel(ch);
  sendFader(ch.index, { level: raw });
}

function currentLevel(ch) {
  const l = localFor(ch.index);
  return l && "level" in l ? l.level : ch.params["Fader/Level"]?.value ?? null;
}

function wireFader(ch, track) {
  track.tabIndex = 0;
  track.setAttribute("role", "slider");
  track.setAttribute("aria-label", "Fader (drag; arrow keys: 1 dB, with Shift 0.1 dB)");
  track.classList.add("writable");
  // Relative drag: a press doesn't jump the fader to the pointer (a live mic), it moves with it.
  track.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    track.focus();
    track.setPointerCapture(e.pointerId);
    dragging = {
      startY: e.clientY, startPos: faderPos(currentLevel(ch)), height: track.getBoundingClientRect().height,
      fine: e.shiftKey,
    };
  });
  track.addEventListener("pointermove", (e) => {
    if (!dragging || !track.hasPointerCapture(e.pointerId)) return;
    const scale = e.shiftKey ? 0.2 : 1;  // Shift: fine
    const pos = Math.min(1, Math.max(0, dragging.startPos + (dragging.startY - e.clientY) / dragging.height * scale));
    if (e.shiftKey !== dragging.fine) {  // re-anchor when Shift changes, so the cap doesn't jump
      Object.assign(dragging, { startY: e.clientY, startPos: faderPos(currentLevel(ch)), fine: e.shiftKey });
      return;
    }
    setLevel(ch, posToDb(pos));
  });
  const end = () => { if (dragging) { dragging = null; setLocal(ch.index, {}); } };
  track.addEventListener("pointerup", end);
  track.addEventListener("pointercancel", end);
  track.addEventListener("keydown", (e) => {
    const step = e.shiftKey ? 0.1 : 1;
    const now = currentLevel(ch);
    let next;
    if (e.key === "ArrowUp" || e.key === "ArrowRight") next = now === null ? -60 : now + step;
    else if (e.key === "ArrowDown" || e.key === "ArrowLeft") next = now === null || now <= -60 ? null : now - step;
    else return;
    e.preventDefault();
    setLevel(ch, next);
  });
}

function wireOn(ch, button) {
  button.onclick = () => {
    const l = localFor(ch.index);
    const isOn = (l && "on" in l ? l.on : ch.params["Fader/On"]?.value) !== 0;
    setLocal(ch.index, { on: isOn ? 0 : 1 });
    updatePanel(ch);
    sendFader(ch.index, { on: isOn ? 0 : 1 });
  };
}

// The sidebar is rebuilt only when its structure changes (another input, a
// send appearing, ...); otherwise values are updated in place, so a fader
// moving on the desk moves smoothly here (with a short CSS transition).
let panelSig = null, refs = null;

// Rebuild the sidebar from scratch on the next frame (e.g. the EQ section appeared).
export function rebuildPanel() {
  panelSig = null;
  schedulePanel();
}

function panelStructure(ch) {
  if (!tf5.connected || !ch || !ch.source) return JSON.stringify([state.input, tf5.connected, ch && ch.source]);
  const p = ch.params;
  return JSON.stringify([state.input, ch.source, !!eqAvailable, faderWritable(), p["Label/Name"]?.value, p["Label/Color"]?.value,
                         !!p["ToStereo/Pan"], !!p["Out/Balance"],
                         Object.keys(p).sort()]);
}

function updatePanel(ch) {
  const p = ch.params, v = (k) => p[k]?.value;
  const l = localFor(ch.index);
  const isOn = (l && "on" in l ? l.on : v("Fader/On")) !== 0;
  refs.on.className = `cp-on ${isOn ? "on" : "off"}${refs.onWritable ? " writable" : ""}`;
  refs.on.textContent = isOn ? "ON" : "OFF";
  if (refs.onWritable) refs.on.setAttribute("aria-pressed", String(isOn));
  const level = l && "level" in l ? l.level : v("Fader/Level"), pos = `${faderPos(level) * 100}%`;
  if (refs.track && refs.track.classList.contains("writable")) {
    refs.track.setAttribute("aria-valuetext", `${fmtDb(level)} dB`);
  }
  refs.cap.classList.toggle("dragging", !!dragging);
  refs.level.textContent = `${fmtDb(level)} dB`;
  refs.fill.style.height = pos;
  refs.cap.style.bottom = pos;
  if (refs.panKey && p[refs.panKey]) {
    const pan = v(refs.panKey);
    refs.panVal.textContent = fmtPan(pan);
    refs.panDot.style.left = `${(pan + 63) / 126 * 100}%`;
  }
  for (const [k, dd] of refs.extra) {
    const x = p[k];
    if (x) dd.textContent = `${x.value ?? "−∞"}${x.unit ? " " + x.unit : ""}`;
  }
}

export function renderPanel() {
  const panel = $("console-panel"), toggle = $("console-toggle"), btn = $("console-btn");
  toggle.hidden = btn.hidden = tf5 === null;
  btn.setAttribute("aria-pressed", String(state.showConsole));
  btn.title = state.showConsole ? "Hide the console channel" : "Show the console channel";
  panel.hidden = tf5 === null || !state.showConsole;
  if (panel.hidden) return;

  const ch = tf5.channels && tf5.channels.find((c) => c.index === state.input);
  const sig = panelStructure(ch);
  if (sig === panelSig && refs) {
    updatePanel(ch);
    return;
  }
  panelSig = sig;
  refs = { extra: new Map() };
  const items = [];
  if (!tf5.connected) {
    items.push(el("p", "cp-note", "TF5 not connected"));
  } else if (!ch || !ch.source) {
    items.push(el("p", "cp-note", `USB ${state.input + 1} is not on the console patch (tf5.usb_patch)`));
  } else {
    const p = ch.params, v = (k) => p[k]?.value;
    // A channel strip like TF StageMix's, top to bottom: its tool row (IN, EQ, GATE, COMP,
    // SEND: strip.js; the sends are on its Sends tab), EQ, pan, ON, fader, and the name in the channel's colour at
    // the foot (the desk's scribble strip).
    items.push(stripTools());
    const head = el("div", "cp-head");
    head.style.setProperty("--ch", TF_COLORS[String(v("Label/Color") || "").toLowerCase()] || "var(--border)");
    head.append(el("span", "cp-name", v("Label/Name") || ch.source), el("span", "cp-src", `${ch.source} · USB ${ch.index + 1}`));

    const writable = faderWritable();
    refs.onWritable = writable;
    refs.on = el(writable ? "button" : "div", "cp-on");
    if (writable) {
      refs.on.type = "button";
      refs.on.title = "Turn the channel on / off on the desk";
      wireOn(ch, refs.on);
    }

    const section = (title) => {
      const sec = el("div", "cp-section");
      const hd = el("div", "cp-sechead");
      const value = el("span", "cp-value");
      hd.append(el("span", "cp-label", title), value);
      sec.append(hd);
      return [sec, value];
    };

    // EQ (only with tf5.eq): the console's curve for this channel as a thumbnail, redrawn
    // whenever the desk pushes a change. A tap shows / hides it on the spectrum, full size.
    if (eqAvailable) {
      const eqSec = el("div", "cp-section");
      refs.eqCanvas = el("canvas", "cp-eq");
      refs.eqCanvas.setAttribute("role", "button");
      refs.eqCanvas.setAttribute("aria-label", "Show the EQ on the spectrum");
      refs.eqCanvas.tabIndex = 0;
      const toggleEq = () => { const cb = $("show-eq"); cb.checked = !cb.checked; cb.dispatchEvent(new Event("change")); };
      refs.eqCanvas.onclick = toggleEq;
      refs.eqCanvas.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleEq(); } };
      refs.eqInfo = el("div", "cp-eqinfo");
      const prof = el("details", "cp-profmenu");
      prof.append(el("summary", "", "Profiles"), profileBar());
      const info = el("div", "cp-eqrow");
      info.append(refs.eqInfo, prof);  // its summary, and Profiles opening over the strip
      eqSec.append(refs.eqCanvas, info);
      items.push(eqSec);
    }

    const panRow = [];

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
        t.dataset.db = d === null ? "-inf" : d;
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
    scaleFit.disconnect();
    scaleFit.observe(track);
    refs.track = track;
    if (writable) {
      wireFader(ch, track);
      refs.faderErr = el("p", "cp-note cp-err");
      refs.faderErr.hidden = true;
      faderSec.append(refs.faderErr);
    }
    faderSec.classList.add("cp-fadersec");

    // Pan (balance for ST): a full-width, centre-detented bar.
    const panKey = p["ToStereo/Pan"] ? "ToStereo/Pan" : "Out/Balance";
    if (p[panKey]) {
      const panSec = el("div", "cp-panrow");
      const bar = el("div", "cp-pan");
      const dot = el("span");
      bar.append(dot);
      const panVal = el("span", "cp-value");
      Object.assign(refs, { panKey, panVal, panDot: dot });
      panSec.append(el("span", "cp-label", panKey === "Out/Balance" ? "Bal" : "Pan"), bar, panVal);
      panRow.push(panSec);
    }

    items.push(...panRow, refs.on, faderSec);
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
      items.splice(items.indexOf(refs.on), 0, dl);  // with the other readings, above ON
    }
    items.push(head);
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

export function drawPanelEq() {
  const cv = refs && refs.eqCanvas;
  if (!cv || !cv.isConnected) return;
  const d = state.eq && state.eq.index === state.input ? state.eq : null;
  const eq = d && d.eq;
  paintTools();   // the EQ tool's light
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

// Two controls, one setting: the Show > Console checkbox and the icon on the plot.
function setShowConsole(on) {
  state.showConsole = on;
  $("show-console").checked = on;
  save("tf.console", on ? 1 : 0);
  renderPanel();
}
$("show-console").checked = state.showConsole;
$("show-console").onchange = (e) => setShowConsole(e.target.checked);
$("console-btn").onclick = () => setShowConsole(!state.showConsole);
