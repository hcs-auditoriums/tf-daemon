// Ring-out: the setup dialog and the popup that stays up while the software drives the desk.
//
// While a ring-out runs, every open audio page shows the popup (and its polling is the
// server's sign that someone is watching: if no page polls for a while, the server pulls
// the faders back by itself). "Mute & cancel" (or Esc) mutes the ring-out's channels at
// once and puts their faders and EQ back. API: /api/vocal/ringout*.

import { $, el, loadStr, save, state } from "./shared.js";

let ro = null;            // GET /api/vocal/ringout
let closedFor = null;     // the session (its start time) whose finished popup was closed
let watched = null;       // the session this page saw running (only its result is shown after)
let busy = false;         // a request from the popup is in flight

const db = (v) => (v === null || v === undefined ? "–" : `${v > 0 ? "+" : ""}${v.toFixed(1)} dB`);
const hz = (f) => (f >= 1000 ? `${+(f / 1000).toFixed(2)} kHz` : `${Math.round(f)} Hz`);

export function micKey(ch) { return `tf.vocalMic.${ch}`; }
export function styleKey(ch) { return `tf.vocalStyle.${ch}`; }

function overlay() {
  let o = $("ringout");
  if (!o) {
    o = el("div", "ro-overlay");
    o.id = "ringout";
    o.hidden = true;
    o.setAttribute("role", "dialog");
    o.setAttribute("aria-modal", "true");
    document.body.append(o);
  }
  return o;
}

export async function pollRingout() {
  let next = 3000;
  try {
    const r = await fetch("/api/vocal/ringout");
    if (r.ok) {
      ro = await r.json();
      if (ro.allowed) {
        if (ro.running) next = 500;
        renderPopup();
      }
    } else if (r.status === 404) {
      return;  // vocal assist off: nothing to watch
    }
  } catch { next = 1000; /* server restarting */ }
  setTimeout(pollRingout, next);
}

