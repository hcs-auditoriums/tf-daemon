// The Config page's settings: every setting of config.toml as a control (a switch, a choice,
// a number or text), grouped by table, with what it does and its default. Each change is
// saved at once (PUT /api/system/setting); Reset takes the setting out of the file, back to
// its default. What the server sends is in tf_daemon/config_edit.py.

import { $, el } from "./common.js";

let hooks = null;      // {busy(): string|null (a reason not to save now), saved(text)}
let rows = new Map();  // key -> {row, control, setting, status}
const open = new Set(["common"]);   // the groups shown unfolded

async function put(key, value) {
  const r = await fetch("/api/system/setting", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key, value }),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `${r.status} ${r.statusText}`);
  return data;
}

const show = (v) => (Array.isArray(v) ? (v.length ? v.join(", ") : "none") : v === "" ? "empty" : String(v));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// ------------------------------------------------------------------ building

export function renderSettings(tables, common, h) {
  hooks = h;
  rows = new Map();
  const box = $("settings");
  const all = Object.fromEntries(tables.flatMap((t) => t.settings.map((s) => [s.key, s])));
  const groups = [{ name: "common", title: "Most used", doc: "", settings: common.map((k) => all[k]).filter(Boolean) }, ...tables];
  box.replaceChildren(...groups.map(group));
  filter($("find").value);
}

function group(t) {
  const d = el("details", "set-group");
  d.dataset.group = t.name;
  d.open = open.has(t.name);
  d.ontoggle = () => {   // remembered unless a search opened it
    if ($("find").value) return;
    if (d.open) open.add(t.name); else open.delete(t.name);
  };
  const sum = el("summary");
  sum.append(el("span", "set-title", t.title));
  if (t.doc) sum.append(el("span", "set-doc", t.doc));
  d.append(sum, ...t.settings.map((s) => row(s, t.name === "common")));
  return d;
}

// ``full``: named with its table (the "Most used" group mixes tables).
function row(s, full) {
  const r = el("div", "set-row");
  r.dataset.key = s.key;
  r.dataset.find = `${s.key} ${s.help}`.toLowerCase();
  const id = `set-${s.key.replace(".", "-")}-${rows.size}`;
  const label = el("label", "set-name", full ? s.key : s.key.split(".")[1]);
  label.htmlFor = id;
  const head = el("div", "set-head");
  head.append(label, el("span", "set-mark", "changed"));
  const text = el("div", "set-text");
  text.append(head);
  if (s.help) text.append(el("p", "set-help", s.help));
  const control = makeControl(s, id);
  const reset = el("button", "set-reset", "Reset");
  reset.type = "button";
  reset.onclick = () => save(s.key, null);
  const status = el("p", "set-status");
  const side = el("div", "set-control");
  side.append(control.node, reset);
  r.append(text, side, status);
  const entry = { row: r, control, setting: s, status, reset };
  (rows.get(s.key) || rows.set(s.key, []).get(s.key)).push(entry);
  paint(entry, s);
  return r;
}

// One control per kind: a switch, side-by-side buttons (a few choices) or a list (more), a
// number box, a text box (a list: items separated by commas).
function makeControl(s, id) {
  if (s.kind === "bool") {
    const box = el("input", "set-switch");
    box.type = "checkbox";
    box.id = id;
    box.setAttribute("role", "switch");
    box.onchange = () => save(s.key, box.checked);
    return { node: box, set: (v) => { box.checked = v; } };
  }
  if (s.choices.length && s.choices.length <= 4) {
    const seg = el("div", "set-seg");
    seg.setAttribute("role", "radiogroup");
    seg.id = id;
    const buttons = s.choices.map((c) => {
      const b = el("button", "", show(c));
      b.type = "button";
      b.setAttribute("role", "radio");
      b.onclick = () => save(s.key, c);
      seg.append(b);
      return [c, b];
    });
    return { node: seg, set: (v) => { for (const [c, b] of buttons) b.setAttribute("aria-checked", String(same(c, v))); } };
  }
  if (s.choices.length) {
    const sel = el("select");
    sel.id = id;
    for (const c of s.choices) sel.append(new Option(show(c), JSON.stringify(c)));
    sel.onchange = () => save(s.key, JSON.parse(sel.value));
    return { node: sel, set: (v) => { sel.value = JSON.stringify(v); } };
  }
  const inp = el("input", "set-input");
  inp.id = id;
  inp.type = s.kind === "int" || s.kind === "float" ? "number" : s.key.endsWith("password") ? "password" : "text";
  if (inp.type === "number") inp.step = "any";
  if (s.kind === "list") inp.placeholder = "items, separated by commas";
  inp.spellcheck = false;
  let shown = "";
  const commit = () => { if (inp.value !== shown) save(s.key, inp.value); };
  inp.addEventListener("change", commit);
  inp.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); commit(); }
    if (e.key === "Escape") { inp.value = shown; inp.blur(); }
  });
  return {
    node: inp,
    set: (v) => { shown = inp.value = s.kind === "list" ? v.join(", ") : String(v); },
  };
}

// A row shows the file's value; "changed" and Reset when the file sets it to something else
// than the default.
function paint(entry, s) {
  entry.setting = s;
  entry.control.set(s.value);
  const changed = s.set && !same(s.value, s.default);
  entry.row.classList.toggle("changed", changed);
  entry.reset.hidden = !s.set;
  entry.reset.title = `Back to the default: ${show(s.default)}`;
}

// ------------------------------------------------------------------ saving

async function save(key, value) {
  const entries = rows.get(key) || [];
  const busy = hooks.busy();
  if (busy) {
    for (const e of entries) { paint(e, e.setting); status(e, busy, true); }
    return;
  }
  for (const e of entries) status(e, "saving…");
  try {
    const r = await put(key, value);
    for (const t of r.settings) for (const s of t.settings) for (const e of rows.get(s.key) || []) paint(e, s);
    for (const e of entries) status(e, value === null ? "back to the default" : "saved");
    hooks.saved(r.text);
  } catch (err) {
    for (const e of entries) { paint(e, e.setting); status(e, err.message, true); }
  }
}

function status(e, text, bad = false) {
  e.status.textContent = text;
  e.status.classList.toggle("error", bad);
  clearTimeout(e.timer);
  if (!bad && text !== "saving…") e.timer = setTimeout(() => { e.status.textContent = ""; }, 2500);
}

// ------------------------------------------------------------------ finding

function filter(text) {
  const words = text.toLowerCase().split(/\s+/).filter(Boolean);
  for (const g of $("settings").querySelectorAll(".set-group")) {
    let any = false;
    for (const r of g.querySelectorAll(".set-row")) {
      const hit = words.every((w) => r.dataset.find.includes(w) || g.dataset.group.includes(w));
      r.hidden = !hit;
      any ||= hit;
    }
    g.hidden = !any || (words.length > 0 && g.dataset.group === "common");   // a search lists each setting once
    if (words.length) g.open = any; else g.open = open.has(g.dataset.group);
  }
}
$("find").addEventListener("input", (e) => filter(e.target.value));
