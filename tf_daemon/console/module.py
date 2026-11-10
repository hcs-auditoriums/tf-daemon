"""TF5Module: the console's names, settings and EQ in the app (routes under /api/tf5)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from flask import Blueprint, Flask, jsonify, request
from flask_sock import Sock

from ..config import TF5Config
from ..dsp.eq import CURVE_FREQS, eq_curve
from .editor.client import TFEditorClient
from .editor.guard import WRITABLE
from .patch import patch_from, source_label
from .rcp import TF5Client

if TYPE_CHECKING:
    from .sim import SimConsole

log = logging.getLogger(__name__)


class TF5Module:
    """Reads the TF5's channel names and settings, and reports them through
    ``on_names`` / ``on_gains`` (per USB input; app.build_modules connects them to
    the audio engine).

    With ``tf5.eq`` it also reads every channel's EQ over the TF Editor protocol
    (console/editor) for the spectrum overlay. With ``tf5.eq_write`` as well, an input
    channel's EQ can be set (``write_eq``), only on a person's request; with
    ``tf5.fader_write`` too, its fader level and on switch (``write_fader``, for the
    vocal assist's ring-out session).

    ``simulator``: a simulated console (console.sim.SimConsole, demo mode) that this
    module owns: closed when it stops, its counters reported in its status.

    Routes:
      GET /api/tf5/status     connection state, discovered parameters
      GET /api/tf5/channels   per USB input: console source and its settings; mix names
      GET /api/tf5/eq/<usb>   EQ of the console channel on that USB input + its curve
                              (404 when tf5.eq is off)
      PUT /api/tf5/eq/<usb>   {"changes": [[path, raw value], ...], "confirm": true}: SET that channel's EQ
                              (the page's EQ editing: a person dragging a band or typing a value; needs
                              tf5.eq_write; paths relative to the channel, e.g. "PEQ/Bank/Band:0/Gain")
      PUT /api/tf5/fader/<usb> {"level": raw | omitted, "on": 0 | 1 | omitted, "confirm": true}: SET that
                              input channel's fader (dB x 100, -32768 = -inf) and / or on switch (the
                              sidebar's fader and ON button: a person's drag or click; needs tf5.fader_write)
    """

    name = "tf5"

    def __init__(
        self,
        cfg: TF5Config,
        on_names: Callable[[dict[int, str]], None],
        on_gains: Callable[[dict[int, float | None]], None] | None = None,
        simulator: SimConsole | None = None,
    ) -> None:
        self.simulator = simulator
        if cfg.username or cfg.password:
            log.info("tf5.username/password are set, but the TF5's remote protocol has no login; not sent")
        self.patch = patch = patch_from(cfg.usb_patch)
        self.client = TF5Client(cfg.host, cfg.port, patch, cfg.poll_s, on_names, on_gains)
        self.eq_client: TFEditorClient | None = None
        self.eq_version = 0  # bumped whenever the console's mixing data (EQ included) changes
        if cfg.eq:
            self.eq_client = TFEditorClient(
                cfg.host, cfg.eq_port, on_change=self._eq_changed, writes=cfg.eq_write, fader_writes=cfg.fader_write
            )

    def bus_inputs(self) -> list[int]:
        """USB inputs carrying the stereo bus: mixes, not sources (for ring attribution)."""
        return [i for i, src in enumerate(self.patch) if src is not None and src[0] == "St"]

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
        for p, _ in changes:  # EQ only: faders go through write_fader (ring-out), never through here
            if not WRITABLE.fullmatch(f"{target}/{p}"):
                raise ValueError(f"{p}: not a writable EQ parameter")
        results = ec.write([(f"{target}/{p}", v) for p, v in changes])
        for r in results:
            r["path"] = r["path"].split("/", 1)[1]
        return results

    @property
    def fader_writable(self) -> bool:
        return self.eq_client is not None and self.eq_client.fader_writes

    def fader(self, usb: int) -> dict[str, Any]:
        """The fader of the input channel on USB input ``usb``, raw (dB x 100, -32768 = -inf;
        on 0/1), as the editor protocol and as RCP report it. Each is None when unknown."""
        target, ec = self.eq_target(usb), self.eq_client
        level = ec.get(f"{target}/Fader/Level") if target and ec else None
        on = ec.get(f"{target}/Fader/On") if target and ec else None
        rcp = self.client.fader(usb) if target else None
        return {
            "level": level if isinstance(level, int) else None,
            "on": on if isinstance(on, int) else None,
            "rcp_level": rcp[0] if rcp else None,
            "rcp_on": rcp[1] if rcp else None,
        }

    def write_fader(self, usb: int, level: int | None = None, on: int | None = None) -> list[dict[str, Any]]:
        """SET the fader level (raw) and / or on switch of the input channel on USB input
        ``usb``. Only for a person's drag / click in the sidebar, or a ring-out a person
        started (see TFEditorClient.write)."""
        target, ec = self.eq_target(usb), self.eq_client
        if ec is None or not ec.fader_writes:
            raise PermissionError("moving faders is off (tf5.fader_write = false)")
        if target is None:
            raise ValueError(f"USB {usb + 1} doesn't carry an input channel; only input faders can be set")
        # Off before the level (muting must not wait); on only after it (unmute at the new level).
        changes = [(f"{target}/Fader/On", on)] if on == 0 else []
        if level is not None:
            changes.append((f"{target}/Fader/Level", level))
        if on is not None and on != 0:
            changes.append((f"{target}/Fader/On", on))
        results = ec.write(changes)
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
            "writable": ec.writes and src is not None and src[0] == "InCh",  # the page may offer editing
            "fader_writable": ec.fader_writes and src is not None and src[0] == "InCh",
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
            return jsonify({"connected": client.connected, "channels": client.channels(), "mixes": client.mixes()})

        @bp.get("/eq/<int:usb>")
        def eq(usb: int):
            if self.eq_client is None:
                return jsonify({"error": "tf5.eq is off"}), 404
            return jsonify(self.eq(usb))

        @bp.put("/eq/<int:usb>")
        def eq_set(usb: int):
            # Changes the desk: only a person's edit (the page sends confirm with each drag / entry).
            body = request.get_json(silent=True) or {}
            if body.get("confirm") is not True:
                return jsonify({"ok": False, "error": 'changing the desk needs "confirm": true'}), 400
            changes = body.get("changes")
            if (
                not isinstance(changes, list)
                or not changes
                or not all(
                    isinstance(c, list)
                    and len(c) == 2
                    and isinstance(c[0], str)
                    and isinstance(c[1], int)
                    and not isinstance(c[1], bool)
                    for c in changes
                )
            ):
                return jsonify({"ok": False, "error": 'body needs "changes": [[path, raw value], ...]'}), 400
            try:
                results = self.write_eq(usb, [(c[0], c[1]) for c in changes])
            except (ValueError, PermissionError) as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            except (ConnectionError, OSError) as e:
                return jsonify({"ok": False, "error": f"desk not reachable: {e}"}), 503
            log.info("EQ edited from the page on USB %d: %s", usb + 1, ", ".join(f"{p}={v}" for p, v in changes))
            return jsonify({"ok": True, "results": results})

        @bp.put("/fader/<int:usb>")
        def fader_set(usb: int):
            # Changes the desk: only a person's drag / click (the page sends confirm with each).
            body = request.get_json(silent=True) or {}
            if body.get("confirm") is not True:
                return jsonify({"ok": False, "error": 'changing the desk needs "confirm": true'}), 400
            level, on = body.get("level"), body.get("on")
            ok_int = lambda v: v is None or (isinstance(v, int) and not isinstance(v, bool))  # noqa: E731
            if not ok_int(level) or on not in (None, 0, 1) or isinstance(on, bool) or (level is None and on is None):
                return jsonify({"ok": False, "error": 'body needs "level": <raw> and / or "on": 0 | 1'}), 400
            try:
                results = self.write_fader(usb, level=level, on=on)
            except (ValueError, PermissionError) as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            except (ConnectionError, OSError) as e:
                return jsonify({"ok": False, "error": f"desk not reachable: {e}"}), 503
            log.info("Fader set from the page on USB %d: level=%s on=%s", usb + 1, level, on)
            return jsonify({"ok": True, "results": results})

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
