// tf-daemon wireless (Shure QLX-D) view. Native ES module, no build step. Read-only.
// Protocol: see tf_daemon/qlxd/engine.py (JSON state snapshots over /ws/qlxd) and module.py.

import { $, connectWs, loadItem, postJson, save } from "./common.js";

// Battery runtime thresholds (minutes) for the warning colors.
const RUN_WARN_MIN = 60;
const RUN_BAD_MIN = 30;
// RF meter scale (dBm).
const RF_MIN = -100, RF_MAX = -20;
const AUDIO_MIN = -50, AUDIO_MAX = 0;

const BATT_NAMES = { LION: "Li-ion", ALKA: "Alkaline", NIMH: "NiMH", LITH: "Lithium" };

const state = {
  snap: null,
  sort: loadItem("qlxd.sort", "") || "name",
  netLoaded: false,
};

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// ------------------------------------------------------------------ websocket

let wsOpen = false;

function connect() {
  connectWs("/ws/qlxd", {
    onOpen: () => { wsOpen = true; renderStatus(); },
    onClose: () => { wsOpen = false; renderStatus(); },
    onMessage: (data) => {
      const msg = JSON.parse(data);
      if (msg.type !== "state") return;
      state.snap = msg;
      if (!state.netLoaded) loadNet(msg);
      renderStatus();
      renderCards();
    },
  });
}

// ------------------------------------------------------------------ network panel

async function loadInterfaces(selected) {
  const sel = $("iface");
  try {
    const list = await (await fetch("/api/qlxd/interfaces")).json();
    sel.replaceChildren(new Option("Auto (first link-local)", ""), ...list.map((i) => {
      const addrs = i.ipv4.length ? i.ipv4.join(", ") : "no IPv4";
      return new Option(`${i.name} — ${addrs}${i.up ? "" : " (down)"}`, i.name);
    }));
    if (selected && !list.some((i) => i.name === selected)) sel.add(new Option(`${selected} (not present)`, selected));
    sel.value = selected || "";
  } catch (e) {
    $("error").textContent = `⚠ could not list interfaces: ${e.message}`;
  }
}

function loadNet(s) {
  state.netLoaded = true;
  $("hosts").value = s.settings.hosts.join("\n");
  loadInterfaces(s.settings.interface);
  if (!s.demo && !s.network.interface) showNet(true);  // nothing usable yet: open the panel
}

function showNet(open) {
  $("net").hidden = !open;
  $("toggle-net").setAttribute("aria-expanded", String(open));
  if (open && state.snap) loadInterfaces($("iface").value || state.snap.settings.interface);
}

async function post(path, body) {
  const data = await postJson(path, body);
  $("error").textContent = data.ok ? "" : `⚠ ${data.error}`;
  return data;
}

$("toggle-net").onclick = () => showNet($("net").hidden);
$("save-net").onclick = async () => {
  const data = await post("/api/qlxd/settings", { interface: $("iface").value, hosts: $("hosts").value });
  if (data.ok) $("hosts").value = data.settings.hosts.join("\n");
};
$("rescan").onclick = () => post("/api/qlxd/rescan");
$("sort").value = state.sort;
$("sort").onchange = (e) => { state.sort = e.target.value; save("qlxd.sort", state.sort); renderCards(); };

function renderStatus() {
  const s = state.snap, el = $("conn");
  const rx = s ? s.receivers : [];
  const online = rx.filter((r) => r.online).length;
  el.classList.toggle("ok", wsOpen && online > 0);
  el.textContent = !wsOpen ? "server disconnected" : !s ? "connecting…" : `${online}/${rx.length} receivers online`;
  if (!s) return;
  const n = s.network;
  $("net-status").textContent = s.demo ? "Demo mode: simulated receivers on 127.0.0.1" :
    n.interface ? `Listening on ${n.interface} (${n.ip}) · ${n.slp_packets} announcements heard · ${n.probing} probing` +
      (n.error ? ` · ⚠ ${n.error}` : "") :
    `⚠ ${n.error || "no interface"}`;
}

// ------------------------------------------------------------------ cards

function fmtRun(min) {
  const h = Math.floor(min / 60), m = min % 60;
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m`;
}

function ago(ts) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.floor(s / 60)}m ago` : `${Math.floor(s / 3600)}h ago`;
}

// Receivers sometimes report TX_TYPE UNKN for a transmitter that is clearly on
// (battery bars and RF present), so either one counts.
const hasTx = (r) => !!r.tx_type || r.batt_bars != null;

// Severity drives the card's left border: battery and RF problems.
function severity(r) {
  if (!r.online) return "offline";
  if (!hasTx(r)) return "idle";
  if (r.interference) return "bad";
  if (r.batt_run_min != null) {
    if (r.batt_run_min < RUN_BAD_MIN) return "bad";
    if (r.batt_run_min < RUN_WARN_MIN) return "warn";
  } else if (r.batt_bars != null && r.batt_bars <= 1) return "bad";
  return "";
}

function battLevel(r) {
  if (r.batt_run_min != null) return r.batt_run_min < RUN_BAD_MIN ? "bad" : r.batt_run_min < RUN_WARN_MIN ? "warn" : "ok";
  if (r.batt_bars != null) return r.batt_bars <= 1 ? "bad" : r.batt_bars === 2 ? "warn" : "ok";
  return "ok";
}

