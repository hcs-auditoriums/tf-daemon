"""TF5Module: the console's names, settings and EQ in the app (routes under /api/tf5)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from flask import Blueprint, Flask, jsonify, request
from flask_sock import Sock

from ..config import TF5Config
from ..dsp.eq import CURVE_FREQS, eq_curve
from . import coverage, desk
from .buses import Buses
from .busproc import BusProcessing
from .editor import tree
from .editor.client import Changes, TFEditorClient
from .editor.guard import FAMILIES, WRITABLE, WRITE_LIMITS, WriteNotAllowed, writable
from .meters import MeterClient
from .patch import patch_from, source_label
from .rcp import TF5Client
from .sends import DeskSends
from .strip import Strip
from .strip_desk import DeskStrip

if TYPE_CHECKING:
    from .sim import SimConsole

log = logging.getLogger(__name__)

# The desk's EQ ranges in Hz, dB and Q, for the page's editing to stay within: guard.WRITE_LIMITS
# (raw Hz x 10, dB x 100, Q x 1000), which refuses a write outside them.
EQ_LIMITS = {
    "hpf_freq": [v / 10 for v in WRITE_LIMITS["HPF/Frequency"]],
    "freq": [v / 10 for v in WRITE_LIMITS["Frequency"]],
    "gain": [v / 100 for v in WRITE_LIMITS["Gain"]],
    "q": [v / 1000 for v in WRITE_LIMITS["Q"]],
}


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
      GET /api/tf5/meters     level meters per USB input (tf5.meters; console/meters.py), 404 when off
      GET /api/tf5/channels   per USB input: console source and its settings; mix names
      GET /api/tf5/eq/<usb>   EQ of the console channel on that USB input + its curve
                              (404 when tf5.eq is off)
      PUT /api/tf5/eq/<usb>   {"changes": [[path, raw value], ...], "confirm": true}: SET that channel's EQ
                              (the page's EQ editing: a person dragging a band or typing a value; needs
                              tf5.eq_write; paths relative to the channel, e.g. "PEQ/Bank/Band:0/Gain")
      PUT /api/tf5/fader/<usb> {"level": raw | omitted, "on": 0 | 1 | omitted, "confirm": true}: SET that
                              input channel's fader (dB x 100, -32768 = -inf) and / or on switch (the
                              sidebar's fader and ON button: a person's drag or click; needs tf5.fader_write)
      GET  /api/tf5/param?func=Mixing&path=DCA:0/Fader/Level
                              any parameter: {"func", "path", "value" (raw; null if not received, or a
                              password), "spec" (editor/tree.py schema), "family" (the enabled write family
                              that covers it, or null), "writable"}; 404 unknown parameter / tf5.eq off
      POST /api/tf5/param     {"func": "Mixing", "changes": [[path, raw], ...], "confirm": true}: SET any
                              parameters a write family allows (guard.FAMILIES, each behind its tf5.*_write
                              switch). 400 without confirm or with a bad value, 403 when no enabled family
                              covers a path; else {"ok", "results": [{path, value, confirmed, actual}]}
      GET/PUT /api/tf5/mix/...  the Mix page's buses, DCAs, mute groups, cue, GEQ, bus PEQ: console/buses.py,
                              console/busproc.py (and GET /mix, the page); the SEND tab's sends come from
                              the desk when the editor protocol has it (console/sends.py)
      POST /api/tf5/event/recall_scene  {"number": "A05", "confirm": true}: RECALL a scene (needs
                              tf5.scene_recall); {"ok", "number", "confirmed", "status"}
      GET /desk, GET /api/tf5/{scenes,recorder,fx,delay,selected}: the Desk page and its reads (desk.py)
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
        self.meters = (
            MeterClient(cfg.host, cfg.port, patch, cfg.meter_interval_ms, cfg.meter_point) if cfg.meters else None
        )
        self.eq_version = 0  # bumped whenever the console's mixing data (EQ included) changes
        if cfg.eq:
            self.eq_client = TFEditorClient(
                cfg.host,
                cfg.eq_port,
                on_change=self._eq_changed,
                writes=cfg.eq_write,
                fader_writes=cfg.fader_write,
                families=[f.name for f in FAMILIES.values() if getattr(cfg, f.switch.removeprefix("tf5."))],
                functions=cfg.functions,
                scene_recall=cfg.scene_recall,
            )
        desk = DeskStrip(self.eq_client) if self.eq_client is not None else None
        self.strip = Strip(patch, self.client, simulator, desk)  # input, EQ, dynamics, sends (strip.py)
        self.buses = Buses(self, simulated=simulator is not None)  # the Mix page (buses.py)
        self.sends = DeskSends(patch, self.buses, self.client.mixes)  # the SEND tab from the desk

    def _eq_changed(self) -> None:
        self.eq_version += 1

    @property
    def eq_available(self) -> bool:
        return self.eq_client is not None

    def snapshot(self) -> tuple[int, dict[str, Any]]:
        return self.client.snapshot()

    def changes_since(self, seq: int) -> tuple[int, dict[str, Any] | None]:
        return self.client.changes_since(seq)

    def meters_since(self, version: int) -> tuple[int, dict[str, Any] | None]:
        return (version, None) if self.meters is None else self.meters.since(version)

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

    def param(self, func: str, path: str) -> dict[str, Any]:
        """Any parameter of the desk: its raw value (None until received, and always for a
        password), its schema and whether an enabled write family covers it. KeyError if the
        V4.55 tree has no such parameter."""
        spec = tree.spec(func, path)
        ec = self.eq_client
        fam = writable(func, path, ec.enabled_families) if ec is not None else None
        return {
            "func": func,
            "path": path,
            "value": ec.get(path, func) if ec is not None else None,
            "spec": spec.as_dict(),
            "family": fam.name if fam else None,
            "writable": fam is not None,
        }

    def write_params(self, func: str, changes: Changes) -> list[dict[str, Any]]:
        """SET parameters of ``func``: ``[(name path, raw value)]``. Only on a person's request
        (POST /api/tf5/param with confirm); only what an enabled write family allows (see
        TFEditorClient.write)."""
        ec = self.eq_client
        if ec is None:
            raise WriteNotAllowed("the console's editor protocol is off (tf5.eq = false)")
        return ec.write(changes, func=func)

    def recall_scene(self, number: str) -> dict[str, Any]:
        """RECALL scene ``number`` ("A05") on the desk. Only on a person's request."""
        ec = self.eq_client
        if ec is None:
            raise WriteNotAllowed("the console's editor protocol is off (tf5.eq = false)")
        return ec.recall_scene(number)

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
            "limits": EQ_LIMITS,
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

        @bp.get("/meters")
        def meters():
            if self.meters is None:
                return jsonify({"error": "tf5.meters is off"}), 404
            return jsonify({**self.meters.levels(), "status": self.meters.status()})

        @bp.get("/coverage")
        def desk_coverage():  # what can be read / written on the desk (docs/desk-coverage.md)
            return jsonify({"features": coverage.as_dicts()})

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

        @bp.get("/param")
        def param_get():
            if self.eq_client is None:
                return jsonify({"error": "tf5.eq is off"}), 404
            func, path = request.args.get("func", "Mixing"), request.args.get("path", "")
            try:
                return jsonify(self.param(func, path))
            except KeyError as e:
                return jsonify({"error": str(e.args[0])}), 404

        @bp.post("/param")
        def param_set():
            # Changes the desk: only a person's edit (the page sends confirm with each).
            body = request.get_json(silent=True) or {}
            if body.get("confirm") is not True:
                return jsonify({"ok": False, "error": 'changing the desk needs "confirm": true'}), 400
            func, changes = body.get("func", "Mixing"), body.get("changes")
            if (
                not isinstance(func, str)
                or not isinstance(changes, list)
                or not changes
                or not all(
                    isinstance(c, list)
                    and len(c) == 2
                    and isinstance(c[0], str)
                    and isinstance(c[1], int | str)
                    and not isinstance(c[1], bool)
                    for c in changes
                )
            ):
                return jsonify({"ok": False, "error": 'body needs "func" and "changes": [[path, raw value], ...]'}), 400
            if self.eq_client is None:
                return jsonify({"ok": False, "error": "tf5.eq is off"}), 404
            return self._desk_call(lambda: {"results": self.write_params(func, [(c[0], c[1]) for c in changes])})

        @bp.post("/event/recall_scene")
        def recall_scene_route():
            # Changes the desk: only a person's click (the page sends confirm with it).
            body = request.get_json(silent=True) or {}
            if body.get("confirm") is not True:
                return jsonify({"ok": False, "error": 'recalling a scene needs "confirm": true'}), 400
            number = body.get("number")
            if not isinstance(number, str):
                return jsonify({"ok": False, "error": 'body needs "number": "A05"'}), 400
            if self.eq_client is None:
                return jsonify({"ok": False, "error": "tf5.eq is off"}), 404
            return self._desk_call(lambda: self.recall_scene(number))

        @bp.get("/strip/<int:usb>")
        def strip_get(usb: int):
            return jsonify(self.sends.over(self.strip.get(usb)))

        @bp.put("/strip/<int:usb>")
        def strip_set(usb: int):
            # {"section": "gate", "changes": {"threshold": -40}, "confirm": true}; sends add "mix".
            body = request.get_json(silent=True) or {}
            if body.get("confirm") is not True:
                return jsonify({"ok": False, "error": 'changing the desk needs "confirm": true'}), 400
            changes = body.get("changes")
            if not isinstance(changes, dict) or not changes:
                return jsonify({"ok": False, "error": 'body needs "changes": {parameter: value}'}), 400
            section, mix = str(body.get("section")), body.get("mix")
            if section == "sends" and self.sends.live:
                return self._desk_call(lambda: {"applied": self.sends.put(usb, mix, changes)})
            return self._desk_call(lambda: self.strip.put(usb, section, changes, mix))

        desk.register(app, bp, lambda: self.eq_client)  # /desk and its reads (scenes, recorder, FX, delay)
        self.buses.register(app, bp, self._desk_call)  # /mix, /api/tf5/mix/* (buses.py, busproc.py)
        BusProcessing(self.buses).register(bp, self._desk_call)
        app.register_blueprint(bp)

    @staticmethod
    def _desk_call(call: Callable[[], dict[str, Any]]) -> Any:
        """Run a confirmed desk change for a route: 403 not allowed, 400 refused, 503 unreachable."""
        try:
            result = call()
        except (WriteNotAllowed, PermissionError) as e:
            return jsonify({"ok": False, "error": str(e)}), 403
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except (ConnectionError, OSError) as e:
            return jsonify({"ok": False, "error": f"desk not reachable: {e}"}), 503
        log.info("Desk changed from the page: %s", result)
        return jsonify({"ok": True, **result})

    def start(self) -> None:
        self.client.start()
        if self.eq_client is not None:
            self.eq_client.start()
        if self.meters is not None:
            self.meters.start()

    def stop(self) -> None:
        self.client.stop()
        if self.eq_client is not None:
            self.eq_client.stop()
        if self.meters is not None:
            self.meters.stop()
        if self.simulator is not None:
            self.simulator.close()

    def status(self) -> dict[str, Any]:
        st = self.client.status()
        st["eq"] = self.eq_client.status() if self.eq_client is not None else None
        st["meters"] = self.meters.status() if self.meters is not None else None
        st["simulator"] = self.simulator.status() if self.simulator is not None else None
        return st
