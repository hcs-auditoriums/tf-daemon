// Keyboard: shortcuts and the command palette, shared by every page.
//
// - Shift+← / Shift+→: previous / next page tab (Audio, RF, Wireless, Config), wrapping.
// - "/" (or Ctrl+K): the command palette. Type to filter, Tab / Shift+Tab (or ↑ ↓) to choose,
//   Enter to run, Esc to close. It lists every command a page registered, with its shortcut,
//   and on every page: the other pages, the server (updates, restart), full screen (F), reload.
// - "?": every command of the page by group, with its shortcut (each one clickable).
// Every shortcut and command is listed for people in docs/keyboard.md.
// - A page's own shortcuts: addCommands() with a `keys` list ("e", "[", "mod+z",
//   "mod+shift+z"; mod is Ctrl, or Cmd on a Mac).
//
// Shortcuts stay out of the way: never while typing or choosing (text fields, the config
// editor, dropdowns, sliders, anything editable), not when a page already handled the key,
// and a command whose `when()` is false lets the key through (Ctrl+Z with no EQ to undo).

import { el } from "./common.js";

// A command: {title, run, keys?: [...], when?: () => bool, group?: "EQ", help?: false}. A provider
// is a function returning commands, called each time the palette opens (e.g. one per channel).
// help: false leaves a command out of the ? list (one per channel would bury the rest); a
// command without `run` is only a line in the ? list ("1–8: tile of this bank").
const providers = [];
export function addCommands(provider) { providers.push(typeof provider === "function" ? provider : () => provider); }

const TYPING = new Set(["text", "number", "search", "email", "url", "tel", "password", "range", "radio",
                        "date", "time", "datetime-local", "month", "week", "color"]);
function busy(node) {
  if (!node || node === document.body) return false;
  if (node.isContentEditable) return true;
  const tag = node.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  return tag === "INPUT" && TYPING.has((node.type || "text").toLowerCase());
}

// The pages, from the tab bar itself.
const tabs = () => [...document.querySelectorAll("nav.tabs a[href]")];
function goPage(by) {
  const all = tabs(), here = all.findIndex((a) => a.getAttribute("aria-current") === "page");
  if (all.length < 2 || here < 0) return;
  window.location.href = all[(here + by + all.length) % all.length].href;
}
addCommands(() => [
  { title: "Next page", keys: ["shift+arrowright"], run: () => goPage(1), group: "Pages" },
  { title: "Previous page", keys: ["shift+arrowleft"], run: () => goPage(-1), group: "Pages" },
  ...tabs().filter((a) => a.getAttribute("aria-current") !== "page")
    .map((a) => ({ title: `Go to ${a.textContent}`, run: () => { window.location.href = a.href; }, group: "Pages" })),
]);

// The server, from any page. Updating is followed on the Config page (it asks first there).
async function restartServer() {
  if (!confirm("Restart tf-daemon now? Every page loses its live view for a few seconds.")) return;
  try {
    const r = await fetch("/api/system/restart", { method: "POST" });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) alert(`Couldn't restart: ${data.error || r.statusText}`);
  } catch (e) { alert(`Couldn't restart: ${e.message}`); }
}
const toConfig = (hash) => () => { window.location.href = `/config${hash}`; };
addCommands(() => [
  { title: "Check for updates", group: "Server", run: toConfig("") },
  { title: "Update now…", group: "Server", run: toConfig("#update") },
  { title: "Restart server…", group: "Server", run: restartServer },
  { title: document.fullscreenElement ? "Leave full screen" : "Full screen", keys: ["f"], group: "Window",
    when: () => !!document.documentElement.requestFullscreen,
    run: () => (document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen()).catch(() => {}) },
  { title: "Reload page", group: "Window", run: () => window.location.reload() },
  { title: "Keyboard shortcuts and commands", keys: ["?"], group: "Help", run: () => openHelp() },
]);

const all = () => providers.flatMap((p) => p() || []);
const usable = (c) => !c.when || c.when();
const runnable = (c) => !!c.run && usable(c);

// "mod+shift+z" for this key press (letters lower-case; Shift is left out for symbols and
// digits, where it already changed the key).
function combo(e) {
  const k = e.key.toLowerCase();
  const letter = /^[a-z]$/.test(k) || k.startsWith("arrow");
  return `${e.ctrlKey || e.metaKey ? "mod+" : ""}${e.altKey ? "alt+" : ""}${e.shiftKey && letter ? "shift+" : ""}${k}`;
}
const MAC = /Mac|iPhone|iPad/.test(navigator.platform);
const showKey = (k) => k.split("+").map((p) => ({ mod: MAC ? "⌘" : "Ctrl", shift: "Shift", alt: "Alt", arrowleft: "←",
  arrowright: "→", arrowup: "↑", arrowdown: "↓" }[p] || (p.length === 1 ? p.toUpperCase() : p))).join(MAC ? "" : "+");

document.addEventListener("keydown", (e) => {
  if (e.defaultPrevented || e.isComposing || busy(e.target) || busy(document.activeElement)) return;
  const k = combo(e);
  if (k === "/" || k === "mod+k") { e.preventDefault(); openPalette(); return; }
  if (e.repeat && k !== "[" && k !== "]") return;   // held: only stepping through channels repeats
  const c = all().find((x) => x.keys && x.keys.includes(k) && runnable(x));
  if (!c) return;
  e.preventDefault();
  c.run();
});

// ------------------------------------------------------------------ the palette

let pal = null;   // {box, input, list, items, sel}

