"""Shared test helpers."""

from __future__ import annotations

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
        time.sleep(0.02)
    raise AssertionError("condition not reached in time")
