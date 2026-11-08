"""System routes: the /config page, config.toml editing, and self-update.

Routes:
  GET  /config                  config / update page
  GET  /api/system/info         version, running revision, update source
  POST /api/system/check        compare the running revision with the remote branch
  POST /api/system/update       run deploy/update.sh in the background (pull, reinstall, restart)
  GET  /api/system/update       update progress: {running, exit_code, log}
  GET  /api/system/config       config.toml text
  PUT  /api/system/config       body: {text}; validated before it is written
  POST /api/system/restart      restart the systemd service

The update runs as the service user. Restarting needs root, so install-service.sh
adds a sudoers rule for exactly the systemctl command below.
"""

from __future__ import annotations

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
from .config import load_config

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent.parent
UPDATE_SCRIPT = APP_DIR / "deploy" / "update.sh"
UPDATE_LOG = APP_DIR / ".update.log"
RESTART_CMD = ["sudo", "-n", "systemctl", "restart", "--no-block", "tf-daemon"]


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


class Updater:
    """Runs one deploy/update.sh at a time and reports on it."""

    def __init__(self, remote: str, branch: str) -> None:
        self.remote, self.branch = remote, branch
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._started: float | None = None

    def start(self) -> bool:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return False
            logf = UPDATE_LOG.open("w")
            # Own session so the script isn't tied to a request thread; it ends by
            # asking systemd to restart us, which stops this whole process tree.
            self._proc = subprocess.Popen(
                ["bash", str(UPDATE_SCRIPT), self.remote, self.branch],
                cwd=APP_DIR, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True,
            )
            logf.close()
            self._started = time.time()
            log.info("Update started (pid %d)", self._proc.pid)
            return True

    def status(self) -> dict[str, Any]:
        with self._lock:
            proc = self._proc
        text = UPDATE_LOG.read_text(errors="replace") if UPDATE_LOG.exists() else ""
        return {
            "running": proc is not None and proc.poll() is None,
            "exit_code": None if proc is None else proc.poll(),
            "started": self._started,
            "log": text[-20000:],
        }


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


def register_system(app: Flask, cfg: dict[str, Any], config_path: str | Path | None) -> None:
    cfg_file = Path(config_path).resolve() if config_path else None
    updater = Updater(str(cfg["update"]["remote"]), str(cfg["update"]["branch"]))
    bp = Blueprint("system", __name__, url_prefix="/api/system")

    @app.get("/config")
    def config_page():
        return app.send_static_file("config.html")

    @bp.get("/info")
    def info():
        return jsonify({
            "version": __version__,
            "revision": current_revision(),
            "git_checkout": (APP_DIR / ".git").exists(),
            "remote": updater.remote,
            "branch": updater.branch,
            "config_path": str(cfg_file) if cfg_file else None,
        })

    @bp.post("/check")
    def check():
        try:
            out = _git("ls-remote", updater.remote, f"refs/heads/{updater.branch}")
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            return jsonify({"ok": False, "error": f"could not reach {updater.remote}: {e}"}), 502
        if not out:
            return jsonify({"ok": False, "error": f"branch {updater.branch!r} not found on the remote"}), 404
        latest = out.split()[0]
        current = current_revision()
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
            return jsonify({"ok": False, "error": "body must be {\"text\": \"...\"}"}), 400
        try:
            _validate_toml(text)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        _write_atomic(cfg_file, text)
        log.info("Config written to %s", cfg_file)
        return jsonify({"ok": True})

    @bp.post("/restart")
    def restart():
        out = subprocess.run(RESTART_CMD, capture_output=True, text=True, timeout=15)
        if out.returncode != 0:
            return jsonify({"ok": False, "error": out.stderr.strip() or "restart failed"}), 500
        return jsonify({"ok": True})

    app.register_blueprint(bp)
