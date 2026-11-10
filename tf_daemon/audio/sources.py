"""Audio sources.

Every source delivers (frames, device_channels) float32 blocks to a sink
callable, which is expected to be ``RingBuffer.write_device_block``. Sinks must
only copy data; analysis happens elsewhere.

* DeviceSource  - real capture via PortAudio (the TF5 on the Pi).
* SynthSource   - pink noise + speech-like harmonics + a faint steady sine.
* WavSource     - loops a WAV file in real time.
"""

from __future__ import annotations

import logging
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from ..config import REQUIRED_SAMPLERATE
from ..dsp.db import db_to_amp

log = logging.getLogger(__name__)

Sink = Callable[[np.ndarray], None]


class SourceError(RuntimeError):
    pass


class Source:
    """Base class. ``channels`` is the number of channels the source delivers."""

    name = "source"
    channels: int = 0
    samplerate: int = REQUIRED_SAMPLERATE

    def start(self, sink: Sink) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        return {}


# --------------------------------------------------------------------------
# Real hardware
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# Paced (non-hardware) sources
# --------------------------------------------------------------------------


class _PacedSource(Source):
    """Runs a generator thread that emits blocks at wall-clock rate."""

    def __init__(self, channels: int, blocksize: int, samplerate: int = REQUIRED_SAMPLERATE) -> None:
        self.channels = channels
        self.blocksize = blocksize
        self.samplerate = samplerate
        self._block = np.zeros((blocksize, channels), dtype=np.float32)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._late = 0

    def _fill(self, block: np.ndarray) -> None:
        raise NotImplementedError

    def start(self, sink: Sink) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(sink,), name=f"{self.name}-source", daemon=True)
        self._thread.start()

    def _run(self, sink: Sink) -> None:
        period = self.blocksize / self.samplerate
        t_next = time.monotonic()
        while not self._stop.is_set():
            self._fill(self._block)
            sink(self._block)
            t_next += period
            delay = t_next - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
            elif delay < -1.0:  # fell badly behind (e.g. laptop slept): resync
                self._late += 1
                t_next = time.monotonic()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def stats(self) -> dict:
        return {"resyncs": self._late}


def sim_levels(speech_db: float, noise_db: float, tone_db: float) -> tuple[float, float, float]:
    """(speech RMS, noise RMS, tone peak) amplitudes for the simulated sources.

    Levels are AES17 dBFS: a full-scale sine is 0 dBFS, so RMS = 10^(dB/20) / sqrt(2).
    This matches the analyzer, where a sine at X dBFS reads X on its peak bin. Voice
    and the noise loop are unit RMS."""
    return db_to_amp(speech_db) / np.sqrt(2.0), db_to_amp(noise_db) / np.sqrt(2.0), db_to_amp(speech_db + tone_db)


