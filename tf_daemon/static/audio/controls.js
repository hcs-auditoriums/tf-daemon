// The input picker, trace toggles, View menu and Reset peak.

import { renderPanel } from "./console.js";
import { eqEditReset } from "./eqedit.js";
import { autoReset } from "./range.js";
import { $, save, state } from "./shared.js";
import { send } from "./socket.js";
import { renderVocal } from "./vocal.js";
import { wfReset } from "./waterfall.js";

// Buttons for the inputs named in config.toml, plus a menu with every input.
export function buildChannelPicker() {
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

export function markSelected(index) {
  for (const b of $("channels").querySelectorAll("button")) {
    b.setAttribute("aria-pressed", String(Number(b.dataset.input) === index));
  }
  $("input").value = index;
}

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
