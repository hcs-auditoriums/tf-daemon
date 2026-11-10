// The EQ toolbar over the spectrum, as on TF StageMix's EQ screen (when the EQ can be edited):
// EQ and HPF on / off, undo / redo, and its menu: Gain flat, Copy, Paste, Compare (A/B with
// the copied EQ, or with the EQ before your changes). Every change goes through eqedit.js.

import { applyEq, canRedo, canUndo, editable, editEq, eqBefore, eqSnapshot, onEqHistory, redoEq, undoEq } from "./eqedit.js";
import { $, el, state } from "./shared.js";

const bar = $("eq-bar");
const CLIP_KEY = "tf.eqClip";   // the copied EQ, kept in this browser (it survives a reload)

function button(text, title, onclick) {
  const b = el("button", "eqb-btn", text);
  b.type = "button";
  b.title = title;
  b.onclick = onclick;
  return b;
}

const eqOn = button("EQ", "EQ on / off", () => editEq("EQ", { on: !state.eq.eq.on }));
const hpfOn = button("HPF", "High-pass filter on / off", () => editEq("HPF", { on: !state.eq.eq.hpf.on }));
const undo = button("↶", "Undo", () => { endCompare(); undoEq(); });
const redo = button("↷", "Redo", () => { endCompare(); redoEq(); });
undo.setAttribute("aria-label", "Undo");
redo.setAttribute("aria-label", "Redo");

// A/B: the EQ shown is swapped for another (written to the desk, so you hear it); pressed again
// it goes back. Any other change ends the comparison where it is.
let compare = null;   // {input, back: eq, label}
function startCompare(other, label) {
  if (!other) return;
  compare = { input: state.input, back: eqSnapshot(), label };
  applyEq(other, false);
  syncEqBar();
}
function endCompare(restore = false) {
  if (!compare) return;
  const back = compare.back;
  compare = null;
  if (restore) applyEq(back, false);
  syncEqBar();
}
const ab = button("A/B", "Compare: hear the other EQ; again to come back", () => {
  if (compare) endCompare(true);
  else startCompare(clip(), "copied");
});

function clip() {
  try { return JSON.parse(localStorage.getItem(CLIP_KEY)); } catch { return null; }
}

// The menu (a <details>, like View): less used, and a confirm for what replaces the EQ.
const menu = el("details", "eqb-menu");
const summary = el("summary", "eqb-btn", "⋯");
summary.title = "More";
summary.setAttribute("aria-label", "EQ menu");
const panel = el("div", "eqb-panel");
const items = {
  flat: button("Gain flat", "Every band's gain to 0 dB", () => {
    const eq = eqSnapshot();
    for (const b of eq.bands) if (b.type !== "hpf" && b.type !== "lpf") b.gain = 0;
    run(eq);
  }),
  copy: button("Copy", "Copy this EQ", () => {
    try { localStorage.setItem(CLIP_KEY, JSON.stringify({ ...eqSnapshot(), from: state.eq.source })); } catch { /* storage off */ }
    menu.open = false;
    syncEqBar();
  }),
  paste: button("Paste", "Replace this EQ with the copied one", () => {
    const c = clip();
    if (c && confirm(`Replace the EQ of ${state.eq.source} with the one copied from ${c.from || "another channel"}?`)) run(c);
  }),
  before: button("Compare with before", "Hear the EQ as it was before your changes; again to come back", () => {
    menu.open = false;
    if (compare) endCompare(true);
    else startCompare(eqBefore(), "before");
  }),
};
function run(eq) {
  endCompare();
  applyEq(eq);
  menu.open = false;
}
panel.append(...Object.values(items));
menu.append(summary, panel);
document.addEventListener("click", (e) => { if (menu.open && !menu.contains(e.target)) menu.open = false; });

bar.append(eqOn, hpfOn, undo, redo, ab, menu);

// Kept in step with the EQ shown (called each frame the plot is drawn; it only touches the DOM
// when something changed).
let shown = null;
export function syncEqBar() {
  const on = !!editable();
  if (compare && compare.input !== state.input) compare = null;
  const eq = on && state.eq.eq, c = on && clip();
  const key = on ? JSON.stringify([eq.on, eq.hpf && eq.hpf.on, canUndo(), canRedo(), !!c, !!compare, compare && compare.label, !!eqBefore()]) : "";
  if (key === shown) return;
  shown = key;
  bar.hidden = !on;
  if (!on) { menu.open = false; return; }
  eqOn.setAttribute("aria-pressed", String(eq.on));
  hpfOn.setAttribute("aria-pressed", String(!!(eq.hpf && eq.hpf.on)));
  hpfOn.hidden = !eq.hpf;
  undo.disabled = !canUndo();
  redo.disabled = !canRedo();
  ab.disabled = !compare && !c;
  ab.setAttribute("aria-pressed", String(!!compare));
  ab.title = compare ? `Hearing the ${compare.label} EQ: press to come back` : c ? `Compare with the EQ copied from ${c.from || "another channel"}` : "Copy an EQ first to compare with it";
  items.paste.disabled = !c;
  items.before.disabled = !compare && !eqBefore();
  items.before.textContent = compare && compare.label === "before" ? "Back to your EQ" : "Compare with before";
}
onEqHistory(syncEqBar);
