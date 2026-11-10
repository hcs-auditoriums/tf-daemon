// The audio page's keyboard shortcuts and command-palette entries (the palette is keys.js).
// Each one presses or ticks the control it stands for, so a key does exactly what a tap does,
// and is offered only while that control is there to use.

import { addCommands } from "../keys.js";
import { selectInput } from "./controls.js";
import { eqCommands } from "./eqbar.js";
import { $, state } from "./shared.js";

const shown = (id) => { const n = $(id); return !!n && !n.hidden && !n.closest("[hidden]"); };
const tick = (id) => () => $(id).click();   // a checkbox or radio: toggles it and fires its change

addCommands(() => [
  { title: "Show / hide the desk EQ", keys: ["e"], group: "View", when: () => shown("eq-toggle"), run: tick("show-eq") },
  { title: "Previous channel", keys: ["["], group: "Channel", run: () => $("ch-prev").click() },
  { title: "Next channel", keys: ["]"], group: "Channel", run: () => $("ch-next").click() },
  ...[1, 2, 3, 4, 5, 6, 7, 8].map((n) => ({
    title: `Tile ${n} of this bank`, keys: [String(n)], group: "Channel", help: false,
    when: () => !!visibleTile(n), run: () => visibleTile(n).click(),
  })),
  ...(state.meta?.channels || []).map((c) => ({
    title: `${c.index + 1} · ${c.label}`, group: "Channel", help: false, run: () => selectInput(c.index),
  })),
  { title: "A tile of the bank shown", keys: ["1–8"], group: "Channel" },   // the ? list's lines for the above
  { title: "Any input: press / and type its number or name", group: "Channel" },
  ...eqCommands(),
  ...["spectrum", "both", "waterfall"].map((v) => ({
    title: `Layout: ${v[0].toUpperCase()}${v.slice(1)}`, group: "View",
    run: () => $("layout").querySelector(`input[value=${v}]`).click(),
  })),
  { title: "Real-time trace on / off", group: "Traces", run: tick("show-live") },
  { title: "Average trace on / off", group: "Traces", run: tick("show-avg") },
  { title: "Peak hold on / off", group: "Traces", run: tick("show-peak") },
  { title: "Reset peak hold", group: "Traces", when: () => !$("reset-peak").disabled, run: () => $("reset-peak").click() },
  { title: "Ring markers on / off", group: "Rings", run: tick("show-rings") },
  { title: "Ring list on / off", group: "Rings", run: tick("show-ring-list") },
  { title: "Clear past rings", group: "Rings", when: () => !!document.querySelector("#ring-list .rl-head .rl-btn"),
    run: () => document.querySelector("#ring-list .rl-head .rl-btn").click() },
  { title: "Console sidebar on / off", group: "View", when: () => shown("console-toggle"), run: tick("show-console") },
  { title: "Suggested EQ on / off", group: "View", when: () => shown("vocal-toggle"), run: tick("show-vocal") },
  { title: "Mic check", group: "Vocal", when: () => shown("mic-check"), run: () => $("mic-check").click() },
]);

const visibleTile = (n) => [...$("channels").querySelectorAll(".ch-chip:not([hidden])")][n - 1];
