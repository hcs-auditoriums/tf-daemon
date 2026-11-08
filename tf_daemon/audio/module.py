"""Audio module: wires the AudioEngine into the web app.

Routes:
  GET  /api/audio/meta         channel labels, display frequencies, DSP params
  GET  /api/audio/status       engine / source counters
  POST /api/audio/reset-peak   {"channel": <input index>} or {} for all
  GET  /api/audio/rings        ring events (see dsp/ring.py RingLog.snapshot)
  POST /api/audio/rings/clear  forget past (not live) ring events
  POST /api/audio/rings/ignore {"freq": hz}: stop reporting rings near hz
  POST /api/audio/rings/unignore {"freq": hz} or {} for all
  WS   /ws/audio               JSON "meta" text message, then binary frames
                               (see engine.py). Client may send
                               {"cmd": "view", "channel": <input index>} to have
                               that input analyzed while it stays connected, and
                               {"cmd": "reset_peak", "channel": <input index|null>}.
                               Ring events arrive as {"type": "rings", ...} text
                               messages whenever they change.
"""

from __future__ import annotations

import json
import logging
import time
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

        @bp.get("/rings")
        def rings():
            return jsonify(engine.rings.snapshot())

        @bp.post("/rings/clear")
        def rings_clear():
            engine.rings.clear()
            return jsonify({"ok": True})

        @bp.post("/rings/ignore")
        def rings_ignore():
            freq = (request.get_json(silent=True) or {}).get("freq")
            if not isinstance(freq, (int, float)) or not 20 <= freq <= 20000:
                return jsonify({"error": "body must be {\"freq\": <Hz>}"}), 400
            engine.rings.ignore(float(freq))
            return jsonify({"ok": True})

        @bp.post("/rings/unignore")
        def rings_unignore():
            freq = (request.get_json(silent=True) or {}).get("freq")
            engine.rings.unignore(float(freq) if isinstance(freq, (int, float)) else None)
            return jsonify({"ok": True})

        app.register_blueprint(bp)

        @sock.route("/ws/audio")
        def ws_audio(ws):
            meta_version = engine.meta_version
            ws.send(json.dumps(engine.metadata()))
            rings_version, rings_sent = None, 0.0
            seq = 0
            view: list[int] = []  # the input this client views, if any (a list so the handler can swap it)
            try:
                while True:
                    _handle_client_messages(ws, engine, view)
                    if engine.meta_version != meta_version:  # e.g. channel names read from the console
                        meta_version = engine.meta_version
                        ws.send(json.dumps(engine.metadata()))
                    # Ring events when they change; live levels refresh every 2 s at most.
                    now = time.monotonic()
                    if engine.rings.version != rings_version or now - rings_sent >= 2.0:
                        rings_version, rings_sent = engine.rings.version, now
                        ws.send(json.dumps(engine.rings.snapshot()))
                    seq, payload = engine.hub.wait(seq, timeout=1.0)
                    if payload is not None:
                        ws.send(payload)
            except ConnectionClosed:
                pass
            finally:
                for index in view:
                    engine.release(index)


def _channel_arg(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid channel {value!r}") from None


def _handle_client_messages(ws, engine: AudioEngine, view: list[int]) -> None:
    while True:
        msg = ws.receive(timeout=0)
        if msg is None:
            return
        try:
            data = json.loads(msg)
            cmd = data.get("cmd")
            if cmd == "reset_peak":
                engine.reset_peak(_channel_arg(data.get("channel")))
            elif cmd == "view":
                index = _channel_arg(data.get("channel"))
                if index is None:
                    raise ValueError("view needs a channel")
                engine.acquire(index)
                for old in view:
                    engine.release(old)
                view[:] = [index]
        except (ValueError, AttributeError) as e:
            log.debug("Ignoring bad client message %r: %s", msg, e)
