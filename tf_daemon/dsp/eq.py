"""The console EQ's magnitude response, computed the way TF Editor draws it."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

# The curve is computed the way TF Editor draws it (decompiled: designer FUN_005196e0,
# band table FUN_0051a080, response FUN_00546ae0): one digital biquad per section at the
# console's 48 kHz, bilinear transform with K = tan(pi f / fs), magnitudes summed in dB.
CURVE_FREQS = np.geomspace(20.0, 20000.0, 241)
FS = 48000.0
BUTTERWORTH_Q = 0.707106


def eq_curve(eq: dict[str, Any] | None, freqs: np.ndarray = CURVE_FREQS) -> np.ndarray:
    """Magnitude response in dB of the console EQ, as TF Editor computes it. EQ off: flat."""
    out = np.zeros(len(freqs))
    if not eq or not eq["on"]:
        return out
    hpf = eq.get("hpf")
    if hpf and hpf["on"]:
        kind = "hpf1" if hpf["slope"] == 6 else "hpf"
        out += section_db(kind, hpf["freq"], 0.0, BUTTERWORTH_Q, freqs)
    for b in eq["bands"]:
        if b["type"] in ("hpf", "lpf"):
            if b.get("on", True):
                out += section_db(b["type"], b["freq"], 0.0, BUTTERWORTH_Q, freqs)
        elif b["gain"] != 0:   # a zero-gain band is flat (TF Editor skips it)
            out += section_db(b["type"], b["freq"], b["gain"], b["q"], freqs)
    return out


def section_db(kind: str, freq: float, gain_db: float, q: float, freqs: np.ndarray = CURVE_FREQS,
               fs: float = FS) -> np.ndarray:
    """|H| in dB of one section at ``freqs``; ``kind``: bell, low_shelf, high_shelf,
    hpf / lpf (2nd order, Q), hpf1 / lpf1 (1st order)."""
    b, a = biquad(kind, freq, gain_db, q, fs)
    z = np.exp(-1j * 2 * np.pi * np.asarray(freqs, dtype=float) / max(fs, 44100.0))   # z^-1
    h = (b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z)
    return 20 * np.log10(np.maximum(np.abs(h), 1e-12))


def biquad(kind: str, freq: float, gain_db: float, q: float, fs: float = FS) -> tuple[list[float], list[float]]:
    """(b, a) of TF Editor's design for one section (a not normalised)."""
    fs = max(fs, 44100.0)
    q = max(q, 0.1)
    k = math.tan(math.pi * max(freq, 16.0) / fs)
    kk = k * k
    v = 10 ** (abs(gain_db) / 20)
    if kind == "bell":   # Zoelzer: the cut mirrors the boost
        num, den = (v * k / q, k / q) if gain_db > 0 else (k / q, v * k / q)
        return [1 + kk + num, 2 * (kk - 1), 1 + kk - num], [1 + kk + den, 2 * (kk - 1), 1 + kk - den]
    if kind in ("low_shelf", "high_shelf"):
        # Two identical first-order shelves (Q not used), corner at freq.
        sv = math.sqrt(v)
        plain = [1 + k, k - 1]
        shelf = [1 + k * sv, k * sv - 1] if kind == "low_shelf" else [k + sv, k - sv]
        top, bottom = (shelf, plain) if gain_db > 0 else (plain, shelf)
        return [float(x) for x in np.convolve(top, top)], [float(x) for x in np.convolve(bottom, bottom)]
    if kind in ("hpf", "lpf"):
        d = [1 + k / q + kk, 2 * (kk - 1), 1 - k / q + kk]
        return ([kk, 2 * kk, kk] if kind == "lpf" else [1.0, -2.0, 1.0]), d
    if kind in ("hpf1", "lpf1"):
        return ([k, k, 0.0] if kind == "lpf1" else [1.0, -1.0, 0.0]), [1 + k, k - 1, 0.0]
    raise ValueError(kind)
