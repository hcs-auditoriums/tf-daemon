// Vocal EQ assist: the mic-check panel and its suggested-EQ overlay.

import { eqAvailable, eqHalf, eqY } from "./eq.js";
import { H, PAD, W, xOf } from "./plot.js";
import { $, C, color, ctx, el, loadInt, save, state } from "./shared.js";

// Optional module (vocal.enabled): during a mic check, listen to the shown
// input while the person talks and suggest HPF / PEQ settings. Suggestions
// only: the desk is never changed. API: /api/vocal/*.
let vocal = null;                 // GET /api/vocal/status; null = module off
let vocalOpen = false;
state.vocalOverlay = loadInt("tf.vocalOverlay", 1) === 1;

export async function pollVocal() {
  let next = 3000;
  try {
    const r = await fetch("/api/vocal/status");
    if (r.status === 404) { vocal = null; $("mic-check").hidden = true; return; }  // module off: stop
    vocal = await r.json();
    $("mic-check").hidden = false;
    const listening = Object.values(vocal.sessions).some((s) => s.state === "listening");
    if (vocalOpen || listening) next = 500;
    renderVocal();
    state.dirty = true;
  } catch { /* server restarting: try again */ }
  setTimeout(pollVocal, next);
}

function vocalPost(path, extra = {}) {
  return fetch(`/api/vocal/${path}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ channel: state.input, ...extra }),
  }).then((r) => r.json()).catch(() => ({ ok: false, error: "server unreachable" }))
    .then((d) => { if (!d.ok && d.error) alert(d.error); setTimeout(pollVocal, 50); });
}

$("mic-check").onclick = () => {
  vocalOpen = !vocalOpen;
  $("mic-check").setAttribute("aria-expanded", String(vocalOpen));
  renderVocal();
  if (vocalOpen) pollVocal();
};

const fmtFreq = (f) => (f >= 1000 ? `${+(f / 1000).toFixed(2)} kHz` : `${Math.round(f)} Hz`);
const fmtGain = (g) => `${g > 0 ? "+" : g < 0 ? "−" : ""}${Math.abs(g).toFixed(1)} dB`;

// The band's current setting on the desk, when the console EQ is readable.
function currentSetting(eq, band) {
  if (!eq) return null;
  if (band === "HPF") return eq.hpf && eq.hpf.on ? `HPF ${fmtFreq(eq.hpf.freq)}` : "HPF off";
  const b = eq.bands.find((x) => x.label === band);
  if (!b) return null;
  if (b.type === "hpf" || b.type === "lpf") return `${b.type.toUpperCase()} ${fmtFreq(b.freq)}`;
  return b.gain ? `${fmtGain(b.gain)} @ ${fmtFreq(b.freq)}, Q ${b.q}` : "flat";
}

export function renderVocal() {
  const panel = $("vocal-panel");
  panel.hidden = !vocal || !vocalOpen;
  if (panel.hidden || !state.meta) return;
  const label = state.meta.channels[state.input]?.label ?? `In ${state.input + 1}`;
  const s = vocal.sessions[String(state.input)];
  const items = [];
  const head = el("div", "vp-head");
  const close = el("button", "vp-x", "×"); close.type = "button"; close.title = "Close";
  close.onclick = () => $("mic-check").click();
  head.append(el("strong", "", `Vocal EQ assist · ${label}`), close);
  items.push(head);
  const button = (text, fn, primary) => {
    const b = el("button", primary ? "vp-btn primary" : "vp-btn", text); b.type = "button"; b.onclick = fn; return b;
  };
  const row = (...els) => { const d = el("div", "vp-row"); d.append(...els); return d; };

  if (vocal.inputs && !vocal.inputs.includes(state.input)) {
    items.push(el("p", "vp-note", `${label} isn't one of the vocal mics (vocal.inputs).`));
  } else if (!s) {
    items.push(el("p", "", "Have the person talk normally into this mic, at their usual distance, for about "
                            + `${Math.round(vocal.listen_s)} s (reading a passage works well).`));
    items.push(row(button("Start listening", () => vocalPost("start"), true),
                   button("Listen live", () => vocalPost("start", { live: true }))));
    items.push(el("p", "vp-note", "Live keeps listening while they talk, updates the suggestion every few "
                                  + "seconds, and checks that the voice reaches the mains."));
  } else if (s.state === "live") {
    items.push(el("p", "vp-live", `● Live · heard ${s.speech_s.toFixed(0)} s of speech`
                                  + (s.f0_hz ? ` · voice pitch ≈ ${Math.round(s.f0_hz)} Hz` : "")));
    const r = s.result || {};
    if (r.mains) items.push(el("p", r.mains.verdict === "weak" ? "vp-error" : "vp-note", `Mains: ${r.mains.note}`));
    if (r.room) items.push(el("p", "vp-note", `Room: ${r.room.note}`));
    if (r.model) items.push(el("p", "vp-note", `${r.model.settled ? "Settled" : "Still settling"} · now ${r.model.off_now_db} dB `
                                              + `from the target, ${r.model.off_after_db} dB with this suggestion`));
    if (r.ok) items.push(...suggestionItems(s, r, label, row, button));
    else items.push(el("p", "vp-note", r.error && s.speech_s >= 8 ? r.error : "First suggestion after about 8 s of speech…"));
    items.push(row(button("Stop", () => vocalPost("stop"), true), button("Clear", () => vocalPost("clear"))));
  } else if (s.state === "listening") {
    const pct = Math.min(100, s.speech_s / s.target_s * 100);
    const bar = el("div", "vp-bar"); const fill = el("span"); fill.style.width = `${pct}%`; bar.append(fill);
    items.push(bar);
    items.push(el("p", "vp-note", `Heard ${s.speech_s.toFixed(1)} of ${Math.round(s.target_s)} s of speech`
                                   + (s.f0_hz ? ` · voice pitch ≈ ${Math.round(s.f0_hz)} Hz` : "")));
    items.push(row(button("Stop & analyze", () => vocalPost("stop"), true), button("Cancel", () => vocalPost("clear"))));
  } else {
    const r = s.result || {};
    if (!r.ok) {
      items.push(el("p", "vp-error", r.error || "No result."));
    } else {
      if (r.mains) items.push(el("p", r.mains.verdict === "weak" ? "vp-error" : "vp-note", `Mains: ${r.mains.note}`));
    if (r.room) items.push(el("p", "vp-note", `Room: ${r.room.note}`));
    if (r.model) items.push(el("p", "vp-note", `${r.model.settled ? "Settled" : "Still settling"} · now ${r.model.off_now_db} dB `
                                              + `from the target, ${r.model.off_after_db} dB with this suggestion`));
      items.push(...suggestionItems(s, r, label, row, button));
    }
    items.push(row(button("Listen again", () => vocalPost("start"), true),
                   button("Listen live", () => vocalPost("start", { live: true })), button("Clear", () => vocalPost("clear"))));
  }
  const key = String(state.input);
  const done = vocal.applied && vocal.applied[key];
  if (done) {
    const when = new Date(done.at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const what = done.action === "undo" ? "Undone" : "Applied to the desk";
    const ok = done.confirmed === done.sent;
    items.push(el("p", ok ? "vp-note" : "vp-error", done.sent
      ? `${what} at ${when}: ${done.confirmed} of ${done.sent} change(s) confirmed by the desk`
        + (ok ? "." : ". Check the channel's EQ on the desk.")
      : `${what} at ${when}: the desk already matched.`));
    if (done.action === "apply" && done.sent) {
      items.push(el("p", "vp-note", "The analyzer listens before the EQ, so the spectrum doesn't change; "
                                   + "the green desk EQ curve shows the new setting."));
    }
  }
  if (vocal.undo && vocal.undo[key]) items.push(row(button("Undo apply", () => vocalDesk("undo", label))));
  items.push(el("p", "vp-foot", vocal.can_write
    ? "The desk is changed only when you press Apply and confirm; Undo puts back what it replaced."
    : "Suggestions only: nothing is changed on the desk."));
  panel.replaceChildren(...items);
}

// The suggestion (table, notes, overlay toggle, compare, apply), for a finished check or live.
function suggestionItems(s, r, label, row, button) {
  const items = [];
  items.push(el("p", "vp-note", `${Math.round(r.speech_s)} s of speech · ${r.voice || "voice"}`
                                 + (r.f0_hz ? ` (≈ ${Math.round(r.f0_hz)} Hz)` : "") + ` · confidence ${r.confidence}`));
  const table = el("table", "vp-table");
  const tr = (cells, cls) => { const t = el("tr", cls || ""); cells.forEach((c) => t.append(el("td", "", c))); return t; };
  const cur = s.current_eq;
  const headRow = el("tr");
  ["Band", "Suggested", "For"].concat(cur ? ["Now"] : []).forEach((h) => headRow.append(el("th", "", h)));
  table.append(headRow);
  if (r.hpf) table.append(tr(["HPF", fmtFreq(r.hpf.freq), r.hpf.issue].concat(cur ? [currentSetting(cur, "HPF") || "–"] : [])));
  for (const b of r.bands) {
    table.append(tr([b.band, `${fmtGain(b.gain)} @ ${fmtFreq(b.freq)}, Q ${b.q}`, b.issue]
      .concat(cur ? [currentSetting(cur, b.band) || "–"] : []), b.kind));
  }
  if (!r.bands.length) table.append(tr(["", "No EQ moves needed", "close to the reference speech spectrum"].concat(cur ? [""] : [])));
  items.push(table);
  for (const n of r.notes) items.push(el("p", "vp-note", n));
  const lbl = el("label", "vp-check");
  const cb = el("input"); cb.type = "checkbox"; cb.checked = state.vocalOverlay;
  cb.onchange = () => { state.vocalOverlay = cb.checked; save("tf.vocalOverlay", cb.checked ? 1 : 0); state.dirty = true; };
  lbl.append(cb, " Show suggested EQ on spectrum");
  items.push(lbl);
  if (eqAvailable && !state.showEq) {  // desk EQ readable but its overlay is off: offer the comparison
    items.push(row(button("Compare with desk EQ", () => {
      const c = $("show-eq"); c.checked = true; c.dispatchEvent(new Event("change"));
      state.vocalOverlay = true; save("tf.vocalOverlay", 1); renderVocal();
    })));
  }
  const ap = s.apply;
  if (vocal.can_write && ap) {
    if (ap.possible) items.push(row(button("Apply to desk…", () => vocalDesk("apply", label, ap.changes), true)));
    else items.push(el("p", "vp-note", `Can't apply to the desk: ${ap.reason}.`));
  }
  return items;
}

// Changes the desk: only from a click, after the person confirms the exact changes.
function vocalDesk(action, label, lines) {
  const msg = action === "apply"
    ? `Change ${label}'s EQ on the desk to the suggestion?\n\n${lines.join("\n")}\n\nUndo puts back the current settings.`
    : `Put ${label}'s EQ on the desk back to how it was before Apply?`;
  if (!window.confirm(msg)) return;
  fetch(`/api/vocal/${action}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ channel: state.input, confirm: true }),
  }).then((r) => r.json()).catch(() => ({ ok: false, error: "server unreachable" }))
    .then((d) => { if (!d.ok) alert(d.error || "Not applied."); setTimeout(pollVocal, 50); });
}

// The suggested EQ (violet, dashed) and, faintly, why: the voice vs reference speech.
export function drawVocal() {
  if (!vocal || !state.vocalOverlay) return;
  const s = vocal.sessions[String(state.input)];
  const r = s && s.result;
  if (!r || !r.ok) return;
  // Same scale as the console EQ overlay (eqY), so the suggested curve lines
  // up with the desk's actual EQ curve when both are shown.
  const top = PAD.t, bot = H - PAD.b;
  const y = (db) => Math.min(bot, Math.max(top, eqY(db)));
  const line = (fs, vs, color, dash, width, alpha) => {
    ctx.beginPath();
    fs.forEach((f, i) => { const xx = xOf(f), yy = y(vs[i]); if (i) ctx.lineTo(xx, yy); else ctx.moveTo(xx, yy); });
    ctx.globalAlpha = alpha; ctx.strokeStyle = color; ctx.lineWidth = width; ctx.setLineDash(dash);
    ctx.stroke(); ctx.setLineDash([]); ctx.globalAlpha = 1;
  };
  const deskEq = state.showEq && eqAvailable && state.eq && state.eq.index === state.input;
  if (!deskEq) {  // otherwise the EQ overlay has drawn this scale already
    ctx.globalAlpha = 0.5; ctx.strokeStyle = C.vocal; ctx.setLineDash([2, 4]); ctx.lineWidth = 1;
    const y0 = Math.round(y(0)) + 0.5;
    ctx.beginPath(); ctx.moveTo(PAD.l, y0); ctx.lineTo(W - PAD.r, y0); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = C.vocal; ctx.font = "11px system-ui, sans-serif"; ctx.textAlign = "right"; ctx.textBaseline = "middle";
    const step = eqHalf() > 40 ? 12 : 6, edge = Math.floor((eqHalf() - 2) / step) * step;
    for (let db = edge; db >= -edge; db -= step) ctx.fillText(`${db > 0 ? "+" : ""}${db}`, W - PAD.r - 4, y(db));
    ctx.globalAlpha = 1;
  }
  line(r.points, r.deviation, C.vocal, [], 1, 0.45);          // why: the voice vs reference speech
  line(r.curve_freqs, r.curve, C.vocal, [7, 4], 2.5, 1);      // what: the suggested EQ
  ctx.font = "11px system-ui, sans-serif"; ctx.textAlign = "left"; ctx.textBaseline = "top";
  ctx.fillStyle = C.vocal;
  ctx.fillText(`suggested EQ (dashed)${deskEq ? " · desk EQ in green" : ""}`, PAD.l + 6, bot - 30);
  ctx.globalAlpha = 0.6;
  ctx.fillText("thin line: voice vs reference speech", PAD.l + 6, bot - 16);
  ctx.globalAlpha = 1; ctx.lineWidth = 1;
}
