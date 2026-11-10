// The Desk page (desk.html): the TF5's scenes, USB recorder, FX and delay and its selected channel,
// read over the TF Editor protocol (GET /api/tf5/{scenes,recorder,fx,delay,selected},
// console/desk.py). Each area is polled once a second with ?since=<version>, so an unchanged
// one costs a few bytes. The desk is changed only by a person's click, each with "confirm": true:
// a scene recall (after its confirmation step, desk/scenes.js) and FX / delay edits
// (desk/effects.js); the server's guard refuses anything its tf5.* switch doesn't allow.

import { $ } from "./common.js";
import { initEffects, renderDelay, renderFx } from "./desk/effects.js";
import { renderRecorder } from "./desk/recorder.js";
import { initScenes, renderScenes, showEmpty } from "./desk/scenes.js";
import { dash, note } from "./desk/ui.js";

const POLL_MS = 1000;

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
const versions = {};

async function poll(area, force = false) {
  const q = new URLSearchParams();
  if (versions[area] && !force) q.set("since", versions[area]);
  if (area === "scenes" && showEmpty()) q.set("all", "1");
  try {
    const res = await fetch(`/api/tf5/${area}?${q}`);
    if (res.status === 404) {
      note($("desk-error"), "The desk's editor protocol is off (tf5.eq = false): nothing to show.", true);
      return;
    }
    const d = await res.json();
    if (d.unchanged) return;
    versions[area] = d.version;
    AREAS[area](d);
  } catch (e) {
    note($("desk-error"), `server not reachable: ${e.message}`, true);
  }
}

// Re-read one area now (``force``: even if unchanged, to drop edits), or all of them (after a recall).
function refresh(area, force = false) {
  if (area) poll(area, force || area === "scenes");
  else for (const a of Object.keys(AREAS)) poll(a, true);
}

async function status() {
  const conn = $("conn");
  try {
    const st = await (await fetch("/api/tf5/status")).json();
    const eq = st.eq;
    conn.classList.toggle("ok", !!eq?.synced);
    conn.textContent = !eq ? "editor protocol off" : eq.synced ? `desk ${eq.host}` : eq.connected ? "syncing…" : "desk offline";
    if (eq) note($("desk-error"), eq.synced || !eq.last_error ? "" : eq.last_error, true);
  } catch {
    conn.classList.remove("ok");
    conn.textContent = "server offline";
  }
}

initScenes(refresh);
initEffects(refresh);
for (const a of Object.keys(AREAS)) poll(a);
status();
setInterval(() => { for (const a of Object.keys(AREAS)) poll(a); }, POLL_MS);
setInterval(status, 2 * POLL_MS);
