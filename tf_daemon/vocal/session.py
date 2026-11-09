"""Mic checks: listen to an input while the person talks, then analyze what was heard."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .ports import AudioInputs
from .tone import analyze
from .voice import VoiceCollector

log = logging.getLogger(__name__)


class _Session:
    def __init__(self, samplerate: int, target_s: float) -> None:
        self.collector = VoiceCollector(samplerate)
        self.target_s = target_s
        self.state = "listening"   # listening | done | error
        self.started = time.time()
        self.finished: float | None = None
        self.result: dict[str, Any] | None = None


class MicChecks:
    """One session per input: listening until ``target_s`` of speech, then a suggestion.

    ``inputs`` limits which inputs may be checked (None: any). While a session
    listens, its input is kept analyzed by the engine even if no page views it.
    """

    def __init__(self, audio: AudioInputs, samplerate: int, *, inputs: set[int] | None, target_s: float,
                 max_cut_db: float, max_boost_db: float) -> None:
        self.engine = audio
        self.inputs = inputs
        self.target_s = target_s
        self.max_cut_db = max_cut_db
        self.max_boost_db = max_boost_db
        self._fs = samplerate
        self._lock = threading.Lock()
        self._sessions: dict[int, _Session] = {}
        self._held: set[int] = set()  # inputs we asked the engine to analyze
        audio.add_tap(self.tap)

    def allowed(self, index: int) -> bool:
        return self.inputs is None or index in self.inputs

    def start(self, index: int) -> None:
        if not self.allowed(index):
            raise ValueError(f"input {index + 1} isn't listed in vocal.inputs")
        self.engine.acquire(index)  # keep it analyzed even if the page looks elsewhere
        with self._lock:
            self._release_locked(index)
            self._sessions[index] = _Session(self._fs, self.target_s)
            self._held.add(index)
        log.info("Vocal mic check started on input %d (%s)", index + 1, self.engine.inputs[index].label)

    def stop(self, index: int) -> None:
        """Stop listening now and analyze what was heard."""
        with self._lock:
            s = self._sessions.get(index)
            if s is None or s.state != "listening":
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
        """The finished, successful suggestion for ``index``; None if there isn't one."""
        with self._lock:
            s = self._sessions.get(index)
        return s.result if s is not None and s.state == "done" and (s.result or {}).get("ok") else None

    def sessions(self) -> list[tuple[int, _Session]]:
        with self._lock:
            return list(self._sessions.items())

    def _release_locked(self, index: int) -> None:
        if index in self._held:
            self._held.discard(index)
            self.engine.release(index)

    def tap(self, index: int, samples: Any, stream_t: float) -> None:  # analysis thread
        s = self._sessions.get(index)
        if s is None or s.state != "listening":
            return
        s.collector.process(samples, stream_t)
        if s.collector.speech_s >= s.target_s:
            self._finish(index, s)

    def _finish(self, index: int, s: _Session) -> None:
        rings = [{"freq": e["freq"], "count": e["count"]} for e in self.engine.rings.snapshot()["events"]
                 if e["channel"] == index and (e["active"] or e["count"] >= 2)]
        try:
            result = analyze(s.collector, rings, max_cut_db=self.max_cut_db, max_boost_db=self.max_boost_db)
        except Exception as e:  # never break the analysis thread over a suggestion
            log.exception("Vocal analysis failed")
            result = {"ok": False, "error": f"analysis failed: {e}"}
        with self._lock:
            if self._sessions.get(index) is not s:
                return  # restarted or cleared meanwhile
            s.result = result
            s.state = "done" if result.get("ok") else "error"
            s.finished = time.time()
            self._release_locked(index)
        log.info("Vocal mic check on input %d: %s", index + 1,
                 f"{len(result.get('bands', []))} band(s)" if result.get("ok") else result.get("error"))
