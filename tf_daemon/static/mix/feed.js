// The Mix page's live data: one websocket (WS /ws/tf5, console/feed.py) that pushes the desk's
// mix data whenever it changes, so nothing is polled. mix.js gets each update; an open GEQ / PEQ
// editor (mix/geq.js, mix/peq.js) re-reads its bus only when the desk changed.

import { connectWs } from "../common.js";

const listeners = new Set();   // called with each "mix" update

let sock = null;
export function startFeed({ onData, onState }) {
  sock = connectWs("/ws/tf5", {
    onOpen: () => { onState(true); sock.send({ areas: ["mix"] }); },
    onClose: () => onState(false),
    onMessage: (raw) => {
      const d = JSON.parse(raw);
      if (d.type !== "mix") return;
      onData(d);
      for (const fn of listeners) fn(d);
    },
  });
}

// Call ``fn`` on every desk change while ``alive()`` holds (an editor's panel is still its own).
export function onDeskChange(fn, alive) {
  const wrapped = (d) => { if (alive()) fn(d); else listeners.delete(wrapped); };
  listeners.add(wrapped);
}
