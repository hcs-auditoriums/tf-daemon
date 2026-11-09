"""Flask application factory, and the one place modules are built and wired together."""

from __future__ import annotations

import atexit
import logging
from dataclasses import replace
from pathlib import Path

from flask import Flask, jsonify
from flask_sock import Sock

from . import __version__
from .audio.engine import AudioEngine
from .audio.module import AudioModule
from .audio.sources import DeviceSource, Source, SynthSource, WavSource
from .config import Config
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


def build_source(cfg: Config) -> Source:
    """The audio source ``audio.source`` names."""
    a = cfg.audio
    if a.source == "device":
        return DeviceSource(a.device, a.blocksize, a.samplerate, channels=a.channels)
    if a.source == "synth":
        idx = [c.index for c in cfg.channels]
        s = cfg.synth
        return SynthSource(
            channels=max(idx) + 1, active=idx, blocksize=a.blocksize, samplerate=a.samplerate,
            speech_db=s.speech_db, noise_db=s.noise_db, tone_hz=s.tone_hz, tone_db=s.tone_db, seed=s.seed,
        )
    return WavSource(a.wav, a.blocksize, a.samplerate)


def build_modules(cfg: Config) -> list[Module]:
    """Every module the config turns on, wired together. Each device's ``source``
    picks the real hardware, a simulation, or off."""
    audio = AudioEngine(cfg, build_source(cfg))
    qlxd = QLXDEngine(cfg.qlxd) if cfg.qlxd.source != "off" else None
    modules: list[Module | None] = [
        AudioModule(audio),
        _rf_module(cfg, qlxd),
        QLXDModule(qlxd) if qlxd is not None else None,
        _tf5_module(cfg, audio),
        VocalModule(cfg.vocal, audio, cfg.audio.samplerate) if cfg.vocal.enabled else None,
    ]
    return [m for m in modules if m is not None]


def _rf_module(cfg: Config, qlxd: QLXDEngine | None) -> RFModule | None:
    if cfg.rf.source == "off":
        return None
    device: RFDevice = TinySADevice() if cfg.rf.source == "device" else DemoDevice()
    return RFModule(RFEngine(cfg.rf, device, range_hint=receiver_range(qlxd)))


def _tf5_module(cfg: Config, audio: AudioEngine) -> TF5Module | None:
    source = cfg.tf5.active_source
    if source == "off":
        return None
    if source == "device":
        return TF5Module(cfg.tf5, audio)
    # Demo: a simulated console on free local ports, with EQ read and write on.
    sim = SimConsole()
    demo = replace(cfg.tf5, host=sim.host, port=sim.rcp_port, eq_port=sim.eq_port, eq=True, eq_write=True)
    return TF5Module(demo, audio, simulator=sim)


def receiver_range(qlxd: QLXDEngine | None) -> RangeHint | None:
    """Integration: the RF sweep's default range spans the known QLX-D receivers."""
    if qlxd is None:
        return None
    return lambda: fit_span(r["frequency_mhz"] for r in qlxd.frequencies())


def create_app(cfg: Config, modules: list[Module], start: bool = True,
               config_path: str | Path | None = None) -> Flask:
    """``config_path`` is the file the /config page edits (None: editing disabled)."""
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    sock = Sock(app)

    for m in modules:
        m.register(app, sock)
    register_system(app, cfg.update, config_path)

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
