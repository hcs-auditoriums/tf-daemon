// What every page shares: element lookup, local storage, JSON POST, the reconnecting
// websocket, HiDPI canvas sizing, elements and the desk's fader law. Native ES module, no build step.

export const $ = (id) => document.getElementById(id);

// Local storage can be missing or full (private windows); a page then just forgets.
export function loadItem(key, def) {
  try { const v = localStorage.getItem(key); return v === null ? def : v; } catch { return def; }
}
export function save(key, value) { try { localStorage.setItem(key, String(value)); } catch { /* ignore */ } }

// POST (or ``method``) a JSON body; the server's JSON reply, or {ok: false, error} if the request failed.
export async function postJson(path, body, method = "POST") {
  try {
    const res = await fetch(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    const data = await res.json();
    if (!data.ok && !data.error) data.error = res.statusText;
    return data;
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

export const putJson = (path, body) => postJson(path, body, "PUT");

// A websocket to this server's ``path`` that reconnects with backoff (0.5 s doubling to 5 s).
// ``onMessage(data, ws)`` gets each message; the handle sends JSON and tells whether it is open.
export function connectWs(path, { onOpen, onClose, onMessage, binaryType }) {
  let ws = null, retryMs = 500;
  const open = () => {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}${path}`);
    if (binaryType) ws.binaryType = binaryType;
    ws.onopen = () => { retryMs = 500; onOpen?.(); };
    ws.onclose = () => {
      onClose?.();
      setTimeout(open, retryMs);
      retryMs = Math.min(retryMs * 2, 5000);
    };
    ws.onerror = () => ws.close();
    ws.onmessage = (ev) => onMessage(ev.data, ws);
  };
  open();
  return {
    get ws() { return ws; },
    isOpen: () => ws !== null && ws.readyState === WebSocket.OPEN,
    send(obj) { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj)); },
  };
}

// Size a canvas's backing store to its CSS box at the device pixel ratio; returns [w, h] in CSS px.
export function fitCanvas(cv, c) {
  const dpr = window.devicePixelRatio || 1;
  const r = cv.getBoundingClientRect();
  cv.width = Math.round(r.width * dpr);
  cv.height = Math.round(r.height * dpr);
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  return [r.width, r.height];
}

// An element with a class and text.
export function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

// Fader position 0..1 for a level in dB (null = -inf), roughly following a
// console fader's scale so the cap sits where it does on the desk.
const LAW = [[10, 1], [5, 0.92], [0, 0.8], [-5, 0.69], [-10, 0.58], [-20, 0.43], [-30, 0.31],
             [-40, 0.21], [-50, 0.13], [-60, 0.07], [-138, 0]];
export function faderPos(db) {
  if (db === null || db === undefined) return 0;
  if (db >= LAW[0][0]) return 1;
  for (let i = 1; i < LAW.length; i++) {
    const [d1, p1] = LAW[i - 1], [d0, p0] = LAW[i];
    if (db >= d0) return p0 + (db - d0) / (d1 - d0) * (p1 - p0);
  }
  return 0;
}
export function posToDb(pos) {  // the inverse: null below the bottom
  if (pos <= 0.004) return null;
  if (pos >= 1) return LAW[0][0];
  for (let i = 1; i < LAW.length; i++) {
    const [d1, p1] = LAW[i - 1], [d0, p0] = LAW[i];
    if (pos >= p0) return d0 + (pos - p0) / (p1 - p0) * (d1 - d0);
  }
  return null;
}
