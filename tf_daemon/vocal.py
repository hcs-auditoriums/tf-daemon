"""Vocal EQ assist: mic-check sessions that suggest EQ for a vocal mic.

Optional (``vocal.enabled``, off by default). During a mic check the operator
starts a session on an input, the person talks normally for ~20 s, and the
analysis (dsp/vocal.py) suggests a high-pass and up to four PEQ bands in the
TF5's terms. When the TF5's EQ is readable (``tf5.eq``), the channel's current EQ
is shown alongside for comparison.

Nothing is changed on the console unless a person asks: with ``tf5.eq_write`` on,
"Apply to desk" (POST /api/vocal/apply with ``confirm: true``, sent by the page
after a confirmation dialog) sets the input channel's EQ to the suggestion, and
"Undo" puts back the values it replaced. Nothing here writes on its own.

Routes:
  GET  /api/vocal/status        sessions per input: state, progress, result
  POST /api/vocal/start         {"channel": n}: start (or restart) listening on input n
  POST /api/vocal/stop          {"channel": n}: stop now and analyze what was heard
  POST /api/vocal/clear         {"channel": n}: forget the session
  POST /api/vocal/apply         {"channel": n, "confirm": true}: set the desk EQ to the suggestion
  POST /api/vocal/undo          {"channel": n, "confirm": true}: put back what apply replaced
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


def _band_path(label: str) -> str:
    """Suggestion / console band label -> PEQ path relative to the channel."""
    if label in ("L", "H"):
        return f"PEQ/Bank/Band{label}"
    return f"PEQ/Bank/Band:{int(label) - 1}"


def eq_plan(result: dict[str, Any], current: dict[str, Any]) -> tuple[list[tuple[str, int]], list[str]]:
    """The desk changes that make the channel's EQ the suggestion, as (path, raw value), and a
    line per change for the person confirming. Suggested bands become bells with the
    suggested settings; the other bands are set flat (0 dB) so the desk matches the
    suggested curve; a low / high band used as a filter (HPF / LPF) is left alone."""
    changes: list[tuple[str, int]] = []
    lines: list[str] = []
    if not current["on"]:
        changes.append(("PEQ/On", 1))
        lines.append("EQ: on")
    hpf = result.get("hpf")
    if hpf and current.get("hpf") is not None:
        changes += [("PEQ/Bank/HPF/On", 1), ("PEQ/Bank/HPF/Frequency", round(hpf["freq"] * 10))]
        lines.append(f"HPF: on, {hpf['freq']:g} Hz")
    suggested = {b["band"]: b for b in result.get("bands", [])}
    labels = {b["label"] for b in current["bands"]}
    if set(suggested) - labels:
        raise ValueError(f"the desk channel has no band {', '.join(sorted(set(suggested) - labels))}")
    for b in current["bands"]:
        label, base, m = b["label"], _band_path(b["label"]), suggested.get(b["label"])
        if m is not None:
            if label in ("L", "H"):
                changes.append((f"{base}/Type", 0))   # bell
            q = min(max(float(m["q"]), 0.1), 10.0)
            changes += [(f"{base}/Frequency", round(m["freq"] * 10)), (f"{base}/Gain", round(m["gain"] * 10) * 10),
                        (f"{base}/Q", round(q * 1000))]
            lines.append(f"{label}: bell {m['gain']:+.1f} dB @ {m['freq']:g} Hz, Q {q:g}")
        elif b["type"] in ("hpf", "lpf"):
            lines.append(f"{label}: {b['type'].upper()} left as it is")
        elif b["gain"] != 0:
            changes.append((f"{base}/Gain", 0))
            lines.append(f"{label}: flat (was {b['gain']:+.1f} dB)")
    return changes, lines


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
        self._apply_lock = threading.Lock()                    # one desk write at a time
        self._undo: dict[int, list[tuple[str, int]]] = {}      # input -> desk values before the first apply
        self._applied: dict[int, dict[str, Any]] = {}          # input -> outcome of the last apply / undo
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

    # ------------------------------------------------------------------ apply to the desk (on request only)

    def _console(self) -> Any:
        return getattr(self.engine, "console", None)

    def apply_blocker(self, index: int) -> str | None:
        """Why the suggestion can't be applied to the desk now; None if it can."""
        console = self._console()
        if console is None or not getattr(console, "eq_writable", False):
            return "writing to the desk is off (tf5.eq_write)"
        with self._lock:
            s = self._sessions.get(index)
        if s is None or s.state != "done" or not (s.result or {}).get("ok"):
            return "no suggestion yet"
        if console.eq_target(index) is None:
            return "this input isn't an input channel on the desk"
        cur = self._current_eq(index)
        if cur is None:
            return "the desk's EQ for this channel hasn't been read yet"
        if cur.get("one_knob"):
            return "1-knob EQ is on for this channel; turn it off on the desk first"
        return None

    def plan(self, index: int) -> tuple[list[tuple[str, int]], list[str]]:
        reason = self.apply_blocker(index)
        if reason:
            raise ValueError(reason)
        with self._lock:
            result = self._sessions[index].result
        cur = self._current_eq(index)
        assert result is not None and cur is not None
        return eq_plan(result, cur)

    def apply_suggestion(self, index: int) -> dict[str, Any]:
        """SET THE DESK EQ to the suggestion. Only from a person's confirmed request."""
        console = self._console()
        with self._apply_lock:
            changes, lines = self.plan(index)
            if not changes:
                return self._record(index, "apply", [], lines)
            before = console.eq_raw(index, [p for p, _ in changes])
            if any(not isinstance(v, int) for v in before.values()):
                raise ValueError("couldn't read the current desk values to keep for Undo")
            results = console.write_eq(index, changes)
            self._undo.setdefault(index, [(p, before[p]) for p, _ in changes])   # keep the original
            out = self._record(index, "apply", results, lines)
        log.info("Vocal assist: applied the suggested EQ to input %d on request (%d/%d confirmed by the desk)",
                 index + 1, out["confirmed"], out["sent"])
        return out

    def undo(self, index: int) -> dict[str, Any]:
        """Put back the desk values the first apply replaced. Only on a person's request."""
        console = self._console()
        with self._apply_lock:
            changes = self._undo.get(index)
            if not changes:
                raise ValueError("nothing to undo")
            if console is None or not getattr(console, "eq_writable", False):
                raise PermissionError("writing to the desk is off (tf5.eq_write)")
            results = console.write_eq(index, changes)
            del self._undo[index]
            out = self._record(index, "undo", results, [f"{p.removeprefix('PEQ/')} = {v}" for p, v in changes])
        log.info("Vocal assist: undid the applied EQ on input %d on request (%d/%d confirmed by the desk)",
                 index + 1, out["confirmed"], out["sent"])
        return out

    def _record(self, index: int, action: str, results: list[dict[str, Any]], lines: list[str]) -> dict[str, Any]:
        out = {"action": action, "at": time.time(), "sent": len(results),
               "confirmed": sum(1 for r in results if r["confirmed"]), "results": results, "changes": lines}
        self._applied[index] = out
        return out

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
                entry["apply"] = self._apply_state(index)
            sessions[str(index)] = entry
        console = self._console()
        return {"enabled": True, "inputs": sorted(self.inputs) if self.inputs is not None else None,
                "listen_s": self.target_s, "sessions": sessions,
                "can_write": bool(console is not None and getattr(console, "eq_writable", False)),
                "undo": {str(i): True for i in self._undo}, "applied": {str(i): a for i, a in self._applied.items()}}

    def _apply_state(self, index: int) -> dict[str, Any]:
        reason = self.apply_blocker(index)
        state: dict[str, Any] = {"possible": reason is None, "reason": reason, "changes": []}
        if reason is None:
            try:
                state["changes"] = self.plan(index)[1]
            except ValueError as e:
                state.update(possible=False, reason=str(e))
        return state

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

        def desk_write(fn):
            # Changes the desk: only with the page's explicit confirmation in the request.
            if (request.get_json(silent=True) or {}).get("confirm") is not True:
                return jsonify({"ok": False, "error": "changing the desk needs \"confirm\": true"}), 400
            try:
                return jsonify({"ok": True, **fn(channel_arg())})
            except (ValueError, PermissionError) as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            except (ConnectionError, OSError) as e:
                return jsonify({"ok": False, "error": f"desk not reachable: {e}"}), 503
            except Exception as e:   # e.g. the client refusing a frame: report, don't crash the request
                log.exception("Writing the vocal EQ to the desk failed")
                return jsonify({"ok": False, "error": f"not applied: {e}"}), 500

        @bp.post("/apply")
        def apply():
            return desk_write(self.apply_suggestion)

        @bp.post("/undo")
        def undo():
            return desk_write(self.undo)

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
