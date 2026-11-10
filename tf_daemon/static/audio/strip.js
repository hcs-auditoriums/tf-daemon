// A channel's input, 1-knob EQ, gate, compressor and sends, as on TF StageMix's INPUT / EQ /
// GATE / COMP / SEND TO screens: one panel over the plot with a tab per screen (openStrip),
// opened from the tool row at the top of the console sidebar (stripTools: IN, EQ, GATE, COMP,
// SEND, each lit when that part is on, as StageMix's tool buttons; EQ shows the EQ on the
// spectrum). Data and writes: /api/tf5/strip/<usb> (console/strip.py, strip_desk.py): input,
// EQ, gate and comp are the desk's own (TF Editor protocol), pushed over /ws/audio as they
// change on the desk (onStripPush); each value the desk won't let us change here is shown
// disabled with the reason. Every change is a person's, sent with "confirm"; the panel says
// whether the desk echoed it.

import { dynGraph } from "./dyngraph.js";
import { $, el, state } from "./shared.js";

const TABS = [["input", "Input"], ["eq", "EQ"], ["gate", "Gate"], ["comp", "Comp"], ["sends", "Sends"]];
let data = null;        // GET /api/tf5/strip/<usb> for state.input
let tab = "input";
let built = null;       // signature of what the panel shows; rebuilt when it changes
const refs = new Map(); // "section.key" (sends: "sends.<mix>.key") -> {set(value)}
const panel = $("strip-panel");

// ------------------------------------------------------------------ data

let timer = null, loading = false;
async function load() {
  clearTimeout(timer);
  if (!loading) {
    loading = true;
    const usb = state.input;
    try {
      const r = await fetch(`/api/tf5/strip/${usb}`);
      if (r.ok && usb === state.input && !busy && !queue.size) { data = await r.json(); render(); }
      else if (r.status === 404) data = null;
    } catch { /* server restarting: try again */ } finally { loading = false; }
  }
  timer = setTimeout(load, panel.hidden ? 4000 : 1500);   // the sends (RCP) and a missed push
}
// Another input shown: its strip at once.
export function stripInputChanged() { data = null; said = ["", false]; render(); load(); }
// The desk changed (/ws/audio "tf5-strip"): taken unless an edit of ours is on its way.
export function onStripPush(msg) {
  if (msg.index !== state.input || busy || queue.size) return;
  data = msg;
  render();
}

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
  status("Sending to the desk…");
  try {
    const r = await fetch(`/api/tf5/strip/${state.input}`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ section, changes, mix: mix === "" ? undefined : Number(mix), confirm: true }),
    });
    const d = await r.json();
    if (!d.ok) status(d.error || "Not changed.", true);
    else if (d.confirmed) {
      if (section !== "sends" && data && data[section].values) Object.assign(data[section].values, d.applied);
      const missed = Object.keys(d.confirmed).filter((key) => !d.confirmed[key]).map((key) => spec(section, key).label);
      status(missed.length ? `Sent; the desk didn't confirm ${missed.join(", ")}.` : "Confirmed by the desk.", missed.length > 0);
    } else status("");
  } catch { status("Server unreachable.", true); } finally { busy = false; }
  if (queue.size) flush(); else load();
}
let said = ["", false];   // the last write's report; kept when the panel is rebuilt
function status(text, bad = false) {
  said = [text, bad];
  const n = panel.querySelector(".sp-status");
  if (!n) return;
  n.textContent = text; n.hidden = !text; n.classList.toggle("bad", bad);
}

// ------------------------------------------------------------------ controls

