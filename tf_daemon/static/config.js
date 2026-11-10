// The Config page: the software (version, updates, another branch or commit to try), the
// settings (settings.js) and config.toml as text. Native ES module, no build step.
// API: see tf_daemon/system.py.

import { $ } from "./common.js";
import { renderSettings } from "./settings.js";

const short = (rev) => (rev ? rev.slice(0, 7) : "unknown");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const RESTART_ASK = "Restart tf-daemon now? Every page loses its live view for a few seconds.";

async function api(method, path, body) {
  const r = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `${r.status} ${r.statusText}`);
  return data;
}

function setMsg(id, text, bad = false) {
  const node = $(id);
  node.textContent = text;
  node.classList.toggle("error", bad);
}

function setConn(ok, text) {
  $("conn").textContent = text;
  $("conn").classList.toggle("ok", ok);
}

// ------------------------------------------------------------------ software

let info = null;

async function loadInfo() {
  info = await api("GET", "/api/system/info");
  $("version").textContent = info.version;
  const what = info.ref && info.ref !== info.branch ? `${info.ref} (trying out; Update now goes back to ${info.branch})` : info.branch;
  $("revision").textContent = `${short(info.revision)} · ${what}` + (info.git_checkout ? "" : " (not a git checkout yet)");
  $("source").textContent = info.remote;
  $("main-branch").textContent = info.branch;
  setConn(true, "connected");
  return info;
}

async function checkForUpdates() {
  $("latest").textContent = "checking…";
  setMsg("update-msg", "");
  try {
    const r = await api("POST", "/api/system/check");
    $("latest").textContent = `${short(r.latest)} on ${info ? info.branch : "the branch"}` + (r.up_to_date ? " (up to date)" : " (update available)");
  } catch (e) {
    $("latest").textContent = "couldn't check";
    setMsg("update-msg", e.message, true);
  }
}
$("check").addEventListener("click", checkForUpdates);

// Update to the configured branch (ref null) or to another branch / commit, and follow it.
async function runUpdate(ref) {
  const what = ref ? `switch to ${ref}` : `pull the latest ${info ? info.branch : "code"}`;
  if (!confirm(`${what[0].toUpperCase()}${what.slice(1)} and restart tf-daemon? Live views drop for a few seconds; ` +
               "if the new version doesn't start, the current one is put back.")) return;
  const buttons = ["update", "check", "switch-ref"].map($);
  for (const b of buttons) b.disabled = true;
  try {
    await api("POST", "/api/system/update", ref ? { ref } : undefined);
    await followUpdate();
  } catch (e) {
    setMsg("update-msg", e.message, true);
  } finally {
    for (const b of buttons) b.disabled = false;
  }
}
$("update").addEventListener("click", () => runUpdate(null));
$("switch-ref").addEventListener("click", () => {
  const ref = $("ref").value.trim();
  if (!ref) { $("ref").focus(); return; }
  runUpdate(ref);
});
$("ref").addEventListener("keydown", (e) => { if (e.key === "Enter") $("switch-ref").click(); });

// The remote's branches, to pick from (a commit id is typed).
async function loadRefs() {
  try {
    const r = await api("GET", "/api/system/refs");
    $("refs").replaceChildren(...r.branches.map((b) => new Option(b, b)));
  } catch { /* offline: type one */ }
}
$("try-ref").addEventListener("toggle", () => { if ($("try-ref").open && !$("refs").options.length) loadRefs(); });

function showLog(text) {
  const node = $("update-log");
  node.hidden = !text;
  node.textContent = text;
  node.scrollTop = node.scrollHeight;
}

// Poll the update state until it is final. The service restarts midway,
// so failed requests just mean "try again"; whichever process answers
// reads the same state file.
const FINAL = { done: false, up_to_date: false, failed: true, rolled_back: true };

async function followUpdate() {
  setMsg("update-msg", "updating…");
  const deadline = Date.now() + 15 * 60 * 1000;
  let down = false;
  while (Date.now() < deadline) {
    let s;
    try {
      s = await api("GET", "/api/system/update");
    } catch {
      if (!down) { down = true; setConn(false, "restarting…"); }
      await sleep(1500);
      continue;
    }
    if (down) { down = false; setConn(true, "connected"); }
    showLog(s.log);
    if (s.state in FINAL) {
      setMsg("update-msg", s.message || s.state, FINAL[s.state]);
      await loadInfo().catch(() => {});
      if (s.state === "done") await loadConfig();
      return;
    }
    setMsg("update-msg", s.message ? `${s.message}…` : "updating…");
    await sleep(1000);
  }
  setMsg("update-msg", "no result after 15 minutes; check the log and journalctl -u tf-daemon-update", true);
}

