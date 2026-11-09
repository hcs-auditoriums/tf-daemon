"""Vocal EQ suggestions from a mic check.

Listen to a vocal mic while the person talks, build the long-term average
spectrum (LTAS) of their speech, compare its *shape* with a reference speech
spectrum, and suggest console EQ settings that would bring it closer. The
suggestions are advice for the operator; nothing here talks to the console.

How
---
VoiceCollector, per hop, on the newest 4096 samples (85 ms, Hann):
  * speech activity: level in 100 Hz-8 kHz more than 12 dB above a tracked
    noise floor (fast down, slow up);
  * pitch: normalized autocorrelation (Boersma's method: the frame's
    autocorrelation divided by the window's), voiced when the peak in the
    70-400 Hz lag range is >= 0.45;
  * active frames are summed into the LTAS (voiced and unvoiced: sibilance
    lives in the unvoiced ones), voiced frames give the pitch statistics.

analyze() then:
  1. reads the LTAS (1/3-octave averages at 1/6-octave points, 160 Hz-8 kHz)
     and compares it with the ANSI S3.5-1997 standard speech spectrum (normal
     vocal effort, Table 3, one-third octave). Only the shape counts: the level
     is chosen so that the voice *after* the suggested EQ has a median
     difference of zero over 200 Hz-6.3 kHz (outside the voice's fundamental),
     found by refitting until it settles, so problem regions can't drag the
     reference with them;
  2. fits up to four peaking EQ bands to the difference,
     greedily: the largest excess becomes a cut (at most ``max_cut_db``, 80 % of
     the excess), a deficit at 2-8 kHz a gentle boost (at most ``max_boost_db``),
     width from where the excess falls to half (a build-up must span at least
     1/3 octave; single-point bumps are harmonic ripple); each band's response
     is taken out before looking for the next;
  3. looks for narrow resonances (peaks 6 dB above the LTAS around them, at
     most 1/8 octave wide, 500 Hz-8 kHz, not harmonics of the voice) and adds
     rings the detector reported on the input, as narrow cuts;
  4. suggests a high-pass just under the voice's lowest pitch;
  5. names each move after the usual vocal descriptors (boomy, boxy, woody,
     nasal, tinny, harsh, sibilant; presence, air) and assigns it to a TF5
     PEQ band (L, 1, 2, H) by frequency.

The descriptors are a vocabulary for where an issue sits, not a diagnosis:
"nasal" and "honky" overlap, and part of nasality is how a person talks. For
higher voices the low mids are sparse (harmonics every ~200+ Hz), so a
build-up shows where the harmonics are and its label there is approximate.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from typing import Any

import numpy as np

# ANSI S3.5-1997 Table 3: standard speech spectrum level, normal vocal effort
# (dB per Hz, one-third octave band centres).
ANSI_FREQS = np.array([160, 200, 250, 315, 400, 500, 630, 800, 1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000,
                       6300, 8000], dtype=float)
ANSI_NORMAL = np.array([32.41, 34.48, 34.75, 33.98, 34.59, 34.27, 32.06, 28.30, 25.01, 23.00, 20.15, 17.32, 13.18,
                        11.55, 9.33, 5.31, 2.59, 1.13])

POINTS = 160.0 * 2.0 ** (np.arange(34) / 6)            # 1/6-octave analysis points, 160 Hz .. 7.2 kHz
CURVE = np.geomspace(40.0, 16000.0, 121)                # for plotting the suggested EQ
NORM_LO, NORM_HI = 200.0, 6300.0

# (low, high, name) where a cut / boost sits; first match wins.
CUT_NAMES = [(0, 250, "boomy / muddy"), (250, 500, "boxy"), (500, 800, "woody / hollow"),
             (800, 1500, "nasal / honky"), (1500, 2600, "tinny / thin"), (2600, 5000, "harsh"),
             (5000, 20000, "sibilant")]
BOOST_NAMES = [(0, 5000, "presence (clarity)"), (5000, 20000, "air")]

# TF5 input PEQ bands and the range each is most at home in.
SLOTS = [("L", 0, 400), ("1", 250, 1600), ("2", 1000, 5000), ("H", 3500, 20000)]


def _name(table: Sequence[tuple[float, float, str]], f: float) -> str:
    return next(n for lo, hi, n in table if lo <= f < hi)


def bell_db(freqs: np.ndarray, f0: float, gain_db: float, q: float) -> np.ndarray:
    """Magnitude (dB) of an analog peaking EQ (RBJ prototype) at ``freqs``."""
    s = 1j * np.asarray(freqs, dtype=float) / f0
    a = 10 ** (gain_db / 40)
    h = (s * s + s * (a / q) + 1) / (s * s + s / (a * q) + 1)
    return 20 * np.log10(np.abs(h))


def hpf_db(freqs: np.ndarray, f0: float, order: int = 2) -> np.ndarray:
    """Butterworth high-pass magnitude (dB), 6 dB/octave per order."""
    return -10 * np.log10(1 + (f0 / np.asarray(freqs, dtype=float)) ** (2 * order))


def q_for_octaves(n: float) -> float:
    """Q of a bell whose half-gain points are ``n`` octaves apart."""
    r = 2.0 ** n
    return math.sqrt(r) / (r - 1)


class VoiceCollector:
    """Accumulates one mic check: speech activity, pitch, long-term spectrum."""

    N = 4096
    ACTIVE_DB = 12.0       # above the noise floor
    MIN_LEVEL_DB = -85.0   # absolute floor for "someone is talking" (dBFS, sine-peak)
    VOICED = 0.45

    def __init__(self, samplerate: int = 48000) -> None:
        self.fs = samplerate
        n = self.N
        self.window = np.hanning(n).astype(np.float64)
        self._wsum = float(np.sum(self.window))
        self._win_ac = np.fft.irfft(np.abs(np.fft.rfft(self.window, 2 * n)) ** 2)[:n]
        self.freqs = np.fft.rfftfreq(n, 1 / samplerate)
        self._k0, self._k1 = int(100 * n / samplerate), int(8000 * n / samplerate)
        self._lag0, self._lag1 = int(samplerate / 400), int(samplerate / 70)
        self.ltas = np.zeros(len(self.freqs))
        self.active_frames = 0
        self.voiced_frames = 0
        self.total_frames = 0
        self.f0s: list[float] = []
        self._floor: float | None = None
        self._speech_levels: list[float] = []
        self._last_t: float | None = None
        self.speech_s = 0.0

    def _db(self, p: float) -> float:
        return 10 * math.log10(p * (2 / self._wsum) ** 2 + 1e-30)

    def process(self, frame: np.ndarray, now: float) -> bool:
        """Analyze the newest N samples at stream time ``now`` (s). True if speech."""
        dt = 0.05 if self._last_t is None else min(max(now - self._last_t, 0.0), 0.5)
        self._last_t = now
        x = np.asarray(frame[-self.N:], dtype=np.float64)
        if len(x) < self.N:
            return False
        self.total_frames += 1
        spec = np.fft.rfft(x * self.window)
        p = np.abs(spec) ** 2
        level = self._db(float(np.sum(p[self._k0:self._k1])))
        # Noise floor: follows the level down at once, creeps up 3 dB/s.
        self._floor = level if self._floor is None else min(level, self._floor + 3.0 * dt)
        active = level > self._floor + self.ACTIVE_DB and level > self.MIN_LEVEL_DB
        if not active:
            return False
        self.ltas += p
        self.active_frames += 1
        self.speech_s += dt
        self._speech_levels.append(level)
        f0, strength = self._pitch(x)
        if strength >= self.VOICED:
            self.voiced_frames += 1
            self.f0s.append(f0)
        return True

    def _pitch(self, x: np.ndarray) -> tuple[float, float]:
        n = self.N
        xw = (x - x.mean()) * self.window
        ac = np.fft.irfft(np.abs(np.fft.rfft(xw, 2 * n)) ** 2)[:n]
        if ac[0] <= 0:
            return 0.0, 0.0
        r = ac / ac[0] / np.maximum(self._win_ac / self._win_ac[0], 1e-3)
        seg = r[self._lag0:self._lag1 + 1]
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
        return self.voiced_frames / self.active_frames if self.active_frames else 0.0

    @property
    def snr_db(self) -> float | None:
        if not self._speech_levels or self._floor is None:
            return None
        return float(np.median(self._speech_levels) - self._floor)

    def progress(self) -> dict[str, Any]:
        return {"speech_s": round(self.speech_s, 1), "voiced_ratio": round(self.voiced_ratio, 2),
                "f0_hz": round(float(np.median(self.f0s)), 1) if len(self.f0s) >= 5 else None}


# --------------------------------------------------------------------------- analysis

def _band_levels(collector: VoiceCollector, centres: np.ndarray, width_oct: float) -> np.ndarray:
    """Mean LTAS power density (dB) in bands of ``width_oct`` around each centre."""
    p = collector.ltas / max(collector.active_frames, 1)
    f = collector.freqs
    out = np.empty(len(centres))
    for i, fc in enumerate(centres):
        lo, hi = fc * 2 ** (-width_oct / 2), fc * 2 ** (width_oct / 2)
        sel = (f >= lo) & (f < hi)
        if not sel.any():
            sel = np.argmin(np.abs(f - fc)) == np.arange(len(f))
        out[i] = 10 * np.log10(float(np.mean(p[sel])) + 1e-30)
    return out


def _difference(collector: VoiceCollector) -> np.ndarray:
    """Measured minus target (dB) at POINTS, before levelling."""
    # 1/3-octave averaging: smooths the voice's harmonic comb (which ripples
    # +-2 dB in narrower bands) while costing well under 1 dB on the
    # octave-wide build-ups that make a voice boxy or nasal.
    measured = _band_levels(collector, POINTS, 1 / 3)
    target = np.interp(np.log(POINTS), np.log(ANSI_FREQS), ANSI_NORMAL)
    return measured - target


def _fit_bands(dev: np.ndarray, max_bands: int, max_cut: float, max_boost: float, *,
               skip: np.ndarray | None = None, cut_thr: float = 3.0, boost_thr: float = 3.0) -> list[dict[str, Any]]:
    """``skip``: points where no cut may be centred (the voice's fundamental, notches)."""
    resid = dev.copy()
    bands: list[dict[str, Any]] = []
    no_cut = np.zeros(len(POINTS), dtype=bool) if skip is None else skip
    for _ in range(max_bands):
        boostable = (POINTS >= 2000) & (POINTS <= 8000)
        score = np.where((resid > cut_thr) & ~no_cut, resid, 0.0)
        score = np.maximum(score, np.where(boostable & (resid < -boost_thr), -resid, 0.0))
        if score.max() <= 0:
            break
        p = int(np.argmax(score))
        cut = resid[p] > 0
        half = resid[p] / 2
        lo = p
        while lo > 0 and (resid[lo - 1] > half if cut else resid[lo - 1] < half):
            lo -= 1
        hi = p
        while hi < len(resid) - 1 and (resid[hi + 1] > half if cut else resid[hi + 1] < half):
            hi += 1
        if hi == lo:
            # A single point: harmonic ripple, not a build-up (narrow problems are
            # the resonance finder's job, which checks them more strictly).
            no_cut = no_cut.copy()
            no_cut[p] = True
            if not cut:
                resid[p] = 0.0
            continue
        octaves = max(np.log2(POINTS[hi] / POINTS[lo]) + 1 / 6, 1 / 3)
        q = float(np.clip(q_for_octaves(octaves), 0.5, 4.0))
        fc = float(np.sqrt(POINTS[lo] * POINTS[hi])) if hi > lo else float(POINTS[p])
        if cut:
            gain = -min(round(resid[p] * 0.8 * 2) / 2, max_cut)
        else:
            gain = min(round(-resid[p] * 0.6 * 2) / 2, max_boost)
        if abs(gain) < 1.0:
            break
        if any(abs(math.log2(fc / b["freq"])) < 1 / 3 for b in bands):
            resid[lo:hi + 1] = 0.0  # already covered; don't stack bands
            continue
        bands.append({"type": "bell", "freq": round(fc, -1 if fc < 1000 else -2), "gain": gain,
                      "q": round(q, 1), "issue": _name(CUT_NAMES if cut else BOOST_NAMES, fc),
                      "deviation_db": round(float(resid[p]), 1), "kind": "cut" if cut else "boost"})
        resid = resid + bell_db(POINTS, fc, gain, q)
    return bands


def _resonances(collector: VoiceCollector, f0: float | None, limit: int = 2) -> list[dict[str, Any]]:
    """Narrow peaks in the LTAS: 6 dB above the median of the +-1/8 octave around
    them, at most 1/8 octave wide at half power, 500 Hz-8 kHz (lower down the
    voice's first harmonics stay peaky), and not a harmonic of the voice."""
    p = collector.ltas / max(collector.active_frames, 1)
    db = 10 * np.log10(p + 1e-30)
    f = collector.freqs
    df = f[1]
    out = []
    for k in range(2, len(f) - 2):
        if not (500 <= f[k] <= 8000) or not (db[k] > db[k - 1] and db[k] >= db[k + 1]):
            continue
        w = max(3, int(0.08 * f[k] / df))
        lo, hi = max(0, k - w), min(len(f), k + w + 1)
        excess = db[k] - float(np.median(db[lo:hi]))
        if excess < 6.0:
            continue
        a, b = k, k  # half-power width
        while a > lo and db[a - 1] > db[k] - 3:
            a -= 1
        while b < hi - 1 and db[b + 1] > db[k] - 3:
            b += 1
        if np.log2(max(f[b], f[k] + df / 2) / max(f[a], df)) > 1 / 8:
            continue
        if f0 and any(abs(f[k] - m * f0) <= 0.03 * m * f0 for m in range(1, 13)):
            continue  # a harmonic of a fairly steady voice, not a resonance
        out.append((excess, f[k]))
    out.sort(reverse=True)
    return [{"type": "bell", "freq": round(float(fr), -1), "gain": -min(round(ex * 0.6 * 2) / 2, 6.0), "q": 8.0,
             "issue": "resonance", "deviation_db": round(float(ex), 1), "kind": "notch"}
            for ex, fr in out[:limit]]


def _assign_slots(moves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give each move a TF5 PEQ band, keeping frequency order, preferring each band's home range."""
    moves = sorted(moves, key=lambda m: m["freq"])

    def outside(s: int, f: float) -> float:  # octaves outside the band's home range
        lo, hi = SLOTS[s][1], SLOTS[s][2]
        return 0.0 if lo <= f <= hi else abs(math.log2(f / (lo if f < lo else hi)))

    best, best_cost = None, math.inf
    for combo in itertools.combinations(range(len(SLOTS)), len(moves)):
        cost = sum(outside(s, m["freq"]) for s, m in zip(combo, moves, strict=True))
        if cost < best_cost:
            best, best_cost = combo, cost
    for s, m in zip(best or (), moves, strict=False):  # best is None when nothing fits
        m["band"] = SLOTS[s][0]
    return moves


def _round_hpf(f: float) -> float:
    return float(5 * round(f / 5))


def analyze(collector: VoiceCollector, rings: list[dict[str, Any]] | None = None, *,
            max_cut_db: float = 6.0, max_boost_db: float = 3.0, min_speech_s: float = 6.0) -> dict[str, Any]:
    """Suggestions from a finished mic check (see the module docstring)."""
    prog = collector.progress()
    if collector.speech_s < min_speech_s:
        return {"ok": False, "error": f"only {collector.speech_s:.1f} s of speech heard; need at least "
                                      f"{min_speech_s:.0f} s", **prog}
    if collector.voiced_ratio < 0.25:
        return {"ok": False, "error": "this doesn't sound like a voice (little pitched sound); "
                                      "is it the right input, and is someone talking?", **prog}
    f0 = float(np.median(collector.f0s)) if len(collector.f0s) >= 20 else None
    f0_low = float(np.percentile(collector.f0s, 10)) if len(collector.f0s) >= 20 else None
    fundamental = np.zeros(len(POINTS), dtype=bool)
    if f0_low is not None:
        # Where the fundamental's energy concentrates: from just under the lowest
        # pitch to a little above the typical one.
        f0_high = float(np.percentile(collector.f0s, 90))
        assert f0 is not None  # set under the same condition as f0_low
        fundamental = (POINTS >= 0.85 * f0_low) & (POINTS <= max(1.25 * f0, f0_high))
    diff = _difference(collector)
    ref = (POINTS >= NORM_LO) & (POINTS <= NORM_HI) & ~fundamental

    notches = _resonances(collector, f0)
    for r in rings or []:
        if not any(abs(math.log2(r["freq"] / n["freq"])) < 1 / 12 for n in notches):
            notches.append({"type": "bell", "freq": round(float(r["freq"]), -1), "gain": -4.0, "q": 8.0,
                            "issue": f"ring ({r.get('count', 1)}× on this input)", "deviation_db": None,
                            "kind": "notch"})
    notches = notches[:2]
    # No broad cut centred on the voice's own fundamental (that energy *is* the
    # voice; the standard spectrum averages low and high voices) or on a notch.
    skip = fundamental.copy()
    for n in notches:
        skip |= np.abs(np.log2(POINTS / n["freq"])) <= 1 / 4
    # Level and fit together: start low (the 35th percentile, so build-ups that
    # cover much of the range still stand out), fit, then re-level so the median
    # of the EQ'd voice is zero, and refit until the level settles.
    off = float(np.percentile(diff[ref], 35))
    for _ in range(6):
        dev_raw = diff - off
        broad = _fit_bands(dev_raw, max_bands=4 - len(notches), max_cut=max_cut_db, max_boost=max_boost_db,
                           skip=skip)
        eq = sum((bell_db(POINTS, b["freq"], b["gain"], b["q"]) for b in broad), np.zeros(len(POINTS)))
        new_off = float(np.median((diff + eq)[ref]))
        if abs(new_off - off) < 0.1:
            break
        off = new_off
    moves = _assign_slots(broad + notches)

    hpf: dict[str, Any] | None = None
    if f0_low:
        freq = _round_hpf(min(max(0.75 * f0_low, 60.0), 180.0))
        hpf = {"band": "HPF", "type": "hpf", "freq": freq,
               "issue": f"rumble / proximity below the voice (lowest pitch ≈ {f0_low:.0f} Hz)"}

    curve = np.zeros(len(CURVE))
    after = dev_raw.copy()
    for m in moves:
        curve += bell_db(CURVE, m["freq"], m["gain"], m["q"])
        after += bell_db(POINTS, m["freq"], m["gain"], m["q"])
    if hpf:
        curve += hpf_db(CURVE, hpf["freq"])

    snr = collector.snr_db
    notes = []
    if collector.speech_s < 15:
        notes.append(f"Only {collector.speech_s:.0f} s of speech: treat these as rough.")
    if snr is not None and snr < 20:
        notes.append(f"Speech is only {snr:.0f} dB above the background: other sound on this mic may skew the result.")
    if collector.voiced_ratio < 0.4:
        notes.append("Less pitched sound than usual for speech: check it's a voice on this input.")
    confidence = "good" if collector.speech_s >= 15 and (snr or 0) >= 20 and collector.voiced_ratio >= 0.4 else \
        "fair" if collector.speech_s >= 8 else "rough"

    return {
        "ok": True,
        **prog,
        "f0_hz": round(f0, 1) if f0 else None,
        "voice": None if f0 is None else ("lower voice" if f0 < 165 else "higher voice"),
        "snr_db": None if snr is None else round(snr, 1),
        "confidence": confidence,
        "notes": notes,
        "hpf": hpf,
        "bands": moves,
        "points": [round(float(f), 1) for f in POINTS],
        "deviation": [round(float(d), 2) for d in dev_raw],
        "after": [round(float(d), 2) for d in after],
        "curve_freqs": [round(float(f), 1) for f in CURVE],
        "curve": [round(float(v), 2) for v in curve],
    }
