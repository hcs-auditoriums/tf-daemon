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
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import numpy as np

from .ports import AudioInputs
from .tone import analyze
from .transfer import Transfer
from .voice import VoiceCollector

log = logging.getLogger(__name__)

LIVE_MEMORY_S = 60.0     # live: how much recent speech the spectrum and mains check follow
LIVE_EVERY_S = 3.0       # live: re-analyze after this much new speech
LIVE_MIN_S = 8.0         # live: first suggestion after this much speech
MIN_MAINS_COHERENCE = 0.5


class _Session:
    def __init__(self, samplerate: int, target_s: float, live: bool = False) -> None:
        self.live = live
        self.collector = VoiceCollector(samplerate, memory_s=LIVE_MEMORY_S if live else None)
        self.mains = Transfer(samplerate, max_delay_s=0.1, memory_s=LIVE_MEMORY_S) if live else None
        self.target_s = target_s
        self.state = "live" if live else "listening"   # listening | live | done | error
        self.started = time.time()
        self.finished: float | None = None
        self.result: dict[str, Any] | None = None
        self.analyzed_at = 0.0   # speech_s at the last live analysis

    @property
    def running(self) -> bool:
        return self.state in ("listening", "live")


class MicChecks:
    """One session per input. ``inputs`` limits which inputs may be checked (None: any);
    ``mains`` are the inputs carrying the stereo bus (for the live mains check). While a
    session runs, its input (and the mains) are kept analyzed by the engine."""

    def __init__(self, audio: AudioInputs, samplerate: int, *, inputs: set[int] | None, target_s: float,
                 max_cut_db: float, max_boost_db: float, mains: tuple[int, ...] = ()) -> None:
        self.engine = audio
        self.inputs = inputs
        self.target_s = target_s
        self.max_cut_db = max_cut_db
        self.max_boost_db = max_boost_db
        self.mains = tuple(mains)
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
        held = [index, *(self.mains if live else ())]
        for i in held:
            self.engine.acquire(i)  # keep them analyzed even if the page looks elsewhere
        with self._lock:
            self._release_locked(index)
            self._sessions[index] = _Session(self._fs, self.target_s, live)
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
            if s.mains is not None and speech and all(m in frames for m in self.mains) and self.mains:
                y = np.mean([frames[m] for m in self.mains], axis=0)
                s.mains.process(x, y)

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
            with self._lock:
                if self._sessions.get(index) is s and s.state == "live":
                    s.result = result
        return speech

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
        try:
            result = analyze(s.collector, self.rings_for(index), max_cut_db=self.max_cut_db,
                             max_boost_db=self.max_boost_db)
        except Exception as e:  # never break the analysis thread over a suggestion
            log.exception("Vocal analysis failed")
            result = {"ok": False, "error": f"analysis failed: {e}"}
        if s.mains is not None:
            result["mains"] = mains_check(s.mains)
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


def mains_check(t: Transfer) -> dict[str, Any]:
    """Is the voice in the mains? From the mic -> mains transfer over the voice band."""
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
    return {**summary, "verdict": verdict, "note": note}
