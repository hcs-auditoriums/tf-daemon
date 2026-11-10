// The channel bar's scrolling. The tiles are one row of every input that scrolls sideways (a
// swipe, the mouse wheel, or a mouse drag: press, hold and move), with no scroll bar. Beside
// it the minimap is the whole desk in small: a bar per input (its colour, as tall as its
// fader, grey when off, the ring's colour while one comes from it, the selected one marked),
// the bank numbers (1, 9, 17, ...) under it, and a window over the inputs in view. Press or
// drag on it (or turn the wheel over it) to move the row there.

import { $ } from "./shared.js";

const BANK = 8;
const row = $("channels"), map = $("minimap");
let bars = [], labels = [], view = null;
const el = (tag, cls, text) => Object.assign(document.createElement(tag), { className: cls, textContent: text ?? "" });

export function buildMinimap(list) {
  const track = el("div", "mm-bars"), under = el("div", "mm-labels");
  view = el("div", "mm-view");
  bars = list.map((c, i) => {
    const b = el("span", i && i % BANK === 0 ? "mm-bar mm-bank" : "mm-bar");
    track.append(b);
    return b;
  });
  labels = [];
  for (let i = 0; i < list.length; i += BANK) {
    const l = el("span", "mm-label", String(list[i].index + 1));
    l.dataset.bar = i;
    labels.push(l);
  }
  under.append(...labels);
  map.replaceChildren(track, under, view);
  map.title = "All the inputs: press or drag to move the row there";
  requestAnimationFrame(placeView);
}

// The tiles' state, copied onto their bars (paintChannelTiles calls this once a frame at most).
export function paintMinimap() {
  const tiles = row.children;
  bars.forEach((b, i) => {
    const t = tiles[i];
    if (!t) return;
    for (const p of ["--ch", "--fader"]) {
      const v = t.style.getPropertyValue(p);
      if (v) b.style.setProperty(p, v); else b.style.removeProperty(p);
    }
    b.classList.toggle("off", t.classList.contains("off"));
    b.classList.toggle("sel", t.getAttribute("aria-pressed") === "true");
    if (t.dataset.ring) b.dataset.ring = t.dataset.ring; else delete b.dataset.ring;
  });
}

// Tile geometry (the row is the tiles' offset parent: a tile's offsetLeft is its scroll
// position): the first tile's left edge and the distance from one tile to the next.
function stride() {
  const t = row.children;
  if (!t.length) return null;
  return { first: t[0].offsetLeft, step: t.length > 1 ? t[1].offsetLeft - t[0].offsetLeft : t[0].offsetWidth };
}
// The minimap x of a (fractional) tile index, and back.
function barX(f) {
  const i = Math.max(0, Math.min(bars.length - 1, Math.floor(f)));
  const b = bars[i], next = bars[i + 1];
  return b.offsetLeft + (f - i) * (next ? next.offsetLeft - b.offsetLeft : b.offsetWidth + 1);
}
function tileAt(x) {
  let i = 0;
  while (i + 1 < bars.length && bars[i + 1].offsetLeft <= x) i++;
  const b = bars[i], next = bars[i + 1];
  return i + (x - b.offsetLeft) / (next ? next.offsetLeft - b.offsetLeft : b.offsetWidth + 1);
}

let placing = false;
function placeView() {
  placing = false;
  const s = stride();
  if (!s || !view || !bars.length) return;
  const under = labels[0]?.parentElement;   // each under its bank's first bar, kept inside the minimap
  for (const l of labels) {
    const x = bars[Number(l.dataset.bar)].offsetLeft - under.offsetLeft;
    l.style.left = `${Math.max(0, Math.min(x, under.clientWidth - l.offsetWidth))}px`;
  }
  const f0 = (row.scrollLeft - s.first) / s.step, f1 = f0 + row.clientWidth / s.step;
  const x0 = barX(Math.max(0, f0)), x1 = barX(Math.min(bars.length, f1));
  view.style.left = `${x0 - 3}px`;
  view.style.width = `${Math.max(6, x1 - x0 + 4)}px`;
}
const queueView = () => { if (!placing) { placing = true; requestAnimationFrame(placeView); } };
row.addEventListener("scroll", queueView, { passive: true });
new ResizeObserver(queueView).observe(row);

