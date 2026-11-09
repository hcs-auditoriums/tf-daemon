"""How close a vocal mic is to feedback, per frequency: the loop gain and its headroom.

The loop is mic -> channel (EQ, fader) -> mains -> PA -> room -> back into the
mic. At a frequency where its gain reaches 0 dB the system rings; the headroom
is how far below that it is:

    headroom(f) = -(forward(f) + leak(f))     dB

* forward: mic -> mains, measured while the person talks (session.py's mains
  transfer). It includes the channel EQ and fader as they are now.
* leak: mains -> mic, the PA arriving back at this mic. Measured while this mic
  is *idle* and the mains carry something else (another talker, music): then
  what the mic picks up of the mains is only the leak. Measuring it while the
  person talks would be skewed by their own voice being in both signals.

Feedback is narrowband, so both are compared in 1/12-octave bands, from 150 Hz
to 5 kHz. Bands where either measurement is unclear (low coherence) are left
out. Until both exist the headroom is unknown, and nothing relies on it.

Checked against the simulated stage, whose loop is known exactly: the headroom
matches to within ~0.3 dB at its resonances. Where the leak measurement is
disturbed it reads *more* leak (less headroom) than there is, which only ever
makes the boosts more cautious.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np

from .transfer import Transfer

# 150 Hz .. 5 kHz, 1/24-octave steps: where speech (and most programme) excites the loop. Above,
# what an idle mic picks up of the mains is mostly its own noise coming back round, which
# correlates with itself and would fake a strong leak.
POINTS = 150.0 * 2.0 ** (np.arange(0, 122) / 24)
WIDTH_OCT = 1 / 12
MIN_COHERENCE = 0.6       # both measurements, per band
MIN_FRAMES = 40           # ~2 s of each kind before trusting it
MIN_GOOD_SHARE = 0.3      # share of the bands that must be clear (the top octave often has little in it)
MARGIN_DB = 6.0           # keep at least this much headroom (for boosts, and auto mode)
IDLE_MAINS_DB = -60.0     # the mains must carry something (dBFS RMS) to measure the leak
MAINS_OVER_MIC_DB = 10.0  # ... clearly louder than the idle mic, so what it hears is mostly the PA


class Feedback:
    def __init__(self, samplerate: int = 48000, memory_s: float | None = 120.0) -> None:
        self.leak = Transfer(samplerate, max_delay_s=0.15, memory_s=memory_s)

    def process_idle(self, mains: np.ndarray, mic: np.ndarray) -> None:
        """One hop while this mic's talker is quiet: learn the PA -> mic leak."""
        level = lambda x: 10 * np.log10(float(np.mean(np.asarray(x[-self.leak.N:], dtype=np.float64) ** 2)) + 1e-30)  # noqa: E731
        m = level(mains)
        if m < IDLE_MAINS_DB or m - level(mic) < MAINS_OVER_MIC_DB:
            return
        self.leak.process(mains, mic)

    def restart(self) -> None:
        self.leak.restart()

    def headroom(self, forward: Transfer | None) -> dict[str, Any]:
        """Headroom per band and its minimum, from the forward (mic -> mains) transfer."""
        if forward is None or forward.frames < MIN_FRAMES or self.leak.frames < MIN_FRAMES:
            need = "the voice in the mains" if forward is None or forward.frames < MIN_FRAMES else \
                "the PA at this mic while it's idle (needs other sound in the mains)"
            return {"known": False, "note": f"feedback headroom: still measuring {need}…"}
        fg, fc = forward.bands(POINTS, WIDTH_OCT)
        lg, lc = self.leak.bands(POINTS, WIDTH_OCT)
        good = (fc >= MIN_COHERENCE) & (lc >= MIN_COHERENCE) & ~np.isnan(fg) & ~np.isnan(lg)
        if good.mean() < MIN_GOOD_SHARE:
            return {"known": False, "note": "feedback headroom: the measurements aren't clear enough yet"}
        head = np.where(good, -(fg + lg), np.nan)
        k = int(np.nanargmin(head))
        out: dict[str, Any] = {"known": True, "min_db": round(float(head[k]), 1), "at_hz": round(float(POINTS[k]), -1),
               "leak_delay_ms": None if self.leak.delay is None else round(self.leak.delay / self.leak.fs * 1000, 1),
               "points": [round(float(f), 1) for f in POINTS],
               "headroom": [None if np.isnan(h) else round(float(h), 1) for h in head]}
        out["note"] = (f"closest to feedback at {out['at_hz']:.0f} Hz: {out['min_db']:.0f} dB of headroom"
                       + (" (less than the safety margin)" if out["min_db"] < MARGIN_DB else ""))
        return out


def headroom_at(fb: dict[str, Any], freq: float, width_oct: float = 1 / 3) -> float | None:
    """The least headroom within ``width_oct`` around ``freq`` (None: unknown there)."""
    if not fb.get("known"):
        return None
    pts, head = np.asarray(fb["points"]), fb["headroom"]
    sel = [h for p, h in zip(pts, head, strict=True) if h is not None and abs(np.log2(p / freq)) <= width_oct / 2]
    return min(sel) if sel else None


def boost_cap(fb: dict[str, Any]) -> Callable[[float], float] | None:
    """The largest boost (dB) at a frequency that keeps MARGIN_DB of headroom; None when
    the headroom is unknown (then only the usual boost limit applies)."""
    if not fb.get("known"):
        return None

    def cap(freq: float) -> float:
        h = headroom_at(fb, freq)
        return float("inf") if h is None else max(0.0, h - MARGIN_DB)
    return cap
