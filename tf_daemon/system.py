"""System routes: the /config page, config.toml editing, and self-update.

Routes:
  GET  /config                  config / update page
  GET  /api/system/info         version, running revision, update source
  POST /api/system/check        compare the running revision with the remote branch
  POST /api/system/update       start deploy/update.sh (pull, reinstall, restart, roll back on failure)
  GET  /api/system/update       update progress: {state, message, from, to, running, log}
  GET  /api/system/config       config.toml text
  PUT  /api/system/config       body: {text}; validated before it is written
  POST /api/system/restart      restart the systemd service

The update runs as the service user, preferably in its own systemd unit
(tf-daemon-update) so it survives our restart, can check the new version comes
up and roll back if not. Without that unit it runs as our child and the restart
ends it; the new process then finishes the bookkeeping (see Updater.status).
Starting the unit and restarting need root, so install-service.sh adds sudoers
rules for exactly the systemctl commands below.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from flask import Blueprint, Flask, jsonify, request

from . import __version__
from .config import UpdateConfig, load_config

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent.parent
UPDATE_SCRIPT = APP_DIR / "deploy" / "update.sh"
UPDATE_LOG = APP_DIR / ".update.log"
UPDATE_STATE = APP_DIR / ".update.json"  # written by update.sh
UPDATE_ENV = APP_DIR / ".update.env"  # read by the tf-daemon-update unit
RESTART_CMD = ["sudo", "-n", "systemctl", "restart", "--no-block", "tf-daemon"]
RESTART_CHECK_CMD = ["sudo", "-n", "-l", *RESTART_CMD[2:]]  # "may we?", without running it
RESTART_DELAY_S = 0.5
START_UPDATE_CMD = ["sudo", "-n", "systemctl", "start", "--no-block", "tf-daemon-update"]
FINAL_STATES = {"done", "up_to_date", "failed", "rolled_back"}
START_GRACE_S = 60  # time for update.sh to write its first state
RESTART_GRACE_S = 180  # time for the restarted service to pick up the update


def _git(*args: str, timeout: float = 15) -> str:
    out = subprocess.run(["git", *args], cwd=APP_DIR, capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"git {args[0]} failed")
    return out.stdout.strip()


def current_revision() -> str | None:
    """HEAD of the checkout, or the REVISION file written by deploy.sh / update.sh."""
    if (APP_DIR / ".git").exists():
        try:
            return _git("rev-parse", "HEAD")
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            pass
    rev = APP_DIR / "REVISION"
    return rev.read_text().strip() or None if rev.exists() else None


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_state() -> dict[str, Any] | None:
    try:
        return json.loads(UPDATE_STATE.read_text())
    except (OSError, ValueError):
        return None


def _write_state(**fields: Any) -> None:
    fields.setdefault("time", int(time.time()))
    tmp = UPDATE_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(fields))
    os.replace(tmp, UPDATE_STATE)


def _start_update_unit() -> bool:
    try:
        return subprocess.run(START_UPDATE_CMD, capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class Updater:
    """Starts deploy/update.sh and reports on it from the state file it writes.

    The state file outlives both the script and this process, so whichever
    tf-daemon process is running when the page asks can answer.
    """

    def __init__(self, remote: str, branch: str, running_revision: str | None) -> None:
        self.remote, self.branch = remote, branch
        self.running_revision = running_revision
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None

    def start(self) -> bool:
        with self._lock:
            if self.status()["running"]:
                return False
            UPDATE_ENV.write_text(f"UPDATE_REMOTE={self.remote}\nUPDATE_BRANCH={self.branch}\n")
            UPDATE_LOG.write_text("")
            _write_state(state="running", message="starting", pid=None, **{"from": self.running_revision})
            if _start_update_unit():
                log.info("Update started in the tf-daemon-update unit")
                return True
            # No unit (install-service.sh not rerun yet): run it as our child.
            env = {**os.environ, "UPDATE_REMOTE": self.remote, "UPDATE_BRANCH": self.branch}
            env.pop("TF_UPDATE_DETACHED", None)
            with UPDATE_LOG.open("a") as logf:
                # Own session so it isn't tied to a request thread.
                self._proc = subprocess.Popen(
                    ["bash", str(UPDATE_SCRIPT)],
                    cwd=APP_DIR,
                    env=env,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            log.info("Update started in-process (pid %d); tf-daemon-update unit not available", self._proc.pid)
            return True

    def status(self) -> dict[str, Any]:
        if self._proc is not None:
            self._proc.poll()  # reap a finished child
        st = _read_state() or {"state": "idle", "message": ""}
        st = self._settle(st)
        text = UPDATE_LOG.read_text(errors="replace") if UPDATE_LOG.exists() else ""
        st["running"] = st["state"] not in FINAL_STATES and st["state"] != "idle"
        st["log"] = text[-20000:]
        return st

    def _settle(self, st: dict[str, Any]) -> dict[str, Any]:
        """Turn states whose writer is gone into a final state (and persist it)."""
        state, age = st.get("state"), time.time() - float(st.get("time", 0))
        if state == "restarting":
            # update.sh ran inside the old process and was stopped by the restart.
            if self.running_revision and self.running_revision == st.get("to"):
                return self._finish(st, "done", f"updated to {self.running_revision[:7]}")
            if age > RESTART_GRACE_S:
                return self._finish(
                    st,
                    "failed",
                    "the service was restarted but is not running the update; check journalctl -u tf-daemon",
                )
        elif state == "running" and not _pid_alive(st.get("pid")):
            if st.get("pid") is not None or age > START_GRACE_S:
                return self._finish(st, "failed", "the update stopped unexpectedly; see the log")
        return st

    def _finish(self, st: dict[str, Any], state: str, message: str) -> dict[str, Any]:
        st = {**st, "state": state, "message": message, "time": int(time.time())}
        try:
            _write_state(**st)
        except OSError:
            log.exception("Could not write %s", UPDATE_STATE)
        return st


def _validate_toml(text: str) -> None:
    """Raise ValueError (TOML syntax or ConfigError) if ``text`` isn't a usable config."""
    fd, tmp = tempfile.mkstemp(suffix=".toml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        load_config(tmp)
    finally:
        os.unlink(tmp)


def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".toml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def _restart_now() -> None:
    log.info("Restarting tf-daemon (requested from /config)")
    try:
        subprocess.run(RESTART_CMD, capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        log.exception("Restart command failed")


def register_system(app: Flask, cfg: UpdateConfig, config_path: str | Path | None) -> None:
    cfg_file = Path(config_path).resolve() if config_path else None
    # The revision this process runs, fixed at startup: the checkout on disk
    # moves ahead of it while an update is in progress.
    running_revision = current_revision()
    updater = Updater(cfg.remote, cfg.branch, running_revision)
    bp = Blueprint("system", __name__, url_prefix="/api/system")

    @app.get("/config")
    def config_page():
        return app.send_static_file("config.html")

    @bp.get("/info")
    def info():
        return jsonify(
            {
                "version": __version__,
                "revision": running_revision,
                "git_checkout": (APP_DIR / ".git").exists(),
                "remote": updater.remote,
                "branch": updater.branch,
                "config_path": str(cfg_file) if cfg_file else None,
            }
        )

    @bp.post("/check")
    def check():
        try:
            out = _git("ls-remote", updater.remote, f"refs/heads/{updater.branch}")
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            return jsonify({"ok": False, "error": f"could not reach {updater.remote}: {e}"}), 502
        if not out:
            return jsonify({"ok": False, "error": f"branch {updater.branch!r} not found on the remote"}), 404
        latest = out.split()[0]
        current = running_revision
        return jsonify({"ok": True, "current": current, "latest": latest, "up_to_date": latest == current})

    @bp.post("/update")
    def update():
        if not updater.start():
            return jsonify({"ok": False, "error": "an update is already running"}), 409
        return jsonify({"ok": True}), 202

    @bp.get("/update")
    def update_status():
        return jsonify(updater.status())

    @bp.get("/config")
    def get_config():
        if cfg_file is None:
            return jsonify({"ok": False, "error": "no config file in use"}), 404
        text = cfg_file.read_text() if cfg_file.exists() else ""
        return jsonify({"ok": True, "path": str(cfg_file), "text": text})

    @bp.put("/config")
    def put_config():
        if cfg_file is None:
            return jsonify({"ok": False, "error": "no config file in use"}), 404
        text = (request.get_json(silent=True) or {}).get("text")
        if not isinstance(text, str):
            return jsonify({"ok": False, "error": 'body must be {"text": "..."}'}), 400
        try:
            _validate_toml(text)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        _write_atomic(cfg_file, text)
        log.info("Config written to %s", cfg_file)
        return jsonify({"ok": True})

    @bp.post("/restart")
    def restart():
        # systemctl runs inside the service it restarts, so systemd may kill it
        # before it exits and its exit code says nothing. Check permission up
        # front, answer, then restart once the response is out.
        try:
            out = subprocess.run(RESTART_CHECK_CMD, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as e:
            return jsonify({"ok": False, "error": f"cannot restart: {e}"}), 500
        if out.returncode != 0:
            return jsonify(
                {
                    "ok": False,
                    "error": "not allowed to restart the service; "
                    "run  sudo bash deploy/install-service.sh  on the server",
                }
            ), 500
        threading.Timer(RESTART_DELAY_S, _restart_now).start()
        return jsonify({"ok": True})

    app.register_blueprint(bp)
