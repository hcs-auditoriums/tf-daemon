// The desk's level meters (tf5.meters, tf_daemon/console/meters.py): a bar along the foot of
// each channel tile and one beside the sidebar's fader. They arrive over /ws/audio as
// {"type": "tf5-meters", scale: "dB" | "raw" | null, max, levels: [per USB input]}; with
// scale "dB" the levels are dBFS (null = no signal), with "raw" the console's own meter steps
// (0..max; their dB scale is unverified, so the bar shows the fraction and says "raw").

import { $, el, state } from "./shared.js";

const FLOOR_DB = -60;  // the bar's bottom
let meters = null, queued = false;

export function onConsoleMeters(msg) {
  meters = msg;
  if (queued) return;
  queued = true;
  requestAnimationFrame(() => { queued = false; paintMeters(); });
}

// Level -> [fill fraction 0..1, colour band, readout].
function reading(v) {
  if (v === null || v === undefined || !meters) return [0, "", "−∞"];
  if (meters.scale === "raw") return [Math.min(1, Math.max(0, v / (meters.max || 127))), "", `${v} raw`];
  const f = Math.min(1, Math.max(0, (v - FLOOR_DB) / -FLOOR_DB));
  return [f, v >= -6 ? "hot" : v >= -18 ? "warm" : "", `${v.toFixed(1)} dBFS`];
}

// The sidebar's meter (console.js puts it beside the fader); painted with every update.
export function meterBar() {
  const bar = el("div", "cp-meter");
  bar.setAttribute("role", "meter");
  bar.setAttribute("aria-label", "Level on the desk");
  bar.append(el("div", "cp-meter-fill"));
  bar.hidden = !(meters && meters.scale && meters.connected);
  return bar;
}

export function paintMeters() {
  const live = !!(meters && meters.scale && meters.connected);
  for (const b of $("channels").querySelectorAll(".ch-chip")) {
    const [f, band] = reading(live ? meters.levels[Number(b.dataset.input)] : null);
    b.classList.toggle("metered", live);
    b.style.setProperty("--lvl", f.toFixed(3));
    b.dataset.lvl = band;
  }
  const bar = document.querySelector("#console-panel .cp-meter");
  if (!bar) return;
  bar.hidden = !live;
  const [f, band, text] = reading(live ? meters.levels[state.input] : null);
  bar.firstChild.style.height = `${f * 100}%`;
  bar.dataset.lvl = band;
  bar.title = `Desk meter (${meters?.point || ""}): ${text}`;
  bar.setAttribute("aria-valuetext", text);
}
