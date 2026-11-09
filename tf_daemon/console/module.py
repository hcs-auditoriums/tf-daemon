"""TF5Module: the console's names, settings and EQ in the app (routes under /api/tf5)."""

from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, Flask, jsonify
from flask_sock import Sock

from ..config import TF5Config
from ..dsp.eq import CURVE_FREQS, eq_curve
from .editor.client import TFEditorClient
from .patch import patch_from, source_label
from .rcp import TF5Client

log = logging.getLogger(__name__)


class TF5Module:
    """Reads the TF5's channel names and settings; feeds names to the audio engine.

    With ``tf5.eq`` it also reads every channel's EQ over the TF Editor protocol
    (console/editor) for the spectrum overlay. With ``tf5.eq_write`` as well, an input
    channel's EQ can be set (``write_eq``), only on a person's request.

    ``simulator``: a simulated console (console.sim.SimConsole, demo mode) that this
    module owns: closed when it stops, its counters reported in its status.

    Routes:
      GET /api/tf5/status     connection state, discovered parameters
      GET /api/tf5/channels   per USB input: console source and its settings; mix names
      GET /api/tf5/eq/<usb>   EQ of the console channel on that USB input + its curve
                              (404 when tf5.eq is off)
    """

    name = "tf5"

    def __init__(self, cfg: TF5Config, audio_engine: Any, simulator: Any = None) -> None:
        t = cfg
        self.simulator = simulator
        if t.username or t.password:
            log.info("tf5.username/password are set, but the TF5's remote protocol has no login; not sent")
        self.patch = patch = patch_from(cfg.usb_patch)
        self.client = TF5Client(t.host, t.port, patch, t.poll_s,
                                audio_engine.set_console_names, audio_engine.set_console_gains)
        # USB channels carrying the stereo bus are mixes, not sources (for ring attribution).
        audio_engine.set_bus_inputs([i for i, src in enumerate(patch) if src is not None and src[0] == "St"])
        self.eq_client: TFEditorClient | None = None
        self.eq_version = 0  # bumped whenever the console's mixing data (EQ included) changes
        if t.eq:
            self.eq_client = TFEditorClient(t.host, t.eq_port, on_change=self._eq_changed, writes=t.eq_write)
        # Open audio pages get desk changes pushed over their /ws/audio connection.
        audio_engine.console = self

    def _eq_changed(self) -> None:
        self.eq_version += 1

    @property
    def eq_available(self) -> bool:
        return self.eq_client is not None

    def snapshot(self) -> tuple[int, dict[str, Any]]:
        return self.client.snapshot()

    def changes_since(self, seq: int) -> tuple[int, dict[str, Any] | None]:
        return self.client.changes_since(seq)

    @property
    def eq_writable(self) -> bool:
        return self.eq_client is not None and self.eq_client.writes

    def eq_target(self, usb: int) -> str | None:
        """Mixing path of the input channel on USB input ``usb`` ("InputChannel:4"); None if
        it isn't an input channel (only input channels' EQ can be written)."""
        src = self.patch[usb] if 0 <= usb < len(self.patch) else None
        return f"InputChannel:{src[1]}" if src is not None and src[0] == "InCh" else None

    def eq_raw(self, usb: int, paths: list[str]) -> dict[str, int | str | None]:
        """Raw values of PEQ parameters (paths relative to the channel) on USB input ``usb``."""
        target, ec = self.eq_target(usb), self.eq_client
        if target is None or ec is None:
            return {p: None for p in paths}
        return {p: ec.get(f"{target}/{p}") for p in paths}

    def write_eq(self, usb: int, changes: list[tuple[str, int]]) -> list[dict[str, Any]]:
        """SET the EQ of the input channel on USB input ``usb``: ``[(path relative to the
        channel, raw value)]``. Only on a person's request (see TFEditorClient.write)."""
        target, ec = self.eq_target(usb), self.eq_client
        if ec is None or not ec.writes:
            raise PermissionError("writing to the console is off (tf5.eq_write = false)")
        if target is None:
            raise ValueError(f"USB {usb + 1} doesn't carry an input channel; only input channel EQ can be set")
        results = ec.write([(f"{target}/{p}", v) for p, v in changes])
        for r in results:
            r["path"] = r["path"].split("/", 1)[1]
        return results

    def eq(self, usb: int) -> dict[str, Any]:
        """EQ of the console channel feeding USB input ``usb`` and its response curve (dB)."""
        ec = self.eq_client
        assert ec is not None
        src = self.patch[usb] if 0 <= usb < len(self.patch) else None
        settings = ec.eq(*src) if src is not None else None
        return {
            "index": usb,
            "source": source_label(src) if src is not None else None,
            "connected": ec.connected,
            "synced": ec.synced,
            "eq": settings,
            "freqs": [round(float(f), 2) for f in CURVE_FREQS] if settings else [],
            "curve": [round(float(v), 2) for v in eq_curve(settings)] if settings else [],
        }

    def register(self, app: Flask, sock: Sock) -> None:
        client = self.client
        bp = Blueprint("tf5", __name__, url_prefix="/api/tf5")

        @bp.get("/status")
        def status():
            return jsonify(self.status())

        @bp.get("/channels")
        def channels():
            return jsonify({"connected": client.connected, "channels": client.channels(),
                            "mixes": client.mixes()})

        @bp.get("/eq/<int:usb>")
        def eq(usb: int):
            if self.eq_client is None:
                return jsonify({"error": "tf5.eq is off"}), 404
            return jsonify(self.eq(usb))

        app.register_blueprint(bp)

    def start(self) -> None:
        self.client.start()
        if self.eq_client is not None:
            self.eq_client.start()

    def stop(self) -> None:
        self.client.stop()
        if self.eq_client is not None:
            self.eq_client.stop()
        if self.simulator is not None:
            self.simulator.close()

    def status(self) -> dict[str, Any]:
        st = self.client.status()
        st["eq"] = self.eq_client.status() if self.eq_client is not None else None
        st["simulator"] = self.simulator.status() if self.simulator is not None else None
        return st
