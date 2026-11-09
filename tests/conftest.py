"""A private demo server for tests/test_live.py and tests/test_browser.py.

Started once per pytest run (by the main process, before parallel workers start, so
they share it) on a free port, with throwaway state files, and only when the run
includes those tests. So editing a file doesn't restart it mid-test (the dev
container's server reloads on save) and tests can't change the dev demo's state.
Set TF_DAEMON_URL to point the tests at a server you started yourself instead.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE_TESTS = ("test_live", "test_browser")
_server: subprocess.Popen | None = None


def _wanted(config) -> bool:
    args = [a for a in config.args if not a.startswith("-")]
    if not args:
        return True
    return any(any(t in a for t in LIVE_TESTS) or Path(a).is_dir() for a in args)


def pytest_configure(config):
    global _server
    if hasattr(config, "workerinput") or os.environ.get("TF_DAEMON_URL") or not _wanted(config):
        return
    tmp = Path(tempfile.mkdtemp(prefix="tf-test-server-"))
    cfg = (ROOT / "config.dev.toml").read_text()
    cfg = cfg.replace('".dev/qlxd_state.json"', f'"{tmp / "qlxd_state.json"}"')
    cfg = cfg.replace('".dev/eq_profiles.json"', f'"{tmp / "eq_profiles.json"}"')
    (tmp / "config.toml").write_text(cfg)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    log = open(tmp / "server.log", "w")  # the server keeps its own copy of the handle
    _server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tf_daemon",
            "--demo",
            "--config",
            str(tmp / "config.toml"),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "WARNING",
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=log,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    log.close()
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and _server.poll() is None:
        try:
            urllib.request.urlopen(url + "/api/status", timeout=1).read()
            os.environ["TF_DAEMON_URL"] = url  # workers start after this and inherit it
            return
        except OSError:
            time.sleep(0.2)
    print(f"\n[tests] the test server didn't start (see {tmp / 'server.log'}): live / browser tests skip")
    _server.kill()
    _server = None


def pytest_unconfigure(config):
    global _server
    if _server is not None and not hasattr(config, "workerinput"):
        _server.terminate()
        try:
            _server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _server.kill()
        _server = None
