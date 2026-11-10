// The gate's and compressor's transfer graphs (input level → output level), as on StageMix's
// GATE / COMP screens: drag the threshold sideways; on the compressor, drag the curve's end
// up / down for the ratio. Used by strip.js.

import { color } from "./shared.js";

const LO = -72, HI = 6, PAD = 6;   // dB shown on both axes (the gate's threshold goes to -72)
// The desk's knee ("HARD", "SOFT-2"): its width in dB, drawn as about 3 dB per soft step.
const kneeDb = (knee) => { const m = /^soft-?(\d*)/i.exec(knee || ""); return m ? 3 * (Number(m[1]) || 2) : 0; };

// Output level for input ``x`` (dB); a gate's range of null is −∞.
function gateOut(v, x) { return v.on === false || x >= v.threshold ? x : x + (v.range ?? -1000); }
function compOut(v, x) {
  const r = v.ratio === "inf" ? Infinity : v.ratio, t = v.threshold, k = kneeDb(v.knee);
  let y;
  if (k && Math.abs(x - t) <= k / 2) y = x + (1 / r - 1) * (x - t + k / 2) ** 2 / (2 * k);   // soft knee
  else y = x < t ? x : t + (x - t) / r;
  return y + (v.gain || 0);
}

// kind: "gate" | "comp"; onChange({threshold} | {ratio}) while dragging.
export function dynGraph(kind, ratios, onChange) {
  const cv = document.createElement("canvas");
  cv.className = "dyn-graph";
  cv.setAttribute("aria-label", kind === "gate" ? "Gate curve: drag the threshold" : "Compressor curve: drag the threshold or the ratio");
  const g = cv.getContext("2d");
  let v = null, W = 0, H = 0, grab = null;
  const xOf = (db) => PAD + (db - LO) / (HI - LO) * (W - 2 * PAD);
  const yOf = (db) => H - PAD - (db - LO) / (HI - LO) * (H - 2 * PAD);
  const dbOfX = (x) => LO + (x - PAD) / (W - 2 * PAD) * (HI - LO);
  const out = (x) => (kind === "gate" ? gateOut(v, x) : compOut(v, x));

  function draw(values) {
    if (values) v = values;
    if (!v) return;
    const r = cv.getBoundingClientRect(), dpr = devicePixelRatio || 1;
    W = r.width; H = r.height;
    if (!W || !H) return;
    cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);
    g.strokeStyle = color("--grid"); g.lineWidth = 1;
    g.beginPath();
    for (let db = -60; db <= 0; db += 12) { g.moveTo(xOf(db), PAD); g.lineTo(xOf(db), H - PAD); g.moveTo(PAD, yOf(db)); g.lineTo(W - PAD, yOf(db)); }
    g.stroke();
    g.strokeStyle = color("--grid-strong"); g.setLineDash([3, 3]);
    g.beginPath(); g.moveTo(xOf(LO), yOf(LO)); g.lineTo(xOf(HI), yOf(HI)); g.stroke();   // unity
    g.setLineDash([]);
    const on = v.on !== false, c = on ? color("--eq") : color("--muted");
    g.strokeStyle = c; g.lineWidth = 2;
    g.beginPath();
    for (let x = LO; x <= HI; x += 0.5) { const y = Math.max(LO, out(x)); if (x === LO) g.moveTo(xOf(x), yOf(y)); else g.lineTo(xOf(x), yOf(y)); }
    g.stroke();
    // Threshold: a line and its handle on the curve; the comp's ratio handle at the top end.
    const tx = xOf(v.threshold);
    g.globalAlpha = 0.6; g.setLineDash([2, 3]);
    g.beginPath(); g.moveTo(tx, PAD); g.lineTo(tx, H - PAD); g.stroke();
    g.setLineDash([]); g.globalAlpha = 1;
    g.fillStyle = c;
    g.beginPath(); g.arc(tx, yOf(Math.max(LO, out(v.threshold))), 6, 0, 2 * Math.PI); g.fill();
    if (kind === "comp") { g.beginPath(); g.arc(xOf(0), yOf(out(0)), 5, 0, 2 * Math.PI); g.fill(); }
  }

  cv.addEventListener("pointerdown", (e) => {
    if (!v) return;
    const r = cv.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
    const nearRatio = kind === "comp" && Math.hypot(x - xOf(0), y - yOf(out(0))) < 18;
    grab = nearRatio ? "ratio" : "threshold";
    cv.setPointerCapture(e.pointerId);
    move(e);
  });
  function move(e) {
    if (!grab) return;
    const r = cv.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
    if (grab === "threshold") {
      onChange({ threshold: Math.round(dbOfX(x)) });
    } else {   // the ratio whose curve at 0 dB in is nearest the pointer
      const want = LO + (H - PAD - y) / (H - 2 * PAD) * (HI - LO) - (v.gain || 0);
      const at = (q) => (q === "inf" ? v.threshold : v.threshold + (0 - v.threshold) / q);
      onChange({ ratio: ratios.reduce((a, b) => (Math.abs(at(b) - want) < Math.abs(at(a) - want) ? b : a)) });
    }
  }
  cv.addEventListener("pointermove", move);
  const end = () => { grab = null; };
  cv.addEventListener("pointerup", end);
  cv.addEventListener("pointercancel", end);
  new ResizeObserver(() => draw()).observe(cv);
  return { el: cv, draw };
}