function openPalette() {
  if (pal) return;
  closeHelp();
  const back = el("div", "pal-back");
  const box = el("div", "pal");
  box.setAttribute("role", "dialog");
  box.setAttribute("aria-label", "Commands");
  const input = el("input", "pal-input");
  input.type = "text";
  input.placeholder = "Type a command or a channel · Tab to move · Enter to run";
  input.setAttribute("aria-label", "Command");
  input.setAttribute("autocomplete", "off");
  const list = el("ul", "pal-list");
  list.setAttribute("role", "listbox");
  box.append(input, list);
  back.append(box);
  document.body.append(back);
  pal = { back, input, list, items: [], sel: 0, commands: all().filter(runnable) };
  back.addEventListener("pointerdown", (e) => { if (e.target === back) closePalette(); });
  input.addEventListener("input", () => { pal.sel = 0; renderPalette(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); closePalette(); }
    else if (paletteStep(e)) {
      e.preventDefault();
      const n = pal.items.length;
      if (n) pal.sel = (pal.sel + paletteStep(e) + n) % n;
      renderPalette();
    } else if (e.key === "Enter") { e.preventDefault(); runSelected(pal.sel); }
  });
  renderPalette();
  input.focus();
}

// Down / up the list: Tab and Shift+Tab, the arrows, or Ctrl+N / Ctrl+P. 0: not a step.
function paletteStep(e) {
  if (e.key === "Tab") return e.shiftKey ? -1 : 1;
  if (e.key === "ArrowDown" || (e.ctrlKey && e.key === "n")) return 1;
  if (e.key === "ArrowUp" || (e.ctrlKey && e.key === "p")) return -1;
  return 0;
}

function closePalette() {
  if (!pal) return;
  pal.back.remove();
  pal = null;
}

function runSelected(i) {
  const c = pal && pal.items[i];
  closePalette();
  if (c) c.run();
}

// Every word typed must start a word of the command (or its group): "eq un" finds "EQ: Undo",
// "12" finds "12 · Snare", "sn" finds it too.
function matches(c, words) {
  const hay = `${c.group || ""} ${c.title}`.toLowerCase().split(/[^a-z0-9.]+/).filter(Boolean);
  return words.every((w) => hay.some((h) => h.startsWith(w)));
}

function renderPalette() {
  const words = pal.input.value.toLowerCase().split(/[^a-z0-9.]+/).filter(Boolean);
  pal.items = pal.commands.filter((c) => matches(c, words)).slice(0, 200);
  pal.sel = Math.min(pal.sel, Math.max(0, pal.items.length - 1));
  pal.list.replaceChildren(...pal.items.map((c, i) => {
    const li = el("li", "pal-item");
    li.setAttribute("role", "option");
    li.setAttribute("aria-selected", String(i === pal.sel));
    if (c.group) li.append(el("span", "pal-group", c.group));
    li.append(el("span", "pal-title", c.title));
    if (c.keys) li.append(el("kbd", "pal-keys", c.keys.map(showKey).join("  ")));
    li.addEventListener("pointerdown", (e) => e.preventDefault());   // keep the focus in the field
    li.addEventListener("click", () => runSelected(i));
    return li;
  }));
  if (!pal.items.length) pal.list.append(el("li", "pal-empty", "No command matches"));
  pal.list.querySelector("[aria-selected=true]")?.scrollIntoView({ block: "nearest" });
}

// ------------------------------------------------------------------ ? : every command, by group

let help = null;   // the overlay while it is open

function openHelp() {
  if (help) { closeHelp(); return; }
  closePalette();
  const back = el("div", "pal-back");
  const box = el("div", "pal help");
  box.setAttribute("role", "dialog");
  box.setAttribute("aria-label", "Keyboard shortcuts and commands");
  const head = el("div", "help-head");
  head.append(el("h2", "", "Keyboard"), el("p", "", ""));
  head.lastChild.append(kbd("/"), " or ", kbd("mod+k"), " finds any command or channel by name · ",
                        kbd("Tab"), " moves · ", kbd("Enter"), " runs · ", kbd("Esc"), " closes");
  const body = el("div", "help-body");
  const groups = new Map();
  for (const c of all().filter((x) => x.help !== false && usable(x))) {
    const g = c.group || "Other";
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(c);
  }
  for (const [name, cmds] of groups) {
    const sec = el("section", "help-group");
    sec.append(el("h3", "", name));
    for (const c of cmds) {
      const row = el(c.run ? "button" : "div", "help-row");
      if (c.run) { row.type = "button"; row.onclick = () => { closeHelp(); c.run(); }; }
      row.append(el("span", "help-title", c.title));
      const keys = el("span", "help-keys");
      (c.keys || []).forEach((k, i) => { if (i) keys.append(" "); keys.append(kbd(k)); });
      row.append(keys);
      sec.append(row);
    }
    body.append(sec);
  }
  box.append(head, body);
  back.append(box);
  document.body.append(back);
  back.addEventListener("pointerdown", (e) => { if (e.target === back) closeHelp(); });
  back.addEventListener("keydown", (e) => {
    if (e.key === "Escape" || e.key === "?") { e.preventDefault(); e.stopPropagation(); closeHelp(); }
  });
  back.tabIndex = -1;
  back.focus();
  help = back;
}

function closeHelp() {
  if (!help) return;
  help.remove();
  help = null;
}

function kbd(k) { return el("kbd", "pal-keys", k.length > 1 && !k.includes("+") && k !== "mod" ? k : showKey(k)); }

const bar = document.querySelector("nav.tabs");
if (bar) bar.title = "Shift+← / Shift+→ to switch pages · / for commands · ? for help";
window.tfCommands = () => all().filter(runnable).map((c) => ({ title: c.title, group: c.group || "", keys: c.keys || [] }));
