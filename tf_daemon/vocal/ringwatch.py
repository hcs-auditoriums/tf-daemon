"""The ring-out's watch on the mains (vocal/ringout.py), fed every hop from the analysis
thread: a narrow peak that rises GROW_DB above where it was a second or more before and
holds its frequency for PERSIST_S is a ring; the mains peaking over RUNAWAY_PEAK counts
as one at once."""

from __future__ import annotations

import threading
from collections import deque

import numpy as np

from ..dsp.db import power_db


class MainsWatch:
    """Narrowband growth and runaway on the mains, fed every hop from the analysis thread."""

    N = 4096
    GROW_DB = 10.0
    PROMINENCE_DB = 10.0
    HARMONIC_DB = 10.0  # a ring has nothing comparable at 2f and 3f (PHPR); a voice's harmonic does
    PERSIST_S = 0.25
    FLOOR_DB = -85.0
    RUNAWAY_PEAK = 0.5  # sample peak (full scale 1): the mains are about to clip
    HISTORY_S = 4.0
    BASE_AGE_S = 1.0

    def __init__(self, samplerate: int) -> None:
        self.fs = samplerate
        self.window = np.hanning(self.N)
        self._scale = (2 / float(np.sum(self.window))) ** 2
        self.freqs = np.fft.rfftfreq(self.N, 1 / samplerate)
        self.k0 = int(150 * self.N / samplerate)
        self.k1 = int(10000 * self.N / samplerate)
        self._lock = threading.Lock()
        self._recent: deque[tuple[float, np.ndarray]] = deque()  # every hop, last 0.5 s
        self._hist: deque[tuple[float, np.ndarray]] = deque()  # every 0.25 s, last HISTORY_S
        self._track: tuple[int, float] | None = None  # (bin, since)
        self._candidate: tuple[float, float, str] | None = None  # (stream t, freq, how)
        self.t = 0.0
        self.levels: deque[float] = deque(maxlen=200)  # mains RMS (dBFS) per hop

    def reset(self) -> None:
        with self._lock:
            self._hist.clear()
            self._track = None
            self._candidate = None
            self.levels.clear()

    def feed(self, x: np.ndarray, t: float) -> None:
        x = np.asarray(x[-self.N :], dtype=np.float64)
        if len(x) < self.N:
            return
        db = power_db(np.abs(np.fft.rfft(x * self.window)) ** 2 * self._scale, 1e-20)
        peak = float(np.max(np.abs(x)))
        rms_db = power_db(float(np.mean(x**2)), 1e-20) + 3.0  # sine-peak full scale, like the rest
        with self._lock:
            self.levels.append(rms_db)
            self.t = t
            self._recent.append((t, db))
            while self._recent and t - self._recent[0][0] > 0.5:
                self._recent.popleft()
            if not self._hist or t - self._hist[-1][0] >= 0.25:
                self._hist.append((t, db))
            while self._hist and t - self._hist[0][0] > self.HISTORY_S:
                self._hist.popleft()
            if peak >= self.RUNAWAY_PEAK:
                k = self.k0 + int(np.argmax(db[self.k0 : self.k1]))
                self._candidate = (t, self._refine(db, k), "runaway")
                return
            old = [d for th, d in self._hist if t - th >= self.BASE_AGE_S]
            if len(old) < 3:
                return
            base = np.median(np.array(old), axis=0)
            grown = self._grown_peak(db, base)
            if grown is None:
                self._track = None
                return
            if self._track is None or abs(self._track[0] - grown) > 1:
                self._track = (grown, t)
            elif t - self._track[1] >= self.PERSIST_S:
                self._candidate = (t, self._refine(db, grown), "growing")

    def _grown_peak(self, db: np.ndarray, base: np.ndarray) -> int | None:
        seg = slice(self.k0, self.k1)
        excess = db[seg] - base[seg]
        order = np.argsort(excess)[::-1][:8]
        for j in order:
            if excess[j] < self.GROW_DB:
                return None
            k = self.k0 + int(j)
            if db[k] < self.FLOOR_DB or not (db[k] >= db[k - 1] and db[k] >= db[k + 1]):
                continue
            w = max(4, int(self.freqs[k] * (2 ** (1 / 6) - 1) * self.N / self.fs))
            if db[k] - float(np.median(db[max(0, k - w) : k + w + 1])) < self.PROMINENCE_DB:
                continue
            if any(
                m * k + 2 < len(db) and db[k] - float(np.max(db[m * k - 2 : m * k + 3])) < self.HARMONIC_DB
                for m in (2, 3)
            ):
                continue  # a harmonic series: a voice or an instrument, not feedback
            return k
        return None

    def _refine(self, db: np.ndarray, k: int) -> float:
        a, b, c = db[k - 1], db[k], db[k + 1]
        den = a - 2 * b + c
        off = 0.5 * (a - c) / den if den < 0 else 0.0
        return float((k + off) * self.fs / self.N)

    def take(self) -> tuple[float, float, str] | None:
        with self._lock:
            c, self._candidate = self._candidate, None
            return c

    def level_at(self, freq: float, within_s: float = 0.3) -> tuple[float, float] | None:
        """Over the last ``within_s``: the highest level (dB) at ``freq`` (+-3 bins), and the
        mean broadband level (median over 150 Hz - 10 kHz, which a single ring doesn't move)."""
        k = int(round(freq * self.N / self.fs))
        with self._lock:
            spectra = [d for th, d in self._recent if self.t - th <= within_s]
        if not spectra or not 3 <= k < len(self.freqs) - 3:
            return None
        peak = float(max(np.max(d[k - 3 : k + 4]) for d in spectra))
        broad = float(np.mean([np.median(d[self.k0 : self.k1]) for d in spectra]))
        return peak, broad
