// A bus's 31-band GEQ (AUX 1-8, ST) on the Mix page: a fader per 1/3-octave band (+-15 dB),
// ON, L/R link, Flat. The TF allows 12 bands off 0 dB at once: a 13th is refused here (and by
// the server, console/busproc.py). Bands where the ring detector hears a ring now are marked
// (GET /api/audio/rings, its "geq" band). Data: GET / PUT /api/tf5/mix/geq/<key>.

import { el, putJson } from "../common.js";
import { onDeskChange } from "./feed.js";

const RINGS_MS = 2000;  // the ring marks follow the room, not the desk: refreshed while open
const fmtF = (f) => (f >= 1000 ? `${+(f / 1000).toFixed(f >= 10000 ? 0 : 2)}k` : `${f}`);

// The panel shows one editor at a time: each open() takes it over, and the others stop updating.
export function takePanel(panel, title) {
  const token = String(Math.random());
  panel.dataset.owner = token;
  panel.hidden = false;
  const head = el("div", "mx-row"), x = el("button", "", "Close");
  x.type = "button";
  x.onclick = () => { panel.hidden = true; panel.dataset.owner = ""; panel.replaceChildren(); };
  head.append(el("h2", "", title), x);
  const body = el("div"), err = el("p", "mx-err");
  err.hidden = true;
  panel.replaceChildren(head, body, err);
  return {
    body,
    mine: () => panel.dataset.owner === token && !panel.hidden,
    error: (t) => { err.textContent = t || ""; err.hidden = !t; },
  };
}

export function openGeq(panel, key, label, focusHz) {
  const p = takePanel(panel, `${label} GEQ`);
  let data = null, side = 0, sig = null, busy = false;
  const faders = [], rows = [];
  const on = el("button", "on", "GEQ ON"), link = el("button", "", "L/R link"), flat = el("button", "", "Flat");
  const count = el("span", "mx-note"), sideSel = el("select");
  for (const b of [on, link, flat]) b.type = "button";
  sideSel.setAttribute("aria-label", "Side");
  sideSel.append(new Option("L", "0"), new Option("R", "1"));
  sideSel.onchange = () => { side = Number(sideSel.value); load(); };
  const bands = el("div", "geq-bands");

  async function write(body) {
    busy = true;
    const d = await putJson(`/api/tf5/mix/geq/${key}`, { ...body, side, confirm: true });
    busy = false;
    p.error(d.ok ? "" : `Not set: ${d.error}`);
    load();
  }
  on.onclick = () => write({ on: data.on === 1 ? 0 : 1 });
  link.onclick = () => write({ lr_link: data.lr_link === 1 ? 0 : 1 });
  flat.onclick = () => write({ gains: Object.fromEntries(data.bands.map((b) => [b.index, 0])) });

  function build() {
    bands.replaceChildren();
    faders.length = rows.length = 0;
    for (const b of data.bands) {
      const box = el("div", "geq-band"), g = el("span", "g"), f = el("input");
      Object.assign(f, { type: "range", min: "-15", max: "15", step: "0.5" });
      f.setAttribute("aria-label", `${fmtF(b.freq)} Hz`);
      f.onchange = () => {
        const v = Number(f.value), was = data.bands[b.index].gain;
        if (v !== 0 && !was && data.adjusted >= data.max_adjusted) {   // the TF's 12-band rule
          f.value = "0";
          p.error(`The TF allows ${data.max_adjusted} adjusted GEQ bands at once: flatten one first.`);
          return;
        }
        data.bands[b.index].gain = v;
        data.adjusted += (v !== 0) - (was !== 0 && was !== null);
        write({ gains: { [b.index]: Math.round(v * 100) } });
      };
      box.append(g, f, el("span", "", fmtF(b.freq)));
      bands.append(box);
      faders.push(f); rows.push([box, g]);
    }
  }
  function update(rings) {
    if (!data) return;
    const s = JSON.stringify([data.bands.length, data.sides]);
    if (s !== sig) { sig = s; build(); }
    on.setAttribute("aria-pressed", String(data.on === 1));
    link.setAttribute("aria-pressed", String(data.lr_link === 1));
    sideSel.hidden = data.sides < 2 || data.lr_link === 1;
    count.textContent = `${data.adjusted} / ${data.max_adjusted} bands adjusted`;
    for (const c of [on, link, flat, ...faders]) c.disabled = !data.writable;
    data.bands.forEach((b, i) => {
      const [box, g] = rows[i];
      if (document.activeElement !== faders[i]) faders[i].value = String(b.gain ?? 0);
      g.textContent = b.gain ? `${b.gain > 0 ? "+" : ""}${b.gain}` : "0";
      box.classList.toggle("adj", !!b.gain);
      box.classList.toggle("ring", rings.has(b.freq) || b.freq === focusHz);
      box.title = rings.has(b.freq) ? "A ring is heard near this band now" : "";
    });
  }
  async function rings() {
    try {
      const r = await fetch("/api/audio/rings");
      const d = r.ok ? await r.json() : { events: [] };
      return new Set(d.events.filter((e) => e.active).map((e) => e.geq));
    } catch { return new Set(); }
  }
  async function load() {
    if (!p.mine()) return;
    try {
      const r = await fetch(`/api/tf5/mix/geq/${key}?side=${side}`);
      const d = await r.json();
      if (!r.ok) { p.error(d.error); return; }
      if (!busy) { data = d; update(await rings()); }
    } catch { p.error("Server unreachable."); }
  }
  const top = el("div", "mx-row");
  top.append(on, link, sideSel, flat, count);
  p.body.append(top, bands);
  load();
  onDeskChange(load, p.mine);  // re-read only when the desk changed (WS /ws/tf5)
  (async function ringLoop() {
    if (!p.mine()) return;
    if (data && !busy) update(await rings());
    setTimeout(ringLoop, RINGS_MS);
  })();
}
