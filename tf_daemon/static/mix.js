// The Mix page: the desk's AUX / ST / MONO / MATRIX masters and DCAs (fader, ON, balance, cue),
// the mute masters, each channel's DCA / mute group assignment and cue, and per bus its PEQ and
// GEQ (mix/peq.js, mix/geq.js). Data: GET /api/tf5/mix, polled with ?since so a change at the
// desk shows within a second; edits: PUT /api/tf5/mix/... with confirm (console/buses.py).

import { $, el, faderPos, posToDb, putJson } from "./common.js";
import { openGeq } from "./mix/geq.js";
import { openPeq } from "./mix/peq.js";

let data = null, version = null, sig = null;
const refs = new Map();   // strip / source key -> {update(entry)}
const POLL_MS = 700;

const fmtDb = (raw) => (raw === null || raw === undefined || raw <= -32768 ? "−∞" : `${raw > 0 ? "+" : ""}${(raw / 100).toFixed(1)}`);
const fmtBal = (v) => (v === 0 ? "C" : v < 0 ? `L${-v}` : `R${v}`);
const rawOf = (db) => (db === null ? -32768 : Math.round(Math.min(db, 10) * 10) * 10);

function error(text) { const e = $("mx-err"); e.textContent = text || ""; e.hidden = !text; }

// One write per target in flight; the newest values wait and merge (a drag sends many).
const pending = new Map(), busy = new Set();
function send(path, body) {
  pending.set(path, { ...(pending.get(path) || {}), ...body });
  flush(path);
}
async function flush(path) {
  if (busy.has(path) || !pending.has(path)) return;
  const body = pending.get(path);
  pending.delete(path);
  busy.add(path);
  const d = await putJson(path, { ...body, confirm: true });
  busy.delete(path);
  error(d.ok ? (d.results || []).some((r) => !r.confirmed) ? "The desk didn't confirm the change." : "" : `Not set: ${d.error}`);
  if (pending.has(path)) flush(path); else poll(true);
}
const setStrip = (key, values) => send(`/api/tf5/mix/strip/${key}`, values);

function toggle(text, cls, onClick) {
  const b = el("button", cls, text);
  b.type = "button";
  b.onclick = onClick;
  return b;
}
const press = (b, on) => b.setAttribute("aria-pressed", String(on === 1 || on === true));

// ------------------------------------------------------------------ master strips

function strip(m) {
  const card = el("div", "mx-strip");
  card.dataset.key = m.key;
  const name = el("div", "nm"), lbl = el("div", "lbl", m.label), val = el("div", "val");
  const on = toggle("ON", "on", () => setStrip(m.key, { on: cur.on === 1 ? 0 : 1 }));
  const cue = toggle("CUE", "cue", () => setStrip(m.key, { cue: cur.cue === 1 ? 0 : 1 }));
  const fader = el("input");
  Object.assign(fader, { type: "range", min: "0", max: "1000" });
  fader.setAttribute("aria-label", `${m.label} level`);
  fader.oninput = () => {
    const raw = rawOf(posToDb(Number(fader.value) / 1000));
    val.textContent = `${fmtDb(raw)} dB`;
    setStrip(m.key, { level: raw });
  };
  const row = el("div", "mx-row");
  row.append(on, cue);
  card.append(lbl, name, row, fader, val);
  let bal = null;
  if (m.balance !== null) {
    bal = el("input");
    Object.assign(bal, { type: "range", min: "-63", max: "63" });
    bal.setAttribute("aria-label", `${m.label} balance`);
    bal.oninput = () => setStrip(m.key, { balance: Number(bal.value) });
    card.append(bal);
  }
  const tools = el("div", "mx-row");
  if (m.peq) tools.append(toggle("EQ", "", () => openPeq($("mx-proc"), m.key, m.label)));
  if (m.geq) tools.append(toggle("GEQ", "", () => openGeq($("mx-proc"), m.key, m.label)));
  if (tools.childElementCount) card.append(tools);
  let cur = m;
  const update = (e) => {
    cur = e;
    name.textContent = e.name || e.label;
    press(on, e.on); press(cue, e.cue);
    if (document.activeElement !== fader) { fader.value = String(Math.round(faderPos(e.level === null || e.level <= -32768 ? null : e.level / 100) * 1000)); val.textContent = `${fmtDb(e.level)} dB`; }
    if (bal && document.activeElement !== bal) { bal.value = String(e.balance ?? 0); bal.title = `Balance ${fmtBal(e.balance ?? 0)}`; }
    for (const c of [on, cue, fader, bal]) if (c) c.disabled = !data.writable.mix;
  };
  return { el: card, update };
}

// ------------------------------------------------------------------ mute masters, assignment

function mutes() {
  const box = $("mx-mutes");
  box.replaceChildren(...data.mute.map((g) => {
    const b = toggle(g.name || `MUTE ${g.index + 1}`, "mute", () => send(`/api/tf5/mix/mute/${g.index}`, { on: g.on === 1 ? 0 : 1 }));
    b.setAttribute("aria-label", `Mute master ${g.index + 1}`);
    press(b, g.on);
    b.disabled = !data.writable.mix;
    return b;
  }));
  const mm = data.mute_map, name = (i) => data.mute[i]?.name || `MUTE ${i + 1}`;
  $("mx-mutemap").textContent = `Channel group g → master g (${mm.channel.map((i) => name(i)).join(", ")}); `
    + `FX groups → ${mm.fx.map((i) => name(i)).join(", ")}. [inferred, not verified on a desk]`;
}