function battHtml(r) {
  if (!r.online) return "";
  if (!hasTx(r)) return `<div class="batt"><span class="batt-time muted">No transmitter</span></div>`;
  const lvl = battLevel(r);
  const bars = r.batt_bars ?? 0;
  const icon = `<span class="batt-icon lvl-${lvl}" title="${r.batt_bars ?? "?"}/5 bars">` +
    [1, 2, 3, 4, 5].map((i) => `<i class="${i <= bars ? "on" : ""}"></i>`).join("") + "</span>";
  // Runtime minutes only exist for Shure rechargeables; AA packs report bars only.
  const cls = `batt-time ${lvl === "ok" ? "" : lvl}`;
  const time = r.batt_run_min != null ? `<span class="${cls}">${fmtRun(r.batt_run_min)}</span>`
    : r.batt_calculating ? `<span class="batt-time muted">Calculating…</span>`
    : r.batt_bars != null ? `<span class="${cls}">${r.batt_bars}/5</span>`
    : `<span class="batt-time muted">Battery n/a</span>`;
  const detail = [BATT_NAMES[r.batt_type] || r.batt_type, r.batt_charge != null ? `${r.batt_charge}%` : null,
    r.batt_run_min == null && r.batt_bars != null ? "bars" : null].filter(Boolean).join(" · ");
  return `<div class="batt">${icon}<div>${time}<div class="batt-detail">${esc(detail)}</div></div></div>`;
}

function meter(label, cls, value, min, max, unit, extra) {
  const pct = value == null ? 0 : Math.max(0, Math.min(100, (value - min) / (max - min) * 100));
  return `<div class="meter-row"><span>${label}</span>` +
    `<div class="meter ${cls}"><b style="width:${pct.toFixed(1)}%"></b></div>` +
    `<span class="meter-val">${value == null ? "—" : `${value} ${unit}`}</span></div>` + (extra || "");
}

function cardHtml(r) {
  const sev = severity(r);
  const badges = [
    r.interference ? `<span class="badge alert">Interference</span>` : "",
    r.tx_muted ? `<span class="badge alert">Muted</span>` : "",
    r.source !== "found" ? `<span class="badge">${esc(r.source)}</span>` : "",
  ].join("");
  const name = r.name || r.device_id || r.host;
  const freq = r.frequency_mhz != null ? `${r.frequency_mhz.toFixed(3)} MHz` : "—";
  const gc = r.group_chan ? `G/Ch ${esc(r.group_chan)}` : "";
  const live = r.online && hasTx(r);
  const ants = `<div class="meter-row"><span>Ant</span><div class="ants">` +
    `<span class="ant ${live && r.ant_a ? "on" : ""}">A</span><span class="ant ${live && r.ant_b ? "on" : ""}">B</span>` +
    `</div><span></span></div>`;
  const meters = r.online
    ? meter("RF", "", live ? r.rf_dbm : null, RF_MIN, RF_MAX, "dBm") + ants +
      meter("Audio", "audio", live ? r.audio_dbfs : null, AUDIO_MIN, AUDIO_MAX, "dBFS")
    : "";
  const foot = r.online
    ? `<span>${esc(r.tx_type || "")}${r.tx_rf_power ? ` · ${esc(r.tx_rf_power)} power` : ""}</span><span>${esc(r.host)}</span>`
    : `<span class="err" title="${esc(r.error)}">Offline${r.last_seen ? ` · seen ${ago(r.last_seen)}` : ""}${r.error ? ` · ${esc(r.error)}` : ""}</span><span>${esc(r.host)}</span>`;
  return `<article class="card ${sev}">` +
    `<div class="card-head"><span class="card-name" title="${esc(name)}">${esc(name)}</span>${badges}</div>` +
    `<div class="card-sub"><span class="freq">${freq}</span><span>${gc}</span></div>` +
    battHtml(r) + meters +
    `<div class="card-foot">${foot}</div></article>`;
}

function sorted(list) {
  const byName = (a, b) => (a.name || a.host).localeCompare(b.name || b.host, undefined, { numeric: true });
  const out = [...list];
  if (state.sort === "battery") {
    // Online transmitters with known runtime first (lowest first), then the rest.
    const key = (r) => (!r.online || !hasTx(r)) ? 1e9 : r.batt_run_min ?? (r.batt_bars != null ? r.batt_bars * 60 : 1e8);
    out.sort((a, b) => key(a) - key(b) || byName(a, b));
  } else if (state.sort === "freq") {
    out.sort((a, b) => (a.frequency_mhz ?? 1e9) - (b.frequency_mhz ?? 1e9) || byName(a, b));
  } else {
    out.sort(byName);
  }
  return out;
}

function renderCards() {
  const s = state.snap;
  if (!s) return;
  $("cards").innerHTML = sorted(s.receivers).map(cardHtml).join("");
  const empty = $("empty");
  empty.hidden = s.receivers.length > 0;
  if (!s.receivers.length) {
    empty.textContent = s.network.error
      ? `No receivers. ${s.network.error}. Open Network… to choose the interface the receivers are on.`
      : `No receivers yet. Listening on ${s.network.interface} for Shure announcements` +
        ` (they usually arrive within a minute). You can also add receiver IPs under Network….`;
  }
}

setInterval(() => { if (state.snap) renderCards(); }, 5000);  // refresh "seen … ago"

renderStatus();
connect();
