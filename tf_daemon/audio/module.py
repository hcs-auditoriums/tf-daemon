"""Audio module: wires the AudioEngine into the web app.

Routes:
  GET  /api/audio/meta         channel labels, display frequencies, DSP params
  GET  /api/audio/status       engine / source counters
  POST /api/audio/reset-peak   {"channel": <position>} or {} for all
  WS   /ws/audio               JSON "meta" text message, then binary frames
                               (see engine.py). Client may send
                               {"cmd": "reset_peak", "channel": <position|null>}.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from flask import Blueprint, Flask, jsonify, request
from flask_sock import ConnectionClosed, Sock

from .engine import AudioEngine

log = logging.getLogger(__name__)


class AudioModule:
    name = "audio"

    def __init__(self, engine: AudioEngine) -> None:
        self.engine = engine

    def start(self) -> None:
        self.engine.start()

    def stop(self) -> None:
        self.engine.stop()

    def status(self) -> dict[str, Any]:
        return self.engine.status()

    def register(self, app: Flask, sock: Sock) -> None:
        engine = self.engine
        bp = Blueprint("audio", __name__, url_prefix="/api/audio")

        @bp.get("/meta")
        def meta():
            return jsonify(engine.metadata())

        @bp.get("/status")
        def status():
            return jsonify(engine.status())

        @bp.post("/reset-peak")
        def reset_peak():
            body = request.get_json(silent=True) or {}
            try:
                engine.reset_peak(_channel_arg(body.get("channel")))
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            return jsonify({"ok": True})

        app.register_blueprint(bp)

        @sock.route("/ws/audio")
        def ws_audio(ws):
            ws.send(json.dumps(engine.metadata()))
            seq = 0
            try:
                while True:
                    _handle_client_messages(ws, engine)
                    seq, payload = engine.hub.wait(seq, timeout=1.0)
                    if payload is not None:
                        ws.send(payload)
            except ConnectionClosed:
                pass


def _channel_arg(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid channel {value!r}") from None


def _handle_client_messages(ws, engine: AudioEngine) -> None:
    while True:
        msg = ws.receive(timeout=0)
        if msg is None:
            return
        try:
            data = json.loads(msg)
            if data.get("cmd") == "reset_peak":
                engine.reset_peak(_channel_arg(data.get("channel")))
        except (ValueError, AttributeError) as e:
            log.debug("Ignoring bad client message %r: %s", msg, e)
