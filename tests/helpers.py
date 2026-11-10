"""Shared test helpers."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

from tf_daemon.config import Config, config_from

# Synthetic audio and no RF / wireless: what most tests need. The TF5 is off
# until a test gives it a host (or a demo source).
BASE: dict[str, Any] = {"audio": {"source": "synth"}, "rf": {"source": "off"}, "qlxd": {"source": "off"}}


def make_config(**sections: Any) -> Config:
    """A validated Config: BASE with ``sections`` merged in, TOML-shaped:
    ``make_config(tf5={"host": "127.0.0.1"}, channels=[{"index": 0}])``."""
    data: dict[str, Any] = {k: dict(v) for k, v in BASE.items()}
    for name, value in sections.items():
        data[name] = {**data.get(name, {}), **value} if isinstance(value, dict) else value
    return config_from(data)


def wait_until(pred: Callable[[], Any], timeout: float = 6.0) -> bool:
    """Poll ``pred`` until it is true (then True) or ``timeout`` s pass (AssertionError).
    Tests wait for a condition like this, never for a fixed time."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)  # poll
    raise AssertionError("condition not reached in time")


def live_url() -> str:
    """The server tests/test_live.py and tests/test_browser*.py run against: TF_DAEMON_URL
    (conftest.py starts a private demo server and sets it)."""
    return os.environ.get("TF_DAEMON_URL", "").rstrip("/")


def open_page(page: Any, path: str) -> None:
    """Load one of the live server's pages in a Playwright page."""
    page.goto(live_url() + path)
    page.wait_for_load_state("domcontentloaded")


# Drawn pixels on a canvas: anything that isn't the page background. Sampled on a
# grid so it is cheap; a blank or failed canvas gives (close to) zero.
DRAWN = """(id) => {
  const c = document.getElementById(id);
  if (!c || !c.width || !c.height) return 0;
  const d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
  const bg = [d[0], d[1], d[2]];
  let n = 0;
  for (let i = 0; i < d.length; i += 4 * 97) {
    if (Math.abs(d[i] - bg[0]) + Math.abs(d[i + 1] - bg[1]) + Math.abs(d[i + 2] - bg[2]) > 30) n++;
  }
  return n;
}"""