function post(path, body = {}) {
  busy = true;
  return fetch(`/api/vocal/ringout/${path}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  }).then((r) => r.json()).catch(() => ({ ok: false, error: "server unreachable" }))
    .then((d) => {
      busy = false;
      if (d.ok) { ro = { allowed: true, ...d }; closedFor = null; }
      renderPopup(d.ok ? null : d.error);
      return d;
    });
}

export function abortRingout() { return post("abort"); }

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && ro && ro.running) { e.preventDefault(); abortRingout(); }
});

// ------------------------------------------------------------------ setup

export function openRingoutSetup(vocal) {
  const o = overlay();
  o.dataset.kind = "setup";
  const box = el("div", "ro-box");
  const chans = vocal.inputs && vocal.inputs.length ? vocal.inputs : [state.input];
  const label = (i) => state.meta?.channels[i]?.label ?? `In ${i + 1}`;
  box.append(el("h3", "ro-title", "Ring out"));
  box.append(el("p", "", "The software takes over the chosen mics' faders and EQ. One mic at a time, it puts the "
    + "high-pass on and raises the fader until the mic starts to ring, notches the ring (or raises the high-pass for "
    + "a low one) and goes on, until it reaches "
    + `${db(ro?.ceiling_db ?? 6)} or runs out of notches; then all of them together. `
    + `Each fader ends at least ${db(ro?.margin_db ?? 6).replace("+", "")} under where it rang, and never above where you had it.`));
  box.append(el("p", "ro-note", "Have each person talk and sing while their mic is rung out: their voice gives the "
    + "vocal curve (high-pass, tone and the EQ type's character) that goes on the bands the notches leave. Put each mic "
    + "where it is used (on the actor, downstage too) and mute other open mics. Keep this page open: Mute & cancel "
    + "(or Esc) mutes them all at once and puts everything back."));
  const table = el("table", "vp-table ro-table");
  const head = el("tr");
  ["", "Mic", "Type", "EQ for"].forEach((h) => head.append(el("th", "", h)));
  table.append(head);
  const rows = [];
  for (const ch of chans) {
    const tr = el("tr");
    const cb = el("input"); cb.type = "checkbox"; cb.checked = ch === state.input || chans.length <= 4;
    cb.setAttribute("aria-label", `ring out ${label(ch)}`);
    const mic = picker(vocal.mic_types, loadStr(micKey(ch), vocal.mic_type, vocal.mic_types.map((m) => m.id)));
    const style = picker(vocal.eq_styles, loadStr(styleKey(ch), vocal.eq_style, vocal.eq_styles.map((m) => m.id)));
    mic.onchange = () => save(micKey(ch), mic.value);
    style.onchange = () => save(styleKey(ch), style.value);
    const cells = [cb, el("span", "", label(ch)), mic, style];
    cells.forEach((c) => { const td = el("td"); td.append(c); tr.append(td); });
    table.append(tr);
    rows.push({ ch, cb, mic, style });
  }
  box.append(table);
  const toneLbl = el("label", "vp-check");
  const tone = el("input"); tone.type = "checkbox"; tone.checked = true;
  toneLbl.append(tone, " Then adjust each mic's tone live while they talk or sing (until you press Done)");
  box.append(toneLbl);
  const err = el("p", "vp-error"); err.hidden = true;
  box.append(err);
  const start = el("button", "vp-btn ro-go", "Start ring-out"); start.type = "button";
  const cancel = el("button", "vp-btn", "Cancel"); cancel.type = "button";
  cancel.onclick = () => { o.hidden = true; };
  start.onclick = async () => {
    const mics = rows.filter((r) => r.cb.checked).map((r) => ({ channel: r.ch, mic: r.mic.value, style: r.style.value }));
    if (!mics.length) { err.textContent = "Choose at least one mic."; err.hidden = false; return; }
    start.disabled = true;
    const d = await post("start", { mics, tone: tone.checked, confirm: true });
    start.disabled = false;
    if (!d.ok) { err.textContent = d.error || "Couldn't start."; err.hidden = false; renderSetupAgain(o, box); }
  };
  const row = el("div", "vp-row"); row.append(start, cancel);
  box.append(row);
  o.replaceChildren(box);
  o.hidden = false;
}

function renderSetupAgain(o, box) { o.dataset.kind = "setup"; o.replaceChildren(box); o.hidden = false; }

function picker(options, value) {
  const s = el("select", "ro-pick");
  for (const opt of options) {
    const o = el("option", "", opt.label); o.value = opt.id; o.title = opt.note; s.append(o);
  }
  s.value = value;
  return s;
}

// ------------------------------------------------------------------ the popup

const PHASE = {
  "ringing out": "Ringing out", together: "All mics together", tone: "Live tone EQ",
  finished: "Finished", aborted: "Muted and cancelled", stopped: "Stopped", error: "Stopped",
};

function renderPopup(error) {
  const o = overlay();
  if (!ro || !ro.allowed || ro.phase === "idle" || !ro.phase) {
    if (o.dataset.kind === "popup") o.hidden = true;
    return;
  }
  if (ro.running) watched = ro.started;
  if (!ro.running && (closedFor === ro.started || watched !== ro.started)) {
    if (o.dataset.kind === "popup") o.hidden = true;
    return;
  }
  if (!ro.running && o.dataset.kind !== "popup" && !o.hidden) return;  // the setup dialog is up
  o.dataset.kind = "popup";
  const box = el("div", `ro-box ro-popup${ro.running ? " running" : ""}`);
  const head = el("div", "ro-head");
  head.append(el("strong", ro.running ? "vp-live" : "", `${ro.running ? "● " : ""}${PHASE[ro.phase] || ro.phase}`));
  box.append(head);
  if (ro.running) {
    const stop = el("button", "ro-abort", "Mute & cancel (Esc)"); stop.type = "button";
    stop.onclick = () => abortRingout();
    box.append(stop);
  }
  box.append(el("p", ro.phase === "error" || ro.phase === "stopped" ? "vp-error" : "", ro.message));
  if (error) box.append(el("p", "vp-error", error));
  const table = el("table", "vp-table ro-table");
  const hr = el("tr");
  ["Mic", "", "Fader", "First rang", "Rings at", "HPF", "Notches"].forEach((h) => hr.append(el("th", "", h)));
  table.append(hr);
  for (const m of ro.mics) {
    const tr = el("tr", ro.current === m.index ? "ro-now" : "");
    const notches = m.notches.map((n) => `${n.band} ${hz(n.freq)} ${n.gain} dB`).join(", ") || "–";
    [m.label, m.state === "ringing out" ? "●" : m.state === "done" ? "✓" : "", `${db(m.level_db)}${m.on ? "" : " (off)"}`,
      db(m.first_ring_db), db(m.limit_db), m.hpf_hz ? hz(m.hpf_hz) : "off", notches]
      .forEach((c) => tr.append(el("td", "", c)));
    table.append(tr);
    const sub = (text) => { const nr = el("tr"); const td = el("td", "ro-mnote", text); td.colSpan = 7; nr.append(td); table.append(nr); };
    if (m.note) sub(m.note);
    if (m.shaped && m.shaped.length) sub(`Vocal curve: ${m.shaped.join("; ")}`);
  }
  box.append(table);
  if (ro.together_db !== null && ro.together_db !== undefined) {
    box.append(el("p", "vp-note", `With all of them open they rang ${db(ro.together_db)} over their safe levels; `
      + "faders were set to keep the margin."));
  }
  for (const s of ro.shared || []) {
    box.append(el("p", "vp-note", `${hz(s.freq)} rang on ${s.mics.join(", ")}: the system rings there `
      + "(PA, room, or the capsules' shared peak). A narrow cut on the stereo bus PEQ would serve them all."));
  }
  const log = (ro.log || []).slice(-4).reverse();
  for (const e of log) {
    const when = new Date(e.at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    box.append(el("p", "vp-note ro-log", `${when} · ${e.why}`));
  }
  const row = el("div", "vp-row");
  if (ro.running) {
    const done = el("button", "vp-btn", ro.phase === "tone" ? "Done" : "Stop here (keep notches)"); done.type = "button";
    done.disabled = busy;
    done.onclick = () => post("finish");
    row.append(done);
  } else {
    if (ro.phase === "aborted") {
      const un = el("button", "vp-btn primary", "Unmute"); un.type = "button";
      un.onclick = () => post("unmute");
      row.append(un);
    }
    const close = el("button", "vp-btn", "Close"); close.type = "button";
    close.onclick = () => { closedFor = ro.started; o.hidden = true; };
    row.append(close);
  }
  box.append(row);
  o.replaceChildren(box);
  o.hidden = false;
}
