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

from flask import Blueprint, jsonify, request

from ..modules import EngineModule
from .devices import RFDeviceError
from .engine import RFEngine


class RFModule(EngineModule):
    name = "rf"

    engine: RFEngine

    def routes(self, bp: Blueprint) -> None:
        engine = self.engine

        @bp.post("/connect")
        def connect():
            try:
                info = engine.connect(by_user=True)
            except RFDeviceError as e:
                return jsonify({"ok": False, "error": str(e)}), 503
            return jsonify({"ok": True, "device": engine.device.name, "info": info})

        @bp.post("/disconnect")
        def disconnect():
            engine.disconnect(by_user=True)
            return jsonify({"ok": True})

        @bp.post("/scan/start")
        def scan_start():
            body = request.get_json(silent=True) or {}
            try:
                settings = engine.start_scan(body, by_user=True)
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            except RFDeviceError as e:
                return jsonify({"ok": False, "error": str(e)}), 409
            return jsonify({"ok": True, "settings": asdict(settings)})

        @bp.post("/scan/stop")
        def scan_stop():
            engine.stop_scan(by_user=True)
            return jsonify({"ok": True})

        @bp.post("/reset-peak")
        def reset_peak():
            engine.reset_peak()
            return jsonify({"ok": True})
