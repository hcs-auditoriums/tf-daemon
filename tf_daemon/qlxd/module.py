"""QLX-D module: read-only monitoring of Shure QLXD4 receivers.

Routes:
  GET  /qlxd                    receivers page
  GET  /api/qlxd/status         counters
  GET  /api/qlxd/state          full state snapshot (same as a /ws/qlxd message)
  GET  /api/qlxd/interfaces     network interfaces to choose from
  POST /api/qlxd/settings       {"interface": "<name>" | "", "hosts": ["ip[:port]", ...]}; persisted
  POST /api/qlxd/rescan         forget found receivers and rediscover
  GET  /api/qlxd/frequencies    known receivers' name / device ID / frequency, for other
                                pages (the /rf graph); includes offline ones
  POST /api/qlxd/known/forget   {"key": "<mac or host:port>"} or {} for all: drop from that list
  WS   /ws/qlxd                 JSON state snapshots (see engine.py)
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..modules import EngineModule
from .engine import QLXDEngine


class QLXDModule(EngineModule):
    name = "qlxd"

    engine: QLXDEngine

    def routes(self, bp: Blueprint) -> None:
        engine = self.engine

        @bp.get("/interfaces")
        def interfaces():
            return jsonify(engine.interfaces())

        @bp.post("/settings")
        def settings():
            body = request.get_json(silent=True)
            if not isinstance(body, dict):
                return jsonify({"ok": False, "error": "expected a JSON object"}), 400
            try:
                saved = engine.update_settings(body)
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            return jsonify({"ok": True, "settings": saved})

        @bp.post("/rescan")
        def rescan():
            engine.rescan()
            return jsonify({"ok": True})

        @bp.get("/frequencies")
        def frequencies():
            return jsonify({"receivers": engine.frequencies()})

        @bp.post("/known/forget")
        def forget_known():
            body = request.get_json(silent=True) or {}
            key = body.get("key")
            if key is not None and not isinstance(key, str):
                return jsonify({"ok": False, "error": "key must be a string"}), 400
            return jsonify({"ok": True, "removed": engine.forget_known(key)})
