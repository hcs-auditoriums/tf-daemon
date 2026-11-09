"""Flask application factory, and the one place modules are built and wired together."""

from __future__ import annotations

import atexit
import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from flask import Flask, jsonify
from flask_sock import Sock

from . import __version__
from .audio.engine import AudioEngine
from .audio.module import AudioModule
from .audio.sources import DeviceSource, Source, SourceError, SynthSource, WavSource
from .audio.stage import DeskMix, StageSettings, StageSource
from .config import Config
from .console.module import TF5Module
from .console.patch import patch_from
from .console.sim import SimConsole
from .modules import Module
from .qlxd.engine import QLXDEngine
from .qlxd.module import QLXDModule
from .rf.devices import RFDevice, SimTinySA, TinySADevice
from .rf.engine import RangeHint, RFEngine, fit_span
from .rf.module import RFModule
from .system import register_system
from .vocal.module import VocalModule

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def build_source(cfg: Config, desk: DeskMix | None = None) -> Source:
    """The audio source ``audio.source`` names. ``desk``: what the simulated stage hears
    the desk doing (the simulated TF5 in demo mode)."""
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
    if a.source == "stage":
        return StageSource(a.channels, a.blocksize, stage_settings(cfg), desk, a.samplerate)
    return WavSource(a.wav, a.blocksize, a.samplerate)


def stage_settings(cfg: Config) -> StageSettings:
    """The simulated stage for this config: talkers on the named inputs, the mains on the
    stereo-bus USB channels, the house mic on ``synth.house_mic``."""
    patch = patch_from(cfg.tf5.usb_patch)
    st = {src[1]: i for i, src in enumerate(patch) if src is not None and src[0] == "St"}
    if 0 not in st or 1 not in st:
        raise SourceError("the stage needs St L and St R on the USB patch (tf5.usb_patch)")
    house = cfg.synth.house_mic if cfg.synth.house_mic >= 0 else None
    talkers = [c.index for c in cfg.channels if c.index not in st.values() and c.index != house]
    inch = {i: src[1] for i, src in enumerate(patch) if src is not None and src[0] == "InCh"}
    s = cfg.synth
    return StageSettings(talkers=talkers, mains=(st[0], st[1]), house_mic=house, desk_channel=inch.get,
                         speech_db=s.speech_db, noise_db=s.noise_db, tone_hz=s.tone_hz, tone_db=s.tone_db,
                         pa_leak_db=s.pa_leak_db, room_rt60=s.room_rt60, seed=s.seed)


def build_modules(cfg: Config) -> list[Module]:
    """Every module the config turns on, wired together. Each device's ``source``
    picks the real hardware, a simulation, or off."""
    # In demo mode one simulated TF5 is both the console the TF5 module talks to and
    # the desk the simulated stage hears (its EQ and faders shape the mains).
    sim = SimConsole() if cfg.tf5.active_source == "demo" else None
    audio = AudioEngine(cfg, build_source(cfg, desk=sim))
    qlxd = QLXDEngine(cfg.qlxd) if cfg.qlxd.source != "off" else None
    # The console names the inputs, its faders weight ring attribution, and its
    # stereo bus inputs are mixes rather than sources.
    tf5 = _tf5_module(cfg, sim, on_names=audio.set_console_names, on_gains=audio.set_console_gains)
    if tf5 is not None:
        audio.set_bus_inputs(tf5.bus_inputs())
    modules: list[Module | None] = [
        AudioModule(audio, console=tf5),
        _rf_module(cfg, qlxd),
        QLXDModule(qlxd) if qlxd is not None else None,
        tf5,
        VocalModule(cfg.vocal, audio, cfg.audio.samplerate, desk=tf5, mains=_mains(cfg)) if cfg.vocal.enabled
        else None,
    ]
    return [m for m in modules if m is not None]


def _rf_module(cfg: Config, qlxd: QLXDEngine | None) -> RFModule | None:
    if cfg.rf.source == "off":
        return None
    device: RFDevice = TinySADevice() if cfg.rf.source == "device" else SimTinySA()
    return RFModule(RFEngine(cfg.rf, device, range_hint=receiver_range(qlxd)))


def _tf5_module(cfg: Config, sim: SimConsole | None, on_names: Callable[[dict[int, str]], None],
                on_gains: Callable[[dict[int, float | None]], None]) -> TF5Module | None:
    source = cfg.tf5.active_source
    if source == "off":
        return None
    if source == "device":
        return TF5Module(cfg.tf5, on_names, on_gains)
    # Demo: the simulated console on free local ports, with EQ read and write on.
    assert sim is not None
    demo = replace(cfg.tf5, host=sim.host, port=sim.rcp_port, eq_port=sim.eq_port, eq=True, eq_write=True)
    return TF5Module(demo, on_names, on_gains, simulator=sim)


def _mains(cfg: Config) -> tuple[int, ...]:
    """The inputs carrying the stereo bus (St L / R on the USB patch)."""
    return tuple(i for i, src in enumerate(patch_from(cfg.tf5.usb_patch)) if src is not None and src[0] == "St")


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
