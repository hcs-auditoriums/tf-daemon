"""A private demo server for tests/test_live.py and tests/test_browser.py.

Started once per pytest run (by the main process, before parallel workers start, so
they share it) on a free port, with throwaway state files, and only when the run
includes those tests. So editing a file doesn't restart it mid-test (the dev
container's server reloads on save) and tests can't change the dev demo's state.
Set TF_DAEMON_URL to point the tests at a server you started yourself instead.

A test not marked ``slow`` fails if it runs longer than SLOW_LIMIT_S, so the quick loop
(``-m "not slow"``) stays quick. Mark a test that takes a second or more.

Also the fixtures several test files share: ``console`` (a simulated TF Editor
endpoint), ``client_for`` (TF Editor clients on it, stopped after the test), and for the
browser tests ``browser`` (headless Chromium) and ``page`` (a page that fails the test on
any script or console error).
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

import pytest
from editor_fakes import SimEditor

from tf_daemon.console.editor.client import TFEditorClient

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
            time.sleep(0.2)  # poll: the server is still starting
    print(f"\n[tests] the test server didn't start (see {tmp / 'server.log'}): live / browser tests skip")
    _server.kill()
    _server = None


# Generous against the 1 s rule: parallel runs share the CPU with the demo server.
SLOW_LIMIT_S = float(os.environ.get("TF_SLOW_LIMIT_S", "2.0"))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if (
        rep.when == "call"
        and rep.passed
        and rep.duration > SLOW_LIMIT_S
        and item.get_closest_marker("slow") is None
        and not item.config.getoption("usepdb", False)
    ):
        rep.outcome = "failed"
        rep.longrepr = (
            f"{item.nodeid} took {rep.duration:.1f} s: speed it up (wait on a condition, shorter "
            f"simulation) or mark it @pytest.mark.slow (limit {SLOW_LIMIT_S} s, TF_SLOW_LIMIT_S)"
        )


def pytest_unconfigure(config):
    global _server
    if _server is not None and not hasattr(config, "workerinput"):
        _server.terminate()
        try:
            _server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _server.kill()
        _server = None


@pytest.fixture
def console():
    fake = SimEditor()
    yield fake
    fake.close()


@pytest.fixture
def client_for():
    clients = []

    def make(fake, **kw):
        c = TFEditorClient("127.0.0.1", fake.port, **kw)
        clients.append(c)
        c.start()
        return c

    yield make
    for c in clients:
        c.stop()


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api", reason="Playwright not installed (the dev image has it)")
    with sync_api.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser):
    """A fresh page that records every script error and console error."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    pg = ctx.new_page()
    pg.errors = []  # type: ignore[attr-defined]
    pg.on("pageerror", lambda e: pg.errors.append(f"pageerror: {e}"))
    pg.on("console", lambda m: m.type == "error" and pg.errors.append(f"console: {m.text}"))
    yield pg
    errors = pg.errors
    ctx.close()
    assert errors == [], errors
