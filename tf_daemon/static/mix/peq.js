// A bus's PEQ (AUX, ST, MONO, MATRIX) on the Mix page: the desk's curve (computed by the server
// with dsp/eq.py, the same maths as the input EQ) and a row per band to edit type, frequency,
// gain and Q. Data: GET / PUT /api/tf5/mix/eq/<key> (console/busproc.py); a stereo bus's two
// halves are written together.

import { el, fitCanvas, putJson } from "../common.js";
import { takePanel } from "./geq.js";

const POLL_MS = 1000;
const css = getComputedStyle(document.documentElement);
const color = (n) => css.getPropertyValue(n).trim();
// Band label -> its path under PEQ (mid bands by index), and the L / H band types (enum 0-2).
const bandPath = (label, i) => (label === "L" ? "Bank/BandL" : label === "H" ? "Bank/BandH" : `Bank/Band:${i - 1}`);
const TYPES = { L: ["Bell", "Low shelf", "HPF"], H: ["Bell", "High shelf", "LPF"] };
const TYPE_OF = { bell: 0, low_shelf: 1, high_shelf: 1, hpf: 2, lpf: 2 };

function draw(cv, d) {
  const g = cv.getContext("2d"), [w, h] = fitCanvas(cv, g);
  g.clearRect(0, 0, w, h);
  const x = (f) => Math.log10(f / 20) / 3 * w, y = (db) => h / 2 - db / 18 * (h / 2);
  g.strokeStyle = color("--grid"); g.lineWidth = 1; g.beginPath();
  for (const f of [100, 1000, 10000]) { g.moveTo(x(f), 0); g.lineTo(x(f), h); }
  for (const db of [-12, -6, 6, 12]) { g.moveTo(0, y(db)); g.lineTo(w, y(db)); }
  g.stroke();
  g.strokeStyle = color("--grid-strong"); g.beginPath(); g.moveTo(0, y(0)); g.lineTo(w, y(0)); g.stroke();
  if (!d.eq) return;
  g.strokeStyle = color("--eq"); g.lineWidth = 1.5;
  if (!d.eq.on) g.setLineDash([4, 3]);
  g.beginPath();
  d.freqs.forEach((f, i) => (i ? g.lineTo : g.moveTo).call(g, x(f), y(d.curve[i])));
  g.stroke();
  g.setLineDash([]);
}

export function openPeq(panel, key, label) {
  const p = takePanel(panel, `${label} PEQ`);
  let data = null, side = 0, sig = null, busy = false;
  const cv = el("canvas"), on = el("button", "on", "EQ ON"), note = el("span", "mx-note");
  const sideSel = el("select"), table = el("table");
  on.type = "button";
  cv.id = "peq-curve";
  cv.setAttribute("aria-label", `${label} EQ curve`);
  sideSel.setAttribute("aria-label", "Side");
  sideSel.append(new Option("L", "0"), new Option("R", "1"));
  sideSel.onchange = () => { side = Number(sideSel.value); sig = null; load(); };
  const fields = [];   // [band label, field, input] to refresh

  async function write(changes) {
    busy = true;
    const d = await putJson(`/api/tf5/mix/eq/${key}`, { changes, confirm: true });
    busy = false;
    p.error(d.ok ? "" : `Not set: ${d.error}`);
    load();
  }
  on.onclick = () => write([["On", data.eq.on ? 0 : 1]]);

  function build() {
    const head = el("tr");
    head.append(...["Band", "Type", "Hz", "dB", "Q"].map((t) => el("th", "", t)));
    table.replaceChildren(head);
    fields.length = 0;
    data.eq.bands.forEach((b, i) => {
      const tr = el("tr"), path = bandPath(b.label, i), td = () => el("td");
      const cells = [td(), td(), td(), td(), td()];
      cells[0].textContent = b.label;
      if (TYPES[b.label]) {
        const s = el("select");
        s.setAttribute("aria-label", `${b.label} type`);
        TYPES[b.label].forEach((t, k) => s.append(new Option(t, String(k))));
        s.onchange = () => {
          const ch = [[`${path}/Type`, Number(s.value)]];
          if (Number(s.value) === 2) ch.push([`${path}/${b.label === "L" ? "HPFOn" : "LPFOn"}`, 1]);
          write(ch);
        };
        cells[1].append(s); fields.push([i, "type", s]);
      }
      for (const [k, field, scale, col] of [[2, "freq", 10, "Frequency"], [3, "gain", 100, "Gain"], [4, "q", 1000, "Q"]]) {
        const inp = el("input");
        Object.assign(inp, { type: "number", step: field === "freq" ? "1" : "0.1" });
        inp.setAttribute("aria-label", `${b.label} ${col}`);
        inp.onchange = () => write([[`${path}/${col}`, Math.round(Number(inp.value) * scale)]]);
        cells[k].append(inp); fields.push([i, field, inp]);
      }
      tr.append(...cells);
      table.append(tr);
    });
  }
  function update() {
    const s = JSON.stringify([side, data.eq && data.eq.bands.map((b) => b.label)]);
    if (s !== sig) { sig = s; if (data.eq) build(); }
    draw(cv, data);
    on.setAttribute("aria-pressed", String(!!(data.eq && data.eq.on)));
    sideSel.hidden = data.sides < 2;
    const lpf = data.eq && data.eq.lpf;
    note.textContent = [data.halves_differ ? "L and R differ; an edit sets both." : "",
                        lpf ? `LPF ${lpf.on ? "on" : "off"} ${lpf.freq} Hz ${lpf.slope} dB/oct (not drawn)` : ""].join(" ");
    if (!data.eq) return;
    for (const [i, field, inp] of fields) {
      const b = data.eq.bands[i];
      if (document.activeElement !== inp) inp.value = field === "type" ? String(TYPE_OF[b.type]) : String(+b[field].toFixed(2));
      inp.disabled = !data.writable;
    }
    on.disabled = !data.writable;
  }
  async function load() {
    if (!p.mine()) return;
    try {
      const r = await fetch(`/api/tf5/mix/eq/${key}?side=${side}`);
      const d = await r.json();
      if (!r.ok) { p.error(d.error); return; }
      if (!busy) { data = d; update(); }
    } catch { p.error("Server unreachable."); }
  }
  const top = el("div", "mx-row");
  top.append(on, sideSel, note);
  const wrap = el("div", "peq");
  wrap.append(cv);
  p.body.append(top, wrap, table);
  (async function loop() { await load(); if (p.mine()) setTimeout(loop, POLL_MS); })();
}
