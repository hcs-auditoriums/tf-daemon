"""Vocal EQ assist: mic-check sessions that suggest EQ for a vocal mic.

Optional (``vocal.enabled``, off by default). During a mic check the operator
starts a session on an input, the person talks normally for ~20 s, and the
analysis (tone.py) suggests a high-pass and up to four PEQ bands in the TF5's
terms. When the TF5's EQ is readable (``tf5.eq``), the channel's current EQ is
shown alongside for comparison.

Nothing is changed on the console unless a person asks: with ``tf5.eq_write`` on,
"Apply to desk" (POST /api/vocal/apply with ``confirm: true``, sent by the page
after a confirmation dialog) sets the input channel's EQ to the suggestion, and
"Undo" puts back the values it replaced (desk.py). The one exception is auto EQ
(auto.py): only with ``vocal.auto_allowed`` in the config AND a person turning it
on for a channel (``confirm: true``); it stops itself the moment anyone changes
that EQ on the desk.

Routes:
  GET  /api/vocal/status        sessions per input: state, progress, result
  POST /api/vocal/start         {"channel": n, "live": bool}: start (or restart) listening on input n;
                                live keeps listening and re-suggesting, and checks the mains
  POST /api/vocal/stop          {"channel": n}: stop now and analyze what was heard
  POST /api/vocal/clear         {"channel": n}: forget the session
  POST /api/vocal/apply         {"channel": n, "confirm": true}: set the desk EQ to the suggestion
  POST /api/vocal/undo          {"channel": n, "confirm": true}: put back what apply replaced
  POST /api/vocal/auto          {"channel": n, "on": true, "confirm": true}: turn auto EQ on (needs
                                vocal.auto_allowed); {"channel": n, "on": false}: off (no confirm needed)
  POST /api/vocal/auto/undo     {"channel": n, "confirm": true}: off, and the EQ from before it was on
  GET  /api/vocal/profiles      saved EQ profiles: name, when, from which channel, a summary
  POST /api/vocal/profiles/save   {"channel": n, "name": s}: save channel n's EQ as profile s
  POST /api/vocal/profiles/recall {"channel": n, "name": s, "confirm": true}: put profile s on channel n
  POST /api/vocal/profiles/copy   {"channel": n, "from": m, "confirm": true}: copy channel m's EQ to n
  POST /api/vocal/profiles/delete {"name": s}
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
from flask import Blueprint, Flask, jsonify, request
from flask_sock import Sock

from ..config import VocalConfig
from ..dsp.eq import eq_curve
from .auto import PATHS, AutoEQ
from .desk import DeskWriter
from .ports import AudioInputs, DeskEQ
from .profiles import ProfileStore, changes_to, summary
from .session import MicChecks

log = logging.getLogger(__name__)


class VocalModule:
    name = "vocal"

    def __init__(self, cfg: VocalConfig, audio: AudioInputs, samplerate: int, desk: DeskEQ | None = None,
                 mains: tuple[int, ...] = ()) -> None:
        self.engine = audio
        self.checks = MicChecks(audio, samplerate, inputs=set(cfg.inputs) or None, target_s=cfg.listen_s,
                                max_cut_db=cfg.max_cut_db, max_boost_db=cfg.max_boost_db, mains=mains,
                                house_mic=cfg.house_mic if cfg.house_mic >= 0 else None,
                                tilt_db_oct=cfg.room_tilt_db_oct, eq_curve=self._eq_curve)
        self.desk = DeskWriter(desk, self.checks.result)
        self.auto = AutoEQ(self.checks, desk, max_cut_db=cfg.max_cut_db, max_boost_db=cfg.max_boost_db) \
            if cfg.auto_allowed else None
        self.profiles = ProfileStore(cfg.profiles_file)

    # ------------------------------------------------------------------ profiles

    def _raw_eq(self, index: int) -> dict[str, Any]:
        desk = self.desk.desk
        if desk is None or not desk.eq_available or desk.eq_target(index) is None:
            raise ValueError("this input's EQ can't be read from the desk")
        return desk.eq_raw(index, PATHS)

    def save_profile(self, index: int, name: Any) -> dict[str, Any]:
        """Save input ``index``'s EQ as a profile (only reads the desk)."""
        return self.profiles.save(name, self._raw_eq(index), self.engine.inputs[index].label)

    def recall_profile(self, index: int, name: Any) -> dict[str, Any]:
        """SET input ``index``'s EQ to a saved profile. Only from a person's confirmed request."""
        values = self.profiles.get(name)
        return self.desk.write_values(index, changes_to(self._raw_eq(index), values), "recall",
                                      [f"profile {name!r}: {summary(values)}"])

    def copy_eq(self, index: int, source: int) -> dict[str, Any]:
        """SET input ``index``'s EQ to input ``source``'s. Only from a person's confirmed request."""
        if source == index:
            raise ValueError("that's the same channel")
        values = self._raw_eq(source)
        if any(not isinstance(values.get(p), int) for p in PATHS):
            raise ValueError("couldn't read that channel's EQ from the desk")
        label = self.engine.inputs[source].label
        return self.desk.write_values(index, changes_to(self._raw_eq(index), values), "copy",
                                      [f"from {label}: {summary(values)}"])

    def _eq_curve(self, index: int, freqs: np.ndarray) -> np.ndarray | None:
        """The channel EQ on the desk now for ``index``, at ``freqs`` (dB); None if unknown."""
        eq = self.desk.current_eq(index)
        return None if eq is None else eq_curve(eq, freqs)

    def snapshot(self) -> dict[str, Any]:
        sessions = {}
        for index, s in self.checks.sessions():
            entry = {"state": s.state, "live": s.live, "target_s": s.target_s, "started": s.started,
                     "finished": s.finished, "label": self.engine.inputs[index].label, **s.collector.progress()}
            if s.result is not None:
                entry["result"] = s.result
                entry["current_eq"] = self.desk.current_eq(index)
                entry["apply"] = self.desk.apply_state(index)
            sessions[str(index)] = entry
        inputs = self.checks.inputs
        return {"enabled": True, "inputs": sorted(inputs) if inputs is not None else None,
                "house_mic": self.checks.house_mic,
                "listen_s": self.checks.target_s, "sessions": sessions,
                "can_write": self.desk.writable() is not None, **self.desk.history(),
                "auto_allowed": self.auto is not None and self.desk.writable() is not None,
                "auto": self.auto.states() if self.auto is not None else {}}

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

        def session_action(fn):
            try:
                fn(channel_arg())
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            return jsonify({"ok": True})

        @bp.post("/start")
        def start():
            live = (request.get_json(silent=True) or {}).get("live") is True
            return session_action(lambda ch: self.checks.start(ch, live=live))

        @bp.post("/stop")
        def stop():
            return session_action(self.checks.stop)

        @bp.post("/clear")
        def clear():
            return session_action(self.checks.clear)

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
            return desk_write(self.desk.apply_suggestion)

        @bp.post("/undo")
        def undo():
            return desk_write(self.desk.undo)

        @bp.get("/profiles")
        def profiles():
            return jsonify({"profiles": self.profiles.list(), "can_write": self.desk.writable() is not None})

        @bp.post("/profiles/save")
        def profile_save():
            body = request.get_json(silent=True) or {}
            try:
                return jsonify({"ok": True, **self.save_profile(channel_arg(), body.get("name"))})
            except (ValueError, OSError) as e:
                return jsonify({"ok": False, "error": str(e)}), 400

        @bp.post("/profiles/recall")
        def profile_recall():
            name = (request.get_json(silent=True) or {}).get("name")
            return desk_write(lambda ch: self.recall_profile(ch, name))

        @bp.post("/profiles/copy")
        def profile_copy():
            source = (request.get_json(silent=True) or {}).get("from")
            if not isinstance(source, int) or not 0 <= source < len(self.engine.inputs):
                return jsonify({"ok": False, "error": "body needs \"from\": <input index>"}), 400
            return desk_write(lambda ch: self.copy_eq(ch, source))

        @bp.post("/profiles/delete")
        def profile_delete():
            try:
                self.profiles.delete((request.get_json(silent=True) or {}).get("name"))
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            return jsonify({"ok": True})

        @bp.post("/auto")
        def auto():
            if self.auto is None:
                return jsonify({"ok": False, "error": "auto EQ isn't allowed (vocal.auto_allowed)"}), 400
            body = request.get_json(silent=True) or {}
            if body.get("on") is False:   # stopping is always allowed
                try:
                    self.auto.disarm(channel_arg())
                except ValueError as e:
                    return jsonify({"ok": False, "error": str(e)}), 400
                return jsonify({"ok": True})
            return desk_write(self.auto.arm)

        @bp.post("/auto/undo")
        def auto_undo():
            if self.auto is None:
                return jsonify({"ok": False, "error": "auto EQ isn't allowed (vocal.auto_allowed)"}), 400
            return desk_write(self.auto.undo)

        app.register_blueprint(bp)

    def start(self) -> None:
        if self.auto is not None:
            self.auto.start()   # idle until a person turns it on for a channel

    def stop(self) -> None:
        if self.auto is not None:
            self.auto.stop()
        self.checks.close()

    def status(self) -> dict[str, Any]:
        return {"sessions": {str(i): s.state for i, s in self.checks.sessions()}}
