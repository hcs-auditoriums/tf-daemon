// /ws/audio: the connection, metadata, and the binary spectrum frames (protocol:
// tf_daemon/audio/engine.py, module.py).

import { onConsoleChanges, onConsoleSnapshot } from "./console.js";
import { buildChannelPicker, markSelected, selectInput } from "./controls.js";
import { onConsoleEq } from "./eq.js";
import { onConsoleMeters } from "./meters.js";
import { autoRange } from "./range.js";
import { onRings } from "./rings.js";
import { connectWs } from "../common.js";
import { $, state } from "./shared.js";
import { wfPush } from "./waterfall.js";

let sock = null;

export function connect() {
  sock = connectWs("/ws/audio", {
    binaryType: "arraybuffer",
    onOpen: () => setConn(true),
    onClose: () => setConn(false),
    onMessage: (data, ws) => {
      if (typeof data !== "string") { onFrame(data); return; }
      const msg = JSON.parse(data);
      if (msg.type === "rings") onRings(msg);
      else if (msg.type === "tf5") onConsoleSnapshot(msg);
      else if (msg.type === "tf5-changes") onConsoleChanges(msg);
      else if (msg.type === "tf5-eq") onConsoleEq(msg);
      else if (msg.type === "tf5-meters") onConsoleMeters(msg);
      else onMeta(msg, ws);
    },
  });
}

export function send(obj) { sock?.send(obj); }

function setConn(ok) {
  const el = $("conn");
  el.classList.toggle("ok", ok);
  el.textContent = ok ? "live" : "disconnected";
}

function onMeta(meta, ws) {
  if (meta.type !== "meta") return;
  // A later meta on the same connection only renames inputs (names read from
  // the console): relabel without dropping the trace or re-sending "view".
  const relabel = state.meta && ws && state.metaWs === ws && meta.freqs.length === state.freqs.length;
  state.meta = meta;
  state.metaWs = ws;
  if (relabel) {
    buildChannelPicker();
    markSelected(state.input);
    return;
  }
  state.freqs = Float32Array.from(meta.freqs);
  state.logMin = Math.log10(meta.freqs[0]);
  state.logMax = Math.log10(meta.freqs[meta.freqs.length - 1]);
  const b = meta.freqs.length;
  state.avg = new Float32Array(b);
  state.peak = new Float32Array(b);
  state.live = new Float32Array(b);
  if (!meta.channels.some((c) => c.index === state.input)) {
    state.input = (meta.channels.find((c) => c.pinned) || meta.channels[0]).index;
  }
  buildChannelPicker();
  selectInput(state.input);
  $("info").textContent =
    `${meta.source} · ${meta.samplerate} Hz · FFT ${meta.fft_size} ` +
    `(${(meta.samplerate / meta.fft_size).toFixed(2)} Hz/bin) · ` +
    `${meta.update_rate.toFixed(1)} upd/s · avg τ ${meta.average_tau} s`;
  state.dirty = true;
}

function onFrame(buf) {
  if (!state.meta) return;
  const dv = new DataView(buf);
  const magic = String.fromCharCode(dv.getUint8(0), dv.getUint8(1), dv.getUint8(2), dv.getUint8(3));
  if (magic !== "TFS3") return;
  state.lastSeq = dv.getUint32(4, true);
  const nRec = dv.getUint16(8, true), nBins = dv.getUint16(10, true);
  if (nBins !== state.freqs.length) return;
  const recSize = 4 + 6 * nBins;  // index, reserved, int16[3][bins]
  for (let r = 0; r < nRec; r++) {
    const off = 12 + r * recSize;
    if (dv.getUint16(off, true) !== state.input) continue;
    const data = new Int16Array(buf, off + 4, 3 * nBins);
    for (let i = 0; i < nBins; i++) {
      state.avg[i] = data[i] / 100;
      state.peak[i] = data[nBins + i] / 100;
      state.live[i] = data[2 * nBins + i] / 100;
    }
    autoRange();
    wfPush(state.wfSource === "avg" ? state.avg : state.live);
    state.frames++;
    state.dirty = true;
    return;
  }
}

setInterval(() => {
  state.fps = state.frames; state.frames = 0;
  if (sock?.isOpen()) {
    // Connected to the server but no frames: the audio device is unplugged or silent.
    $("conn").textContent = state.fps ? `live · ${state.fps} fps` : "no audio (device disconnected?)";
    $("conn").classList.toggle("ok", state.fps > 0);
  }
}, 1000);
