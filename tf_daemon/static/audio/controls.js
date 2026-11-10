// The input picker, trace toggles, View menu and Reset peak.

import { channelColor, renderPanel } from "./console.js";
import { eqEditReset } from "./eqedit.js";
import { autoReset } from "./range.js";
import { $, save, state } from "./shared.js";
import { send } from "./socket.js";
import { renderVocal } from "./vocal.js";
import { wfReset } from "./waterfall.js";

// The channel bar, like TF StageMix's: a chip per input in order (number, name, the
// console's colour), swiped or scrolled sideways, with ‹ › to step through them. The
// inputs listed in config.toml are always analyzed; the others while shown.
export function buildChannelPicker() {
  const nav = $("channels");
  nav.replaceChildren();
  for (const ch of state.meta.channels) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = `ch-chip${ch.pinned ? " pinned" : ""}${ch.label === `Ch ${ch.index + 1}` ? " unnamed" : ""}`;
    b.textContent = ch.label;
    b.title = `Input ${ch.index + 1}`;
    b.dataset.input = ch.index;
    b.dataset.num = ch.index + 1;
    b.onclick = () => selectInput(ch.index);
    nav.appendChild(b);
  }
  paintChannelColors();
}

// The console's channel colours on the chips (when the TF5 is read; called as they change).
export function paintChannelColors() {
  for (const b of $("channels").querySelectorAll(".ch-chip")) {
    const c = channelColor(Number(b.dataset.input));
    if (c) b.style.setProperty("--ch", c);
    else b.style.removeProperty("--ch");
  }
}

export function markSelected(index) {
  for (const b of $("channels").querySelectorAll(".ch-chip")) {
    const on = Number(b.dataset.input) === index;
    b.setAttribute("aria-pressed", String(on));
    if (on) b.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
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
  state.dirty = true;
}

// View menu (a <details>): close on a click outside it or Escape.
const viewMenu = $("view-menu");
document.addEventListener("click", (e) => { if (viewMenu.open && !viewMenu.contains(e.target)) viewMenu.open = false; });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") viewMenu.open = false; });

$("show-avg").onchange = (e) => { state.showAvg = e.target.checked; state.dirty = true; };
$("show-peak").onchange = (e) => { state.showPeak = e.target.checked; state.dirty = true; };
$("show-live").onchange = (e) => { state.showLive = e.target.checked; state.dirty = true; };

$("reset-peak").onclick = () => send({ cmd: "reset_peak", channel: state.input });