async function waitForRestart() {
  const before = info && info.revision;
  setConn(false, "restarting…");
  setMsg("update-msg", "restarting…");
  await sleep(2000);
  for (let i = 0; i < 90; i++) {
    try {
      const now = await loadInfo();
      setMsg("update-msg", now.revision !== before ? `updated to ${short(now.revision)}` : "restarted");
      $("restart-bar").hidden = true;
      await loadConfig();
      return;
    } catch {
      await sleep(1000);
    }
  }
  setConn(false, "offline");
  setMsg("update-msg", "the service did not come back; check journalctl -u tf-daemon", true);
}

async function restart() {
  if (!confirm(RESTART_ASK)) return false;
  try {
    await api("POST", "/api/system/restart");
  } catch (e) {
    setMsg("settings-msg", e.message, true);
    return false;
  }
  await waitForRestart();
  return true;
}
$("restart").addEventListener("click", restart);

// ------------------------------------------------------------------ config.toml

let savedText = "";
const rawDirty = () => $("config").value !== savedText;

// Saved to the file, not yet running: the bar with Restart now.
function savedNotApplied(text) {
  savedText = text;
  if (!rawDirty() || document.activeElement !== $("config")) $("config").value = text;
  $("restart-bar").hidden = false;
}

async function loadConfig() {
  try {
    const r = await api("GET", "/api/system/config");
    savedText = r.text;
    $("config").value = r.text;
    $("config-path").textContent = r.path;
    setMsg("config-msg", "");
    if (r.settings) {
      setMsg("settings-msg", "");
      renderSettings(r.settings, r.common, {
        busy: () => (rawDirty() ? "Save or discard your edits to config.toml as text first" : null),
        saved: savedNotApplied,
      });
    } else {
      $("settings").replaceChildren();
      setMsg("settings-msg", "config.toml has an error, so its settings can't be shown: fix it as text below.", true);
      $("raw").open = true;
    }
  } catch (e) {
    $("config").disabled = $("save").disabled = $("save-restart").disabled = true;
    setMsg("settings-msg", e.message, true);
  }
}

async function saveText(andRestart) {
  if (andRestart && !confirm(RESTART_ASK)) return;
  setMsg("config-msg", "saving…");
  try {
    await api("PUT", "/api/system/config", { text: $("config").value });
    savedText = $("config").value;
    if (!andRestart) {
      setMsg("config-msg", "saved");
      $("restart-bar").hidden = false;
      await loadConfig();
      return;
    }
    await api("POST", "/api/system/restart");
    setMsg("config-msg", "saved");
    await waitForRestart();
  } catch (e) {
    setMsg("config-msg", e.message, true);
  }
}

$("save").addEventListener("click", () => saveText(false));
$("save-restart").addEventListener("click", () => saveText(true));
$("reload").addEventListener("click", () => {
  $("config").value = savedText;
  setMsg("config-msg", "");
});
window.addEventListener("beforeunload", (e) => {
  if (rawDirty()) e.preventDefault();
});

// ------------------------------------------------------------------ boot

(async () => {
  try {
    await loadInfo();
    const s = await api("GET", "/api/system/update");
    const hash = window.location.hash;
    if (hash) history.replaceState(null, "", window.location.pathname);
    if (s.running) {
      for (const id of ["update", "check", "switch-ref"]) $(id).disabled = true;
      await followUpdate();
      for (const id of ["update", "check", "switch-ref"]) $(id).disabled = false;
    } else if (hash === "#update") {   // "Update now…" in the command palette
      $("update").click();
    } else {
      if (hash === "#switch") { $("try-ref").open = true; $("ref").focus(); }   // the palette's "Run a branch or commit…"
      checkForUpdates();  // on opening the page, so "Latest" is filled in without a click
    }
  } catch (e) {
    setConn(false, "offline");
    setMsg("update-msg", e.message, true);
  }
  await loadConfig();
})();
