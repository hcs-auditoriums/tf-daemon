// A channel's gain, gate, compressor and sends, as on TF StageMix's INPUT / GATE / COMP / SEND
// TO screens: one panel over the plot with a tab per screen (openStrip), opened from the
// tool row at the top of the console sidebar (stripTools: IN, EQ, GATE, COMP, SEND, each lit
// when that part is on, as StageMix's tool buttons; EQ shows the EQ on the spectrum). Data and writes: /api/tf5/strip/<usb> (console/strip.py).
// What the desk doesn't give yet is simulated in demo mode, marked so, and on a real desk
// shown as not available (docs/desk-coverage.md).

import { dynGraph } from "./dyngraph.js";
import { $, el, loadStr, save, state } from "./shared.js";

const TABS = [["input", "Input"], ["gate", "Gate"], ["comp", "Comp"], ["sends", "Sends"]];
let data = null;        // GET /api/tf5/strip/<usb> for state.input
let tab = "input";
let built = null;       // signature of what the panel shows; rebuilt when it changes
const refs = new Map(); // "section.key" (sends: "sends.<mix>.key") -> {set(value)}
const panel = $("strip-panel");
const PHONE = matchMedia("(max-width: 720px), (max-height: 500px)");   // style.css's phone rules
let picked = loadStr("tf.stripTab", "eq", ["input", "eq", "gate", "comp", "sends"]);   // a phone's tab (below)

// ------------------------------------------------------------------ data

let timer = null, loading = false;
async function load() {
  clearTimeout(timer);
  if (!loading) {
    loading = true;
    const usb = state.input;
    try {
      const r = await fetch(`/api/tf5/strip/${usb}`);
      if (r.ok && usb === state.input) { data = await r.json(); render(); }
      else if (r.status === 404) data = null;
    } catch { /* server restarting: try again */ } finally { loading = false; }
  }
  timer = setTimeout(load, panel.hidden ? 4000 : 1000);   // changes made at the desk show (the tools' lights)
}
// Another input shown: its strip at once.
export function stripInputChanged() { data = null; render(); load(); }

// Writes: one in flight per section, the latest values merged into the next.
const queue = new Map();   // "section|mix" -> changes
let busy = false;
function write(section, changes, mix) {
  const k = `${section}|${mix ?? ""}`;
  queue.set(k, { ...(queue.get(k) || {}), ...changes });
  if (section === "sends") {
    const row = data.sends.mixes.find((m) => m.mix === mix);
    if (row) Object.assign(row, changes);
  } else Object.assign(data[section].values, changes);
  update();
  flush();
}
async function flush() {
  if (busy || !queue.size) return;
  const [k, changes] = queue.entries().next().value;
  queue.delete(k);
  const [section, mix] = k.split("|");
  busy = true;
  try {
    const r = await fetch(`/api/tf5/strip/${state.input}`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ section, changes, mix: mix === "" ? undefined : Number(mix), confirm: true }),
    });
    const d = await r.json();
    note(d.ok ? "" : d.error || "Not changed.");
  } catch { note("Server unreachable."); } finally { busy = false; }
  if (queue.size) flush(); else load();
}
function note(text) { const n = panel.querySelector(".sp-error"); if (n) { n.textContent = text; n.hidden = !text; } }

// ------------------------------------------------------------------ controls

