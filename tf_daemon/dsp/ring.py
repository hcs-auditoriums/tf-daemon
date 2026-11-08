"""Ring (feedback) detection.

Background
----------
Just below the point of feedback, the loudspeaker -> room -> microphone loop
behaves like a very narrow resonant boost: with loop gain g at a frequency and
round-trip delay tau, the boost is 1/(1-g) and the resonance decays at
20*log10(1/g)/tau dB/s. At g = 0.95 and tau = 30 ms that is ~ +26 dB, about
0.5 Hz wide, decaying at ~15 dB/s, against ~60 dB/s for a room with a 1 s
reverberation time. So a ring:

  * does not have to be louder than the programme: it is the programme's own
    energy at one frequency, boosted and smeared out in time;
  * holds its frequency (a fraction of a percent), unlike voiced speech, whose
    harmonics glide with the pitch, or singing, which carries vibrato;
  * has no harmonic series (unless the system is clipping), unlike voice and
    tonal music;
  * outlasts the sound that excited it: after a line of dialogue the room dies
    away quickly and the ring is left behind.

Howling-detection research (van Waterschoot & Moonen, "Comparative evaluation of
howling detection criteria in notch-filter-based howling suppression", JAES 58(11),
2010) found the peak-to-harmonic power ratio (PHPR, m = 2, 3) the best single
criterion, and PHPR combined with a peak-to-neighbouring ratio over bins +-2..4
and the interframe magnitude slope deviation the best combination.

Method
------
Every hop the newest ``n_fft`` samples of an input are transformed. Candidate
peaks are local maxima that stand ``min_prominence_db`` above a background
(median of 1/24-octave band means across +-1/8 octave, which ignores the peak's
own leakage into adjacent bins) and pass the harmonic check: no comparable
energy at 2f or 3f (PHPR). Two paths look for rings:

  slow   on a ~1 s average of the power spectrum. Moving speech harmonics smear
         out; a steady ring stands up even when it is quieter than the voice.
         A peak that holds its frequency there for ``sustain_s`` is "sustained".
  fast   on the instantaneous spectrum:
           growing  level rising steadily (fitted slope >= growth_db_s with a
                    small deviation, cf. IMSD): runaway feedback.
           tail     the broadband level dropped (end of a phrase) but this peak
                    fell much less: it rings on after the excitation stopped.

RingLog turns per-input reports into events for the UI (with how often a ring
came back, which is the one to EQ out) and keeps an ignore list for steady
tones that aren't feedback.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .spectrum import blackman_harris

# ISO 1/3-octave centres (the TF's GEQ bands).
GEQ_BANDS = [20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000, 1250,
             1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500, 16000, 20000]

# sensitivity -> (min prominence dB, sustain seconds)
SENSITIVITY = {"low": (14.0, 1.2), "normal": (10.0, 0.8), "high": (7.0, 0.5)}

KIND_RANK = {"tail": 1, "sustained": 2, "growing": 3}


def geq_band(freq: float) -> float:
    return min(GEQ_BANDS, key=lambda b: abs(math.log(freq / b)))


@dataclass(frozen=True)
class RingSettings:
    n_fft: int = 16384
    fmin: float = 80.0
    fmax: float = 12000.0
    min_prominence_db: float = 10.0
    min_level_db: float = -75.0     # dBFS (sine-peak); quieter peaks are ignored
    sustain_s: float = 0.8
    avg_tau_s: float = 1.0          # slow path averaging
    phpr_db: float = 12.0           # peak must exceed its 2nd/3rd harmonic by this
    mains_hz: float = 60.0          # hum harmonics up to 1 kHz are skipped; 0 = off
    growth_db_s: float = 6.0
    tail_bb_drop_db: float = 10.0   # broadband fall that marks the end of a phrase
    tail_margin_db: float = 8.0     # how much less than that the ring must have fallen

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> RingSettings:
        r = cfg["ring"]
        prom, sustain = SENSITIVITY[str(r["sensitivity"]).lower()]
        return cls(fmin=float(r["fmin"]), fmax=float(r["fmax"]), min_prominence_db=prom, sustain_s=sustain,
                   mains_hz=float(r["mains_hz"]))


@dataclass
class _Track:
    f: float
    born: float
    last: float
    hist: deque = field(default_factory=lambda: deque(maxlen=400))  # (t, level_db, prominence_db, f)
    misses: int = 0
    kind: str | None = None
    seen: deque = field(default_factory=lambda: deque(maxlen=400))  # (t, also a peak in the instantaneous spectrum)

    def window(self, now: float, seconds: float) -> list[tuple[float, float, float, float]]:
        return [h for h in self.hist if now - h[0] <= seconds + 1e-9]


class _Tracker:
    """Follows peaks across hops: a peak continues a track within a small frequency tolerance."""

    def __init__(self, df: float, max_misses: int) -> None:
        self.df = df
        self.max_misses = max_misses
        self.tracks: list[_Track] = []

    def tol(self, f: float) -> float:
        return max(1.5 * self.df, 0.004 * f)

    def update(self, peaks: list[tuple[float, float, float]], now: float) -> None:
        free = set(range(len(self.tracks)))
        for f, level, prom in peaks:  # strongest first
            best, dist = None, None
            for i in free:
                d = abs(self.tracks[i].f - f)
                if d <= self.tol(f) and (dist is None or d < dist):
                    best, dist = i, d
            if best is None:
                t = _Track(f=f, born=now, last=now)
                t.hist.append((now, level, prom, f))
                self.tracks.append(t)
                continue
            free.discard(best)
            t = self.tracks[best]
            t.f += 0.3 * (f - t.f)
            t.last, t.misses = now, 0
            t.hist.append((now, level, prom, f))
        for i in free:
            self.tracks[i].misses += 1
        self.tracks = [t for t in self.tracks if t.misses <= self.max_misses]


class RingDetector:
    """Ring detection for one input. Call ``process`` once per hop."""

    MAX_PEAKS = 24  # candidates per hop and path

    def __init__(self, samplerate: int, hop: int, settings: RingSettings) -> None:
        self.fs = samplerate
        self.s = settings
        n = settings.n_fft
        self.window = blackman_harris(n)
        self._db_offset = 10.0 * math.log10((2.0 / float(np.sum(self.window, dtype=np.float64))) ** 2)
        self.df = samplerate / n
        self.freqs = np.arange(n // 2 + 1) * self.df
        self.k0 = max(4, int(math.ceil(settings.fmin / self.df)))
        self.k1 = min(n // 2 - 4, int(settings.fmax / self.df))
        self._alpha = math.exp(-hop / (samplerate * settings.avg_tau_s))
        self._p_avg: np.ndarray | None = None

        # Background: mean power in 1/24-octave bands, median across +-3 bands
        # (+-1/8 octave), interpolated back onto the FFT bins in log frequency.
        nb = int(24 * math.log2(settings.fmax / settings.fmin)) + 2
        edges = settings.fmin * 2.0 ** (np.arange(nb) / 24)
        starts = np.unique(np.clip(np.ceil(edges / self.df).astype(int), self.k0, self.k1 + 1))
        self._band_starts = starts[:-1] - self.k0
        self._band_counts = np.diff(starts)
        self._band_logf = np.log((starts[:-1] + starts[1:] - 1) / 2 * self.df)
        self._bin_logf = np.log(self.freqs[self.k0:self.k1 + 1])

        hum = [settings.mains_hz * k for k in range(1, int(1000 // settings.mains_hz) + 1)] if settings.mains_hz else []
        self._hum = np.array(hum)

        self.fast = _Tracker(self.df, max_misses=2)
        self.slow = _Tracker(self.df, max_misses=4)
        self._bb: deque[tuple[float, float]] = deque(maxlen=200)  # (t, programme level dB)
        self._last_band_median = -math.inf

    # ------------------------------------------------------------------ per hop

    def process(self, frame: np.ndarray, now: float) -> list[dict[str, Any]]:
        """Analyze the newest ``n_fft`` samples (1-D) at stream time ``now`` (s).

        Returns the rings currently present on this input.
        """
        x = frame[-self.s.n_fft:].astype(np.float64) * self.window
        p = np.abs(np.fft.rfft(x)) ** 2
        if self._p_avg is None:
            self._p_avg = p.copy()
        else:
            self._p_avg *= self._alpha
            self._p_avg += (1.0 - self._alpha) * p

        fast_peaks = self._peaks(p)
        # Programme level: median of the band levels, so a ring (one narrow
        # peak) doesn't count as programme while everything else dies away.
        self._bb.append((now, self._last_band_median))
        self.fast.update(fast_peaks, now)
        self.slow.update(self._peaks(self._p_avg), now)
        for t in self.slow.tracks:  # persistence: is the steady peak also there right now?
            if not t.misses:
                t.seen.append((now, any(abs(f - t.f) <= self.slow.tol(t.f) for f, _, _ in fast_peaks)))
        return self._classify(now)

    def _db(self, p: np.ndarray) -> np.ndarray:
        return 10.0 * np.log10(p + 1e-30) + self._db_offset

    def _background(self, p: np.ndarray) -> np.ndarray:
        seg = p[self.k0:self.k1 + 1]
        sums = np.add.reduceat(seg, self._band_starts)
        bands_db = self._db(sums / self._band_counts)
        self._last_band_median = float(np.median(bands_db))
        padded = np.pad(bands_db, 3, mode="edge")
        med = np.median(np.lib.stride_tricks.sliding_window_view(padded, 7), axis=1)
        return np.interp(self._bin_logf, self._band_logf, med)

    @staticmethod
    def _span(km: int) -> int:
        """Search half-width around a (sub)harmonic bin: +-1.5% covers piano
        inharmonicity and vibrato, at least +-2 bins for window leakage."""
        return max(2, int(round(0.015 * km)))

    def phpr(self, db: np.ndarray, k: int) -> float:
        """Peak power over the strongest of its 2nd and 3rd harmonics (dB); inf if out of range."""
        worst = math.inf
        for m in (2, 3):
            km = int(round(k * m))
            w = self._span(km)
            if km + w >= len(db):
                continue
            worst = min(worst, float(db[k] - np.max(db[km - w:km + w + 1])))
        return worst

    def _in_series(self, db: np.ndarray, prom: np.ndarray, k: int) -> bool:
        """A louder, prominent peak at f/2..f/10: this is an upper partial of a note."""
        for m in range(2, 11):
            km = int(round(k / m))
            w = self._span(km)
            if km - w < self.k0:
                break
            j = km - w + int(np.argmax(db[km - w:km + w + 1]))
            if db[j] >= db[k] - 3.0 and prom[j - self.k0] >= self.s.min_prominence_db:
                return True
        return False

    def _peaks(self, p: np.ndarray) -> list[tuple[float, float, float]]:
        db = self._db(p)
        seg = db[self.k0:self.k1 + 1]
        prom = seg - self._background(p)
        is_max = np.zeros(seg.shape, dtype=bool)
        is_max[1:-1] = (seg[1:-1] > seg[:-2]) & (seg[1:-1] >= seg[2:])
        cand = np.flatnonzero(is_max & (prom >= self.s.min_prominence_db) & (seg >= self.s.min_level_db))
        if cand.size == 0:
            return []
        cand = cand[np.argsort(prom[cand])[::-1][:self.MAX_PEAKS]]
        out = []
        for c in cand:
            k = int(c) + self.k0
            if self.phpr(db, k) < self.s.phpr_db:
                continue  # has harmonics above it: voice or a musical note
            if self._in_series(db, prom, k):
                continue  # an upper partial of a note (its own harmonics may be out of range)
            a, b, g = db[k - 1], db[k], db[k + 1]
            den = a - 2 * b + g
            d = 0.5 * (a - g) / den if den < 0 else 0.0  # parabolic interpolation
            f = (k + d) * self.df
            if self._hum.size and np.min(np.abs(self._hum - f)) <= max(1.0, self.df):
                continue
            out.append((f, float(b - 0.25 * (a - g) * d), float(prom[c])))
        return out

    # ------------------------------------------------------------------ classification

    def _stable(self, hist: list[tuple[float, float, float, float]]) -> bool:
        fs = [h[3] for h in hist]
        return max(fs) - min(fs) <= max(1.0 * self.df, 0.003 * float(np.mean(fs)))

    def _classify(self, now: float) -> list[dict[str, Any]]:
        s = self.s
        found: list[tuple[_Track, str]] = []

        # Sustained: steady in the averaged spectrum for the whole window, well
        # above it (a room's own response has narrow peaks too, but ~+5..10 dB,
        # where a ring near feedback is +20 dB), still present in most recent
        # instantaneous frames (persistence), and not fading: once its source
        # stops, a short note's trace in the average fades at 4.3 dB/s per
        # second of avg_tau, while a live ring holds its level.
        win_s = s.sustain_s + 0.4
        for t in self.slow.tracks:
            if t.misses:
                continue
            if t.kind is None and now - t.born >= win_s and self._stable(t.window(now, win_s)):
                seen = [v for ts, v in t.seen if now - ts <= 0.6 + 1e-9]
                win = t.window(now, win_s)
                ts = np.array([h[0] for h in win]) - win[0][0]
                slope = float(np.polyfit(ts, [h[1] for h in win], 1)[0]) if len(win) >= 4 else -math.inf
                prom = float(np.median([h[2] for h in win]))
                if (seen and sum(seen) / len(seen) >= 0.6 and slope >= -2.0
                        and prom >= s.min_prominence_db + 5):
                    t.kind = "sustained"
            if t.kind:
                found.append((t, t.kind))

        bb = [b for tb, b in self._bb if now - tb <= 0.6 + 1e-9]
        for t in self.fast.tracks:
            if t.misses or now - t.born < 0.2:
                continue
            kind = None
            age = now - t.born
            # Growing: a steady rise over a full second that is still rising.
            # (The FFT window turns any note onset into a ~0.34 s ramp; runaway
            # feedback keeps climbing for seconds.)
            if age >= 1.0 and self._stable(t.window(now, 1.0)):
                win = t.window(now, 1.0)
                if len(win) >= 12:
                    ts = np.array([h[0] for h in win]) - win[0][0]
                    lv = np.array([h[1] for h in win])
                    slope, icpt = np.polyfit(ts, lv, 1)
                    resid = float(np.std(lv - (slope * ts + icpt)))
                    half = ts >= ts[-1] - 0.5
                    late = float(np.polyfit(ts[half], lv[half], 1)[0])
                    if slope >= s.growth_db_s and late >= s.growth_db_s and resid <= 3.0:
                        kind = "growing"
            if kind is None and age >= 0.25 and bb and self._stable(t.window(now, 0.25)):
                recent = t.window(now, 0.6)
                bb_drop = max(bb) - bb[-1]
                ring_drop = max(h[1] for h in recent) - recent[-1][1]
                if (bb_drop >= s.tail_bb_drop_db and ring_drop <= bb_drop - s.tail_margin_db
                        and recent[-1][2] >= s.min_prominence_db + 3):
                    kind = "tail"
            if kind and (t.kind is None or KIND_RANK[kind] > KIND_RANK[t.kind]):
                t.kind = kind  # a track keeps its strongest classification
            if t.kind:
                found.append((t, t.kind))

        # One report per frequency (a ring usually shows on both paths).
        rings: list[dict[str, Any]] = []
        for t, kind in sorted(found, key=lambda tk: -KIND_RANK[tk[1]]):
            if any(abs(r["freq"] - t.f) <= max(2 * self.df, 0.01 * t.f) for r in rings):
                continue
            h = t.hist[-1]
            rings.append({"freq": round(t.f, 1), "level": round(h[1], 1), "prominence": round(h[2], 1),
                          "kind": kind, "since": t.born})
        return rings


@dataclass
class RingEvent:
    id: int
    channel: int
    label: str
    freq: float
    kind: str
    level: float          # loudest seen
    prominence: float     # largest seen
    first_seen: float     # wall clock
    last_seen: float
    count: int = 1        # separate occurrences
    active: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "channel": self.channel, "label": self.label, "freq": self.freq, "kind": self.kind,
                "level": self.level, "prominence": self.prominence, "first_seen": self.first_seen,
                "last_seen": self.last_seen, "count": self.count, "active": self.active,
                "geq": geq_band(self.freq)}


class RingLog:
    """Ring events across inputs, for the UI. Thread-safe.

    A ring that comes back on the same input at the same frequency (within 1%)
    within ``merge_s`` is the same event with ``count`` incremented: a ring that
    keeps coming back is the one to EQ out. An event ends only after ``GRACE_S``
    unseen, so a detection that flickers for a few frames isn't a "comeback".
    """

    GRACE_S = 1.5
    MATCH = 0.01
    IGNORE_MATCH = 0.015
    KEEP = 40

    def __init__(self, merge_s: float = 300.0) -> None:
        self.merge_s = merge_s
        self._lock = threading.Lock()
        self._events: list[RingEvent] = []
        self._ignored: list[float] = []
        self._next_id = 1
        self.version = 0

    def update(self, channel: int, label: str, rings: list[dict[str, Any]], now: float) -> None:
        with self._lock:
            changed = False
            seen: set[int] = set()
            for r in rings:
                if any(abs(r["freq"] - f) <= self.IGNORE_MATCH * f for f in self._ignored):
                    continue
                ev = next((e for e in self._events if e.channel == channel and e.id not in seen
                           and abs(e.freq - r["freq"]) <= self.MATCH * e.freq
                           and (e.active or now - e.last_seen <= self.merge_s)), None)
                if ev is None:
                    ev = RingEvent(self._next_id, channel, label, r["freq"], r["kind"], r["level"],
                                   r["prominence"], now, now)
                    self._next_id += 1
                    self._events.append(ev)
                    changed = True
                else:
                    if not ev.active:
                        ev.count += 1
                        ev.active = True
                        changed = True
                    elif now - ev.last_seen > self.GRACE_S:
                        ev.count += 1  # gone long enough to be a new occurrence
                        changed = True
                    if KIND_RANK[r["kind"]] > KIND_RANK[ev.kind]:
                        ev.kind = r["kind"]
                        changed = True
                    ev.freq = r["freq"]
                    ev.level = max(ev.level, r["level"])
                    ev.prominence = max(ev.prominence, r["prominence"])
                    ev.last_seen = now
                    ev.label = label
                seen.add(ev.id)
            for e in self._events:
                if e.channel == channel and e.active and e.id not in seen and now - e.last_seen >= self.GRACE_S:
                    e.active = False
                    changed = True
            if len(self._events) > self.KEEP:
                self._events = sorted(self._events, key=lambda e: (e.active, e.last_seen))[-self.KEEP:]
            if changed:
                self.version += 1

    def end_channel(self, channel: int) -> None:
        """The input stopped being analyzed: its rings are no longer live."""
        with self._lock:
            for e in self._events:
                if e.channel == channel and e.active:
                    e.active = False
                    self.version += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            events = sorted(self._events, key=lambda e: (not e.active, -e.last_seen))
            return {"type": "rings", "version": self.version, "events": [e.to_dict() for e in events],
                    "ignored": list(self._ignored)}

    def clear(self) -> None:
        """Forget past events (live ones stay)."""
        with self._lock:
            self._events = [e for e in self._events if e.active]
            self.version += 1

    def ignore(self, freq: float) -> None:
        with self._lock:
            if not any(abs(freq - f) <= self.IGNORE_MATCH * f for f in self._ignored):
                self._ignored.append(round(freq, 1))
            self._events = [e for e in self._events if abs(e.freq - freq) > self.IGNORE_MATCH * freq]
            self.version += 1

    def unignore(self, freq: float | None = None) -> None:
        with self._lock:
            self._ignored = [] if freq is None else [f for f in self._ignored
                                                     if abs(f - freq) > self.IGNORE_MATCH * f]
            self.version += 1
