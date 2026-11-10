// The Desk page's FX and Delay cards (GET /api/tf5/fx, /api/tf5/delay; console/effects.py).
//
// FX 1/2 and the six insert FX: bypass (a click sets it), mix balance and the 64 raw parameter
// values (what each means depends on the plugin and is unknown, so: index and raw value). Typed
// changes wait for the unit's Apply button. Delay of the ST IN channels and matrices: on (a
// click), time and offset in ms (Apply). Every change is POST /api/tf5/param with "confirm":
// true, refused by the server unless tf5.fx_write / tf5.processing_write is on.

import { $ } from "../common.js";
import { clearDirty, dash, describe, el, markDirty, note, sendParams, setValue } from "./ui.js";

const units = new Map(); // "FX:0" -> {bypass, mix, values[], apply, revert, msg, plugin, returns}
const strips = new Map(); // "Matrix:0" -> {on, time, offset, link, apply, msg, name}
let refreshArea = () => {};

export function initEffects(refresh) { refreshArea = refresh; }

async function send(func, changes, msg, area, inputs) {
  note(msg, "setting…");
  const res = await sendParams(func, changes);
  note(msg, ...describe(res));
  if (res.ok) clearDirty(...inputs);
  refreshArea(area);
}

function numberInput(lim, onchange, extra = {}) {
  const i = el("input", { type: "number", min: lim[0], max: lim[1], step: 1, ...extra });
  i.addEventListener("input", () => { markDirty(i); onchange(); });
  return i;
}

function buildUnit(u, limits) {
  const msg = el("span", { class: "muted" });
  const parts = { msg, plugin: el("span", { class: "fx-plugin" }), returns: el("span", { class: "muted" }) };
  parts.bypass = el("input", {
    type: "checkbox",
    onchange: () => send("FX", [[`${u.path}/Bypass`, parts.bypass.checked ? 1 : 0]], msg, "fx", [parts.bypass]),
  });
  const dirty = () => { parts.apply.disabled = !units.get(u.path).writable; };
  parts.mix = numberInput(limits.mix_balance, dirty, { "aria-label": `${u.label} mix balance` });
  parts.values = u.values.map((_, i) => numberInput(limits.value, dirty, { "aria-label": `${u.label} parameter ${i}` }));
  parts.apply = el("button", { type: "button", class: "primary", disabled: true, onclick: () => applyUnit(u.path) }, "Apply");
  parts.revert = el("button", {
    type: "button",
    onclick: () => { clearDirty(parts.mix, ...parts.values); parts.apply.disabled = true; refreshArea("fx", true); },
  }, "Revert");
  parts.node = el(
    "article",
    { class: "fx-unit", "data-path": u.path },
    el("div", { class: "fx-head" }, el("b", {}, u.label), parts.plugin, parts.returns),
    el("div", { class: "controls" },
      el("label", {}, parts.bypass, "Bypass"),
      el("label", {}, "Mix", parts.mix, "%"),
      parts.apply, parts.revert, msg),
    el("details", {}, el("summary", {}, "Parameters (raw values)"),
      el("div", { class: "fx-values" }, parts.values.map((inp, i) => el("label", {}, el("span", {}, `#${i}`), inp)))),
  );
  return parts;
}

function applyUnit(path) {
  const u = units.get(path);
  const changes = [];
  if (u.mix.dataset.dirty) changes.push([`${path}/MixBalance`, Number(u.mix.value)]);
  u.values.forEach((inp, i) => { if (inp.dataset.dirty) changes.push([`${path}/Param/Bank/Value:${i}`, Number(inp.value)]); });
  u.apply.disabled = true;
  if (changes.length) send("FX", changes, u.msg, "fx", [u.mix, ...u.values]);
}

export function renderFx(d) {
  const box = $("fx-units");
  $("fx-note").textContent = !d.subscribed ? "The FX function isn't subscribed (tf5.functions)." : d.synced ? d.note : "waiting for the desk's FX data…";
  for (const u of d.units) {
    let p = units.get(u.path);
    if (!p) {
      p = buildUnit(u, d.limits);
      units.set(u.path, p);
      box.append(p.node);
    }
    p.writable = u.writable;
    p.plugin.textContent = `${dash(u.plugin)}${u.library ? ` · ${u.library}` : ""}`;
    p.returns.textContent = u.returns ? `returns: ${u.returns.map((r) => r.name || r.strip).join(" / ")}` : "";
    setValue(p.bypass, u.bypass === 1);
    setValue(p.mix, u.mix_balance);
    u.values.forEach((v, i) => setValue(p.values[i], v));
    for (const i of [p.bypass, p.mix, ...p.values]) {
      i.disabled = !u.writable;
      i.title = u.writable ? "" : `read-only: ${d.switch} is off`;
    }
  }
}

function buildStrip(s, limits, scale) {
  const msg = el("span", { class: "muted" });
  const parts = { msg, name: el("td", {}), link: el("td", { class: "num" }) };
  const dirty = () => { parts.apply.disabled = !strips.get(s.strip).writable; };
  const ms = (lim) => [lim[0] / scale, lim[1] / scale];
  parts.on = el("input", {
    type: "checkbox",
    "aria-label": `${s.label} delay on`,
    onchange: () => send("Mixing", [[`${s.strip}/Delay/On`, parts.on.checked ? 1 : 0]], msg, "delay", [parts.on]),
  });
  parts.time = numberInput(ms(limits.time), dirty, { step: 0.001, "aria-label": `${s.label} delay time` });
  parts.offset = numberInput(ms(limits.offset), dirty, { step: 0.001, "aria-label": `${s.label} delay offset` });
  parts.apply = el("button", {
    type: "button",
    disabled: true,
    onclick: () => {
      const changes = [];
      if (parts.time.dataset.dirty) changes.push([`${s.strip}/Delay/Time`, Math.round(Number(parts.time.value) * scale)]);
      if (parts.offset.dataset.dirty) changes.push([`${s.strip}/Delay/TimeOffset`, Math.round(Number(parts.offset.value) * scale)]);
      parts.apply.disabled = true;
      if (changes.length) send("Mixing", changes, msg, "delay", [parts.time, parts.offset]);
    },
  }, "Apply");
  parts.node = el("tr", { "data-strip": s.strip },
    el("td", { class: "num" }, s.label), parts.name, el("td", {}, parts.on),
    el("td", {}, parts.time), el("td", {}, parts.offset), parts.link, el("td", {}, parts.apply, " ", msg));
  return parts;
}

export function renderDelay(d) {
  const body = $("delay-rows");
  for (const s of d.strips) {
    let p = strips.get(s.strip);
    if (!p) {
      p = buildStrip(s, d.limits, d.scale);
      strips.set(s.strip, p);
      body.append(p.node);
    }
    p.writable = s.writable;
    p.name.textContent = s.name;
    p.link.textContent = s.lr_link ? "linked" : "";
    setValue(p.on, s.on === 1);
    setValue(p.time, s.time_ms);
    setValue(p.offset, s.offset_ms);
    for (const i of [p.on, p.time, p.offset]) {
      i.disabled = !s.writable;
      i.title = s.writable ? "" : `read-only: ${d.switch} is off`;
    }
  }
}
