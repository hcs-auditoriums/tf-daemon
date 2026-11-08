"""Vocal EQ assist: mic-check sessions that suggest EQ for a vocal mic.

Optional (``vocal.enabled``, off by default). During a mic check the operator
starts a session on an input, the person talks normally for ~20 s, and the
analysis (dsp/vocal.py) suggests a high-pass and up to four PEQ bands in the
TF5's terms. Suggestions only: this module never changes anything on the
console. When the TF5's EQ is readable (``tf5.eq``), the channel's current EQ is
shown alongside for comparison.

Routes:
  GET  /api/vocal/status        sessions per input: state, progress, result
  POST /api/vocal/start         {"channel": n}: start (or restart) listening on input n
  POST /api/vocal/stop          {"channel": n}: stop now and analyze what was heard
  POST /api/vocal/clear         {"channel": n}: forget the session
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from flask import Blueprint, Flask, jsonify, request
from flask_sock import Sock

from .dsp.vocal import VoiceCollector, analyze

log = logging.getLogger(__name__)


class _Session:
    def __init__(self, samplerate: int, target_s: float) -> None:
        self.collector = VoiceCollector(samplerate)
        self.target_s = target_s
        self.state = "listening"   # listening | done | error
        self.started = time.time()
        self.finished: float | None = None
        self.result: dict[str, Any] | None = None


class VocalModule:
    name = "vocal"

    def __init__(self, cfg: dict[str, Any], audio_engine: Any) -> None:
        v = cfg["vocal"]
        self.engine = audio_engine
        self.inputs = set(int(i) for i in v["inputs"]) or None  # None: any input
        self.target_s = float(v["listen_s"])
        self.max_cut_db = float(v["max_cut_db"])
        self.max_boost_db = float(v["max_boost_db"])
        self._fs = int(cfg["audio"]["samplerate"])
        self._lock = threading.Lock()
        self._sessions: dict[int, _Session] = {}
        self._held: set[int] = set()  # inputs we asked the engine to analyze
        audio_engine.add_tap(self._tap)

    # ------------------------------------------------------------------ sessions

    def allowed(self, index: int) -> bool:
        return self.inputs is None or index in self.inputs

    def start_session(self, index: int) -> None:
        if not self.allowed(index):
            raise ValueError(f"input {index + 1} isn't listed in vocal.inputs")
        self.engine.acquire(index)  # keep it analyzed even if the page looks elsewhere
        with self._lock:
            self._release_locked(index)
            self._sessions[index] = _Session(self._fs, self.target_s)
            self._held.add(index)
        log.info("Vocal mic check started on input %d (%s)", index + 1, self.engine.inputs[index].label)

    def stop_session(self, index: int) -> None:
        """Stop listening now and analyze what was heard."""
        with self._lock:
            s = self._sessions.get(index)
            if s is None or s.state != "listening":
                return
        self._finish(index, s)

    def clear_session(self, index: int) -> None:
        with self._lock:
            self._sessions.pop(index, None)
            self._release_locked(index)

    def _release_locked(self, index: int) -> None:
        if index in self._held:
            self._held.discard(index)
            self.engine.release(index)

    def _tap(self, index: int, samples: Any, stream_t: float) -> None:  # analysis thread
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

    # ------------------------------------------------------------------ status

    def _current_eq(self, index: int) -> dict[str, Any] | None:
        console = getattr(self.engine, "console", None)
        if console is None or not getattr(console, "eq_available", False):
            return None
        try:
            return console.eq(index).get("eq")
        except Exception:
            return None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            items = list(self._sessions.items())
        sessions = {}
        for index, s in items:
            entry = {"state": s.state, "target_s": s.target_s, "started": s.started, "finished": s.finished,
                     "label": self.engine.inputs[index].label, **s.collector.progress()}
            if s.result is not None:
                entry["result"] = s.result
                entry["current_eq"] = self._current_eq(index)
            sessions[str(index)] = entry
        return {"enabled": True, "inputs": sorted(self.inputs) if self.inputs is not None else None,
                "listen_s": self.target_s, "sessions": sessions}

    # ------------------------------------------------------------------ module plumbing

    def register(self, app: Flask, sock: Sock) -> None:
        bp = Blueprint("vocal", __name__, url_prefix="/api/vocal")

        def channel_arg() -> int:
            ch = (request.get_json(silent=True) or {}).get("channel")
            if not isinstance(ch, int) or not 0 <= ch < len(self.engine.inputs):
                raise ValueError("body must be {\"channel\": <input index>}")
            return ch

        @bp.get("/status")
        def status():
            return jsonify(self.snapshot())

        @bp.post("/start")
        def start():
            try:
                self.start_session(channel_arg())
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            return jsonify({"ok": True})

        @bp.post("/stop")
        def stop():
            try:
                self.stop_session(channel_arg())
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            return jsonify({"ok": True})

        @bp.post("/clear")
        def clear():
            try:
                self.clear_session(channel_arg())
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            return jsonify({"ok": True})

        app.register_blueprint(bp)

    def start(self) -> None:
        pass  # nothing runs until a mic check begins

    def stop(self) -> None:
        with self._lock:
            for index in list(self._held):
                self._release_locked(index)
            self._sessions.clear()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"sessions": {str(i): s.state for i, s in self._sessions.items()}}
