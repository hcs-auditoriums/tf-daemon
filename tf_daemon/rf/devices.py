"""RF spectrum devices.

Every device does blocking sweeps: ``sweep(start_hz, stop_hz, points)`` returns
one float32 array of levels in dBm. Calls come from a single thread at a time
(the engine serializes them).

* TinySADevice  - a tinySA over USB serial, via the optional ``tsapython`` package.
* SimTinySA    - synthetic noise floor plus a few carriers; no hardware needed.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

MAX_POINTS = 450  # tinySA Ultra firmware limit
BASIC_MAX_POINTS = 290  # original tinySA (no scan data comes back above this)

# The original tinySA has two inputs on separate SMA connectors; a sweep only
# sees the one selected with `mode low|high input` (it does not switch itself).
BASIC_LOW_MAX_HZ = 350_000_000  # LOW input:  0.1 - 350 MHz
BASIC_HIGH_MIN_HZ = 240_000_000  # HIGH input: 240 - 960 MHz


SERIAL_TIMEOUT_S = 20.0  # longest wait for a tinySA reply (a 450-point sweep takes a few seconds)


class RFDeviceError(RuntimeError):
    pass


class RFDevice:
    name = "rf"
    max_points = MAX_POINTS  # may be lowered by open() once the model is known

    def open(self) -> str:
        """Connect; returns a human-readable device description."""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def set_rbw(self, rbw: str) -> None:
        """``"auto"`` or a resolution bandwidth in kHz."""

    def input_for(self, start_hz: int, stop_hz: int) -> str | None:
        """The input (``"low"``/``"high"``) that covers the sweep, or None for a device with a
        single input. Raises ValueError if no input covers it. No I/O."""
        return None

    def set_input(self, which: str) -> None:
        """Select an input returned by ``input_for``."""

    def sweep(self, start_hz: int, stop_hz: int, points: int) -> np.ndarray:
        raise NotImplementedError


def parse_scan(raw: bytes | bytearray | None) -> np.ndarray:
    """Parse ``scan ... 2`` output (one level per line) into dBm values."""
    if raw is None:
        return np.zeros(0, dtype=np.float32)
    # Known firmware bug: some values come back as '-:.0' instead of '-10.0'.
    text = bytes(raw).replace(b"-:.0", b"-10.0").decode("utf-8", errors="replace")
    levels = []
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        try:
            levels.append(float(parts[0]))
        except ValueError:
            pass
    return np.asarray(levels, dtype=np.float32)


class TinySADevice(RFDevice):
    name = "tinysa"

    def __init__(self) -> None:
        self._tsa: Any = None
        self.ultra = False

    def open(self) -> str:
        try:
            from tsapython import tinySA
        except ImportError:
            raise RFDeviceError("tsapython is not installed (pip install -e '.[rf]')") from None
        # A tinySA that hangs without an error must not block the sweep thread
        # (and the device lock) for tsapython's default 120 s.
        try:
            tsa = tinySA(serial_timeout=SERIAL_TIMEOUT_S)
        except TypeError:  # tsapython < 4 has no serial_timeout
            tsa = tinySA()
        tsa.set_error_byte_return(True)
        found, connected = tsa.autoconnect()
        if not found:
            raise RFDeviceError("tinySA not found. Is it plugged in?")
        if not connected:
            raise RFDeviceError("tinySA found but its serial port could not be opened")
        self._tsa = tsa
        info = bytes(tsa.info() or b"").decode("utf-8", errors="replace").strip()
        # The Ultra identifies as "tinySA4" / "ULTRA"; the original model only sweeps 290 points.
        self.ultra = bool(re.search(r"tinySA4|ultra", info, re.I))
        self.max_points = MAX_POINTS if self.ultra else BASIC_MAX_POINTS
        log.info("tinySA connected: %s (max %d points)", info.splitlines()[0] if info else "(no info)", self.max_points)
        return info

    def close(self) -> None:
        tsa, self._tsa = self._tsa, None
        if tsa is not None:
            try:
                tsa.disconnect()
            except Exception:
                log.exception("Error closing tinySA")

    def set_rbw(self, rbw: str) -> None:
        self._require().rbw("auto" if rbw == "auto" else int(rbw))

    def input_for(self, start_hz: int, stop_hz: int) -> str | None:
        if self.ultra:
            return None  # one input; the Ultra picks its own path
        return basic_input_for(start_hz, stop_hz)

    def set_input(self, which: str) -> None:
        self._require().mode(which, "input")

    def sweep(self, start_hz: int, stop_hz: int, points: int) -> np.ndarray:
        levels = parse_scan(self._require().scan(start_hz, stop_hz, points, 2))
        if levels.size == 0:
            raise RFDeviceError("tinySA returned no scan data")
        return levels

    def _require(self):
        if self._tsa is None:
            raise RFDeviceError("tinySA is not connected")
        return self._tsa


def basic_input_for(start_hz: int, stop_hz: int) -> str:
    """Which input of an original tinySA covers ``start_hz``..``stop_hz``."""
    if stop_hz <= BASIC_LOW_MAX_HZ:
        return "low"
    if start_hz >= BASIC_HIGH_MIN_HZ:
        return "high"
    raise ValueError(
        f"{start_hz / 1e6:g}-{stop_hz / 1e6:g} MHz spans both tinySA inputs: LOW covers up to "
        f"{BASIC_LOW_MAX_HZ / 1e6:g} MHz, HIGH from {BASIC_HIGH_MIN_HZ / 1e6:g} MHz; pick a range within one"
    )


class SimTinySA(RFDevice):
    name = "demo"

    def __init__(self, seed: int | None = None) -> None:
        self._rng = np.random.default_rng(seed)

    def open(self) -> str:
        return "Demo device (synthetic spectrum)"

    def close(self) -> None:
        pass

    def sweep(self, start_hz: int, stop_hz: int, points: int) -> np.ndarray:
        span = stop_hz - start_hz
        freqs = np.linspace(start_hz, stop_hz, points)
        levels = self._rng.normal(-80.0, 2.0, points)
        # Carriers at fixed fractions of the span: (position, level dBm, width / span).
        for pos, dbm, bw in ((0.2, -30.0, 0.005), (0.5, -45.0, 0.003), (0.75, -55.0, 0.004)):
            levels += np.exp(-0.5 * ((freqs - (start_hz + span * pos)) / (span * bw / 2)) ** 2) * (dbm + 80.0)
        return levels.astype(np.float32)
