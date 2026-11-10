// The Desk page (desk.html): the TF5's scenes, USB recorder, FX and delay and its selected channel,
// read over the TF Editor protocol. They arrive over one websocket (WS /ws/tf5, console/feed.py),
// pushed when the desk's data changes, so the page sends no requests while it just watches. The
// desk is changed only by a person's click, each with "confirm": true: a scene recall (after its
// confirmation step, desk/scenes.js) and FX / delay edits (desk/effects.js); the server's guard
// refuses anything its tf5.* switch doesn't allow.

import { $, connectWs } from "./common.js";
import { initEffects, renderDelay, renderFx } from "./desk/effects.js";
import { renderRecorder } from "./desk/recorder.js";
import { initScenes, renderScenes, showEmpty } from "./desk/scenes.js";
import { dash, note } from "./desk/ui.js";

function renderSelected(d) {
  const text = d.label ? `${d.label}${d.name ? ` “${d.name}”` : ""}` : dash(d.hex);
  $("selected-ch").textContent = d.raw === null ? "–" : `${text} [inferred]`;
  $("selected-ch").title = `${d.hex ?? ""}: ${d.note}`;
}

const AREAS = {
  scenes: renderScenes,
  recorder: renderRecorder,
  fx: renderFx,
  delay: renderDelay,
  selected: renderSelected,
};
const last = {};   // area -> its latest data from the server

// Scenes come with every slot; the page hides the empty ones unless asked.
function show(area) {
  const d = last[area];
  if (!d) return;
  AREAS[area](area === "scenes" && !showEmpty() ? { ...d, scenes: d.scenes.filter((s) => s.stored) } : d);
}

// Show one area again from what the server last sent (dropping unsent edits), or all of them.
// Changes on the desk, a recall included, arrive by themselves.
function refresh(area) {
  for (const a of area ? [area] : Object.keys(AREAS)) show(a);
}

function renderStatus(st) {
  const conn = $("conn"), eq = st.eq;
  conn.classList.toggle("ok", !!eq?.synced);
  conn.textContent = !eq ? "editor protocol off" : eq.synced ? `desk ${eq.host}` : eq.connected ? "syncing…" : "desk offline";
  if (eq) note($("desk-error"), eq.synced || !eq.last_error ? "" : eq.last_error, true);
  else note($("desk-error"), "The desk's editor protocol is off (tf5.eq = false): nothing to show.", true);
}

const feed = connectWs("/ws/tf5", {
  onOpen: () => feed.send({ areas: ["status", ...Object.keys(AREAS)] }),
  onClose: () => { $("conn").classList.remove("ok"); $("conn").textContent = "server offline"; },
  onMessage: (raw) => {
    const d = JSON.parse(raw);
    if (d.type === "status") renderStatus(d.data);
    else if (!d.error && AREAS[d.type]) { last[d.type] = d; show(d.type); }
  },
});

initScenes(refresh);
initEffects(refresh);