const spec = (section, key) => data.specs[section === "sends" ? "send" : section].find((s) => s.key === key);
const fmt = (s, v) => {
  if (s.kind === "choice") return s.key === "ratio" ? `${v === "inf" ? "∞" : v}:1` : String(v);
  if (v === null || v === undefined) return "−∞";
  if (s.key === "pan") return v === 0 ? "C" : v < 0 ? `L${-v}` : `R${v}`;
  const n = (x) => String(+x.toFixed(2));
  if (s.unit === "ms") return v >= 1000 ? `${n(v / 1000)} s` : `${n(v)} ms`;
  if (s.unit === "Hz") return v >= 1000 ? `${n(v / 1000)} kHz` : `${Math.round(v)} Hz`;
  if (s.unit.startsWith("dB")) return `${v > 0 ? "+" : ""}${n(v)} ${s.unit}`;
  return s.unit ? `${n(v)} ${s.unit}` : n(v);
};
// Slider position 0..1000 <-> value (log for times; choices by index; a −∞ spec's left end is −∞).
const hasInf = (s) => s.inf || s.default === null;
function toPos(s, v) {
  if (s.kind === "choice") return s.choices.indexOf(v);
  if (v === null) return 0;
  if (s.log) return Math.round(1000 * Math.log(v / s.lo) / Math.log(s.hi / s.lo));
  return Math.round(1000 * (v - s.lo) / (s.hi - s.lo));
}
function fromPos(s, p) {
  if (s.kind === "choice") return s.choices[p];
  if (hasInf(s) && p === 0) return null;
  const v = s.log ? s.lo * (s.hi / s.lo) ** (p / 1000) : s.lo + (s.hi - s.lo) * p / 1000;
  return Math.min(s.hi, Math.max(s.lo, Math.round(v / s.step) * s.step));
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
function choices(section, key, extra = "") {
  const s = spec(section, key), box = el("div", "sp-seg");
  box.setAttribute("role", "group"); box.setAttribute("aria-label", s.label);
  const bs = s.choices.map((c) => { const b = el("button", "sp-toggle", String(c)); b.type = "button"; b.onclick = () => write(section, { [key]: c }); box.append(b); return b; });
  const row = el("div", "sp-row"); row.append(el("span", "sp-name", s.label + extra), box);
  return { el: row, set: (v) => bs.forEach((b, i) => b.setAttribute("aria-pressed", String(s.choices[i] === v))) };
}
function shown(section, key) {   // a value the desk reports that isn't changed from here (e.g. a type)
  const val = el("span", "sp-val"), row = el("div", "sp-row");
  row.append(el("span", "sp-name", spec(section, key).label), val);
  return { el: row, set: (v) => { val.textContent = v || "—"; } };
}
function lock(node, reason) {
  node.querySelectorAll("input, button").forEach((x) => { x.disabled = true; });
  if (node.matches("input, button")) node.disabled = true;
  node.title = reason;
}

// ------------------------------------------------------------------ the tabs

function section(name) {
  const box = [], sec = data[name];
  const has = (k) => name === "sends" || k in sec.values;
  // A control for each value the desk gave; disabled (with the reason) where it can't be changed.
  const add = (k, c, into = box) => {
    if (!has(k)) return;
    refs.set(`${name}.${k}`, c);
    if (sec.locked && sec.locked[k]) lock(c.el, sec.locked[k]);
    into.push ? into.push(c.el) : into.append(c.el);
  };
  const set = (k) => (v) => write(name, { [k]: v });
  const switches = () => { const row = el("div", "sp-switches"); box.push(row); return row; };
  if (name === "input") {
    add("source", choices("input", "source", sec.port ? ` (${sec.port})` : ""));
    const top = switches();
    const phantom = (k, what) => toggle(k === "phantom" ? "+48V" : "+48V master", (v) => {
      if (confirm(`Turn ${what} ${v ? "on" : "off"}? Bring the faders down first.`)) write("input", { [k]: v });
    }, "danger");
    add("phantom", phantom("phantom", `phantom power for ${data.source}`), top);
    const ph = toggle("Ø", set("phase"));
    ph.el.title = "Phase invert";
    add("phase", ph, top);
    add("phantom_master", phantom("phantom_master", "the desk's phantom master"), top);
    add("gain", slider("input", "gain", set("gain"), "Gain"));
    add("digital_gain", slider("input", "digital_gain", set("digital_gain")));
  } else if (name === "eq") {
    add("type", shown("eq", "type"));
    add("one_knob", toggle("1-knob EQ", set("one_knob")), switches());
    add("mode", choices("eq", "mode"));
    add("depth", slider("eq", "depth", set("depth")));
  } else if (name === "gate" || name === "comp") {
    const top = switches();
    add("on", toggle(name === "gate" ? "GATE" : "COMP", set("on")), top);
    if (name === "comp") add("one_knob", toggle("1-knob", set("one_knob")), top);
    add("type", shown(name, "type"));
    if (name === "comp" && sec.values.one_knob) add("level", slider("comp", "level", set("level"), "Amount"));
    else {
      const gr = dynGraph(name, name === "comp" ? spec("comp", "ratio").choices : [], (c) => write(name, c));
      refs.set(`${name}.graph`, { el: gr.el, set: () => gr.draw(sec.values) });
      box.push(gr.el);
      const keys = name === "gate" ? ["threshold", "range", "attack", "hold", "decay"] : ["threshold", "ratio", "attack", "release", "gain"];
      for (const k of keys) add(k, slider(name, k, set(k)));
      if (name === "comp") add("knee", choices("comp", "knee"));
    }
    if (name === "comp" && has("ds_threshold")) {   // the de-esser's settings (used when the comp type is a de-esser)
      const ds = el("details", "sp-more");
      ds.append(el("summary", "", "De-esser"));
      for (const k of ["ds_threshold", "ds_freq", "ds_q"]) add(k, slider("comp", k, set(k)), ds);
      add("ds_type", shown("comp", "ds_type"), ds);
      box.push(ds);
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
    if (sec.writable === false) for (const n of box) lock(n, sec.reason || "");
  }
  // What the desk didn't give for this channel (e.g. no head amp on a USB source), once per reason.
  const absent = new Map();
  for (const [k, why] of Object.entries(sec.absent || {})) absent.set(why, [...(absent.get(why) || []), spec(name, k).label]);
  for (const [why, labels] of absent) box.push(el("p", "sp-note", `${labels.join(", ")}: ${why}`));
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
                              sec && sec.values && Object.keys(sec.values), sec && sec.locked, sec && sec.port,
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
    const foot = el("p", "sp-note sp-foot", data && data.simulated ? "Demo: a simulated desk (sends' pre / pan are kept by the app)." : "");
    foot.hidden = !(data && data.simulated);
    const st = el("p", "sp-note sp-status");
    panel.replaceChildren(head, body, st, foot);
    status(...said);
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

document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !panel.hidden) close(); });

