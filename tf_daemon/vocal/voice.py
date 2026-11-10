"""The voice on a mic: speech activity, pitch and long-term average spectrum (LTAS).

VoiceCollector, per hop, on the newest 4096 samples (85 ms, Hann):
  * speech activity: level in 100 Hz-8 kHz more than 12 dB above a tracked
    noise floor (fast down, slow up);
  * pitch: normalized autocorrelation (Boersma's method: the frame's
    autocorrelation divided by the window's), voiced when the peak in the
    70-400 Hz lag range is >= 0.45;
  * active frames are summed into the LTAS (voiced and unvoiced: sibilance
    lives in the unvoiced ones), voiced frames give the pitch statistics.

With ``memory_s`` (live listening) older speech fades out exponentially, so the
spectrum follows the last ~memory_s seconds of talking; without it (a mic check)
every active frame counts the same.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any

import numpy as np


class VoiceCollector:
    """Accumulates a voice: speech activity, pitch, long-term spectrum."""

    N = 4096
    ACTIVE_DB = 12.0  # above the noise floor
    MIN_LEVEL_DB = -85.0  # absolute floor for "someone is talking" (dBFS, sine-peak)
    VOICED = 0.45

    def __init__(self, samplerate: int = 48000, memory_s: float | None = None) -> None:
        self.fs = samplerate
        self.memory_s = memory_s
        n = self.N
        self.window = np.hanning(n).astype(np.float64)
        self._wsum = float(np.sum(self.window))
        self._win_ac = np.fft.irfft(np.abs(np.fft.rfft(self.window, 2 * n)) ** 2)[:n]
        self.freqs = np.fft.rfftfreq(n, 1 / samplerate)
        self._k0, self._k1 = int(100 * n / samplerate), int(8000 * n / samplerate)
        self._lag0, self._lag1 = int(samplerate / 400), int(samplerate / 70)
        self.ltas = np.zeros(len(self.freqs))
        self.weight = 0.0  # sum of the frames' weights in ltas (= active frames without memory)
        self._active_w = 0.0
        self._voiced_w = 0.0
        keep = None if memory_s is None else max(50, int(memory_s * 20))  # ~20 hops / s
        self.f0s: deque[float] = deque(maxlen=keep)
        self._floor: float | None = None
        self._speech_levels: deque[float] = deque(maxlen=keep)
        self._last_t: float | None = None
        self.speech_s = 0.0

    def _db(self, p: float) -> float:
        return 10 * math.log10(p * (2 / self._wsum) ** 2 + 1e-30)

    def process(self, frame: np.ndarray, now: float) -> bool:
        """Analyze the newest N samples at stream time ``now`` (s). True if speech."""
        dt = 0.05 if self._last_t is None else min(max(now - self._last_t, 0.0), 0.5)
        self._last_t = now
        x = np.asarray(frame[-self.N :], dtype=np.float64)
        if len(x) < self.N:
            return False
        spec = np.fft.rfft(x * self.window)
        p = np.abs(spec) ** 2
        level = self._db(float(np.sum(p[self._k0 : self._k1])))
        # Noise floor: follows the level down at once, creeps up 3 dB/s.
        self._floor = level if self._floor is None else min(level, self._floor + 3.0 * dt)
        active = level > self._floor + self.ACTIVE_DB and level > self.MIN_LEVEL_DB
        if not active:
            return False
        a = 1.0 if self.memory_s is None else math.exp(-dt / self.memory_s)
        self.ltas = self.ltas * a + p if a != 1.0 else self.ltas + p
        self.weight = self.weight * a + 1.0
        self._active_w = self._active_w * a + 1.0
        self._voiced_w *= a
        self.speech_s += dt
        self._speech_levels.append(level)
        f0, strength = self._pitch(x)
        if strength >= self.VOICED:
            self._voiced_w += 1.0
            self.f0s.append(f0)
        return True

    @property
    def mean_ltas(self) -> np.ndarray:
        """The long-term average power spectrum (per active frame)."""
        return self.ltas / max(self.weight, 1.0)

    def _pitch(self, x: np.ndarray) -> tuple[float, float]:
        n = self.N
        xw = (x - x.mean()) * self.window
        ac = np.fft.irfft(np.abs(np.fft.rfft(xw, 2 * n)) ** 2)[:n]
        if ac[0] <= 0:
            return 0.0, 0.0
        r = ac / ac[0] / np.maximum(self._win_ac / self._win_ac[0], 1e-3)
        seg = r[self._lag0 : self._lag1 + 1]
        best = float(seg.max())
        # Prefer the shortest lag close to the best (avoids picking an octave below).
        lag = self._lag0 + int(np.flatnonzero(seg >= 0.9 * best)[0])
        if 1 <= lag < n - 1:
            a, b, c = r[lag - 1], r[lag], r[lag + 1]
            den = a - 2 * b + c
            lag_f = lag + (0.5 * (a - c) / den if den < 0 else 0.0)
        else:
            lag_f = float(lag)
        return self.fs / lag_f, best

    @property
    def voiced_ratio(self) -> float:
        return self._voiced_w / self._active_w if self._active_w else 0.0

    @property
    def snr_db(self) -> float | None:
        if not self._speech_levels or self._floor is None:
            return None
        return float(np.median(self._speech_levels) - self._floor)

    def progress(self) -> dict[str, Any]:
        return {
            "speech_s": round(self.speech_s, 1),
            "voiced_ratio": round(self.voiced_ratio, 2),
            "f0_hz": round(float(np.median(self.f0s)), 1) if len(self.f0s) >= 5 else None,
        }
