"""Audio engine: source -> ring buffer -> analysis thread -> frame hub.

Every input the source provides is captured into the ring buffer, but only
*active* inputs are analyzed: the ``[[channels]]`` listed in the config (always)
plus any input a client is currently viewing (see ``acquire`` / ``release``).
An input nobody views and the config doesn't list costs no FFT time.

Threads:
  * capture   - PortAudio callback (or synthetic generator); only copies into
                the ring buffer.
  * analysis  - waits for a hop of new samples, runs one analyzer per active
                input, encodes a binary frame and publishes it to the hub.
  * websocket - one per connected client (gunicorn gthread workers); each waits
                on the hub for the latest frame. Slow clients skip frames.

Binary frame format (little-endian), sent over /ws/audio:
    char[4]  magic "TFS2"
    uint32   sequence number
    uint16   record count C (active inputs)
    uint16   display bin count B
    C records of:
        uint16  input index (0-based device channel)
        uint16  reserved (0)
        int16   data[2][B]   centi-dBFS; [0] = average, [1] = peak hold
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from collections import Counter
from typing import Any

import numpy as np

from ..config import Channel, channels_from
from ..dsp.spectrum import SpectrumAnalyzer
from ..hub import FrameHub
from .ringbuffer import RingBuffer
from .sources import Source, SourceError

log = logging.getLogger(__name__)

MAGIC = b"TFS2"
_HEADER = struct.Struct("<4sIHH")
_RECORD = struct.Struct("<HH")


class AudioEngine:
    def __init__(self, cfg: dict[str, Any], source: Source) -> None:
        self.cfg = cfg
        self.source = source
        self.channels: list[Channel] = channels_from(cfg)  # the listed ("pinned") inputs
        dsp = cfg["dsp"]
        fs = int(cfg["audio"]["samplerate"])
        if source.samplerate != fs:
            raise SourceError(f"source runs at {source.samplerate} Hz, expected {fs}")

        bad = [c for c in self.channels if c.index >= source.channels]
        if bad:
            raise SourceError(
                f"{source.name} source provides {source.channels} channels (0..{source.channels - 1}) but config "
                f"asks for {', '.join(f'{c.index} ({c.label})' for c in bad)}"
            )
        labels = {c.index: c.label for c in self.channels}
        self.inputs = [Channel(i, labels.get(i, f"Ch {i + 1}")) for i in range(source.channels)]
        self._pinned = frozenset(labels)

        self.n_fft = int(dsp["fft_size"])
        self.hop = int(dsp["hop"])
        capacity = int(float(cfg["audio"]["buffer_seconds"]) * fs)
        self.ring = RingBuffer(range(source.channels), capacity)
        self._analyzer_args = dict(
            channels=1,
            samplerate=fs,
            n_fft=self.n_fft,
            hop=self.hop,
            average_tau=float(dsp["average_tau"]),
            fmin=float(dsp["display_fmin"]),
            fmax=float(dsp["display_fmax"]),
            bands_per_octave=int(dsp["display_bands_per_octave"]),
            db_floor=float(dsp["db_floor"]),
        )
        # Reference analyzer for metadata (bins, update rate); not run.
        self.analyzer = SpectrumAnalyzer(**self._analyzer_args)
        self.hub = FrameHub()

        self._lock = threading.Lock()  # guards _analyzers and _viewers
        self._viewers: Counter[int] = Counter()
        self._analyzers: dict[int, SpectrumAnalyzer] = {
            i: SpectrumAnalyzer(**self._analyzer_args) for i in sorted(self._pinned)
        }

        n_bins = self.analyzer.bins.count
        self._frame = np.zeros((source.channels, self.n_fft), dtype=np.float32)
        self._scaled = np.zeros(n_bins, dtype=np.float32)

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._seq = 0
        self._frames = 0
        self._skipped_hops = 0
        self._proc_time_ewma = 0.0
        self._started_at: float | None = None

    # ------------------------------------------------------------------ control

    def start(self) -> None:
        if self._thread is not None:
            return
        log.info("%d input(s); always analyzing %s, others on demand", len(self.inputs),
                 ", ".join(f"{c.index}={c.label}" for c in self.channels))
        log.info("FFT %d (%.2f Hz/bin), hop %d (%.1f updates/s), %d display bins",
                 self.n_fft, self.analyzer.samplerate / self.n_fft, self.hop,
                 self.analyzer.update_rate, self.analyzer.bins.count)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="analysis", daemon=True)
        self._thread.start()
        self.source.start(self.ring.write_device_block)
        self._started_at = time.time()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.source.stop()
        finally:
            self.ring.close()
            self.hub.close()
            if self._thread is not None:
                self._thread.join(timeout=2)
                self._thread = None

    def _check_input(self, index: int) -> None:
        if not 0 <= index < len(self.inputs):
            raise ValueError(f"input {index} out of range (0..{len(self.inputs) - 1})")

    def acquire(self, index: int) -> None:
        """A client started viewing ``index``: analyze it until released."""
        self._check_input(index)
        with self._lock:
            self._viewers[index] += 1
            if index not in self._analyzers:
                self._analyzers[index] = SpectrumAnalyzer(**self._analyzer_args)
                log.info("Analyzing input %d (%s) on demand", index, self.inputs[index].label)

    def release(self, index: int) -> None:
        with self._lock:
            if self._viewers[index] <= 0:
                return
            self._viewers[index] -= 1
            if self._viewers[index] == 0:
                del self._viewers[index]
                if index not in self._pinned:
                    self._analyzers.pop(index, None)
                    log.info("Stopped analyzing input %d", index)

    def active_inputs(self) -> list[int]:
        with self._lock:
            return sorted(self._analyzers)

    def reset_peak(self, index: int | None = None) -> None:
        with self._lock:
            if index is None:
                targets = list(self._analyzers.values())
            elif index in self._analyzers:
                targets = [self._analyzers[index]]
            else:
                raise ValueError(f"input {index} is not being analyzed")
        for a in targets:
            a.reset_peak()

    # ------------------------------------------------------------------ info

    def metadata(self) -> dict[str, Any]:
        a = self.analyzer
        return {
            "type": "meta",
            "samplerate": a.samplerate,
            "fft_size": self.n_fft,
            "hop": self.hop,
            "update_rate": a.update_rate,
            "average_tau": float(self.cfg["dsp"]["average_tau"]),
            "db_floor": float(self.cfg["dsp"]["db_floor"]),
            "source": self.source.name,
            "channels": [{"index": c.index, "label": c.label, "pinned": c.index in self._pinned}
                         for c in self.inputs],
            "freqs": [round(float(f), 2) for f in a.bins.centers],
        }

    def status(self) -> dict[str, Any]:
        return {
            "source": self.source.name,
            "source_channels": self.source.channels,
            "source_stats": self.source.stats(),
            "active_inputs": self.active_inputs(),
            "frames": self._frames,
            "skipped_hops": self._skipped_hops,
            "analysis_ms": round(self._proc_time_ewma * 1000, 3),
            "uptime_s": round(time.time() - self._started_at, 1) if self._started_at else 0,
        }

    # ------------------------------------------------------------------ analysis thread

    def _run(self) -> None:
        target = self.n_fft  # wait until one full frame exists
        try:
            while not self._stop.is_set():
                written = self.ring.wait_for(target, timeout=0.5)
                if written < target:
                    continue
                t0 = time.perf_counter()
                with self._lock:
                    active = sorted(self._analyzers.items())
                frame = self._frame[:len(active)]
                self.ring.read_latest(frame, [i for i, _ in active])
                for row, (_, an) in enumerate(active):
                    an.process(frame[row:row + 1])
                self._publish(active)
                dt = time.perf_counter() - t0
                self._proc_time_ewma += 0.05 * (dt - self._proc_time_ewma)
                self._frames += 1

                target += self.hop
                if written - target > self.n_fft:  # fell behind: drop to the newest data
                    self._skipped_hops += (written - target) // self.hop
                    target = written + self.hop
        except Exception:
            log.exception("Analysis thread crashed")
            raise

    def _publish(self, active: list[tuple[int, SpectrumAnalyzer]]) -> None:
        n_bins = self.analyzer.bins.count
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        rec_size = _RECORD.size + 2 * n_bins * 2
        payload = bytearray(_HEADER.size + len(active) * rec_size)
        _HEADER.pack_into(payload, 0, MAGIC, self._seq, len(active), n_bins)
        for k, (index, an) in enumerate(active):
            off = _HEADER.size + k * rec_size
            _RECORD.pack_into(payload, off, index, 0)
            data = np.frombuffer(payload, dtype="<i2", offset=off + _RECORD.size, count=2 * n_bins).reshape(2, n_bins)
            for slot, src in ((0, an.avg_db[0]), (1, an.peak_db[0])):
                np.multiply(src, np.float32(100.0), out=self._scaled)
                np.rint(self._scaled, out=self._scaled)
                np.clip(self._scaled, -32768, 32767, out=self._scaled)
                data[slot] = self._scaled
        self.hub.publish(bytes(payload))
