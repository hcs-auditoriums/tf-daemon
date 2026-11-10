"""RF engine: device connection, sweep thread, peak hold, state hub.

Threads:
  * supervisor - in auto mode (``rf.auto_connect``, the default): keeps the
                device connected and sweeping whether or not anyone is viewing,
                retrying while it is unplugged. Stop / Disconnect from a user
                pause auto mode until Start / Connect.
  * sweep     - while scanning, sweeps the device every ``interval_s`` and
                publishes a state snapshot. A failed sweep closes the device
                (it was most likely unplugged) so it can be reopened.
  * websocket - one per connected client; each waits on the hub for the
                latest snapshot.

Every published message is a full JSON state snapshot (sent as text over
/ws/rf), so a client that joins late or skips messages loses nothing:

    {"type": "state", "connected": bool, "scanning": bool, "error": str|null,
     "device": "tinysa"|"demo", "info": str, "settings": {...}, "sweeps": int,
     "sweep": null | {"start_hz", "stop_hz", "points", "live": [dBm...], "peak": [dBm...]}}

Sweep frequencies are ``linspace(start_hz, stop_hz, points)``. The last ``HISTORY`` sweeps
of the current geometry are kept (history()) so a page that opens fills its waterfall at once.

Auto range: a ``range_hint`` callable (wired in app.py to the QLX-D receivers'
frequencies) supplies the default Start/Stop. While the current range came from
the hint it follows the hint as receivers are added or retuned; a range a user
picks by hand is kept.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import asdict
from typing import Any

import numpy as np

from ..config import RFConfig
from ..hub import FrameHub
from .devices import RFDevice, RFDeviceError
from .settings import MAX_HZ, RBW_CHOICES, SweepSettings

log = logging.getLogger(__name__)

SUPERVISE_INTERVAL_S = 3.0  # how often auto mode checks the device / range
FIT_MARGIN_MHZ = 2.0  # space beyond the outermost receivers
FIT_MIN_SPAN_MHZ = 10.0  # never zoom in narrower than this

RangeHint = Callable[[], "tuple[int, int] | None"]


def fit_span(freqs_mhz: Iterable[float]) -> tuple[int, int] | None:
    """Start/stop (Hz) covering ``freqs_mhz`` with a margin, whole MHz, at least FIT_MIN_SPAN_MHZ wide."""
    f = sorted(x for x in freqs_mhz if x and x > 0)
    if not f:
        return None
    lo, hi = math.floor(f[0] - FIT_MARGIN_MHZ), math.ceil(f[-1] + FIT_MARGIN_MHZ)
    if hi - lo < FIT_MIN_SPAN_MHZ:
        mid = (f[0] + f[-1]) / 2
        lo, hi = math.floor(mid - FIT_MIN_SPAN_MHZ / 2), math.ceil(mid + FIT_MIN_SPAN_MHZ / 2)
    lo = max(0, lo)
    return int(lo * 1_000_000), int(min(hi * 1_000_000, MAX_HZ))


class RFEngine:
    HISTORY = 200  # sweeps kept for a page's waterfall (static/rf.js WF_ROWS)

    def __init__(self, cfg: RFConfig, device: RFDevice, range_hint: RangeHint | None = None) -> None:
        self.device = device
        self.settings = cfg.sweep
        self.auto = cfg.auto_connect
        self.range_hint = range_hint
        self.hub = FrameHub()

        self._control = threading.RLock()  # serializes connect/disconnect/start/stop
        self._lock = threading.Lock()  # guards the state below
        self._device_lock = threading.Lock()  # serializes device I/O
        self._connected = False
        self._scanning = False
        self._error: str | None = None
        self._info = ""
        self._input: str | None = None  # "low" / "high" on an original tinySA
        self._sweeps = 0
        self._sweep_ms_ewma = 0.0
        self._sweep: dict[str, Any] | None = None  # current sweep geometry
        self._live: np.ndarray | None = None
        self._peak: np.ndarray | None = None
        self._history: deque[np.ndarray] = deque(maxlen=self.HISTORY)  # newest last, current geometry only

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        self._paused = False  # a user stopped / disconnected: auto mode waits for them
        self._auto_range = True  # current Start/Stop came from range_hint: keep following it
        self._sup_thread: threading.Thread | None = None
        self._sup_stop = threading.Event()
        self._sup_wake = threading.Event()
        self._publish()

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Start the auto-mode supervisor (no-op unless ``rf.auto_connect``)."""
        if self.auto and self._sup_thread is None:
            self._sup_stop.clear()
            self._sup_thread = threading.Thread(target=self._supervise, name="rf-supervisor", daemon=True)
            self._sup_thread.start()

    def stop(self) -> None:
        self._sup_stop.set()
        self._sup_wake.set()
        if self._sup_thread is not None:
            self._sup_thread.join(timeout=15)
            self._sup_thread = None
        try:
            self.disconnect()
        finally:
            self.hub.close()

    # ------------------------------------------------------------------ control
    # ``by_user=True`` marks requests from the page: they steer auto mode.

    def connect(self, by_user: bool = False) -> str:
        if by_user:
            with self._lock:
                self._paused = False
            self._sup_wake.set()  # start sweeping right away in auto mode
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

    def disconnect(self, by_user: bool = False) -> None:
        with self._control:
            if by_user:
                with self._lock:
                    self._paused = True
            self.stop_scan()
            with self._device_lock:
                self.device.close()
            with self._lock:
                self._connected, self._info, self._input = False, "", None
        self._publish()

    def start_scan(self, overrides: dict[str, Any] | None = None, by_user: bool = False) -> SweepSettings:
        """Start sweeping (restarting if already running). Raises ValueError / RFDeviceError."""
        with self._control:
            settings = self.settings.updated(overrides or {})
            if by_user:
                # Starting on the suggested range (e.g. Start pressed without edits) keeps following it.
                hint = self._hint()
                auto_range = hint is not None and (settings.start_hz, settings.stop_hz) == hint
                with self._lock:
                    self._paused, self._auto_range = False, auto_range
            if not self._connected:
                raise RFDeviceError("not connected")
            if settings.points > self.device.max_points:
                raise ValueError(f"this {self.device.name} sweeps at most {self.device.max_points} points")
            which = self.device.input_for(settings.start_hz, settings.stop_hz)  # ValueError before stopping anything
            self.stop_scan()
            with self._device_lock:
                try:
                    # Every start, not just on connect: the device may have been power-cycled.
                    if which:
                        self.device.set_input(which)
                except Exception as e:
                    raise RFDeviceError(f"could not select the {which} input: {e}") from e
                try:
                    self.device.set_rbw(settings.rbw)
                except Exception as e:
                    raise RFDeviceError(f"could not set RBW: {e}") from e
            if which:
                log.info("%s: %s input", self.device.name, which.upper())
            with self._lock:
                self._input = which
                self.settings = settings
                self._scanning, self._error = True, None
                self._live = self._peak = None
                self._sweep = None
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, args=(settings,), name="rf-sweep", daemon=True)
            self._thread.start()
            self._publish()
        return settings

    def stop_scan(self, by_user: bool = False) -> None:
        with self._control:
            if by_user:
                with self._lock:
                    self._paused = True
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
                "auto": self.auto,
                "paused": self._paused,
            }

    def history(self) -> dict[str, Any] | None:
        """The kept sweeps (whole dBm, oldest first) with their geometry; None before the first."""
        with self._lock:
            if self._sweep is None or not self._history:
                return None
            return {
                **self._sweep,
                "sweeps": self._sweeps,
                "rows": [np.round(r).astype(int).tolist() for r in self._history],
            }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            sweep = None
            if self._sweep is not None and self._live is not None and self._peak is not None:
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
                "input": self._input,
                "auto": self.auto,
                "paused": self._paused,
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
                log.warning("Sweep failed, closing %s: %s", self.device.name, e)
                # Most likely unplugged: close it so connect() (or auto mode) can reopen it.
                with self._device_lock:
                    try:
                        self.device.close()
                    except Exception:
                        log.exception("Error closing %s", self.device.name)
                with self._lock:
                    self._error = f"sweep failed: {e}"
                    self._scanning = False
                    self._connected, self._info, self._input = False, "", None
                self._publish()
                self._sup_wake.set()
                return
            dt = time.perf_counter() - t0

            with self._lock:
                if self._peak is None or self._peak.shape != levels.shape:
                    self._peak = levels.copy()
                else:
                    np.maximum(self._peak, levels, out=self._peak)
                self._live = levels
                # The device may return a different point count than requested.
                sweep = {"start_hz": s.start_hz, "stop_hz": s.stop_hz, "points": int(levels.size)}
                if sweep != self._sweep:
                    self._history.clear()
                self._history.append(levels)
                self._sweep = sweep
                self._sweeps += 1
                self._sweep_ms_ewma += 0.2 * (dt * 1000 - self._sweep_ms_ewma)
            self._publish()
            self._stop.wait(s.interval_s)

    # ------------------------------------------------------------------ auto mode

    def _hint(self) -> tuple[int, int] | None:
        if self.range_hint is None:
            return None
        try:
            return self.range_hint()
        except Exception:
            log.exception("RF range hint failed")
            return None

    def _auto_overrides(self) -> dict[str, Any]:
        out: dict[str, Any] = {"points": self.device.max_points}  # finest resolution the device allows
        hint = self._hint() if self._auto_range else None
        if hint is not None:
            out["start_hz"], out["stop_hz"] = hint
        return out

    def _supervise(self) -> None:
        last_error = None
        while not self._sup_stop.is_set():
            with self._lock:
                paused, connected, scanning = self._paused, self._connected, self._scanning
                auto_range, current = self._auto_range, (self.settings.start_hz, self.settings.stop_hz)
            try:
                if paused:
                    pass
                elif not connected:
                    self.connect()
                    log.info("RF auto mode: %s connected", self.device.name)
                    last_error = None
                    continue  # straight on to starting the sweep
                elif not scanning:
                    s = self.start_scan(self._auto_overrides())
                    log.info(
                        "RF auto mode: sweeping %.3f-%.3f MHz, %d points", s.start_hz / 1e6, s.stop_hz / 1e6, s.points
                    )
                elif auto_range:
                    hint = self._hint()
                    if hint is not None and hint != current:
                        log.info(
                            "RF auto mode: receivers changed, range now %.3f-%.3f MHz", hint[0] / 1e6, hint[1] / 1e6
                        )
                        self.start_scan(self._auto_overrides())
            except (RFDeviceError, ValueError) as e:
                if str(e) != last_error:  # log each distinct problem once, not every retry
                    log.warning("RF auto mode: %s (retrying every %.0f s)", e, SUPERVISE_INTERVAL_S)
                    last_error = str(e)
                if isinstance(e, ValueError):
                    self._set_error(str(e))  # e.g. receiver range spans both inputs: show it on the page
            except Exception:
                log.exception("RF auto mode error")
            self._sup_wake.wait(SUPERVISE_INTERVAL_S)
            self._sup_wake.clear()

    def _set_error(self, msg: str) -> None:
        with self._lock:
            self._error = msg
        self._publish()

    def _publish(self) -> None:
        self.hub.publish(json.dumps(self.snapshot(), separators=(",", ":")))
