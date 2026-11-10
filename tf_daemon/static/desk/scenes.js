// The Desk page's Scenes card: the desk's stored scenes (banks A and B, GET /api/tf5/scenes), the
// current one, and recall with an explicit confirmation step, since a recall changes the whole desk
// (POST /api/tf5/event/recall_scene with "confirm": true; tf5.scene_recall). Storing and undoing
// have no known remote command: shown as unavailable, with the server's reason.

import { $, postJson } from "../common.js";
import { dash, el, note } from "./ui.js";

let pending = null; // the scene number waiting for the person's confirmation
let latest = null; // the last /api/tf5/scenes reply
let refreshAll = () => {};

export const showEmpty = () => $("scene-empty").checked;

export function initScenes(refresh) {
  refreshAll = refresh;
  $("scene-empty").addEventListener("change", () => refresh("scenes"));
  $("scene-cancel").addEventListener("click", () => { pending = null; drawConfirm(); });
  $("scene-confirm-btn").addEventListener("click", recall);
}

function title(number) {
  const s = latest?.scenes.find((x) => x.number === number);
  return s?.title ? `${number} “${s.title}”` : number;
}

export function renderScenes(d) {
  latest = d;
  const cur = d.current;
  $("scene-current").textContent = cur.number ? title(cur.number) + (cur.edited ? " (edited since recall)" : "") : "–";
  $("scene-selected").textContent = dash(cur.selected);
  const lr = cur.last_recalled;
  $("scene-last").textContent = lr ? `${dash(lr.number)}: ${dash(lr.status)} at ${new Date(lr.at * 1000).toLocaleTimeString()}` : "none since connecting";
  $("scene-count").textContent = d.synced ? `${d.stored} of ${d.slots} slots stored` : "waiting for the desk's scene data…";
  for (const [id, what] of [["scene-store", d.store], ["scene-undo", d.undo]]) {
    $(id).disabled = !what.available;
    $(id).title = what.reason || "";
  }
  $("scene-unavailable").textContent = [`Store: ${d.store.reason}.`, `Undo: ${d.undo.reason}.`].join(" ");
  $("scene-recall-off").textContent = d.recall.available ? "" : d.recall.reason;
  const body = $("scene-rows");
  body.replaceChildren(
    ...d.scenes.map((s) =>
      el(
        "tr",
        { class: [s.number === cur.number ? "current" : "", s.stored ? "" : "empty"].join(" ").trim() },
        el("td", { class: "num" }, s.number),
        el("td", {}, s.title || (s.stored ? "(no title)" : "(empty)"), s.protect ? el("span", { class: "tag", title: "protected on the desk" }, "protected") : null),
        el("td", { class: "muted" }, s.comment),
        el("td", { class: "num" }, s.fade.on ? `${s.fade.time_s} s` : ""),
        el("td", { class: "num muted" }, dash(s.time_stamp)),
        el(
          "td",
          {},
          el(
            "button",
            {
              type: "button",
              class: "recall",
              "data-number": s.number,
              disabled: !d.recall.available || !d.synced || !s.stored,
              title: d.recall.available ? `Recall ${s.number} (asks first)` : d.recall.reason,
              onclick: () => { pending = s.number; drawConfirm(); },
            },
            "Recall…",
          ),
        ),
      ),
    ),
  );
  if (!d.scenes.length) body.append(el("tr", {}, el("td", { colspan: 6, class: "muted" }, d.synced ? "No stored scenes." : "–")));
  drawConfirm();
}

function drawConfirm() {
  const box = $("scene-confirm");
  box.hidden = pending === null;
  if (pending === null) return;
  $("scene-confirm-text").textContent =
    `Recall ${title(pending)}? This changes the whole desk: faders, EQ, dynamics, routing and every other ` +
    "scene parameter, except what is recall-safe.";
  $("scene-confirm-btn").textContent = `Recall ${pending}`;
  $("scene-confirm-btn").disabled = false;
}

async function recall() {
  const number = pending;
  if (number === null) return;
  $("scene-confirm-btn").disabled = true;
  note($("scene-msg"), `recalling ${number}…`);
  const res = await postJson("/api/tf5/event/recall_scene", { number, confirm: true });
  pending = null;
  drawConfirm();
  if (!res.ok) note($("scene-msg"), `recall ${number}: ${res.error}`, true);
  else if (!res.confirmed) note($("scene-msg"), `recall ${number} sent; the desk hasn't confirmed it`, true);
  else note($("scene-msg"), `recalled ${number} (desk: ${res.status})`);
  refreshAll(); // the whole desk may have changed
}