// ------------------------------------------------------------------ the sidebar's tool row

// IN · EQ · GATE · COMP · SEND, as StageMix's tool buttons: a dot lights when that part is on
// (+48V for IN); the one whose tab is open (or EQ, when it's on the spectrum) is outlined.
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
    if (k === "eq") { const cb = $("show-eq"); cb.checked = !cb.checked; cb.dispatchEvent(new Event("change")); paintTools(); }
    else if (!panel.hidden && tab === k) close();
    else openStrip(k);
  };
  tools.append(b);
  return [k, b];
}));
export function stripTools() { paintTools(); return tools; }
export function paintTools() {
  const eq = state.eq && state.eq.index === state.input && state.eq.eq;
  for (const [k, b] of toolBtn) {
    const sec = data && k !== "eq" ? data[k] : null;
    const lit = k === "eq" ? !!(eq && eq.on)
      : sec && sec.available && (k === "input" ? sec.values.phantom : k === "sends" ? sec.mixes.some((m) => m.on && m.level !== null) : sec.values.on);
    b.classList.toggle("lit", !!lit);
    b.classList.toggle("p48", k === "input" && !!lit);
    b.setAttribute("aria-pressed", String(k === "eq" ? !!state.showEq : !panel.hidden && tab === k));
    b.hidden = k === "eq" && !eq && !(state.eq && state.eq.index === state.input);
  }
}
