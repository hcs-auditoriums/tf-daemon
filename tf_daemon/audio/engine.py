"""Audio engine: source -> ring buffer -> analysis thread -> frame hub.

Threads:
  * capture   - PortAudio callback (or synthetic generator); only copies into
                the ring buffer.
  * analysis  - waits for a hop of new samples, runs the FFT analyzer, encodes
                a binary frame and publishes it to the hub.
  * websocket - one per connected client (gunicorn gthread workers); each waits
                on the hub for the latest frame. Slow clients skip frames.

Binary frame format (little-endian), sent over /ws/audio:
    char[4]  magic "TFS1"
    uint32   sequence number
    uint16   channel count C
    uint16   display bin count B
    int16    data[C][2][B]   centi-dBFS; [c][0] = average, [c][1] = peak hold
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from typing import Any

import numpy as np

from ..config import Channel, channels_from
from ..dsp.spectrum import SpectrumAnalyzer
from ..hub import FrameHub
from .ringbuffer import RingBuffer
from .sources import Source, SourceError

log = logging.getLogger(__name__)

MAGIC = b"TFS1"
_HEADER = struct.Struct("<4sIHH")


class AudioEngine:
    def __init__(self, cfg: dict[str, Any], source: Source) -> None:
        self.cfg = cfg
        self.source = source
        self.channels: list[Channel] = channels_from(cfg)
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

        self.n_fft = int(dsp["fft_size"])
        self.hop = int(dsp["hop"])
        capacity = int(float(cfg["audio"]["buffer_seconds"]) * fs)
        self.ring = RingBuffer([c.index for c in self.channels], capacity)
        self.analyzer = SpectrumAnalyzer(
            channels=len(self.channels),
            samplerate=fs,
            n_fft=self.n_fft,
            hop=self.hop,
            average_tau=float(dsp["average_tau"]),
            fmin=float(dsp["display_fmin"]),
            fmax=float(dsp["display_fmax"]),
            bands_per_octave=int(dsp["display_bands_per_octave"]),
            db_floor=float(dsp["db_floor"]),
        )
        self.hub = FrameHub()

        n_ch, n_bins = len(self.channels), self.analyzer.bins.count
        self._frame = np.zeros((n_ch, self.n_fft), dtype=np.float32)
        self._payload = bytearray(_HEADER.size + n_ch * 2 * n_bins * 2)
        self._payload_data = np.frombuffer(self._payload, dtype="<i2", offset=_HEADER.size).reshape(n_ch, 2, n_bins)
        self._scaled = np.zeros((n_ch, n_bins), dtype=np.float32)

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
        log.info("Analyzing %d channel(s): %s", len(self.channels),
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

    def reset_peak(self, channel: int | None = None) -> None:
        if channel is not None and not 0 <= channel < len(self.channels):
            raise ValueError(f"channel position {channel} out of range")
        self.analyzer.reset_peak(channel)

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
            "channels": [{"position": i, "index": c.index, "label": c.label} for i, c in enumerate(self.channels)],
            "freqs": [round(float(f), 2) for f in a.bins.centers],
        }

    def status(self) -> dict[str, Any]:
        return {
            "source": self.source.name,
            "source_channels": self.source.channels,
            "source_stats": self.source.stats(),
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
                self.ring.read_latest(self._frame)
                self.analyzer.process(self._frame)
                self._publish()
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

    def _publish(self) -> None:
        a = self.analyzer
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        _HEADER.pack_into(self._payload, 0, MAGIC, self._seq, len(self.channels), a.bins.count)
        for slot, src in ((0, a.avg_db), (1, a.peak_db)):
            np.multiply(src, np.float32(100.0), out=self._scaled)
            np.rint(self._scaled, out=self._scaled)
            np.clip(self._scaled, -32768, 32767, out=self._scaled)
            self._payload_data[:, slot, :] = self._scaled
        self.hub.publish(bytes(self._payload))
