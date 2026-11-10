// The input picker, the header menus (open / close) and the spectrum traces with Reset.

import { channelTile, renderPanel } from "./console.js";
import { eqEditReset } from "./eqedit.js";
import { autoReset } from "./range.js";
import { $, loadInt, save, state } from "./shared.js";
import { send } from "./socket.js";
import { stripInputChanged } from "./strip.js";
import { renderVocal } from "./vocal.js";
import { wfReset } from "./waterfall.js";

// The channel bar, like the TF desk and StageMix: banks of BANK inputs (1-8, 9-16, ...);
// the bank's inputs fill the bar as big tiles (number, name, the console's colour). Any
// input is two taps away and nothing scrolls; ‹ › step through them one by one. The
// inputs listed in config.toml are always analyzed; the others while shown.
const BANK = 8;
let bank = 0;  // the bank shown (it follows the selected input; a bank tab just shows it)

export function buildChannelPicker() {
  const nav = $("channels"), banks = $("banks");
  nav.replaceChildren();
  banks.replaceChildren();
  const list = state.meta.channels;
  for (let start = 0; start < list.length; start += BANK) {
    const b = document.createElement("button");
    b.type = "button";
    const end = Math.min(start + BANK, list.length);
    b.textContent = `${list[start].index + 1}–${list[end - 1].index + 1}`;
    b.title = `Inputs ${b.textContent}`;
    b.dataset.bank = start / BANK;
    b.onclick = () => showBank(start / BANK);
    banks.appendChild(b);
  }
  for (const ch of list) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = `ch-chip${ch.label === `Ch ${ch.index + 1}` ? " unnamed" : ""}`;
    b.textContent = ch.label;
    b.title = `Input ${ch.index + 1}`;
    b.dataset.input = ch.index;
    b.dataset.num = ch.index + 1;
    b.onclick = () => selectInput(ch.index);
    nav.appendChild(b);
  }
  paintChannelTiles();
  showBank(bankOf(state.input));
}

const bankOf = (input) => Math.max(0, Math.floor((state.meta?.channels || []).findIndex((c) => c.index === input) / BANK));

function showBank(n) {
  bank = n;
  const chips = [...$("channels").querySelectorAll(".ch-chip")];
  chips.forEach((b, i) => { b.hidden = Math.floor(i / BANK) !== n; });
  for (const b of $("banks").querySelectorAll("button")) b.setAttribute("aria-pressed", String(Number(b.dataset.bank) === n));
}

// What the desk says about each tile's channel: its colour (the top edge), on or off (an off
// channel is dimmed), its fader (a bar along the bottom), and a dot while a ring comes from it
// (state.ringingInputs, from rings.js). Once a frame at most: a moving fader sends many changes.
let tilesQueued = false;
export function paintChannelTiles() {
  if (tilesQueued) return;
  tilesQueued = true;
  requestAnimationFrame(() => {
    tilesQueued = false;
    for (const b of $("channels").querySelectorAll(".ch-chip")) {
      const usb = Number(b.dataset.input), t = channelTile(usb), ring = state.ringingInputs?.get(usb);
      if (t.color) b.style.setProperty("--ch", t.color); else b.style.removeProperty("--ch");
      if (t.pos === null) b.style.removeProperty("--fader"); else b.style.setProperty("--fader", `${(t.pos * 100).toFixed(1)}%`);
      b.classList.toggle("off", t.on === false);
      if (ring) b.dataset.ring = ring; else delete b.dataset.ring;
      const name = state.meta.channels.find((c) => c.index === usb)?.label ?? "";
      b.title = `Input ${usb + 1}${t.on === false ? " · off" : ""}${ring ? ` · ${ring === "feedback" ? "feedback" : "ring"} from ${name}` : ""}`;
    }
  });
}

export function markSelected(index) {
  for (const b of $("channels").querySelectorAll(".ch-chip")) {
    const on = Number(b.dataset.input) === index;
    b.setAttribute("aria-pressed", String(on));
  }
  if (bankOf(index) !== bank) showBank(bankOf(index));
}

function step(by) {
  const list = state.meta?.channels || [];
  const i = list.findIndex((c) => c.index === state.input);
  const next = list[Math.min(list.length - 1, Math.max(0, i + by))];
  if (next && next.index !== state.input) selectInput(next.index);
}
$("ch-prev").onclick = () => step(-1);
$("ch-next").onclick = () => step(1);

export function selectInput(index) {
  eqEditReset();
  // The server sends an input's EQ when it changes, not again for the same input, so keep
  // what we have when the input stays the same (e.g. its button clicked again).
  if (index !== state.input || !state.eq || state.eq.index !== index) state.eq = null;
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
  stripInputChanged();
  state.dirty = true;
}

// The header menus (View, Traces, Show; each a <details>): one open at a time, closed by a
// click outside it or Escape.
const menus = [...document.querySelectorAll("header details.menu")];
for (const m of menus) {
  m.addEventListener("toggle", () => {
    if (!m.open) return;
    for (const o of menus) if (o !== m) o.open = false;
    const panel = m.querySelector(".menu-panel");  // kept on screen (a phone's narrow top bar)
    panel.style.left = "";
    const over = panel.getBoundingClientRect().right - (document.documentElement.clientWidth - 8);
    if (over > 0) panel.style.left = `${-over}px`;
  });
}
document.addEventListener("click", (e) => { for (const m of menus) if (m.open && !m.contains(e.target)) m.open = false; });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") for (const m of menus) m.open = false; });

// ☰ (a phone): the menus row shown or hidden.
$("menus-btn").onclick = () => {
  const open = document.querySelector("header").classList.toggle("menus-open");
  $("menus-btn").setAttribute("aria-expanded", String(open));
};

// Spectrum traces (remembered in this browser); Reset sits on the Peak hold row and only
// works while that trace is shown.
for (const [id, key, store] of [["show-live", "showLive", "tf.live"], ["show-avg", "showAvg", "tf.avg"], ["show-peak", "showPeak", "tf.peak"]]) {
  const box = $(id);
  state[key] = box.checked = loadInt(store, 1) === 1;
  box.onchange = () => {
    state[key] = box.checked;
    save(store, box.checked ? 1 : 0);
    $("reset-peak").disabled = !state.showPeak;
    state.dirty = true;
  };
}
$("reset-peak").disabled = !state.showPeak;
$("reset-peak").onclick = () => send({ cmd: "reset_peak", channel: state.input });
