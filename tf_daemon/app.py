"""Flask application factory and source/module construction."""

from __future__ import annotations

import atexit
import logging
from pathlib import Path
from typing import Any

from flask import Flask, jsonify
from flask_sock import Sock

from . import __version__
from .audio.engine import AudioEngine
from .audio.module import AudioModule
from .audio.sources import DeviceSource, Source, SourceError, SynthSource, WavSource
from .config import channels_from
from .modules import Module
from .qlxd.engine import QLXDEngine
from .qlxd.module import QLXDModule
from .rf.devices import DemoDevice, RFDevice, TinySADevice
from .rf.engine import RangeHint, RFEngine, fit_span
from .rf.module import RFModule
from .system import register_system
from .tf5 import TF5Module
from .tf5sim import SimConsole
from .vocal import VocalModule

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def build_source(cfg: dict[str, Any], kind: str, wav_path: str | None = None) -> Source:
    audio = cfg["audio"]
    blocksize = int(audio["blocksize"])
    fs = int(audio["samplerate"])
    if kind == "device":
        return DeviceSource(str(audio["device"]), blocksize, fs, channels=int(audio["channels"]))
    if kind == "synth":
        idx = [c.index for c in channels_from(cfg)]
        s = cfg["synth"]
        return SynthSource(
            channels=max(idx) + 1, active=idx, blocksize=blocksize, samplerate=fs,
            speech_db=float(s["speech_db"]), noise_db=float(s["noise_db"]),
            tone_hz=float(s["tone_hz"]), tone_db=float(s["tone_db"]), seed=int(s["seed"]),
        )
    if kind == "wav":
        if not wav_path:
            raise SourceError("--source wav needs a file path")
        return WavSource(wav_path, blocksize, fs)
    raise SourceError(f"unknown source {kind!r} (expected device, synth or wav)")


def build_rf_device(kind: str) -> RFDevice:
    if kind == "device":
        return TinySADevice()
    if kind == "demo":
        return DemoDevice()
    raise SourceError(f"unknown rf source {kind!r} (expected device or demo)")


def build_modules(cfg: dict[str, Any], source_kind: str, wav_path: str | None = None,
                  rf_source: str | None = None, qlxd_source: str | None = None,
                  tf5_source: str | None = None) -> list[Module]:
    """``rf_source`` / ``qlxd_source`` override ``rf.source`` / ``qlxd.source``; ``"off"`` disables.

    ``tf5_source``: ``"device"`` (the console at ``tf5.host``; the default when it is
    set), ``"demo"`` (a simulated TF5 in-process, EQ read and write on) or ``"off"``.
    """
    source = build_source(cfg, source_kind, wav_path)
    modules: list[Module] = [AudioModule(AudioEngine(cfg, source))]

    qlxd_kind = (qlxd_source or str(cfg["qlxd"]["source"])).lower()
    if qlxd_kind not in ("device", "demo", "off"):
        raise SourceError(f"unknown qlxd source {qlxd_kind!r} (expected device, demo or off)")
    qlxd = QLXDEngine(cfg, demo=qlxd_kind == "demo") if cfg["qlxd"]["enabled"] and qlxd_kind != "off" else None

    rf_kind = (rf_source or str(cfg["rf"]["source"])).lower()
    if cfg["rf"]["enabled"] and rf_kind != "off":
        modules.append(RFModule(RFEngine(cfg, build_rf_device(rf_kind), range_hint=receiver_range(qlxd))))
    if qlxd is not None:
        modules.append(QLXDModule(qlxd))
    tf5_kind = (tf5_source or ("device" if str(cfg["tf5"]["host"]).strip() else "off")).lower()
    if tf5_kind not in ("device", "demo", "off"):
        raise SourceError(f"unknown tf5 source {tf5_kind!r} (expected device, demo or off)")
    if tf5_kind == "device" and not str(cfg["tf5"]["host"]).strip():
        raise SourceError("tf5 source 'device' needs tf5.host")
    if tf5_kind == "demo":
        sim = SimConsole()
        modules.append(sim)
        demo = {**cfg["tf5"], "host": sim.host, "port": sim.rcp_port, "eq_port": sim.eq_port,
                "eq": True, "eq_write": True}
        modules.append(TF5Module({**cfg, "tf5": demo}, modules[0].engine))
    elif tf5_kind == "device":
        modules.append(TF5Module(cfg, modules[0].engine))
    if cfg["vocal"]["enabled"]:
        modules.append(VocalModule(cfg, modules[0].engine))
    return modules


def receiver_range(qlxd: QLXDEngine | None) -> RangeHint | None:
    """Integration: the RF sweep's default range spans the known QLX-D receivers."""
    if qlxd is None:
        return None
    return lambda: fit_span(r["frequency_mhz"] for r in qlxd.frequencies())


def create_app(cfg: dict[str, Any], modules: list[Module], start: bool = True,
               config_path: str | Path | None = None) -> Flask:
    """``config_path`` is the file the /config page edits (None: editing disabled)."""
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    app.config["TF_CONFIG"] = cfg
    sock = Sock(app)

    for m in modules:
        m.register(app, sock)
    register_system(app, cfg, config_path)

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/status")
    def status():
        return jsonify({"version": __version__, "modules": {m.name: m.status() for m in modules}})

    if start:
        for m in modules:
            m.start()

        def _stop_all() -> None:
            for m in reversed(modules):
                try:
                    m.stop()
                except Exception:
                    log.exception("Error stopping module %s", m.name)

        atexit.register(_stop_all)
        app.extensions["tf_stop"] = _stop_all

    return app
