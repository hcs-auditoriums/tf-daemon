// Small DOM helpers the Desk page's cards share (desk.js, desk/*.js).

import { postJson } from "../common.js";

// An element: el("button", { class: "x", type: "button", onclick: fn, disabled: true }, "text", child, ...).
export function el(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === false || v === null || v === undefined) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k in e && typeof v !== "string") e[k] = v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined) e.append(kid instanceof Node ? kid : String(kid));
  return e;
}

// Show the desk's value in an input, unless a person is editing it (a field focused, or changed and
// not applied yet). A checkbox always shows the desk (a click sets it at once).
export function setValue(input, value) {
  if (input.dataset.dirty || (input.type !== "checkbox" && document.activeElement === input)) return;
  if (input.type === "checkbox") input.checked = !!value;
  else input.value = value ?? "";
}

// Mark an input as changed by a person (setValue then leaves it alone until it is applied or reverted).
export function markDirty(input) { input.dataset.dirty = "1"; }
export function clearDirty(...inputs) { for (const i of inputs) delete i.dataset.dirty; }

// A status line: text, red when bad.
export function note(node, text, bad = false) {
  node.textContent = text;
  node.classList.toggle("error", !!bad);
}

// SET parameters on the desk (POST /api/tf5/param, guard.py checks each); only ever called from a
// person's click. The reply: {ok, results: [{path, confirmed, actual}]} or {ok: false, error}.
export function sendParams(func, changes) {
  return postJson("/api/tf5/param", { func, changes, confirm: true });
}

// A one-line summary of a sendParams reply.
export function describe(res) {
  if (!res.ok) return [res.error || "failed", true];
  const late = res.results.filter((r) => !r.confirmed).length;
  return late ? [`sent; ${late} not confirmed by the desk yet`, true] : ["set on the desk", false];
}

export const dash = (v) => (v === null || v === undefined || v === "" ? "–" : String(v));