const spec = (section, key) => data.specs[section === "sends" ? "send" : section].find((s) => s.key === key);
const fmt = (s, v) => {
  if (s.kind === "choice") return s.key === "ratio" ? `${v === "inf" ? "∞" : v}:1` : String(v);
  if (v === null || v === undefined) return "−∞";
  if (s.key === "pan") return v === 0 ? "C" : v < 0 ? `L${-v}` : `R${v}`;
  const n = Math.abs(v) >= 1000 && s.unit === "ms" ? `${+(v / 1000).toFixed(2)} s` : `${v > 0 && s.unit === "dB" ? "+" : ""}${+v.toFixed(2)}${s.unit && s.unit !== "ms" ? ` ${s.unit}` : " ms"}`;
  return s.unit ? n : String(v);
};
// Slider position 0..1000 <-> value (log for times; choices by index; the send's left end is −∞).
function toPos(s, v) {
  if (s.kind === "choice") return s.choices.indexOf(v);
  if (v === null) return 0;
  if (s.log) return Math.round(1000 * Math.log(v / s.lo) / Math.log(s.hi / s.lo));
  return Math.round(1000 * (v - s.lo) / (s.hi - s.lo));
}
function fromPos(s, p) {
  if (s.kind === "choice") return s.choices[p];
  if (s.default === null && p === 0) return null;
  const v = s.log ? s.lo * (s.hi / s.lo) ** (p / 1000) : s.lo + (s.hi - s.lo) * p / 1000;
  return Math.round(v / s.step) * s.step;
}

function slider(section, key, onSet, label) {
  const s = spec(section, key), row = el("label", "sp-row");
  const val = el("span", "sp-val"), inp = el("input");
  inp.type = "range"; inp.min = "0"; inp.max = String(s.kind === "choice" ? s.choices.length - 1 : 1000);
  inp.setAttribute("aria-label", label || s.label);
  inp.oninput = () => onSet(fromPos(s, Number(inp.value)));
  row.append(el("span", "sp-name", label || s.label), val, inp);
  return { el: row, set: (v) => { if (document.activeElement !== inp) inp.value = String(toPos(s, v)); val.textContent = fmt(s, v); } };
}
function toggle(text, onSet, cls = "") {
  const b = el("button", `sp-toggle ${cls}`, text);
  b.type = "button";
  let on = false;
  b.onclick = () => onSet(!on);
  return { el: b, set: (v) => { on = !!v; b.setAttribute("aria-pressed", String(on)); } };
}
function choices(section, key) {
  const s = spec(section, key), box = el("div", "sp-seg");
  box.setAttribute("role", "group"); box.setAttribute("aria-label", s.label);
  const bs = s.choices.map((c) => { const b = el("button", "sp-toggle", String(c)); b.type = "button"; b.onclick = () => write(section, { [key]: c }); box.append(b); return b; });
  const row = el("div", "sp-row"); row.append(el("span", "sp-name", s.label), box);
  return { el: row, set: (v) => bs.forEach((b, i) => b.setAttribute("aria-pressed", String(s.choices[i] === v))) };
}

// ------------------------------------------------------------------ the tabs

