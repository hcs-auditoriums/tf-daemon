"""FFT spectrum analysis for all analyzed channels at once.

Pipeline per hop (vectorized across channels, arrays shaped (channels, ...)):

    frame * window -> rfft -> |X|^2 -> exponential average  -> max into display bins -> dBFS
                                    -> peak hold (max)       -> max into display bins -> dBFS

Averaging and peak hold happen in the linear power domain on full FFT
resolution; the reduction to log-spaced display bins takes the *max* of the
FFT bins inside each display bin so narrow peaks are not averaged away.

Every array is preallocated in __init__; ``process`` allocates nothing of its
own (numpy's pocketfft may use internal scratch).

Level reference: a full-scale sine (amplitude 1.0) centered on a bin reads
0 dBFS.
"""

from __future__ import annotations

import math
import threading

import numpy as np


def blackman_harris(n: int) -> np.ndarray:
    """4-term Blackman-Harris window (periodic / DFT-even), float32."""
    a0, a1, a2, a3 = 0.35875, 0.48829, 0.14128, 0.01168
    x = 2.0 * np.pi * np.arange(n) / n
    w = a0 - a1 * np.cos(x) + a2 * np.cos(2 * x) - a3 * np.cos(3 * x)
    return w.astype(np.float32)


class DisplayBins:
    """Log-spaced display bins (``bands_per_octave``) mapped onto FFT bins.

    ``starts`` are reduceat indices into the FFT power array sliced to
    ``[:end]``. A display bin narrower than an FFT bin (at low frequencies)
    gets the single nearest-above FFT bin.
    """

    def __init__(self, samplerate: int, n_fft: int, fmin: float, fmax: float, bands_per_octave: int) -> None:
        df = samplerate / n_fft
        count = int(math.ceil(bands_per_octave * math.log2(fmax / fmin) - 1e-9))
        edges = fmin * 2.0 ** (np.arange(count + 1) / bands_per_octave)
        edges[-1] = fmax  # last bin is slightly narrower so the range ends exactly at fmax
        self.edges = edges
        self.centers = np.sqrt(edges[:-1] * edges[1:])
        self.count = count
        n_bins = n_fft // 2 + 1
        starts = np.ceil(edges[:-1] / df - 1e-9).astype(np.intp)
        starts = np.clip(starts, 1, n_bins - 1)
        self.starts = starts
        self.end = int(min(max(math.ceil(edges[-1] / df - 1e-9), starts[-1] + 1), n_bins))


class SpectrumAnalyzer:
    def __init__(
        self,
        channels: int,
        samplerate: int,
        n_fft: int,
        hop: int,
        average_tau: float,
        fmin: float,
        fmax: float,
        bands_per_octave: int,
        db_floor: float = -140.0,
    ) -> None:
        self.channels = channels
        self.samplerate = samplerate
        self.n_fft = n_fft
        self.hop = hop
        self.window = blackman_harris(n_fft)
        self.bins = DisplayBins(samplerate, n_fft, fmin, fmax, bands_per_octave)

        # Exponential averaging: alpha per hop for time constant tau.
        self.alpha = float(math.exp(-hop / (samplerate * average_tau)))
        self._beta = np.float32(1.0 - self.alpha)
        self._alpha32 = np.float32(self.alpha)

        # Power -> dBFS (sine-peak referenced): amp = |X| * 2 / sum(w).
        self._db_offset = np.float32(10.0 * math.log10((2.0 / float(np.sum(self.window, dtype=np.float64))) ** 2))
        self._db_floor = np.float32(db_floor)
        self._tiny = np.float32(1e-30)

        nf = n_fft // 2 + 1
        nd = self.bins.count
        self._windowed = np.zeros((channels, n_fft), dtype=np.float32)
        self._spec = np.zeros((channels, nf), dtype=np.complex64)
        self.power = np.zeros((channels, nf), dtype=np.float32)  # latest instantaneous
        self.avg = np.zeros((channels, nf), dtype=np.float32)
        self.peak = np.zeros((channels, nf), dtype=np.float32)
        self._tmp = np.zeros((channels, nf), dtype=np.float32)
        self.avg_db = np.full((channels, nd), db_floor, dtype=np.float32)
        self.peak_db = np.full((channels, nd), db_floor, dtype=np.float32)
        self.live_db = np.full((channels, nd), db_floor, dtype=np.float32)  # this hop, unaveraged

        self._primed = False
        self._reset_lock = threading.Lock()
        self._reset_mask = np.zeros(channels, dtype=bool)

    @property
    def update_rate(self) -> float:
        return self.samplerate / self.hop

    def reset_peak(self, channel: int | None = None) -> None:
        """Request a peak-hold reset (applied on the next frame; thread-safe)."""
        with self._reset_lock:
            if channel is None:
                self._reset_mask[:] = True
            else:
                self._reset_mask[channel] = True

    def process(self, frame: np.ndarray) -> None:
        """Analyze one (channels, n_fft) float32 frame; updates avg_db / peak_db / live_db."""
        np.multiply(frame, self.window, out=self._windowed)
        np.fft.rfft(self._windowed, axis=-1, out=self._spec)
        power = self.power
        np.abs(self._spec, out=power)
        np.square(power, out=power)

        avg, peak, tmp = self.avg, self.peak, self._tmp
        if not self._primed:
            np.copyto(avg, power)
            np.copyto(peak, power)
            self._primed = True
        else:
            # avg = alpha*avg + (1-alpha)*power
            np.multiply(power, self._beta, out=tmp)
            np.multiply(avg, self._alpha32, out=avg)
            np.add(avg, tmp, out=avg)

            with self._reset_lock:
                if self._reset_mask.any():
                    for ch in np.flatnonzero(self._reset_mask):
                        peak[ch] = power[ch]
                    self._reset_mask[:] = False
            np.maximum(peak, power, out=peak)

        self._to_display(avg, self.avg_db)
        self._to_display(peak, self.peak_db)
        self._to_display(power, self.live_db)

    def _to_display(self, src: np.ndarray, out: np.ndarray) -> None:
        b = self.bins
        np.maximum.reduceat(src[:, : b.end], b.starts, axis=1, out=out)
        np.add(out, self._tiny, out=out)
        np.log10(out, out=out)
        np.multiply(out, np.float32(10.0), out=out)
        np.add(out, self._db_offset, out=out)
        np.maximum(out, self._db_floor, out=out)