// Bring a tile into view (the selected input), scrolling only as far as needed, to a tile's
// edge (where the row snaps: anywhere else it would snap back and cut the tile off).
export function revealTile(index) {
  const t = row.querySelector(`[data-input="${index}"]`);
  if (!t) return;
  const left = t.offsetLeft, right = left + t.offsetWidth;
  if (left < row.scrollLeft) row.scrollTo({ left, behavior: "smooth" });
  else if (right > row.scrollLeft + row.clientWidth) {
    const edge = [...row.children].find((c) => c.offsetLeft >= right - row.clientWidth) || t;
    row.scrollTo({ left: edge.offsetLeft, behavior: "smooth" });
  }
}

// The tiles fully in view, left to right (for the 1-8 keys).
export function tilesInView() {
  const lo = row.scrollLeft - 1, hi = row.scrollLeft + row.clientWidth + 1;
  return [...row.children].filter((t) => t.offsetLeft >= lo && t.offsetLeft + t.offsetWidth <= hi);
}

// The minimap: press or drag to centre the row there; the wheel scrolls the row.
function goTo(e) {
  const s = stride();
  if (!s) return;
  const x = e.clientX - map.getBoundingClientRect().left - map.clientLeft;
  row.scrollLeft = s.first + tileAt(x) * s.step - row.clientWidth / 2;
}
let mapDrag = null;
map.addEventListener("pointerdown", (e) => {
  if (e.button !== 0) return;
  mapDrag = e.pointerId;
  map.setPointerCapture(e.pointerId);
  row.classList.add("dragging");
  goTo(e);
});
map.addEventListener("pointermove", (e) => { if (mapDrag === e.pointerId) goTo(e); });
const mapEnd = () => { mapDrag = null; row.classList.remove("dragging"); };
map.addEventListener("pointerup", mapEnd);
map.addEventListener("pointercancel", mapEnd);

// The wheel scrolls the row sideways (over the tiles or the minimap).
function wheel(e) {
  const d = Math.abs(e.deltaY) > Math.abs(e.deltaX) ? e.deltaY : e.deltaX;
  if (!d) return;
  e.preventDefault();
  row.scrollLeft += d;
}
row.addEventListener("wheel", wheel, { passive: false });
map.addEventListener("wheel", wheel, { passive: false });

// A mouse drag on the tiles scrolls them (touch scrolls natively). Past a few pixels it's a
// drag, not a tap: the click that ends it doesn't select the tile under the pointer.
let drag = null, swallowClick = false;
row.addEventListener("pointerdown", (e) => {
  swallowClick = false;
  if (e.pointerType !== "mouse" || e.button !== 0) return;
  drag = { id: e.pointerId, x: e.clientX, left: row.scrollLeft, moved: false };
});
row.addEventListener("pointermove", (e) => {
  if (!drag || drag.id !== e.pointerId) return;
  const dx = e.clientX - drag.x;
  if (!drag.moved && Math.abs(dx) > 6) {
    drag.moved = true;
    row.setPointerCapture(e.pointerId);
    row.classList.add("dragging");
  }
  if (drag.moved) row.scrollLeft = drag.left - dx;
});
const dragEnd = () => {
  if (drag?.moved) swallowClick = true;
  drag = null;
  row.classList.remove("dragging");
};
row.addEventListener("pointerup", dragEnd);
row.addEventListener("pointercancel", dragEnd);
row.addEventListener("click", (e) => {
  if (!swallowClick) return;
  swallowClick = false;
  e.stopPropagation();
  e.preventDefault();
}, true);
