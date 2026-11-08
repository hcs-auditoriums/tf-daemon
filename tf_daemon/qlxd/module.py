"""QLX-D module: read-only monitoring of Shure QLXD4 receivers.

Routes:
  GET  /qlxd                    receivers page
  GET  /api/qlxd/status         counters
  GET  /api/qlxd/state          full state snapshot (same as a /ws/qlxd message)
  GET  /api/qlxd/interfaces     network interfaces to choose from
  POST /api/qlxd/settings       {"interface": "<name>" | "", "hosts": ["ip[:port]", ...]}; persisted
  POST /api/qlxd/rescan         forget found receivers and rediscover
  WS   /ws/qlxd                 JSON state snapshots (see engine.py)
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Flask, jsonify, request
from flask_sock import ConnectionClosed, Sock

from .engine import QLXDEngine


class QLXDModule:
    name = "qlxd"

    def __init__(self, engine: QLXDEngine) -> None:
        self.engine = engine

    def start(self) -> None:
        self.engine.start()

    def stop(self) -> None:
        self.engine.stop()

    def status(self) -> dict[str, Any]:
        return self.engine.status()

    def register(self, app: Flask, sock: Sock) -> None:
        engine = self.engine
        bp = Blueprint("qlxd", __name__, url_prefix="/api/qlxd")

        @app.get("/qlxd")
        def qlxd_page():
            return app.send_static_file("qlxd.html")

        @bp.get("/status")
        def status():
            return jsonify(engine.status())

        @bp.get("/state")
        def state():
            return jsonify(engine.snapshot())

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

        app.register_blueprint(bp)

        @sock.route("/ws/qlxd")
        def ws_qlxd(ws):
            seq = 0  # the hub always holds a snapshot, so the first wait returns at once
            try:
                while True:
                    seq, payload = engine.hub.wait(seq, timeout=1.0)
                    if payload is not None:
                        ws.send(payload)
            except ConnectionClosed:
                pass
