"""RF engine: device connection, sweep thread, peak hold, state hub.

Threads:
  * sweep     - while scanning, sweeps the device every ``interval_s`` and
                publishes a state snapshot.
  * websocket - one per connected client; each waits on the hub for the
                latest snapshot.

Every published message is a full JSON state snapshot (sent as text over
/ws/rf), so a client that joins late or skips messages loses nothing:

    {"type": "state", "connected": bool, "scanning": bool, "error": str|null,
     "device": "tinysa"|"demo", "info": str, "settings": {...}, "sweeps": int,
     "sweep": null | {"start_hz", "stop_hz", "points", "live": [dBm...], "peak": [dBm...]}}

Sweep frequencies are ``linspace(start_hz, stop_hz, points)``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from ..hub import FrameHub
from .devices import MAX_POINTS, RFDevice, RFDeviceError

log = logging.getLogger(__name__)

RBW_CHOICES = ("auto", "3", "10", "30", "100", "300", "600", "850")  # kHz
MAX_HZ = 15_000_000_000


@dataclass(frozen=True)
class SweepSettings:
    start_hz: int
    stop_hz: int
    points: int
    rbw: str
    interval_s: float

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SweepSettings:
        try:
            s = cls(int(d["start_hz"]), int(d["stop_hz"]), int(d["points"]), str(d["rbw"]), float(d["interval_s"]))
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"invalid sweep settings: {e}") from None
        if not 0 <= s.start_hz < s.stop_hz <= MAX_HZ:
            raise ValueError(f"need 0 <= start_hz < stop_hz <= {MAX_HZ}")
        if not 2 <= s.points <= MAX_POINTS:
            raise ValueError(f"points must be between 2 and {MAX_POINTS}")
        if s.rbw not in RBW_CHOICES:
            raise ValueError(f"rbw must be one of {', '.join(RBW_CHOICES)}")
        if not 0.1 <= s.interval_s <= 60:
            raise ValueError("interval_s must be between 0.1 and 60")
        return s

    def updated(self, d: dict[str, Any]) -> SweepSettings:
        """These settings with any keys present in ``d`` replaced, validated."""
        known = {k: d[k] for k in asdict(self) if d.get(k) is not None}
        return SweepSettings.from_dict({**asdict(self), **known})


class RFEngine:
    def __init__(self, cfg: dict[str, Any], device: RFDevice) -> None:
        self.device = device
        self.settings = SweepSettings.from_dict(cfg["rf"])
        self.hub = FrameHub()

        self._control = threading.RLock()     # serializes connect/disconnect/start/stop
        self._lock = threading.Lock()         # guards the state below
        self._device_lock = threading.Lock()  # serializes device I/O
        self._connected = False
        self._scanning = False
        self._error: str | None = None
        self._info = ""
        self._sweeps = 0
        self._sweep_ms_ewma = 0.0
        self._sweep: dict[str, Any] | None = None  # current sweep geometry
        self._live: np.ndarray | None = None
        self._peak: np.ndarray | None = None

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._publish()

    # ------------------------------------------------------------------ control

    def connect(self) -> str:
        with self._control, self._device_lock:
            if self._connected:
                return self._info
            try:
                info = self.device.open()
            except RFDeviceError as e:
                self._set_error(str(e))
                raise
            except Exception as e:
                self._set_error(f"connect failed: {e}")
                raise RFDeviceError(f"connect failed: {e}") from e
        with self._lock:
            self._connected, self._info, self._error = True, info, None
        self._publish()
        return info

    def disconnect(self) -> None:
        with self._control:
            self.stop_scan()
            with self._device_lock:
                self.device.close()
            with self._lock:
                self._connected, self._info = False, ""
        self._publish()

    def start_scan(self, overrides: dict[str, Any] | None = None) -> SweepSettings:
        """Start sweeping (restarting if already running). Raises ValueError / RFDeviceError."""
        with self._control:
            settings = self.settings.updated(overrides or {})
            if not self._connected:
                raise RFDeviceError("not connected")
            if settings.points > self.device.max_points:
                raise ValueError(f"this {self.device.name} sweeps at most {self.device.max_points} points")
            self.stop_scan()
            with self._device_lock:
                try:
                    self.device.set_rbw(settings.rbw)
                except Exception as e:
                    raise RFDeviceError(f"could not set RBW: {e}") from e
            with self._lock:
                self.settings = settings
                self._scanning, self._error = True, None
                self._live = self._peak = None
                self._sweep = None
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, args=(settings,), name="rf-sweep", daemon=True)
            self._thread.start()
            self._publish()
        return settings

    def stop_scan(self) -> None:
        with self._control:
            self._stop.set()
            thread, self._thread = self._thread, None
            if thread is not None:
                thread.join(timeout=10)  # a wide tinySA sweep can take several seconds
            with self._lock:
                changed = self._scanning
                self._scanning = False
        if changed:
            self._publish()

    def reset_peak(self) -> None:
        with self._lock:
            if self._live is not None:
                self._peak = self._live.copy()
        self._publish()

    def stop(self) -> None:
        try:
            self.disconnect()
        finally:
            self.hub.close()

    # ------------------------------------------------------------------ info

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "device": self.device.name,
                "connected": self._connected,
                "scanning": self._scanning,
                "error": self._error,
                "sweeps": self._sweeps,
                "sweep_ms": round(self._sweep_ms_ewma, 1),
                "settings": asdict(self.settings),
            }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            sweep = None
            if self._sweep is not None and self._live is not None:
                sweep = {
                    **self._sweep,
                    "live": np.round(self._live, 1).tolist(),
                    "peak": np.round(self._peak, 1).tolist(),
                }
            return {
                "type": "state",
                "connected": self._connected,
                "scanning": self._scanning,
                "error": self._error,
                "device": self.device.name,
                "info": self._info,
                "settings": asdict(self.settings),
                "rbw_choices": list(RBW_CHOICES),
                "max_points": self.device.max_points,
                "sweeps": self._sweeps,
                "sweep": sweep,
            }

    # ------------------------------------------------------------------ sweep thread

    def _run(self, s: SweepSettings) -> None:
        while not self._stop.is_set():
            t0 = time.perf_counter()
            try:
                with self._device_lock:
                    levels = self.device.sweep(s.start_hz, s.stop_hz, s.points)
            except Exception as e:
                log.warning("Sweep failed: %s", e)
                with self._lock:
                    self._error = f"sweep failed: {e}"
                    self._scanning = False
                self._publish()
                return
            dt = time.perf_counter() - t0

            with self._lock:
                if self._peak is None or self._peak.shape != levels.shape:
                    self._peak = levels.copy()
                else:
                    np.maximum(self._peak, levels, out=self._peak)
                self._live = levels
                # The device may return a different point count than requested.
                self._sweep = {"start_hz": s.start_hz, "stop_hz": s.stop_hz, "points": int(levels.size)}
                self._sweeps += 1
                self._sweep_ms_ewma += 0.2 * (dt * 1000 - self._sweep_ms_ewma)
            self._publish()
            self._stop.wait(s.interval_s)

    def _set_error(self, msg: str) -> None:
        with self._lock:
            self._error = msg
        self._publish()

    def _publish(self) -> None:
        self.hub.publish(json.dumps(self.snapshot(), separators=(",", ":")))
