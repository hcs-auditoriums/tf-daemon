"""Dual-channel transfer function: how a measured signal y relates to a reference x.

The way measurement tools such as Smaart work, with the vocal mic as the
reference: per frame, the cross-spectrum of x and y and both auto-spectra are
averaged, then

    H(f)   = Sxy / Sxx                     how y relates to x (gain, colour)
    coh(f) = |Sxy|^2 / (Sxx * Syy)         0..1: how much of y is explained by x

Coherence is what lets this work with everything else going on: other mics,
music and noise in y average out of Sxy (they don't correlate with x) but stay
in Syy, so they lower the coherence instead of corrupting H. Bands with low
coherence are reported as such and shouldn't be trusted.

y lags x (desk latency, the PA's distance from the house mic), so y is aligned
to x first: the delay is found by GCC-PHAT (the phase-only cross-correlation)
over the first frames, then held. With ``memory_s``, older frames fade out so
the estimate follows changes (EQ moves, a fader, the talker turning away).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np


class Transfer:
    N = 8192  # frame (170 ms at 48 kHz): ~6 Hz bins
    DELAY_FRAMES = 12  # frames of cross-correlation before the delay is fixed
    MIN_PEAK = 4.0  # GCC-PHAT peak / median: below this, keep looking

    def __init__(self, samplerate: int = 48000, max_delay_s: float = 0.25, memory_s: float | None = None) -> None:
        self.fs = samplerate
        self.max_delay = int(max_delay_s * samplerate)
        self.memory_s = memory_s
        n = self.N
        self.window = np.hanning(n)
        self.freqs = np.fft.rfftfreq(n, 1 / samplerate)
        self.delay: int | None = None
        self._gcc = np.zeros(2 * n, dtype=complex)  # summed PHAT cross-spectrum at double length
        self._gcc_frames = 0
        self.sxx = np.zeros(len(self.freqs))
        self.syy = np.zeros(len(self.freqs))
        self.sxy = np.zeros(len(self.freqs), dtype=complex)
        self.frames = 0
        self.weight = 0.0

    def restart(self) -> None:
        """Forget the averages (e.g. the path changed: an EQ move), keeping the delay."""
        self.sxx[:] = 0
        self.syy[:] = 0
        self.sxy[:] = 0
        self.weight = 0.0
        self.frames = 0

    @property
    def history_needed(self) -> int:
        """Samples of x / y history ``process`` needs (newest last)."""
        return self.N + self.max_delay

    def process(self, x: np.ndarray, y: np.ndarray, dt: float = 0.05) -> bool:
        """Add one frame from the newest samples of ``x`` and ``y`` (equal length, newest
        last). Returns True once the delay is known and the frame was averaged in."""
        n = self.N
        if len(x) < self.history_needed or len(y) < self.history_needed:
            return False
        if self.delay is None:
            self._find_delay(np.asarray(x[-n:], dtype=np.float64), np.asarray(y[-n:], dtype=np.float64))
            if self.delay is None:
                return False
        d = self.delay
        xs = np.asarray(x[len(x) - n - d : len(x) - d], dtype=np.float64)
        ys = np.asarray(y[-n:], dtype=np.float64)
        X = np.fft.rfft(xs * self.window)
        Y = np.fft.rfft(ys * self.window)
        a = 1.0 if self.memory_s is None else math.exp(-dt / self.memory_s)
        self.sxx = self.sxx * a + (X.real**2 + X.imag**2)
        self.syy = self.syy * a + (Y.real**2 + Y.imag**2)
        self.sxy = self.sxy * a + np.conj(X) * Y
        self.weight = self.weight * a + 1.0
        self.frames += 1
        return True

    def _find_delay(self, x: np.ndarray, y: np.ndarray) -> None:
        """Accumulate GCC-PHAT on the newest N of x and y (zero-padded to 2N so lags up to
        N show); fix the delay once the peak is clear."""
        n = self.N
        X = np.fft.rfft(x * self.window, 2 * n)
        Y = np.fft.rfft(y * self.window, 2 * n)
        c = np.conj(X) * Y
        self._gcc[: len(c)] += c / np.maximum(np.abs(c), 1e-12)
        self._gcc_frames += 1
        if self._gcc_frames < self.DELAY_FRAMES:
            return
        xc = np.fft.irfft(self._gcc[: n + 1], 2 * n)
        span = xc[: min(self.max_delay, n) + 1]  # y lags x: non-negative lags only
        k = int(np.argmax(span))
        if span[k] >= self.MIN_PEAK * float(np.median(np.abs(xc))) and span[k] > 0:
            self.delay = k
        else:  # not clear yet: start over
            self._gcc[:] = 0
            self._gcc_frames = 0

    def bands(self, centres: np.ndarray, width_oct: float = 1 / 3) -> tuple[np.ndarray, np.ndarray]:
        """(gain dB of y re x, coherence 0..1) in bands of ``width_oct`` around each centre."""
        gain = np.full(len(centres), np.nan)
        coh = np.zeros(len(centres))
        if not self.weight:
            return gain, coh
        for i, fc in enumerate(centres):
            sel = (self.freqs >= fc * 2 ** (-width_oct / 2)) & (self.freqs < fc * 2 ** (width_oct / 2))
            if not sel.any():
                continue
            sxx, syy, sxy2 = self.sxx[sel], self.syy[sel], np.abs(self.sxy[sel]) ** 2
            total = float(sxx.sum())
            if total <= 0 or float(syy.sum()) <= 0:
                continue
            # Per-bin |H|^2 and coherence, averaged weighted by the reference's power: a
            # magnitude average, so a phase that turns across the band (a resonance, a
            # delay) doesn't cancel itself out the way averaging Sxy first would.
            gain[i] = 10 * math.log10(max(float(np.sum(sxy2 / np.maximum(sxx, 1e-30))) / total, 1e-24))
            coh[i] = min(1.0, float(np.sum(sxy2 / np.maximum(syy, 1e-30))) / total)
        return gain, coh

    def summary(self, lo: float = 200.0, hi: float = 5000.0) -> dict[str, Any]:
        """Over the voice band: median gain and coherence, and the delay."""
        centres = 125.0 * 2.0 ** (np.arange(30) / 3)
        centres = centres[(centres >= lo) & (centres <= hi)]
        gain, coh = self.bands(centres)
        ok = ~np.isnan(gain)
        return {
            "delay_ms": None if self.delay is None else round(self.delay / self.fs * 1000, 1),
            "frames": self.frames,
            "gain_db": round(float(np.median(gain[ok])), 1) if ok.any() else None,
            "coherence": round(float(np.median(coh[ok])), 2) if ok.any() else None,
        }
