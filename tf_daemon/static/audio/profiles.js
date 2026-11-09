// EQ profiles in the console sidebar: save the shown channel's EQ under a name (e.g. per
// actor), recall a profile onto it, copy another channel's EQ onto it, undo. Recall and copy
// change the desk, so they ask first. API: /api/vocal/profiles* (vocal assist on).

import { el, state } from "./shared.js";

let profiles = null;    // null: not available (vocal assist off, or no desk EQ)
let canWrite = false;
let bar = null;
const undoable = new Set();   // inputs where the last recall / copy can be undone here

export function profileBar() {
  bar = el("div", "cp-profiles");
  bar.hidden = true;
  fill();
  refreshProfiles();
  return bar;
}

export async function refreshProfiles() {
  try {
    const r = await fetch("/api/vocal/profiles");
    if (r.ok) {
      const d = await r.json();
      profiles = d.profiles;
      canWrite = d.can_write;
    } else {
      profiles = null;
    }
  } catch { /* server restarting: keep what we have */ }
  fill();
}

function post(path, body) {
  return fetch(`/api/vocal/${path}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  }).then((r) => r.json()).catch(() => ({ ok: false, error: "server unreachable" }))
    .then((d) => { if (!d.ok) alert(d.error || "Not done."); refreshProfiles(); return d; });
}

function button(text, title, fn) {
  const b = el("button", "cp-btn", text);
  b.type = "button";
  b.title = title;
  b.onclick = fn;
  return b;
}

function fill() {
  if (!bar) return;
  bar.hidden = profiles === null;
  if (profiles === null) return;
  const input = state.input;
  const label = state.meta?.channels?.[input]?.label ?? `In ${input + 1}`;

  const pick = el("select", "cp-pick");
  pick.setAttribute("aria-label", "EQ profile");
  pick.append(new Option(profiles.length ? "Profiles…" : "No profiles yet", ""));
  for (const p of profiles) {
    const o = new Option(p.name, p.name);
    o.title = `${p.summary}${p.from ? ` · saved from ${p.from}` : ""}`;
    pick.append(o);
  }
  const chosen = () => profiles.find((p) => p.name === pick.value);
  const recall = button("Recall", "Put this profile on the channel's EQ", () => {
    const p = chosen();
    if (!p || !window.confirm(`Put profile "${p.name}" on ${label}'s EQ?\n\n${p.summary}\n\nUndo puts back the current EQ.`)) return;
    post("profiles/recall", { channel: input, name: p.name, confirm: true }).then((d) => { if (d.ok) undoable.add(input); });
  });
  const del = button("Delete", "Delete this profile", () => {
    const p = chosen();
    if (p && window.confirm(`Delete profile "${p.name}"?`)) post("profiles/delete", { name: p.name });
  });
  recall.disabled = del.disabled = true;
  pick.onchange = () => { recall.disabled = !pick.value || !canWrite; del.disabled = !pick.value; };

  const saveAs = button("Save as…", "Save this channel's EQ as a profile", () => {
    const name = window.prompt(`Save ${label}'s EQ as:`, label);
    if (name && name.trim()) post("profiles/save", { channel: input, name });
  });

  const copy = el("select", "cp-pick");
  copy.setAttribute("aria-label", "Copy EQ from");
  copy.append(new Option("Copy from…", ""));
  for (const ch of state.meta?.channels || []) {
    if (ch.index !== input) copy.append(new Option(ch.label, String(ch.index)));
  }
  copy.disabled = !canWrite;
  copy.onchange = () => {
    const from = Number(copy.value);
    const src = copy.options[copy.selectedIndex].text;
    copy.value = "";
    if (!Number.isInteger(from) || !window.confirm(`Copy ${src}'s EQ onto ${label}?\n\nUndo puts back the current EQ.`)) return;
    post("profiles/copy", { channel: input, from, confirm: true }).then((d) => { if (d.ok) undoable.add(input); });
  };

  const rows = [el("div", "cp-prow"), el("div", "cp-prow")];
  rows[0].append(pick, recall, del);
  rows[1].append(saveAs, copy);
  if (undoable.has(input)) {
    rows[1].append(button("Undo", "Put back the EQ from before the recall / copy", () => {
      if (!window.confirm(`Put ${label}'s EQ back to how it was?`)) return;
      post("undo", { channel: input, confirm: true }).then((d) => { if (d.ok) undoable.delete(input); });
    }));
  }
  bar.replaceChildren(...rows);
}