def make_pink_noise(n: int, rng: np.random.Generator) -> np.ndarray:
    """Unit-RMS pink (1/f power) noise of length n, made by spectral shaping."""
    spec = rng.standard_normal(n // 2 + 1) + 1j * rng.standard_normal(n // 2 + 1)
    f = np.arange(n // 2 + 1, dtype=np.float64)
    f[0] = 1.0
    spec /= np.sqrt(f)
    spec[0] = 0.0
    x = np.fft.irfft(spec, n)
    x /= np.sqrt(np.mean(x * x))
    return x.astype(np.float32)


class VoiceGen:
    """A speech-like voice: a harmonic series whose pitch glides between random
    targets, in syllables that stop and start. ``block(n)`` is unit RMS while voiced."""

    MAX_HARMONICS = 48
    VOICE_FMAX = 5000.0

    def __init__(
        self, rng: np.random.Generator, samplerate: int, blocksize: int, f0_range: tuple[float, float] = (95.0, 240.0)
    ) -> None:
        self._rng = rng
        self.samplerate = samplerate
        self.f0_range = f0_range
        self._f0 = 140.0
        self._f0_target = 140.0
        self._phase = 0.0
        self._env = 0.0
        self._voiced = False
        self._seg_left = 0  # samples remaining in current syllable or gap
        self._k = np.arange(1, self.MAX_HARMONICS + 1, dtype=np.float64)[:, None]
        self._k_amp = 1.0 / self._k  # ~ -6 dB/octave rolloff
        self._t = np.arange(1, blocksize + 1, dtype=np.float64)

    def _next_segment(self) -> None:
        fs = self.samplerate
        self._voiced = not self._voiced
        if self._voiced:
            self._seg_left = int(fs * self._rng.uniform(0.12, 0.45))
            self._f0_target = float(self._rng.uniform(*self.f0_range))
        else:
            self._seg_left = int(fs * self._rng.uniform(0.04, 0.35))

    def block(self, n: int) -> np.ndarray:
        fs = self.samplerate
        if self._seg_left <= 0:
            self._next_segment()
        self._seg_left -= n

        # Pitch glides toward the target (time constant ~80 ms), occasionally retargets.
        if self._voiced and self._rng.random() < 0.02:
            self._f0_target = float(self._rng.uniform(*self.f0_range))
        a = 1.0 - np.exp(-n / (0.08 * fs))
        f0_end = self._f0 + (self._f0_target - self._f0) * a
        f0 = self._f0 + (f0_end - self._f0) * (self._t[:n] / n)
        self._f0 = f0_end

        phase = self._phase + np.cumsum(2 * np.pi * f0 / fs)
        self._phase = float(phase[-1] % (2 * np.pi))

        # Harmonics below the voice bandwidth, normalized to unit RMS.
        mask = (self._k * f0[None, :]) < self.VOICE_FMAX
        amps = self._k_amp * mask
        norm = np.sqrt(0.5 * np.sum(amps * amps, axis=0))
        v = np.sum(amps * np.sin(self._k * phase[None, :]), axis=0) / norm

        # Syllable envelope: ~10 ms attack/release.
        target = 1.0 if self._voiced else 0.0
        env_end = self._env + (target - self._env) * (1.0 - np.exp(-n / (0.01 * fs)))
        env = self._env + (env_end - self._env) * (self._t[:n] / n)
        self._env = float(env_end)
        return v * env


class SynthSource(_PacedSource):
    """Synthetic test signal for development and for exercising the ring detector.

    Each analyzed channel gets: independent pink noise, a speech-like voice
    (VoiceGen), and a faint *steady* sine at ``tone_hz`` - the simulated
    feedback ring.
    """

    name = "synth"

    def __init__(
        self,
        channels: int,
        active: list[int],
        blocksize: int,
        *,
        speech_db: float,
        noise_db: float,
        tone_hz: float,
        tone_db: float,
        seed: int = 1,
        samplerate: int = REQUIRED_SAMPLERATE,
    ) -> None:
        super().__init__(channels, blocksize, samplerate)
        self.active = list(active)
        self._rng = np.random.default_rng(seed)
        fs = samplerate

        self._speech_amp, self._noise_amp, self._tone_peak = sim_levels(speech_db, noise_db, tone_db)
        self._tone_w = 2 * np.pi * tone_hz / fs
        self._tone_phase = 0.0

        # Pink noise: one long precomputed loop, a different offset per channel.
        self._noise = make_pink_noise(1 << 19, self._rng)
        self._noise_pos = [int(self._rng.integers(0, len(self._noise))) for _ in self.active]

        self._voice_gen = VoiceGen(self._rng, fs, blocksize)
        self._t = np.arange(1, blocksize + 1, dtype=np.float64)

    def _fill(self, block: np.ndarray) -> None:
        n = block.shape[0]
        voice = self._voice_gen.block(n) * self._speech_amp
        ph = self._tone_phase + self._tone_w * self._t[:n]
        tone = self._tone_peak * np.sin(ph)
        self._tone_phase = float(ph[-1] % (2 * np.pi))
        common = voice + tone

        noise, L = self._noise, len(self._noise)
        for j, col in enumerate(self.active):
            p = self._noise_pos[j]
            idx = (p + np.arange(n)) % L if p + n > L else slice(p, p + n)
            block[:, col] = common + self._noise_amp * noise[idx]
            self._noise_pos[j] = (p + n) % L


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Read a PCM WAV into float32 (frames, channels). Stdlib only."""
    try:
        with wave.open(str(path), "rb") as w:
            ch, width, fs, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(n)
    except wave.Error as e:
        raise SourceError(
            f"Cannot read {path}: {e}. Only integer PCM WAV is supported "
            "(convert float WAVs with e.g. `sox in.wav -b 24 out.wav`)."
        ) from e
    if width == 1:
        x = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v & 0x800000, v - 0x1000000, v)
        x = v.astype(np.float32) / 8388608.0
    elif width == 4:
        x = np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648.0
    else:
        raise SourceError(f"Unsupported WAV sample width {width} bytes")
    return x.reshape(-1, ch), fs


class WavSource(_PacedSource):
    name = "wav"

    def __init__(self, path: str | Path, blocksize: int, samplerate: int = REQUIRED_SAMPLERATE) -> None:
        path = Path(path)
        if not path.exists():
            raise SourceError(f"WAV file not found: {path}")
        data, fs = _read_wav(path)
        if fs != samplerate:
            raise SourceError(
                f"{path} is {fs} Hz; tf-daemon requires {samplerate} Hz "
                f"(resample with e.g. `sox in.wav -r {samplerate} out.wav`)"
            )
        if len(data) < blocksize:
            raise SourceError(f"{path} is shorter than one block")
        super().__init__(data.shape[1], blocksize, samplerate)
        self.path = path
        self._data = np.ascontiguousarray(data)
        self._pos = 0
        log.info("WAV source %s: %d ch, %.1f s, looping", path, data.shape[1], len(data) / fs)

    def _fill(self, block: np.ndarray) -> None:
        n, L = block.shape[0], len(self._data)
        first = min(n, L - self._pos)
        block[:first] = self._data[self._pos : self._pos + first]
        if first < n:
            block[first:] = self._data[: n - first]
        self._pos = (self._pos + n) % L
