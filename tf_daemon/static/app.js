// tf-daemon spectrum view (the audio page). Native ES modules, no build step.
// The server analyzes an input only while some client views it, so we tell it
// which one we show ({"cmd": "view"}) and pick that input's record out of each frame.

import "./audio/commands.js";
import { drawEq } from "./audio/eq.js";
import { syncEqBar } from "./audio/eqbar.js";
import { drawGrid, drawHover, H, PAD, resize, trace, W } from "./audio/plot.js";
import { drawRingMarkers, syncRingList } from "./audio/rings.js";
import { C, ctx, state } from "./audio/shared.js";
import { connect } from "./audio/socket.js";
import { pollRingout } from "./audio/ringout.js";
import { drawVocal, pollVocal } from "./audio/vocal.js";
import { drawWaterfall, wf, wfHoverReadout } from "./audio/waterfall.js";

function draw() {
  ctx.clearRect(0, 0, W, H);
  if (!state.meta || W < 100 || H < 80) return;
  drawGrid();
  ctx.save();
  ctx.beginPath();
  ctx.rect(PAD.l, PAD.t, W - PAD.l - PAD.r, H - PAD.t - PAD.b);
  ctx.clip();
  if (state.showPeak) trace(state.peak, C.peak, null);
  if (state.showAvg) trace(state.avg, C.avg, C.avgFill);
  if (state.showLive) trace(state.live, C.rta, null, 1);
  drawRingMarkers();
  drawEq();
  drawVocal();
  ctx.restore();
  drawHover();
  syncEqBar();
  syncRingList();
}

// A bug in any overlay must not freeze the analyzer: log it (once a few
// seconds) and keep animating.
let lastDrawError = 0;
function loop() {
  requestAnimationFrame(loop);
  try {
    if (state.dirty) { state.dirty = false; draw(); }
    if (wf.dirty && state.layout !== "spectrum") { wf.dirty = false; drawWaterfall(); wfHoverReadout(); }
  } catch (e) {
    ctx.restore();  // draw() may have stopped inside its clip
    if (performance.now() - lastDrawError > 5000) { console.error("draw failed:", e); lastDrawError = performance.now(); }
  }
}

resize();
connect();
pollVocal();
pollRingout();
requestAnimationFrame(loop);
