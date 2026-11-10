// The Desk page's USB recorder card, read-only (GET /api/tf5/recorder; console/recorder.py):
// transport state, now playing, the playlist and the recorder's levels. The transport buttons are
// shown but never enabled: the desk's transport command strings are unverified.

import { $ } from "../common.js";
import { dash, el } from "./ui.js";

const clock = (s) => (Number.isInteger(s) ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : "–");
const db = (v) => (v === null || v === undefined ? "–" : `${v > 0 ? "+" : ""}${v.toFixed(1)} dB`);

export function renderRecorder(d) {
  const t = d.transport, cur = d.current, pl = d.playlist, st = d.setup;
  $("rec-note").textContent = d.synced ? `Read-only: ${d.reason}.` : "waiting for the desk's recorder status (Status function)…";
  const facts = [
    ["Transport", `state ${dash(t.state)} · rec state ${dash(t.rec_state)} · continue ${dash(t.play_continue)} (raw: what each number means is unverified)`],
    ["Now playing", cur.file ? `${cur.file} (song no. ${dash(cur.song_no)}) · ${clock(cur.position)} / ${clock(cur.play_time)}` : "–"],
    ["Folder", `${dash(pl.dir)} · ${dash(pl.total)} songs · device ${dash(pl.device_kind)}`],
    ["Record", `${db(st.record_level_db)} · ${dash(st.format)} · source ${dash(st.source)}`],
    ["Playback", `${db(st.playback_level_db)} · mode ${dash(st.playback_mode)}`],
  ];
  $("rec-facts").replaceChildren(...facts.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]));
  $("rec-controls").replaceChildren(
    ...d.controls.map((c) => el("button", { type: "button", disabled: !c.available, title: c.reason || "" }, c.name)),
  );
  $("rec-songs").replaceChildren(
    ...pl.songs.map((s) =>
      el(
        "tr",
        { class: cur.file && s.file === cur.file ? "current" : "" },
        el("td", { class: "num" }, s.index + 1),
        el("td", {}, dash(s.file)),
        el("td", { class: "muted" }, s.artist || ""),
        el("td", { class: "num" }, clock(s.play_time)),
        el("td", {}, s.checked ? "✓" : ""),
      ),
    ),
  );
}
