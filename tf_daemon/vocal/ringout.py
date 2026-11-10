"""Ring-out: finds where each vocal mic feeds back, notches it, and sets the faders, on its own.

ON ONLY WHEN both: ``tf5.fader_write`` (and ``eq_write``) in the config, and a person
starts it for chosen inputs (POST /api/vocal/ringout/start with ``confirm: true``).
While it runs, the page keeps a popup open with **Mute & cancel** (``abort``): that
mutes every session channel at once, from the request itself (not via this loop),
then puts their faders and EQ back as they were. The channels stay muted until a
person unmutes them.

What a sound engineer does by hand, in that order:

1. Each mic alone (the other session mics muted). The high-pass goes on at the mic
   type's floor (or stays higher if it was), and the voice is measured all the way
   through (the USB taps are before the fader, so the ramp doesn't skew it):
   * the fader starts low (RAMP_START_DB under its level, at most the ceiling) and
     rises RAMP_DB every RAMP_EVERY_S, never above ``vocal.ringout_ceiling_db``;
   * a ring (see below) pulls the fader down DROP_DB at once. If the ring then falls
     clearly further than the fader did, it was feedback: a narrow notch goes on a
     free PEQ band (the mic type's Q, NOTCH_START_DB, deeper each time the same
     frequency comes back, down to NOTCH_MAX_DB) and the ramp carries on from just
     under where it rang. A ring low enough (under HPF_RING_MAX_HZ) is taken out with
     the high-pass instead, as far as the voice allows. If it fell only as far as the
     fader, it's a steady tone in the room (hum, a sustained note): ignored from then on.
   * every band stays in frequency order (L <= LM <= HM <= H, bands.py): placing a
     notch can move the channel's other bands along (same settings, another band), and
     when there's no room the desk band doing least gives way.
   * done at the ceiling, when MAX_NOTCHES are used, when a notched ring comes back
     at full depth, or when rings start at several frequencies within WIDE_DB of
     each other (the system's limit: more notches buy nothing).
2. All mics together, each at its own safe level: the same ramp, all faders at once,
   up to the margin. More open mics feed back sooner (about 3 dB per doubling); a
   ring here is notched on the mic the ring detector names as its source.
3. Faders end at most ``vocal.ringout_margin_db`` under where they rang (alone and
   together), never above where the operator had them; a fader that was down stays
   down. On / off goes back to how it was.
4. The vocal curve: from the voice heard during the ring-out, the suggestion for its
   mic type and EQ style (tone.py: the high-pass under the voice but never below what
   a ring needed, cuts where it builds up, the style's character) goes on the bands
   the notches left, in one step, in frequency order.
5. Optionally, live tone EQ (auto.py) on each mic with its mic type and EQ style,
   the notches reserved, while the person talks or sings, until a person finishes.

Rings are found two ways: the ring detector's events (dsp/ring.py) on the mic or the
mains, and a ramp-specific watch on the mains here (_Watch): a narrow peak that rises
GROW_DB above where it was a second or more before and holds its frequency for
PERSIST_S. The mains peaking over RUNAWAY_PEAK counts as a ring at once.

Safety, besides Mute & cancel:
  * any change to a session channel's fader or (during 1-2) EQ that ring-out didn't
    make: an operator has taken over. It stops at once and leaves the desk alone.
  * the page stops polling for HEARTBEAT_S while faders are being pushed: faders back
    to where they were, stop.
  * before starting, the fader the editor protocol reports must equal what RCP reports
    (same channel, same scale), and after every fader move RCP must follow within
    RCP_FOLLOW_S; otherwise it stops and puts the faders back.
  * the desk becoming unreachable, or anything unexpected: faders back, stop.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..dsp.db import power_db
from . import bands
from .auto import PATHS, AutoEQ
from .plan import eq_plan
from .ports import AudioInputs, DeskFaders
from .session import MicChecks
from .targets import MIC_TYPES

log = logging.getLogger(__name__)

NEG_INF = -32768
TICK_S = 0.05
RAMP_START_DB = 10.0
RAMP_FLOOR_DB = -20.0  # a fader lower than this starts the ramp as if it were here
RAMP_DB = 1.0
RAMP_EVERY_S = 1.0
DROP_DB = 6.0
VERIFY_S = 0.8  # after the drop: how long until the ring must have fallen
VERIFY_EXTRA_DB = 4.0  # ... at least this much further than the fader did
CLEAR_S = 0.7  # then this long before the ramp goes on
RESUME_BELOW_DB = 3.0  # the ramp goes on from this far under where it rang
NOTCH_START_DB = -3.0
NOTCH_STEP_DB = 3.0
NOTCH_MAX_DB = -9.0
MAX_NOTCHES = 3  # per mic (one band stays for tone)
WIDE_DB = 1.5
SAME_OCT = 1 / 24  # a ring this close to a notch is the same ring
NEAR_OCT = 1 / 6  # ... this close: the same resonance ringing beside the notch (the loop's delay
#                    makes it ring at one of several frequencies ~1/delay apart): widen that notch
MIN_NOTCH_Q = 3.0
HPF_RING_MAX_HZ = 170.0  # a ring under this is taken out with the high-pass when the voice allows
HPF_MAX_HZ = 200.0  # the highest ring-out puts the high-pass
HPF_RING_RATIO = 1.3  # the high-pass goes this far above the ring
HANDLED_S = 3.0  # a ring dealt with: the detector's event for it is ignored this long (it lingers)
HEARTBEAT_S = 10.0
MAINS_LISTEN_S = 2.0  # before touching a fader: listen to the mains this long...
MAINS_SILENT_DB = -80.0  # ... and stop if they carry less than this (RMS dBFS): no PA, no loop to ring
RCP_FOLLOW_S = 2.0
LOG_KEEP = 60


# ---------------------------------------------------------------------- the mains watch


class _Watch:
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


# ---------------------------------------------------------------------- per mic


@dataclass
class _Mic:
    index: int
    label: str
    mic: str
    style: str
    base_level: int  # raw fader level before the session
    base_on: int
    base_eq: dict[str, int]
    level: int = NEG_INF  # what we last set
    on: int = 0
    known_eq: dict[str, int] = field(default_factory=dict)
    state: str = "waiting"  # waiting | ringing out | done | stopped | cancelled
    note: str = ""
    rings: list[dict[str, Any]] = field(default_factory=list)  # confirmed: freq, at_db, band, gain
    ignored: list[float] = field(default_factory=list)  # steady tones, not feedback
    notches: dict[str, dict[str, Any]] = field(default_factory=dict)  # band path -> freq, gain, q
    first_ring_db: float | None = None
    limit_db: float | None = None  # alone: where it last rang
    final: int | None = None  # raw level it ends at
    hpf_floor: float = 0.0  # Hz: the high-pass may not go below this (the mic type, a low ring)
    shaped: list[str] = field(default_factory=list)  # the vocal curve it set, a line per band

    def summary(self) -> dict[str, Any]:
        db = lambda raw: None if raw is None or raw <= NEG_INF else round(raw / 100, 1)  # noqa: E731
        return {
            "index": self.index,
            "label": self.label,
            "mic": self.mic,
            "style": self.style,
            "state": self.state,
            "note": self.note,
            "level_db": db(self.level),
            "on": bool(self.on),
            "baseline_db": db(self.base_level),
            "first_ring_db": self.first_ring_db,
            "limit_db": self.limit_db,
            "final_db": db(self.final),
            "gained_db": None
            if self.first_ring_db is None or self.limit_db is None
            else round(self.limit_db - self.first_ring_db, 1),
            "rings": list(self.rings),
            "hpf_hz": (self.known_eq.get("PEQ/Bank/HPF/Frequency", 0) / 10)
            if self.known_eq.get("PEQ/Bank/HPF/On")
            else None,
            "shaped": list(self.shaped),
            "notches": [
                {"band": _band_label(b), "freq": n["freq"], "gain": n["gain"], "q": n["q"]}
                for b, n in self.notches.items()
            ],
        }


def _band_label(path: str) -> str:
    return {"PEQ/Bank/BandL": "L", "PEQ/Bank/Band:0": "LM", "PEQ/Bank/Band:1": "HM", "PEQ/Bank/BandH": "H"}[path]


class Stop(Exception):  # noqa: N818 - control flow inside the loop, not an error
    """Leave the loop: ``how`` = done | takeover | pullback (faders back to where they were)."""

    def __init__(self, how: str, why: str) -> None:
        super().__init__(why)
        self.how, self.why = how, why


# ---------------------------------------------------------------------- the session


class RingOut:
    """One ring-out at a time, over one or more vocal inputs (see the module docstring)."""

    def __init__(
        self,
        audio: AudioInputs,
        checks: MicChecks,
        desk: DeskFaders,
        auto: AutoEQ,
        *,
        mains: tuple[int, ...],
        ceiling_db: float,
        margin_db: float,
        samplerate: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], object] | None = None,
    ) -> None:
        """``clock`` / ``sleep``: time for the loop (tests drive the simulated stage with them)."""
        self.engine = audio
        self.checks = checks
        self.desk = desk
        self.auto = auto
        self.mains = tuple(mains)
        self.ceiling = round(ceiling_db * 100)
        self.margin = round(margin_db * 100)
        self.clock = clock
        self.watch = _Watch(samplerate)
        self._lock = threading.Lock()
        self._gate = threading.Lock()  # a write and the abort check before it, as one
        self._aborted = threading.Event()
        self._sleep = sleep or self._aborted.wait
        self._thread: threading.Thread | None = None
        self.mics: list[_Mic] = []
        self.phase = "idle"  # idle | ringing out | together | tone | finished | aborted | stopped | error
        self.message = ""
        self.tone = False
        self.current: int | None = None  # the mic being rung out
        self.together_db: float | None = None  # rang with all open, at this offset re their safe levels
        self.log: deque[dict[str, Any]] = deque(maxlen=LOG_KEEP)
        self.heartbeat = clock()
        self.started: float | None = None
        self._held: list[int] = []
        self._rcp_due: dict[int, tuple[float, int]] = {}  # index -> (deadline, level RCP must show)
        self._handled: list[tuple[float, float]] = []  # (freq, until): rings just dealt with
        audio.add_frame_tap(self._frames)

    # ------------------------------------------------------------------ a person's requests

    @property
    def running(self) -> bool:
        return self.phase in ("ringing out", "together", "tone")

    def channels(self) -> set[int]:
        return {m.index for m in self.mics} if self.running else set()

    def start(self, mics: list[tuple[int, str, str]], tone: bool) -> dict[str, Any]:
        """Start a ring-out over ``[(input, mic type, EQ style)]``. Raises ValueError /
        PermissionError (nothing changed) when it can't."""
        if self.running:
            raise ValueError("a ring-out is already running")
        desk = self.desk
        if not (desk.eq_writable and desk.fader_writable):
            raise PermissionError("ring-out needs tf5.eq_write and tf5.fader_write")
        if not self.mains:
            raise ValueError("ring-out listens to the mains: put St L / St R on the USB patch (tf5.usb_patch)")
        if not mics or len({i for i, _, _ in mics}) != len(mics):
            raise ValueError("choose one or more different inputs")
        if self.auto.states() and any(self.auto.state(i)["on"] for i, _, _ in mics):
            raise ValueError("turn auto EQ off on those channels first")
        built = []
        for index, mic, style in mics:
            label = self.engine.inputs[index].name
            if desk.eq_target(index) is None:
                raise ValueError(f"{label} isn't an input channel on the desk")
            eq = desk.eq(index).get("eq")
            if not eq:
                raise ValueError(f"{label}: the desk's EQ hasn't been read yet")
            if not eq["on"] or eq.get("one_knob"):
                raise ValueError(f"{label}: turn the channel's EQ on (and 1-knob off) on the desk first")
            raw = desk.eq_raw(index, PATHS)
            if any(not isinstance(raw.get(p), int) for p in PATHS):
                raise ValueError(f"{label}: couldn't read the channel's EQ from the desk")
            f = desk.fader(index)
            if f["level"] is None or f["on"] is None:
                raise ValueError(f"{label}: couldn't read the fader from the desk")
            if f["rcp_level"] is None:
                raise ValueError(f"{label}: the fader isn't known over RCP yet (needed to check every move)")
            if f["rcp_level"] != f["level"] or f["rcp_on"] != f["on"]:
                raise ValueError(
                    f"{label}: the two console protocols disagree about the fader "
                    f"({f['level']} vs {f['rcp_level']}); not moving it"
                )
            eq_now = {p: int(v) for p, v in raw.items() if isinstance(v, int)}
            built.append(
                _Mic(
                    index,
                    label,
                    mic,
                    style,
                    f["level"],
                    f["on"],
                    eq_now,
                    level=f["level"],
                    on=f["on"],
                    known_eq=dict(eq_now),
                )
            )
        with self._lock:
            self.mics = built
            self.tone = tone
            self.phase = "ringing out"
            self.message = "starting"
            self.current = None
            self.together_db = None
            self.log.clear()
            self._aborted.clear()
            self.heartbeat = self.clock()
            self.started = time.time()
            self._rcp_due.clear()
            self._handled.clear()
        self._held = [*(m.index for m in built), *self.mains]
        for i in self._held:
            self.engine.acquire(i)
        for m in built:  # the voice, measured while it rings out (the taps are before the fader)
            self.checks.start(m.index, live=True, mic=m.mic, style=m.style)
        self.watch.reset()
        self._log("start", f"ring-out of {', '.join(m.label for m in built)} started by a person")
        self._thread = threading.Thread(target=self._run, name="vocal-ringout", daemon=True)
        self._thread.start()
        return self.status()

    def abort(self) -> dict[str, Any]:
        """MUTE & CANCEL: mute every session channel now, then put faders and EQ back. The
        channels stay muted. Always allowed; needs no confirmation."""
        self._aborted.set()
        mics = list(self.mics)
        muted, errors = [], []
        with self._gate:  # after any write in flight; nothing from the loop after this
            for m in mics:
                try:
                    self.desk.write_fader(m.index, on=0)
                    m.on = 0
                    muted.append(m.label)
                except Exception as e:  # keep muting the others
                    errors.append(f"{m.label}: {e}")
        self.auto_off("ring-out cancelled")
        self._join()
        for m in mics:
            try:
                self._restore(m, on=False)
            except Exception as e:
                errors.append(f"{m.label}: {e}")
        self._finish_phase(
            "aborted",
            "Muted and put back: faders and EQ as before, channels muted until you unmute them."
            + (f" Problems: {'; '.join(errors)}" if errors else ""),
        )
        log.warning("Ring-out cancelled by a person: muted %s", ", ".join(muted) or "nothing")
        return self.status()

    def unmute(self) -> dict[str, Any]:
        """After Mute & cancel: put the channels' on switches back as they were."""
        if self.phase != "aborted":
            raise ValueError("nothing to unmute")
        for m in self.mics:
            if m.base_on:
                self.desk.write_fader(m.index, on=1)
                m.on = 1
        self._log("unmute", "on switches put back by a person")
        return self.status()

    def finish(self) -> dict[str, Any]:
        """End the session normally: tone EQ stops, the desk stays as it is now."""
        if not self.running:
            raise ValueError("no ring-out is running")
        self._aborted.set()  # the loop stops writing
        self.auto_off("ring-out finished")
        self._join()
        if self.phase in ("ringing out", "together"):  # finished early: faders back to a safe level
            for m in self.mics:
                self._restore(m, on=True, eq=False)
        self._finish_phase("finished", "Finished by a person.")
        return self.status()

    def touch(self) -> None:
        self.heartbeat = self.clock()

    def auto_off(self, why: str) -> None:
        for m in self.mics:
            if self.auto.state(m.index)["on"]:
                self.auto.disarm(m.index, why)

    def status(self) -> dict[str, Any]:
        mics = list(self.mics)
        shared = _shared_rings(mics)
        return {
            "phase": self.phase,
            "running": self.running,
            "message": self.message,
            "current": self.current,
            "tone": self.tone,
            "started": self.started,
            "ceiling_db": self.ceiling / 100,
            "margin_db": self.margin / 100,
            "together_db": self.together_db,
            "mics": [m.summary() for m in mics],
            "shared": shared,
            "log": list(self.log),
            "auto": {str(m.index): self.auto.state(m.index) for m in mics} if self.phase == "tone" else {},
        }

    # ------------------------------------------------------------------ the analysis thread

    def _frames(self, frames: dict[int, Any], stream_t: float) -> None:
        if not self.running or not self.mains or not all(m in frames for m in self.mains):
            return
        self.watch.feed(np.mean([frames[m] for m in self.mains], axis=0), stream_t)

    # ------------------------------------------------------------------ the loop

    def _run(self) -> None:
        how, why = "done", ""
        try:
            self._listen_to_mains()
            for m in self.mics:
                self._ring_out_alone(m)
            self._together()
            self._settle()
            self._shape()
            if self.tone:
                self._tone()
        except Stop as s:
            how, why = s.how, s.why
        except Exception as e:  # never leave faders up over something we didn't expect
            log.exception("Ring-out failed")
            how, why = "pullback", f"stopped: {e}"
        if self._aborted.is_set():
            return  # abort / finish own the desk now
        if how == "pullback":
            for m in self.mics:
                try:
                    self._restore(m, on=True, eq=False)
                except Exception:
                    log.exception("Ring-out couldn't put %s's fader back", m.label)
            self._finish_phase("error" if why.startswith("stopped:") else "stopped", why + " Faders put back.")
        elif how == "takeover":
            self.auto_off(why)
            self._finish_phase("stopped", why + " The desk is left as it is.")
        elif not self.tone:
            self._finish_phase("finished", "Done.")

    def _listen_to_mains(self) -> None:
        """Ring-out hears feedback on the mains. Silent mains (the bus muted, the PA off, USB not
        carrying it) mean there's no loop to ring and nothing to check a ring against: stop
        before touching anything."""
        self.message = "listening to the mains"
        self._wait(MAINS_LISTEN_S)
        levels = list(self.watch.levels)
        if not levels:
            raise Stop(
                "pullback", "Stopped: no sound from the mains on USB (check tf5.usb_patch); nothing was changed."
            )
        if float(np.median(levels)) < MAINS_SILENT_DB:
            raise Stop(
                "pullback",
                "Stopped: the mains are silent (main bus muted or the PA off?). Ring-out needs the PA on to "
                "hear feedback; nothing was changed.",
            )

    def _ring_out_alone(self, m: _Mic) -> None:
        self.current = m.index
        m.state = "ringing out"
        for other in self.mics:  # the others muted while this one is rung out
            if other is not m and other.on:
                self._fader(other, on=0)
        self._hpf_on(m)
        start = min(self.ceiling, max(m.base_level, round(RAMP_FLOOR_DB * 100))) - round(RAMP_START_DB * 100)
        self._fader(m, level=start, on=1)
        self.message = f"ringing out {m.label}: raising the fader"
        self.watch.reset()
        self._ramp([m], m.base_level, alone=True)
        self._fader(m, on=0)
        m.state = "done"

    def _ramp(self, mics: list[_Mic], cap: int, alone: bool) -> None:
        """Raise ``mics`` RAMP_DB at a time until the ceiling (``alone``) or ``cap`` above
        their safe levels (together), handling each ring on the way."""
        step = round(RAMP_DB * 100)
        top = {id(m): (self.ceiling if alone else min(self.ceiling, m.level + cap)) for m in mics}
        next_step = self.clock() + RAMP_EVERY_S
        last_ring_level: float | None = None
        while True:
            self._wait(TICK_S)
            ring = self._ring(mics)
            if ring is not None:
                freq, how = ring
                at = {id(m): m.level for m in mics}
                for m in mics:
                    self._fader(m, level=max(NEG_INF + 1, m.level - round(DROP_DB * 100)))
                fell = self._verify(freq)
                self._handled.append((freq, self.clock() + HANDLED_S))
                lead = mics[0] if alone else self._source(mics, freq)
                if not fell:
                    lead.ignored.append(freq)
                    self._log("tone", f"{freq:.0f} Hz on {lead.label} is a steady tone, not feedback: ignored")
                    for m in mics:
                        self._fader(m, level=at[id(m)])
                    self.watch.reset()
                    next_step = self.clock() + RAMP_EVERY_S
                    continue
                ring_db = at[id(lead)] / 100
                self._log("ring", f"{lead.label} rang at {freq:.0f} Hz with the fader at {ring_db:+.1f} dB ({how})")
                if alone:
                    lead.first_ring_db = ring_db if lead.first_ring_db is None else lead.first_ring_db
                    lead.limit_db = ring_db
                else:
                    self.together_db = round((at[id(lead)] - (lead.final or 0)) / 100, 1)
                # A new frequency (not the same resonance beside a notch) ringing about as soon as
                # the last one: the system's limit, more notches would only dull the mic.
                wide = (
                    last_ring_level is not None
                    and abs(ring_db - last_ring_level) <= WIDE_DB
                    and not any(abs(math.log2(freq / n["freq"])) < NEAR_OCT for n in lead.notches.values())
                )
                last_ring_level = ring_db
                notched = self._notch(lead, freq, ring_db)
                if not notched or wide:
                    if wide:
                        lead.note = "rings at several frequencies close together: the system's limit"
                    for m in mics:  # the limit: leave them under it
                        self._fader(m, level=at[id(m)] - round(RESUME_BELOW_DB * 100))
                    return
                self._wait(CLEAR_S)
                for m in mics:
                    self._fader(m, level=at[id(m)] - round(RESUME_BELOW_DB * 100))
                self.watch.reset()
                self._handled.append((freq, self.clock() + HANDLED_S))
                next_step = self.clock() + RAMP_EVERY_S
                continue
            if self.clock() < next_step:
                continue
            if all(m.level >= top[id(m)] for m in mics):
                if alone and not mics[0].note:
                    lead = mics[0]
                    lead.note = (
                        f"no feedback up to {self.ceiling / 100:+.0f} dB"
                        if not lead.rings
                        else f"reached {self.ceiling / 100:+.0f} dB after {len(lead.notches)} notch(es)"
                    )
                    if lead.rings:  # it no longer rings below the ceiling
                        lead.limit_db = self.ceiling / 100
                return
            for m in mics:
                if m.level < top[id(m)]:
                    self._fader(m, level=min(top[id(m)], m.level + step))
            self.message = (
                f"ringing out {mics[0].label}: fader at {mics[0].level / 100:+.0f} dB"
                if alone
                else f"all together: +{(mics[0].level - (mics[0].final or 0)) / 100:.0f} dB over their safe levels"
            )
            next_step = self.clock() + RAMP_EVERY_S

    def _together(self) -> None:
        """All mics open at their safe levels, raised together up to the margin."""
        if len(self.mics) < 2:
            return
        self.current = None
        for m in self.mics:
            m.final = self._safe(m)
        live = [m for m in self.mics if m.final is not None and m.final > NEG_INF]
        if len(live) < 2:
            return
        self.phase = "together"
        self.message = "all together: opening the mics at their safe levels"
        for m in live:
            self._fader(m, level=m.final, on=1)
        self.watch.reset()
        self._wait(1.0)
        self._ramp(live, self.margin, alone=False)
        if self.together_db is not None:  # rang with all open: keep the margin from there
            short = self.margin - round(self.together_db * 100)
            for m in live:
                if m.final is not None and short > 0:
                    m.final -= short
            self._log("together", f"all together they rang {self.together_db:+.1f} dB over their safe levels")

    def _settle(self) -> None:
        """Faders to their safe levels, on / off as before."""
        self.current = None
        for m in self.mics:
            if m.final is None:
                m.final = self._safe(m)
            self._fader(m, level=m.final, on=m.base_on)
            m.state = "done"
        self.phase = "ringing out"  # still pushing until the next phase says otherwise
        self._log(
            "settle",
            "faders set: "
            + ", ".join(
                f"{m.label} {('-inf' if m.final <= NEG_INF else f'{m.final / 100:+.1f}')} dB"
                for m in self.mics
                if m.final is not None
            ),
        )

    def _tone(self) -> None:
        """Live tone EQ on each mic with its notches reserved, until a person finishes."""
        self.phase = "tone"
        self.message = "live tone EQ: have each person talk or sing; press Done when it sounds right"
        for m in self.mics:
            if not any(i == m.index and s.live and s.running for i, s in self.checks.sessions()):
                self.checks.start(m.index, live=True, mic=m.mic, style=m.style)
            self.checks.set_reserved(m.index, self._reserved(m), hpf_floor=m.hpf_floor)
            self.auto.arm(m.index, reserved=set(m.notches))
        self._log("tone", "live tone EQ on")
        while True:  # the fader watch keeps running: a ring now pulls everything down
            self._wait(TICK_S)
            c = self.watch.take()
            if c is not None and c[2] == "runaway":
                for m in self.mics:
                    if m.level > NEG_INF:
                        self._fader(m, level=max(NEG_INF + 1, m.level - round(DROP_DB * 100)))
                self._log("runaway", f"the mains ran away at {c[1]:.0f} Hz: every fader down {DROP_DB:.0f} dB")

    # ------------------------------------------------------------------ rings

    def _ring(self, mics: list[_Mic]) -> tuple[float, str] | None:
        """A ring to deal with now: (freq, how) or None."""
        c = self.watch.take()
        if c is not None:
            return c[1], c[2]
        watched = {m.index for m in mics} | set(self.mains)
        now = self.clock()
        self._handled = [(f, until) for f, until in self._handled if until > now]
        for e in self.engine.rings.snapshot()["events"]:
            if not e["active"] or e["channel"] not in watched:
                continue
            f = float(e["freq"])
            if any(abs(math.log2(f / g)) < SAME_OCT / 2 for m in mics for g in m.ignored):
                continue
            if any(abs(math.log2(f / g)) < SAME_OCT for g, _ in self._handled):
                continue
            if e.get("last_seen") is not None and self.started is not None and e["last_seen"] < self.started:
                continue
            return f, e.get("kind", "ring")
        return None

    def _verify(self, freq: float) -> bool:
        """After the fader drop: did the ring fall clearly further than the fader did, and further
        than everything else did (a talker pausing makes the whole spectrum fall)?"""
        before = self.watch.level_at(freq, within_s=0.4)
        self._wait(VERIFY_S)
        after = self.watch.level_at(freq, within_s=0.3)
        if before is None or after is None:
            return True  # can't tell: treat it as feedback (the safe way)
        fell, broad_fell = before[0] - after[0], before[1] - after[1]
        return fell >= DROP_DB + VERIFY_EXTRA_DB and fell - max(broad_fell, DROP_DB) >= VERIFY_EXTRA_DB

    def _source(self, mics: list[_Mic], freq: float) -> _Mic:
        """With several mics open: the one the ring detector names, else the one that rang
        nearest this frequency alone, else the loudest."""
        by_index = {m.index: m for m in mics}
        for e in self.engine.rings.snapshot()["events"]:
            if e["active"] and abs(math.log2(float(e["freq"]) / freq)) < SAME_OCT:
                for src in e.get("sources") or []:
                    if src["channel"] in by_index:
                        return by_index[src["channel"]]
        near = [(abs(math.log2(r["freq"] / freq)), m) for m in mics for r in m.rings]
        if near:
            return min(near, key=lambda t: t[0])[1]
        return max(mics, key=lambda m: m.level)

    def _notch(self, m: _Mic, freq: float, ring_db: float) -> bool:
        """Take ``freq`` out of ``m``: with the high-pass if it's low enough, else a notch (deeper,
        or wider, if one is on it or beside it already). False when it can't."""
        if self._hpf_ring(m, freq, ring_db):
            return True
        notches = {id(n): dict(n) for n in m.notches.values()}
        near = sorted((abs(math.log2(n["freq"] / freq)), k) for k, n in notches.items())
        near = [t for t in near if t[0] < NEAR_OCT]
        if near:
            dist, k = near[0]
            n = notches[k]
            if n["gain"] - NOTCH_STEP_DB < NOTCH_MAX_DB:
                m.note = f"the ring at {freq:.0f} Hz came back through a {NOTCH_MAX_DB:.0f} dB notch: its limit"
                return False
            n["gain"] -= NOTCH_STEP_DB
            if dist >= SAME_OCT:  # beside the notch: centre it between, wide enough for both
                n["lo"], n["hi"] = min(freq, n["lo"]), max(freq, n["hi"])
                n["freq"] = round(math.sqrt(n["lo"] * n["hi"]), 1)
                n["q"] = round(max(MIN_NOTCH_Q, min(n["q"], n["freq"] / (1.5 * (n["hi"] - n["lo"])))), 2)
            new = n
        else:
            if len(notches) >= MAX_NOTCHES:
                m.note = f"{MAX_NOTCHES} notches used: its limit"
                return False
            new = {
                "freq": round(freq, 1),
                "gain": NOTCH_START_DB,
                "q": MIC_TYPES[m.mic].notch_q,
                "lo": freq,
                "hi": freq,
            }
            notches[id(new)] = new
        # Every band in frequency order: the notches stay, the desk's other bands move along (or,
        # without room, the one doing least gives way).
        cur = dict(m.known_eq)
        keep = [
            {"type": "bell", "freq": n["freq"], "gain": n["gain"], "q": n["q"], "notch": n} for n in notches.values()
        ]
        desk = bands.from_desk(cur, skip=list(m.notches))
        keep += [d for d in desk if d["type"] in ("hpf", "lpf")]
        try:
            placed, dropped = bands.fit([d for d in desk if d["type"] not in ("hpf", "lpf")], keep=keep)
        except ValueError:
            m.note = f"no PEQ band free for {freq:.0f} Hz in frequency order: its limit"
            return False
        self._eq(m, bands.to_desk(placed, cur))
        m.notches = {bands.PATH[label]: s["notch"] for label, s in placed.items() if "notch" in s}
        band = next(label for label, s in placed.items() if s.get("notch") is new)
        for d in dropped:
            self._log(
                "replaced", f"{m.label}: the desk's {d['gain']:+.1f} dB at {d['freq']:.0f} Hz gave way to a notch"
            )
        m.rings.append({"freq": round(freq, 1), "at_db": ring_db, "band": band, "gain": new["gain"]})
        self._log("notch", f"{m.label}: {band} notch {new['gain']:+.0f} dB at {new['freq']:.0f} Hz, Q {new['q']:g}")
        return True

    # ------------------------------------------------------------------ the high-pass and the vocal curve

    def _hpf_on(self, m: _Mic) -> None:
        """The high-pass on, at least at the mic type's floor (a higher one stays)."""
        floor = MIC_TYPES[m.mic].hpf_min_hz
        m.hpf_floor = max(m.hpf_floor, floor)
        cur = m.known_eq
        on, f = cur["PEQ/Bank/HPF/On"], cur["PEQ/Bank/HPF/Frequency"] / 10
        if on and f >= floor:
            m.hpf_floor = max(m.hpf_floor, f)
            return
        self._eq(m, [("PEQ/Bank/HPF/Frequency", round(max(f if on else 0, floor) * 10)), ("PEQ/Bank/HPF/On", 1)])
        self._log("hpf", f"{m.label}: high-pass on at {max(f if on else 0, floor):.0f} Hz ({MIC_TYPES[m.mic].label})")

    def _hpf_ring(self, m: _Mic, freq: float, ring_db: float) -> bool:
        """A low ring: raise the high-pass over it, if the voice leaves room. True if it did."""
        if freq >= HPF_RING_MAX_HZ:
            return False
        cap = HPF_MAX_HZ
        f0_low = self._lowest_pitch(m.index)
        if f0_low is not None:  # not into the voice's own fundamental
            cap = min(cap, f0_low)
        cur = m.known_eq["PEQ/Bank/HPF/Frequency"] / 10
        want = min(cap, 5 * round(freq * HPF_RING_RATIO / 5))
        if want < freq * 1.15 or want <= cur:
            return False  # not enough room above the ring: a notch instead
        self._eq(m, [("PEQ/Bank/HPF/Frequency", round(want * 10)), ("PEQ/Bank/HPF/On", 1)])
        m.hpf_floor = max(m.hpf_floor, want)
        m.rings.append({"freq": round(freq, 1), "at_db": ring_db, "band": "HPF", "gain": None})
        self._log("hpf", f"{m.label}: high-pass up to {want:.0f} Hz for the ring at {freq:.0f} Hz")
        return True

    def _lowest_pitch(self, index: int) -> float | None:
        s = next((s for i, s in self.checks.sessions() if i == index), None)
        f0s = s.collector.f0s if s is not None else []
        return float(np.percentile(f0s, 10)) if len(f0s) >= 20 else None

    @staticmethod
    def _reserved(m: _Mic) -> list[dict[str, Any]]:
        return [
            {"band": bands.LABEL[b], "type": "bell", "freq": n["freq"], "gain": n["gain"], "q": n["q"]}
            for b, n in m.notches.items()
        ]

    def _shape(self) -> None:
        """The vocal curve on the bands the notches left, from the voice heard so far."""
        self.message = "setting each mic's vocal curve"
        for m in self.mics:
            self.checks.set_reserved(m.index, self._reserved(m), hpf_floor=m.hpf_floor)
            result = self.checks.analyze_now(m.index)
            if result is None or not result.get("ok"):
                why = (result or {}).get("error") or "no voice heard"
                self._log("shape", f"{m.label}: no vocal curve yet ({why}); have them talk or sing")
                continue
            eq = self.desk.eq(m.index).get("eq")
            if not eq:
                continue
            changes, lines = eq_plan(result, eq)
            if changes:
                self._eq(m, changes)
            m.shaped = lines
            self._log("shape", f"{m.label}: vocal curve set ({m.mic}, {m.style}): " + "; ".join(lines))

    def _safe(self, m: _Mic) -> int:
        """The level a mic ends at: under its limit by the margin, never above where it was."""
        if m.base_level <= NEG_INF:
            return NEG_INF
        if m.limit_db is None:
            return min(m.base_level, self.ceiling - self.margin)
        return min(m.base_level, round(m.limit_db * 100) - self.margin)

    # ------------------------------------------------------------------ desk access (the loop's only writes)

    def _fader(self, m: _Mic, level: int | None = None, on: int | None = None) -> None:
        if level is not None:
            level = max(NEG_INF, min(level, self.ceiling, 1000))
            if level == m.level:
                level = None
        if on is not None and on == m.on:
            on = None
        if level is None and on is None:
            return
        with self._gate:
            if self._aborted.is_set():
                raise Stop("done", "")
            results = self.desk.write_fader(m.index, level=level, on=on)
        for r in results:
            v = r.get("actual") if r.get("confirmed") else None
            if r["path"].endswith("Fader/Level"):
                m.level = int(v) if isinstance(v, int) else int(r["value"])
            else:
                m.on = int(v) if isinstance(v, int) else int(r["value"])
        if any(not r.get("confirmed") for r in results):
            raise Stop("pullback", f"stopped: the desk didn't confirm a fader move on {m.label}.")
        if level is not None:
            self._rcp_due[m.index] = (self.clock() + RCP_FOLLOW_S, m.level)

    def _eq(self, m: _Mic, changes: list[tuple[str, int]]) -> None:
        with self._gate:
            if self._aborted.is_set():
                raise Stop("done", "")
            results = self.desk.write_eq(m.index, changes)
        if any(not r.get("confirmed") for r in results):
            raise Stop("pullback", f"stopped: the desk didn't confirm an EQ change on {m.label}.")
        raw = self.desk.eq_raw(m.index, PATHS)
        m.known_eq = {p: int(v) for p, v in raw.items() if isinstance(v, int)}

    def _restore(self, m: _Mic, *, on: bool, eq: bool = True) -> None:
        """Fader level (and EQ) back to before the session; ``on``: the on switch too."""
        if eq:
            now = self.desk.eq_raw(m.index, PATHS)
            changes = [(p, v) for p, v in m.base_eq.items() if now.get(p) != v]
            if changes:
                self.desk.write_eq(m.index, changes)
        f = self.desk.fader(m.index)
        level = m.base_level if f["level"] != m.base_level else None
        switch = m.base_on if on and f["on"] != m.base_on else None
        if level is not None or switch is not None:
            self.desk.write_fader(m.index, level=level, on=switch)
            m.level = m.base_level
            if switch is not None:
                m.on = m.base_on

    # ------------------------------------------------------------------ checks, every tick

    def _wait(self, seconds: float) -> None:
        end = self.clock() + seconds
        while True:
            self._check()
            left = end - self.clock()
            if left <= 0:
                return
            self._sleep(min(TICK_S, left))
            if self._aborted.is_set():
                raise Stop("done", "")

    def _check(self) -> None:
        if self._aborted.is_set():
            raise Stop("done", "")
        desk = self.desk
        if not (desk.eq_writable and desk.fader_writable):
            raise Stop("pullback", "stopped: the desk isn't writable any more.")
        pushing = self.phase in ("ringing out", "together")
        if self.clock() - self.heartbeat > HEARTBEAT_S:
            if pushing:
                raise Stop("pullback", "Stopped: the page stopped watching (closed or lost its connection).")
            raise Stop("takeover", "Stopped: the page stopped watching; live tone EQ is off.")
        now = self.clock()
        for m in self.mics:
            f = desk.fader(m.index)
            if f["level"] is None:
                raise Stop("pullback", f"stopped: lost {m.label}'s fader on the desk.")
            if f["level"] != m.level or f["on"] != m.on:
                raise Stop("takeover", f"Stopped: {m.label}'s fader was moved on the desk.")
            due = self._rcp_due.get(m.index)
            if due is not None:
                if f["rcp_level"] == due[1]:
                    del self._rcp_due[m.index]
                elif now > due[0]:
                    raise Stop("pullback", f"stopped: the desk's fader for {m.label} didn't follow (RCP).")
            if pushing:
                raw = desk.eq_raw(m.index, PATHS)
                if any(raw.get(p) != v for p, v in m.known_eq.items()):
                    raise Stop("takeover", f"Stopped: {m.label}'s EQ was changed on the desk.")

    # ------------------------------------------------------------------ helpers

    def _join(self) -> None:
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=5)

    def _finish_phase(self, phase: str, message: str) -> None:
        with self._lock:
            if self.phase in ("aborted",) and phase != "aborted":
                return
            self.phase = phase
            self.message = message
            self.current = None
            for m in self.mics:
                if m.state in ("waiting", "ringing out"):
                    m.state = "cancelled" if phase == "aborted" else "stopped"
        for i in self._held:
            self.engine.release(i)
        self._held = []
        self._log(phase, message)
        log.info("Ring-out %s: %s", phase, message)

    def _log(self, action: str, why: str) -> None:
        self.log.append({"at": time.time(), "action": action, "why": why})
        if action not in ("aborted", "finished", "stopped", "error"):
            log.info("Ring-out: %s", why)

    def close(self) -> None:
        if self.running:
            self.abort()


def _shared_rings(mics: list[_Mic]) -> list[dict[str, Any]]:
    """Frequencies that rang on two or more mics: the system rings there (PA, room, the mics'
    shared capsule peak), so a notch on the stereo bus would serve them all."""
    rings = sorted((r["freq"], m.label) for m in mics for r in m.rings)
    out: list[dict[str, Any]] = []
    for f, label in rings:
        if out and abs(math.log2(f / out[-1]["freq"])) < 1 / 12:
            if label not in out[-1]["mics"]:
                out[-1]["mics"].append(label)
        else:
            out.append({"freq": f, "mics": [label]})
    return [s for s in out if len(s["mics"]) >= 2]
