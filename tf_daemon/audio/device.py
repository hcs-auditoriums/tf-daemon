"""Real capture via PortAudio (the TF5 on the Pi), reopened when the device comes back."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import numpy as np

from ..config import REQUIRED_SAMPLERATE
from .sources import Sink, Source, SourceError

log = logging.getLogger(__name__)


class DeviceSource(Source):
    """The mixer over USB audio, kept open across unplug / replug.

    A supervisor thread opens the first input device whose name contains
    ``device_match``. If there is none, or it can't be opened (wrong sample rate,
    busy, PortAudio missing), it retries every ``RETRY_S`` and reports why in
    ``stats()``: the app starts and runs either way. While the stream runs it
    checks that callbacks keep arriving; an unplugged USB device usually just
    stops calling back (or the stream goes inactive), so after ``STALL_S`` of
    silence the stream is closed, PortAudio re-initialized (otherwise a
    re-plugged device stays invisible) and the device reopened.

    The rest of the app always sees ``channels`` columns (the TF5's 34); a
    device with fewer is zero-padded.
    """

    name = "device"
    RETRY_S = 3.0
    STALL_S = 2.0
    CHECK_S = 0.5

    def __init__(
        self,
        device_match: str,
        blocksize: int,
        samplerate: int = REQUIRED_SAMPLERATE,
        channels: int = 34,
        sd_module=None,
    ) -> None:
        self.device_match = device_match
        self.blocksize = blocksize
        self.samplerate = samplerate
        self.channels = channels
        self._sd = sd_module
        self._load_error: str | None = None
        if self._sd is None:
            try:
                import sounddevice as sd

                self._sd = sd
            except (OSError, ImportError) as e:  # PortAudio shared library (or the package) missing
                self._load_error = f"Could not load PortAudio ({e}). On Raspberry Pi OS: sudo apt install libportaudio2"
        self.device_name: str | None = None
        self._stream: Any = None
        self._pad = np.zeros((blocksize, channels), dtype=np.float32)
        self._sink: Sink | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._overflows = 0
        self._callbacks = 0
        self._reconnects = 0
        self._last_cb = 0.0
        self._error: str | None = self._load_error

    # ------------------------------------------------------------------ control

    def start(self, sink: Sink) -> None:
        self._sink = sink
        self._stop.clear()
        self._thread = threading.Thread(target=self._supervise, name="audio-device", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        self._close()

    def stats(self) -> dict:
        return {
            "device": self.device_name,
            "connected": self._stream is not None,
            "error": self._error,
            "overflows": self._overflows,
            "callbacks": self._callbacks,
            "reconnects": self._reconnects,
        }

    # ------------------------------------------------------------------ supervisor thread

    def _supervise(self) -> None:
        last_logged = None
        while not self._stop.is_set():
            if self._stream is None:
                try:
                    self._open()
                    self._error = None
                    last_logged = None
                except Exception as e:  # never let a missing device stop the app
                    self._error = str(e)
                    if str(e) != last_logged:  # log each distinct problem once, not every retry
                        log.warning("Audio device unavailable: %s (retrying every %.0f s)", e, self.RETRY_S)
                        last_logged = str(e)
                    self._rescan()
                    self._stop.wait(self.RETRY_S)
                    continue
            else:
                silent = time.monotonic() - self._last_cb
                if not self._stream_active() or silent > self.STALL_S:
                    log.warning(
                        "Audio device %s stopped delivering audio (%.1f s silent); reopening", self.device_name, silent
                    )
                    self._error = "device stopped delivering audio (unplugged?)"
                    self._close()
                    self._reconnects += 1
                    self._rescan()
                    continue
            self._stop.wait(self.CHECK_S)

    def _open(self) -> None:
        if self._sd is None:
            raise SourceError(self._load_error or "PortAudio not available")
        sd = self._sd
        devices = sd.query_devices()
        inputs = [(i, d) for i, d in enumerate(devices) if d["max_input_channels"] > 0]
        needle = self.device_match.casefold()
        matches = [(i, d) for i, d in inputs if needle in d["name"].casefold()]
        if not matches:
            names = ", ".join(d["name"] for _, d in inputs) or "none"
            raise SourceError(f"no input device matching {self.device_match!r} (inputs: {names})")
        index, info = matches[0]
        open_ch = min(int(info["max_input_channels"]), self.channels)
        sd.check_input_settings(device=index, channels=open_ch, samplerate=self.samplerate, dtype="float32")
        stream = sd.InputStream(
            device=index,
            channels=open_ch,
            samplerate=self.samplerate,
            blocksize=self.blocksize,
            dtype="float32",
            callback=self._callback,
        )
        self._last_cb = time.monotonic()  # grace period for the first callback
        stream.start()
        self._stream = stream
        self.device_name = info["name"]
        if open_ch < self.channels:
            log.warning(
                "%s has %d input channels; inputs %d-%d stay silent", info["name"], open_ch, open_ch + 1, self.channels
            )
        log.info("Audio device [%d] %s open: %d channels @ %d Hz", index, info["name"], open_ch, self.samplerate)

    def _callback(self, indata, frames, time_info, status) -> None:  # PortAudio thread
        self._callbacks += 1
        self._last_cb = time.monotonic()
        if status.input_overflow:
            self._overflows += 1
        sink = self._sink
        if sink is None:
            return
        if indata.shape[1] == self.channels:
            sink(indata)
            return
        if frames > self._pad.shape[0]:
            self._pad = np.zeros((frames, self.channels), dtype=np.float32)
        buf = self._pad[:frames]
        w = min(indata.shape[1], self.channels)
        buf[:, :w] = indata[:, :w]
        buf[:, w:] = 0.0
        sink(buf)

    def _stream_active(self) -> bool:
        try:
            return bool(self._stream.active)
        except Exception:
            return False

    def _close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            for op in (stream.stop, stream.close):
                try:
                    op()
                except Exception:  # an unplugged device can fail to stop cleanly
                    pass

    def _rescan(self) -> None:
        """Re-initialize PortAudio so newly plugged devices show up (only with no stream open)."""
        sd = self._sd
        if sd is None or self._stream is not None:
            return
        if hasattr(sd, "_terminate") and hasattr(sd, "_initialize"):
            try:
                sd._terminate()
                sd._initialize()
            except Exception:
                log.debug("PortAudio re-initialization failed", exc_info=True)
