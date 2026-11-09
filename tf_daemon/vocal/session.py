"""Listening to a vocal mic while the person talks, and the suggestion from what was heard.

Two kinds of session per input:

* a mic check: listen until ``listen_s`` of speech, analyze once, done;
* live: keep listening. The voice spectrum follows the last ~``LIVE_MEMORY_S`` of
  talking and the suggestion is re-analyzed every ``LIVE_EVERY_S`` of new speech,
  so it tracks the person and the room. It also measures the mic against the
  mains (transfer.py): whether the voice is in the mains at all, at what gain,
  and how cleanly (coherence).

Rings count towards a suggestion (as narrow cuts) when they're on this input, or
when this input is the strongest source of a ring on the mains.

With a house mic (``vocal.house_mic``) both kinds also measure the mic against it,
and the suggestion aims at the voice as the room hears it (room.py).

Live, it also learns how close the mic is to feedback (feedback.py): while the
person talks, mic -> mains; while the mic is idle, mains -> mic (the PA leaking
back). Boosts are held to what keeps a safety margin of headroom.

Live, the suggestion is a model that keeps being revised as data comes in: each
analysis uses everything heard so far (weighted to the last minute), measured
through the EQ on the desk now, so once a suggestion is applied the next round
sees its effect and corrects what the prediction got wrong. ``model`` in the
result says how much the suggestion moved since the last revision (settled once
three in a row move less than SETTLED_DB), how far the voice is from the target
now and how far it would be after the suggestion; ``mains`` says whether the
mains follow the desk EQ (if not, something else in the path shapes the sound).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import numpy as np

from .feedback import Feedback, boost_cap
from .ports import AudioInputs
from .room import room_correction
from .tone import POINTS, analyze
from .transfer import Transfer
from .voice import VoiceCollector

log = logging.getLogger(__name__)

LIVE_MEMORY_S = 60.0     # live: how much recent speech the spectrum and mains check follow
LIVE_EVERY_S = 3.0       # live: re-analyze after this much new speech
LIVE_MIN_S = 8.0         # live: first suggestion after this much speech
MIN_MAINS_COHERENCE = 0.5
SETTLED_DB = 1.0         # live: a suggestion that moves less than this, three times running, has settled
MAINS_MISMATCH_DB = 3.0  # mains shape vs desk EQ shape: more than this is worth saying
EQ_CHECK_HOPS = 20       # look at the channel EQ on the desk about once a second
IDLE_AFTER_S = 0.75      # a mic counts as idle (for the feedback leak) this long after its last speech


class _Session:
    def __init__(self, samplerate: int, target_s: float, live: bool = False, house: bool = False) -> None:
        self.live = live
        memory = LIVE_MEMORY_S if live else None
        self.collector = VoiceCollector(samplerate, memory_s=memory)
        self.mains = Transfer(samplerate, max_delay_s=0.1, memory_s=memory) if live else None
        self.feedback = Feedback(samplerate) if live else None
        self.last_speech_t = float("-inf")
        self.house = Transfer(samplerate, max_delay_s=0.25, memory_s=memory) if house else None
        self.target_s = target_s
        self.state = "live" if live else "listening"   # listening | live | done | error
        self.started = time.time()
        self.finished: float | None = None
        self.result: dict[str, Any] | None = None
        self.analyzed_at = 0.0   # speech_s at the last live analysis
        self.revisions = 0
        self.eq_seen: np.ndarray | None = None   # the channel EQ the transfers were measured through
        self.hops = 0
        self._last_curve: np.ndarray | None = None
        self._changes: deque[float] = deque(maxlen=3)

    def revise(self, result: dict[str, Any], eq_now: np.ndarray | None) -> None:
        """Track how the suggestion evolves and what it predicts (adds ``result["model"]``)."""
        if not result.get("ok"):
            return
        # The suggested bands over the analysis range (not the HPF's far-low tail).
        curve = np.asarray(result["after"], dtype=float) - np.asarray(result["deviation"], dtype=float)
        change = None
        if self._last_curve is not None and len(self._last_curve) == len(curve):
            change = float(np.max(np.abs(curve - self._last_curve)))
            self._changes.append(change)
        self._last_curve = curve
        self.revisions += 1
        points = np.asarray(result["points"])
        ref = (points >= 200) & (points <= 6300)
        dev = np.asarray(result["deviation"]) + (eq_now if eq_now is not None else 0.0)
        after = np.asarray(result["after"])
        rms = lambda d: round(float(np.sqrt(np.mean((d[ref] - np.median(d[ref])) ** 2))), 1)   # noqa: E731
        result["model"] = {
            "revision": self.revisions,
            "change_db": None if change is None else round(change, 1),
            "settled": len(self._changes) == 3 and max(self._changes) < SETTLED_DB,
            "off_now_db": rms(dev),           # how far the voice is from the target with the desk EQ now
            "off_after_db": rms(after),       # ... and with the suggestion instead
        }

    @property
    def running(self) -> bool:
        return self.state in ("listening", "live")


class MicChecks:
    """One session per input. ``inputs`` limits which inputs may be checked (None: any);
    ``mains`` are the inputs carrying the stereo bus (for the live mains check). While a
    session runs, its input (and the mains) are kept analyzed by the engine."""

    def __init__(self, audio: AudioInputs, samplerate: int, *, inputs: set[int] | None, target_s: float,
                 max_cut_db: float, max_boost_db: float, mains: tuple[int, ...] = (), house_mic: int | None = None,
                 tilt_db_oct: float = 0.0,
                 eq_curve: Callable[[int, np.ndarray], np.ndarray | None] = lambda index, freqs: None) -> None:
        self.engine = audio
        self.inputs = inputs
        self.target_s = target_s
        self.max_cut_db = max_cut_db
        self.max_boost_db = max_boost_db
        self.mains = tuple(mains)
        self.house_mic = house_mic
        self.tilt_db_oct = tilt_db_oct
        self._eq_curve = eq_curve   # the channel EQ on the desk now, at given freqs (dB)
        self._fs = samplerate
        self._lock = threading.Lock()
        self._sessions: dict[int, _Session] = {}
        self._held: dict[int, list[int]] = {}  # input -> what we asked the engine to analyze for it
        audio.add_frame_tap(self.frames)

    def allowed(self, index: int) -> bool:
        return self.inputs is None or index in self.inputs

    def start(self, index: int, live: bool = False) -> None:
        if not self.allowed(index):
            raise ValueError(f"input {index + 1} isn't listed in vocal.inputs")
        house = self.house_mic if self.house_mic is not None and self.house_mic != index else None
        held = [index, *(self.mains if live else ()), *([house] if house is not None else [])]
        for i in held:
            self.engine.acquire(i)  # keep them analyzed even if the page looks elsewhere
        with self._lock:
            self._release_locked(index)
            self._sessions[index] = _Session(self._fs, self.target_s, live, house is not None)
            self._held[index] = held
        log.info("Vocal %s started on input %d (%s)", "live listening" if live else "mic check", index + 1,
                 self.engine.inputs[index].label)

    def stop(self, index: int) -> None:
        """Stop listening now and keep (a mic check: make) the suggestion from what was heard."""
        with self._lock:
            s = self._sessions.get(index)
            if s is None or not s.running:
                return
        self._finish(index, s)

    def clear(self, index: int) -> None:
        with self._lock:
            self._sessions.pop(index, None)
            self._release_locked(index)

    def close(self) -> None:
        with self._lock:
            for index in list(self._held):
                self._release_locked(index)
            self._sessions.clear()

    def result(self, index: int) -> dict[str, Any] | None:
        """The latest successful suggestion for ``index`` (a finished check or live); else None."""
        with self._lock:
            s = self._sessions.get(index)
        if s is None or s.state not in ("done", "live") or not (s.result or {}).get("ok"):
            return None
        return s.result

    def sessions(self) -> list[tuple[int, _Session]]:
        with self._lock:
            return list(self._sessions.items())

    def _release_locked(self, index: int) -> None:
        for i in self._held.pop(index, []):
            self.engine.release(i)

    # ------------------------------------------------------------------ analysis thread

    def frames(self, frames: dict[int, Any], stream_t: float) -> None:
        for index, s in list(self._sessions.items()):
            x = frames.get(index)
            if x is None or not s.running:
                continue
            speech = self.tap(index, x, stream_t)
            s.hops += 1
            if s.hops % EQ_CHECK_HOPS == 1 and (s.mains is not None or s.house is not None):
                self._follow_eq(index, s)
            mains = (np.mean([frames[m] for m in self.mains], axis=0)
                     if self.mains and all(m in frames for m in self.mains) else None)
            if speech:
                s.last_speech_t = stream_t
            else:
                # The PA leaking back into this mic, once it has been quiet a while: a voice's
                # soft tails correlate with itself at the leak delay and would read as leak.
                if s.feedback is not None and mains is not None and stream_t - s.last_speech_t >= IDLE_AFTER_S:
                    s.feedback.process_idle(mains, x)
                continue
            if s.mains is not None and mains is not None:
                s.mains.process(x, mains)
            if s.house is not None and self.house_mic in frames:
                s.house.process(x, frames[self.house_mic])

    def tap(self, index: int, samples: Any, stream_t: float) -> bool:
        """Feed one hop of ``index``'s samples to its session; True if it was speech."""
        s = self._sessions.get(index)
        if s is None or not s.running:
            return False
        speech = s.collector.process(samples, stream_t)
        if not s.live and s.collector.speech_s >= s.target_s:
            self._finish(index, s)
        elif s.live and s.collector.speech_s >= LIVE_MIN_S and s.collector.speech_s - s.analyzed_at >= LIVE_EVERY_S:
            s.analyzed_at = s.collector.speech_s
            result = self._analyze(index, s)
            s.revise(result, self._eq_curve(index, POINTS))
            with self._lock:
                if self._sessions.get(index) is s and s.state == "live":
                    s.result = result
        return speech

    def _follow_eq(self, index: int, s: _Session) -> None:
        """When the channel EQ changes, the mic -> mains / house paths change: start their
        averages over, so they never mix sound from before and after the move."""
        curve = self._eq_curve(index, POINTS)
        eq = curve if curve is not None else np.zeros(len(POINTS))   # unknown: taken as flat
        if s.eq_seen is not None and not np.allclose(eq, s.eq_seen, atol=0.05):
            for t in (s.mains, s.house):
                if t is not None:
                    t.restart()
        s.eq_seen = eq

    def rings_for(self, index: int) -> list[dict[str, Any]]:
        """Recurring or live rings this input causes: on the input itself, or with it as the
        strongest source of a ring on the mains."""
        out = []
        for e in self.engine.rings.snapshot()["events"]:
            if not (e["active"] or e["count"] >= 2):
                continue
            src = e.get("sources") or []
            if e["channel"] == index or (src and src[0]["channel"] == index):
                out.append({"freq": e["freq"], "count": e["count"]})
        return out

    def _analyze(self, index: int, s: _Session) -> dict[str, Any]:
        room_db, room = None, None
        try:
            if s.house is not None:
                room_db, room = room_correction(s.house, POINTS, self._eq_curve(index, POINTS))
            fb = s.feedback.headroom(s.mains) if s.feedback is not None else None
            result = analyze(s.collector, self.rings_for(index), max_cut_db=self.max_cut_db,
                             max_boost_db=self.max_boost_db, room_db=room_db, tilt_db_oct=self.tilt_db_oct,
                             boost_cap=boost_cap(fb) if fb else None)
            if fb is not None:
                result["feedback"] = fb
        except Exception as e:  # never break the analysis thread over a suggestion
            log.exception("Vocal analysis failed")
            result = {"ok": False, "error": f"analysis failed: {e}"}
        if s.mains is not None:
            result["mains"] = mains_check(s.mains, self._eq_curve(index, POINTS))
        if room is not None:
            result["room"] = room
        return result

    def _finish(self, index: int, s: _Session) -> None:
        result = self._analyze(index, s)
        with self._lock:
            if self._sessions.get(index) is not s:
                return  # restarted or cleared meanwhile
            if s.live and not result.get("ok") and s.result is not None and s.result.get("ok"):
                result = s.result   # stopping live: keep the last good suggestion
            s.result = result
            s.state = "done" if result.get("ok") else "error"
            s.finished = time.time()
            self._release_locked(index)
        log.info("Vocal %s on input %d: %s", "live listening" if s.live else "mic check", index + 1,
                 f"{len(result.get('bands', []))} band(s)" if result.get("ok") else result.get("error"))


