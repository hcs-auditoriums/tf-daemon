// tf-daemon config / update page. Plain JS, no build step.
// API: see tf_daemon/system.py.
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const short = (rev) => (rev ? rev.slice(0, 7) : "unknown");
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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
    const el = $(id);
    el.textContent = text;
    el.classList.toggle("error", bad);
  }

  function setConn(ok, text) {
    $("conn").textContent = text;
    $("conn").classList.toggle("ok", ok);
  }

  // ------------------------------------------------------------------ software

  let revision = null;

  async function loadInfo() {
    const info = await api("GET", "/api/system/info");
    revision = info.revision;
    $("version").textContent = info.version;
    $("revision").textContent = short(info.revision) + (info.git_checkout ? "" : " (not a git checkout yet)");
    $("source").textContent = `${info.remote} · ${info.branch}`;
    setConn(true, "connected");
    return info;
  }

  $("check").addEventListener("click", async () => {
    setMsg("update-msg", "checking…");
    try {
      const r = await api("POST", "/api/system/check");
      $("latest").textContent = short(r.latest) + (r.up_to_date ? " (up to date)" : " (update available)");
      setMsg("update-msg", "");
    } catch (e) {
      setMsg("update-msg", e.message, true);
    }
  });

  $("update").addEventListener("click", async () => {
    if (!confirm("Pull the latest code and restart tf-daemon? Live views will drop for a few seconds.")) return;
    $("update").disabled = $("check").disabled = true;
    try {
      await api("POST", "/api/system/update");
      await followUpdate();
    } catch (e) {
      setMsg("update-msg", e.message, true);
    } finally {
      $("update").disabled = $("check").disabled = false;
    }
  });

  function showLog(text) {
    const el = $("update-log");
    el.hidden = !text;
    el.textContent = text;
    el.scrollTop = el.scrollHeight;
  }

  // Poll the update log until the script ends. It ends by restarting the
  // service, so the server going away is the normal path, not an error.
  async function followUpdate() {
    setMsg("update-msg", "updating…");
    const before = revision;
    for (;;) {
      let s;
      try {
        s = await api("GET", "/api/system/update");
      } catch {
        break;  // server went down for the restart
      }
      showLog(s.log);
      if (!s.running) {
        if (s.exit_code !== 0) {
          setMsg("update-msg", `update failed (exit ${s.exit_code}); see the log`, true);
          return;
        }
        if (!/restarting service/.test(s.log)) {
          setMsg("update-msg", "already up to date");
          return;
        }
        break;
      }
      await sleep(1000);
    }
    await waitForRestart(before);
  }

  async function waitForRestart(before) {
    setConn(false, "restarting…");
    setMsg("update-msg", "restarting…");
    await sleep(2000);
    for (let i = 0; i < 90; i++) {
      try {
        const info = await loadInfo();
        const log = await api("GET", "/api/system/update");
        showLog(log.log);
        setMsg("update-msg", info.revision !== before
          ? `updated to ${short(info.revision)}`
          : "restarted");
        await loadConfig();
        return;
      } catch {
        await sleep(1000);
      }
    }
    setConn(false, "offline");
    setMsg("update-msg", "the service did not come back; check journalctl -u tf-daemon", true);
  }

  // ------------------------------------------------------------------ config

  let savedText = "";

  async function loadConfig() {
    try {
      const r = await api("GET", "/api/system/config");
      savedText = r.text;
      $("config").value = r.text;
      $("config-path").textContent = r.path;
      setMsg("config-msg", "");
    } catch (e) {
      $("config").disabled = $("save").disabled = $("save-restart").disabled = true;
      setMsg("config-msg", e.message, true);
    }
  }

  async function save(restart) {
    setMsg("config-msg", "saving…");
    try {
      await api("PUT", "/api/system/config", { text: $("config").value });
      savedText = $("config").value;
      if (!restart) {
        setMsg("config-msg", "saved; restart to apply");
        return;
      }
      await api("POST", "/api/system/restart");
      setMsg("config-msg", "saved");
      await waitForRestart(revision);
    } catch (e) {
      setMsg("config-msg", e.message, true);
    }
  }

  $("save").addEventListener("click", () => save(false));
  $("save-restart").addEventListener("click", () => save(true));
  $("reload").addEventListener("click", () => {
    $("config").value = savedText;
    setMsg("config-msg", "");
  });
  window.addEventListener("beforeunload", (e) => {
    if ($("config").value !== savedText) e.preventDefault();
  });

  // ------------------------------------------------------------------ boot

  (async () => {
    try {
      await loadInfo();
      const s = await api("GET", "/api/system/update");
      if (s.running) {
        $("update").disabled = $("check").disabled = true;
        await followUpdate();
        $("update").disabled = $("check").disabled = false;
      }
    } catch (e) {
      setConn(false, "offline");
      setMsg("update-msg", e.message, true);
    }
    await loadConfig();
  })();
})();