function assignTable() {
  const t = el("table"), head = el("tr");
  head.append(el("th", "", "Channel"), ...Array.from({ length: 8 }, (_, d) => el("th", "", `DCA ${d + 1}`)),
              ...data.mute_map.channel.map((g) => el("th", "", `Mute ${g + 1}`)), el("th", "", "Cue"));
  t.append(head);
  const box = (key, title, onClick) => {
    const c = el("input");
    c.type = "checkbox";
    c.setAttribute("aria-label", title);
    c.onchange = onClick;
    return c;
  };
  for (const s of data.sources) {
    const tr = el("tr"), name = el("td");
    const dca = s.dca.map((_, d) => box(s.key, `${s.label} DCA ${d + 1}`, (e) => send("/api/tf5/mix/assign", { source: s.key, dca: d, on: e.target.checked ? 1 : 0 })));
    const mute = s.mute.map((_, g) => box(s.key, `${s.label} mute ${g + 1}`, (e) => send("/api/tf5/mix/assign", { source: s.key, mute: g, on: e.target.checked ? 1 : 0 })));
    let cur = s;
    const cue = toggle("CUE", "cue", () => setStrip(s.key, { cue: cur.cue === 1 ? 0 : 1 }));
    tr.append(name, ...[...dca, ...mute].map((c) => { const td = el("td"); td.append(c); return td; }));
    const td = el("td"); td.append(cue); tr.append(td);
    t.append(tr);
    refs.set(s.key, { update: (e) => {
      cur = e;
      name.textContent = e.name && e.name !== e.label ? `${e.label} ${e.name}` : e.label;
      e.dca.forEach((v, i) => { dca[i].checked = v === 1; dca[i].disabled = !data.writable.mix; });
      e.mute.forEach((v, i) => { mute[i].checked = v === 1; mute[i].disabled = !data.writable.mix; });
      press(cue, e.cue); cue.disabled = !data.writable.mix;
    } });
  }
  const fx = el("table"), fh = el("tr");
  fh.append(el("th", "", "FX"), ...data.mute_map.fx.map((m) => el("th", "", data.mute[m]?.name || `MUTE ${m + 1}`)));
  fx.append(fh);
  for (const f of data.fx) {
    const tr = el("tr");
    tr.append(el("td", "", f.plugin ? `${f.label} (${f.plugin})` : f.label));
    const cs = f.mute.map((_, g) => box(f.key, `${f.label} mute group ${g + 1}`, (e) => send("/api/tf5/mix/assign", { source: f.key, mute: g, on: e.target.checked ? 1 : 0 })));
    tr.append(...cs.map((c) => { const td = el("td"); td.append(c); return td; }));
    fx.append(tr);
    refs.set(f.key, { update: (e) => e.mute.forEach((v, i) => { cs[i].checked = v === 1; cs[i].disabled = !data.writable.mix; }) });
  }
  $("mx-assign").replaceChildren(t, el("h2", "", "FX mute groups"), fx);
}

// ------------------------------------------------------------------ render, poll

function render() {
  const s = JSON.stringify([data.masters.map((m) => m.key), data.sources.length, data.fx.length]);
  if (s !== sig) {
    sig = s;
    refs.clear();
    const make = (group) => data.masters.filter((m) => (group === "dca") === (m.group === "dca")).map((m) => {
      const r = strip(m); refs.set(m.key, r); return r.el;
    });
    $("mx-masters").replaceChildren(...make("bus"));
    $("mx-dcas").replaceChildren(...make("dca"));
    assignTable();
  }
  for (const e of [...data.masters, ...data.sources, ...data.fx]) refs.get(e.key)?.update(e);
  mutes();
  $("mx-state").textContent = !data.available ? "The desk's editor protocol is off (tf5.eq)."
    : !data.synced ? "Reading the desk…"
    : `${data.simulated ? "Simulated desk. " : ""}${data.writable.mix ? "" : "Read only (tf5.mix_write is off). "}`;
}

let timer = null;
async function poll(now) {
  clearTimeout(timer);
  try {
    const r = await fetch(`/api/tf5/mix${version === null || now ? "" : `?since=${version}`}`);
    const conn = $("conn");
    if (r.status === 404) { conn.textContent = "no TF5"; return; }
    const d = await r.json();
    conn.textContent = "connected";
    if (!d.unchanged) { data = d; render(); }
    version = d.version;
  } catch { $("conn").textContent = "server unreachable"; }
  timer = setTimeout(poll, POLL_MS);
}
poll();

// /mix?geq=st&hz=2500 (a ring's GEQ band, from the ring list) opens that GEQ with the band marked.
const q = new URLSearchParams(location.search);
if (q.get("geq")) openGeq($("mx-proc"), q.get("geq"), q.get("geq").toUpperCase(), Number(q.get("hz")));