def mains_check(t: Transfer, eq_db: np.ndarray | None = None) -> dict[str, Any]:
    """Is the voice in the mains? From the mic -> mains transfer over the voice band. With
    the desk EQ curve (``eq_db`` at POINTS), also whether the mains follow its shape."""
    summary = t.summary()
    coh = summary["coherence"]
    if summary["delay_ms"] is None:
        verdict, note = "unknown", "listening for the voice in the mains…"
    elif coh is None or coh < MIN_MAINS_COHERENCE:
        barely = coh is not None and coh < 0.2
        verdict, note = "weak", ("the voice is barely in the mains (fader down, channel off, or muted?)" if barely
                                 else "the voice is mixed with a lot else in the mains")
    else:
        verdict, note = "ok", f"in the mains at {summary['gain_db']:+.1f} dB re the mic"
        mismatch = _eq_mismatch(t, eq_db)
        summary["eq_mismatch_db"] = mismatch
        if mismatch is not None and mismatch > MAINS_MISMATCH_DB:
            verdict = "differs"
            note += (f"; its tone differs from the desk EQ by up to {mismatch:.0f} dB "
                     "(dynamics, a bus EQ or another channel in the path?)")
    return {**summary, "verdict": verdict, "note": note}


def _eq_mismatch(t: Transfer, eq_db: np.ndarray | None) -> float | None:
    """Largest difference (dB) between the measured mic -> mains shape and the desk EQ's,
    over the voice band where the measurement is clean."""
    if eq_db is None:
        return None
    gain, coh = t.bands(POINTS, 1 / 3)
    good = (coh >= 0.7) & ~np.isnan(gain) & (POINTS >= 200) & (POINTS <= 6300)
    if good.sum() < 6:
        return None
    measured, expected = gain[good], np.asarray(eq_db)[good]
    d = (measured - np.median(measured)) - (expected - np.median(expected))
    return round(float(np.max(np.abs(d))), 1)
