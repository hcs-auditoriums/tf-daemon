// The level range: fixed choices, or Auto following the shown input.

import { drawPanelEq } from "./console.js";
import { $, loadStr, save, state } from "./shared.js";
import { wfRecolor } from "./waterfall.js";

// Levels are per 1/48-octave display bin (a full-scale sine reads 0 dB): speech
// harmonics land around -25..-40, a hot mix -10..-25, rings up to ~0, and below
// ~-90 there's only preamp hiss and silence (-130..-140). The pre-fader inputs
// sit wherever their head-amp gain puts them, so the default, Auto, keeps a
// 70 dB window on whatever the shown input is doing: the top 5 dB above the
// loudest point of the last 5 s, rounded up to 10 dB. It moves up at once and
// back down only after 8 s quieter, so it doesn't pump between phrases. The loudest
// point counts the peak-hold trace when it is shown, so that isn't cut off at the top.
const RANGES = ["auto", "-90,-10", "-80,0", "-70,-20", "-130,0"];
const AUTO_SPAN = 70, AUTO_TOP_MIN = -40, AUTO_TOP_MAX = 0, AUTO_HOLD_S = 8, AUTO_WINDOW_S = 5;
state.range = loadStr("tf.range", "auto", RANGES);
const auto = { maxima: [], quietSince: null, fitUntil: 0 };

function setRange(min, max) {
  if (min === state.dbMin && max === state.dbMax) return;
  state.dbMin = min; state.dbMax = max;
  wfRecolor();
  drawPanelEq();   // the sidebar EQ uses the same scale
  state.dirty = true;
}

function autoLabel() {
  $("range").options[0].textContent = state.range === "auto" ? `Auto (${state.dbMin}…${state.dbMax} dB)` : "Auto";
}

export function autoReset() {  // new input: fit straight away, both directions
  auto.maxima = []; auto.quietSince = null; auto.fitUntil = performance.now() + 1500;
}

export function autoRange() {
  if (state.range !== "auto" || !state.meta) return;
  let m = -Infinity;
  for (let i = 0; i < state.live.length; i++) if (state.live[i] > m) m = state.live[i];
  if (state.showPeak) for (let i = 0; i < state.peak.length; i++) if (state.peak[i] > m) m = state.peak[i];
  const keep = Math.round(AUTO_WINDOW_S * state.meta.update_rate);
  auto.maxima.push(m);
  if (auto.maxima.length > keep) auto.maxima.shift();
  const loudest = Math.max(...auto.maxima);
  const top = Math.min(AUTO_TOP_MAX, Math.max(AUTO_TOP_MIN, Math.ceil((loudest + 5) / 10) * 10));
  const now = performance.now();
  if (top > state.dbMax || now < auto.fitUntil) {
    auto.quietSince = null;
    setRange(top - AUTO_SPAN, top);
  } else if (top < state.dbMax) {
    if (auto.quietSince === null) auto.quietSince = now;
    if (now - auto.quietSince >= AUTO_HOLD_S * 1000) {
      auto.quietSince = null;
      setRange(state.dbMin - 10, state.dbMax - 10);  // one step at a time
    }
  } else {
    auto.quietSince = null;
  }
  autoLabel();
}

$("range").value = state.range;
$("range").onchange = (e) => {
  state.range = e.target.value;
  save("tf.range", state.range);
  if (state.range === "auto") autoReset();
  else setRange(...state.range.split(",").map(Number));
  autoLabel();
};
// At load there's no waterfall history to recolour (and it's defined further down).
if (state.range !== "auto") [state.dbMin, state.dbMax] = state.range.split(",").map(Number);
autoLabel();