function section(name) {
  const box = [], sec = data[name];
  const add = (k, c) => { refs.set(`${name}.${k}`, c); box.push(c.el); };
  const set = (k) => (v) => write(name, { [k]: v });
  if (name === "input") {
    const top = el("div", "sp-switches");
    const p48 = toggle("+48V", (v) => {
      if (confirm(`Turn phantom power ${v ? "on" : "off"} for ${data.source}? Bring its fader down first.`)) write("input", { phantom: v });
    }, "danger");
    const ph = toggle("Ø", set("phase"));
    ph.el.title = "Phase invert";
    refs.set("input.phantom", p48); refs.set("input.phase", ph);
    top.append(p48.el, ph.el);
    box.push(top);
    add("gain", slider("input", "gain", set("gain"), "Gain"));
    add("digital_gain", slider("input", "digital_gain", set("digital_gain")));
  } else if (name === "gate" || name === "comp") {
    const top = el("div", "sp-switches");
    const on = toggle(name === "gate" ? "GATE" : "COMP", set("on"));
    refs.set(`${name}.on`, on); top.append(on.el);
    if (name === "comp") { const k = toggle("1-knob", set("one_knob")); refs.set("comp.one_knob", k); top.append(k.el); }
    box.push(top);
    const knob = name === "comp" && sec.values.one_knob;
    if (knob) add("level", slider("comp", "level", set("level"), "Amount"));
    else {
      const gr = dynGraph(name, name === "comp" ? spec("comp", "ratio").choices : [], (c) => write(name, c));
      refs.set(`${name}.graph`, { el: gr.el, set: () => gr.draw(sec.values) });
      box.push(gr.el);
      const keys = name === "gate" ? ["threshold", "range", "attack", "hold", "decay"] : ["threshold", "ratio", "attack", "release", "gain"];
      for (const k of keys) add(k, slider(name, k, set(k)));
      if (name === "comp") add("knee", choices("comp", "knee"));
    }
  } else {
    // SEND TO: each mix (a stereo pair once): ON, PRE, level, and pan for a pair.
    const mixes = sec.mixes;
    for (let i = 0; i < mixes.length; i++) {
      const m = mixes[i], pair = mixes[i + 1] && mixes[i + 1].mix === m.mix + 1 && mixes[i + 1].name === m.name;
      const targets = pair ? [m.mix, m.mix + 1] : [m.mix];
      const w = (c) => targets.forEach((t) => write("sends", c, t));
      const row = el("div", "sp-send");
      const head = el("div", "sp-sendhead");
      const on = toggle("ON", (v) => w({ on: v }));
      head.append(el("span", "sp-name", m.name), on.el);
      refs.set(`sends.${m.mix}.on`, on);
      if (sec.pre_pan) { const pre = toggle("PRE", (v) => w({ pre: v })); head.append(pre.el); refs.set(`sends.${m.mix}.pre`, pre); }
      row.append(head);
      const lv = slider("sends", "level", (v) => w({ level: v }), `${m.name} level`);
      lv.el.querySelector(".sp-name").textContent = "";
      refs.set(`sends.${m.mix}.level`, lv); row.append(lv.el);
      if (pair && sec.pre_pan) { const pan = slider("sends", "pan", (v) => w({ pan: v }), `${m.name} pan`); refs.set(`sends.${m.mix}.pan`, pan); row.append(pan.el); }
      box.push(row);
      if (pair) i++;
    }
  }
  if (sec.writable === false) for (const n of box) n.querySelectorAll("input, button").forEach((x) => { x.disabled = true; });
  return box;
}

// ------------------------------------------------------------------ render

export function openStrip(which) {
  tab = which || tab;
  panel.hidden = false;
  built = null;
  render();
  load();
}
function close() { panel.hidden = true; paintTools(); }

function render() {
  paintTools();
  if (panel.hidden) return;
  const sec = data && data[tab];
  const sig = JSON.stringify([state.input, tab, !!data, sec && sec.available, sec && sec.writable,
                              tab === "comp" && sec && sec.values && sec.values.one_knob,
                              tab === "sends" && sec && (sec.mixes || []).map((m) => m.mix)]);
  if (sig !== built) {
    built = sig;
    refs.clear();
    const head = el("div", "sp-head");
    const tabs = el("div", "sp-tabs");
    tabs.setAttribute("role", "tablist");
    for (const [k, label] of TABS) {
      const b = el("button", "sp-tab", label);
      b.type = "button"; b.setAttribute("role", "tab"); b.setAttribute("aria-selected", String(k === tab));
      b.onclick = () => { tab = k; built = null; render(); };
      tabs.append(b);
    }
    const x = el("button", "sp-x", "×"); x.type = "button"; x.title = "Close"; x.onclick = close;
    head.append(tabs, x);
    const body = el("div", "sp-body");
    if (!data) body.append(el("p", "sp-note", "Reading…"));
    else if (!sec.available) body.append(el("p", "sp-note", `${data.source || "This input"}: ${sec.reason}`));
    else body.append(...section(tab));
    const foot = el("p", "sp-note sp-foot", data && data.simulated ? "Simulated: the desk protocol for this isn't done yet (sends are on the simulated desk)." : "");
    foot.hidden = !(data && data.simulated && tab !== "sends");
    const err = el("p", "sp-error"); err.hidden = true;
    panel.replaceChildren(head, body, err, foot);
  }
  update();
}

