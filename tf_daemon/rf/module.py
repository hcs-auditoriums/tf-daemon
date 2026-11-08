"""RF module: wires the RFEngine (tinySA) into the web app.

Routes:
  GET  /rf                     RF spectrum page
  GET  /api/rf/status          connection / sweep counters
  GET  /api/rf/state           full state snapshot (same as a /ws/rf message)
  POST /api/rf/connect         open the device
  POST /api/rf/disconnect      stop sweeping and close the device
  POST /api/rf/scan/start      body: any of {start_hz, stop_hz, points, rbw, interval_s}
  POST /api/rf/scan/stop
  POST /api/rf/reset-peak
  WS   /ws/rf                  JSON state snapshots (see engine.py)
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from flask import Blueprint, Flask, jsonify, request
from flask_sock import ConnectionClosed, Sock

from .devices import RFDeviceError
from .engine import RFEngine


class RFModule:
    name = "rf"

    def __init__(self, engine: RFEngine) -> None:
        self.engine = engine

    def start(self) -> None:
        pass  # the device is opened on demand via /api/rf/connect

    def stop(self) -> None:
        self.engine.stop()

    def status(self) -> dict[str, Any]:
        return self.engine.status()

    def register(self, app: Flask, sock: Sock) -> None:
        engine = self.engine
        bp = Blueprint("rf", __name__, url_prefix="/api/rf")

        @app.get("/rf")
        def rf_page():
            return app.send_static_file("rf.html")

        @bp.get("/status")
        def status():
            return jsonify(engine.status())

        @bp.get("/state")
        def state():
            return jsonify(engine.snapshot())

        @bp.post("/connect")
        def connect():
            try:
                info = engine.connect()
            except RFDeviceError as e:
                return jsonify({"ok": False, "error": str(e)}), 503
            return jsonify({"ok": True, "device": engine.device.name, "info": info})

        @bp.post("/disconnect")
        def disconnect():
            engine.disconnect()
            return jsonify({"ok": True})

        @bp.post("/scan/start")
        def scan_start():
            body = request.get_json(silent=True) or {}
            try:
                settings = engine.start_scan(body)
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            except RFDeviceError as e:
                return jsonify({"ok": False, "error": str(e)}), 409
            return jsonify({"ok": True, "settings": asdict(settings)})

        @bp.post("/scan/stop")
        def scan_stop():
            engine.stop_scan()
            return jsonify({"ok": True})

        @bp.post("/reset-peak")
        def reset_peak():
            engine.reset_peak()
            return jsonify({"ok": True})

        app.register_blueprint(bp)

        @sock.route("/ws/rf")
        def ws_rf(ws):
            seq = 0  # the hub always holds a snapshot, so the first wait returns at once
            try:
                while True:
                    seq, payload = engine.hub.wait(seq, timeout=1.0)
                    if payload is not None:
                        ws.send(payload)
            except ConnectionClosed:
                pass