// Values into the controls already built (none rebuilt under a finger).
function update() {
  paintTools();
  if (!data || panel.hidden) return;
  for (const [k, c] of refs) {
    const [section, a, b] = k.split(".");
    if (section === "sends") { const m = data.sends.mixes.find((x) => x.mix === Number(a)); if (m) c.set(m[b]); }
    else if (a === "graph") c.set();
    else c.set(data[section].values[a]);
  }
}

document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !panel.hidden && !PHONE.matches) close(); });

// ------------------------------------------------------------------ the sidebar's tool row

// IN · EQ · GATE · COMP · SEND, as StageMix's tool buttons: the label lights green when that part is on
// (red: +48V on IN), selected or not; the one whose tab is open (or EQ, when it's on the spectrum) is filled.
const tools = el("div", "cp-tools");
tools.setAttribute("role", "toolbar");
tools.setAttribute("aria-label", "Channel settings");
const TOOLS = [["input", "IN", "Gain, +48V, phase"], ["eq", "EQ", "Show the EQ on the spectrum"],
               ["gate", "GATE", "Gate"], ["comp", "COMP", "Compressor"], ["sends", "SEND", "Sends to the mixes"]];
const toolBtn = new Map(TOOLS.map(([k, label, title]) => {
  const b = el("button", "cp-tool", label);
  b.type = "button";
  b.title = title;
  b.onclick = () => {
    if (PHONE.matches) pickTab(k);
    else if (k === "eq") { const cb = $("show-eq"); cb.checked = !cb.checked; cb.dispatchEvent(new Event("change")); paintTools(); }
    else if (!panel.hidden && tab === k) close();
    else openStrip(k);
  };
  tools.append(b);
  return [k, b];
}));
export function stripTools() { if (PHONE.matches) queueMicrotask(() => pickTab(shownTab())); paintTools(); return tools; }

// A phone: the tool row is a tab bar, one tab always shown under it (remembered), none closed.
// EQ shows the EQ on the plot (edited there) and, under the tabs, its summary and Profiles
// (console.js's EQ section; style.css shows it while the strip carries .eq-tab).
const eqHere = () => !!(state.eq && state.eq.index === state.input);
const shownTab = () => (picked === "eq" && !eqHere() ? "input" : picked);   // no EQ on this input
function pickTab(k) {
  if (k !== shownTab()) { picked = k; save("tf.stripTab", k); }
  const eq = k === "eq";
  $("console-panel").classList.toggle("eq-tab", eq);
  if (eq) {
    panel.hidden = true;
    const cb = $("show-eq");
    if (!cb.checked) { cb.checked = true; cb.dispatchEvent(new Event("change")); }
    paintTools();
  } else if (panel.hidden || tab !== k) openStrip(k);
}
PHONE.addEventListener("change", () => {
  if (PHONE.matches) pickTab(shownTab());
  else { $("console-panel").classList.remove("eq-tab"); close(); }
});
export function paintTools() {
  const eq = state.eq && state.eq.index === state.input && state.eq.eq;
  for (const [k, b] of toolBtn) {
    const sec = data && k !== "eq" ? data[k] : null;
    const lit = k === "eq" ? !!(eq && eq.on)
      : sec && sec.available && (k === "input" ? sec.values.phantom : k === "sends" ? sec.mixes.some((m) => m.on && m.level !== null) : sec.values.on);
    b.classList.toggle("lit", !!lit);
    b.classList.toggle("p48", k === "input" && !!lit);
    const on = PHONE.matches ? k === shownTab() : k === "eq" ? !!state.showEq : !panel.hidden && tab === k;
    b.setAttribute("aria-pressed", String(on));
    b.hidden = k === "eq" && !eq && !(state.eq && state.eq.index === state.input);
  }
}
