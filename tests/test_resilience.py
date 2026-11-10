"""Devices come and go: the app must keep running and pick them up again."""

import threading
import time

import numpy as np
import pytest
from helpers import make_config, wait_until

from tf_daemon.app import build_modules, create_app
from tf_daemon.audio.device import DeviceSource
from tf_daemon.audio.engine import AudioEngine
from tf_daemon.config import load_config

BLOCK = 1200


class _Status:
    input_overflow = False


class FakeStream:
    def __init__(self, sd, channels, callback, blocksize, **_):
        self.sd, self.channels, self.callback, self.blocksize = sd, channels, callback, blocksize
        self.active = False
        self._thread = None

    def start(self):
        self.active = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        t = 0
        while self.active:
            if self.sd.plugged:  # an unplugged USB device simply stops calling back
                n = np.arange(t, t + self.blocksize)
                block = np.tile((0.1 * np.sin(2 * np.pi * 1000 * n / 48000))[:, None], (1, self.channels))
                self.callback(block.astype(np.float32), self.blocksize, None, _Status())
                t += self.blocksize
            time.sleep(self.blocksize / 48000)  # paces the fake device at 48 kHz

    def stop(self):
        self.active = False

    def close(self):
        self.active = False


class FakeSD:
    """Stands in for the sounddevice module."""

    def __init__(self, channels=34, plugged=False, rate_ok=True):
        self.channels, self.plugged, self.rate_ok = channels, plugged, rate_ok
        self.rescans = 0

    def query_devices(self):
        devices = [{"name": "Built-in Microphone", "max_input_channels": 2}]
        if self.plugged:
            devices.append({"name": "Yamaha TF", "max_input_channels": self.channels})
        return devices

    def check_input_settings(self, **kw):
        if not self.rate_ok:
            raise RuntimeError("Invalid sample rate")

    def InputStream(self, **kw):
        return FakeStream(self, **kw)

    def _terminate(self):
        self.rescans += 1

    def _initialize(self):
        pass


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(DeviceSource, "RETRY_S", 0.1)
    monkeypatch.setattr(DeviceSource, "STALL_S", 0.4)
    monkeypatch.setattr(DeviceSource, "CHECK_S", 0.05)


def frames_flowing(engine, timeout=4.0):
    seq, _ = engine.hub.wait(0, timeout=0.1)
    deadline = time.time() + timeout
    while time.time() < deadline:
        new, payload = engine.hub.wait(seq, timeout=0.5)
        if payload is not None and new != seq:
            return True
    return False


@pytest.mark.slow
def test_audio_device_unplug_and_replug(fast):
    sd = FakeSD(plugged=False)
    cfg = load_config(None)
    src = DeviceSource("TF", BLOCK, sd_module=sd)
    engine = AudioEngine(cfg, src)  # no device yet: the engine still builds and starts
    engine.start()
    try:
        wait_until(lambda: "no input device matching 'TF'" in (src.stats()["error"] or "") and sd.rescans >= 1)
        assert not src.stats()["connected"]  # and looks again for newly plugged devices

        sd.plugged = True
        wait_until(lambda: src.stats()["connected"])
        assert src.stats()["device"] == "Yamaha TF" and src.stats()["error"] is None
        assert frames_flowing(engine)

        sd.plugged = False  # unplug mid-stream: callbacks stop
        wait_until(lambda: not src.stats()["connected"])
        assert "stopped delivering audio" in src.stats()["error"] or "no input device" in src.stats()["error"]
        assert not frames_flowing(engine, timeout=1.0)

        sd.plugged = True  # back again
        wait_until(lambda: src.stats()["connected"])
        assert src.stats()["reconnects"] >= 1
        assert frames_flowing(engine)
    finally:
        engine.stop()


def test_device_with_fewer_channels_is_zero_padded(fast):
    sd = FakeSD(channels=2, plugged=True)
    src = DeviceSource("TF", BLOCK, sd_module=sd, channels=4)
    blocks = []
    src.start(lambda b: blocks.append(b.copy()))
    try:
        wait_until(lambda: len(blocks) >= 3)
        b = blocks[-1]
        assert b.shape == (BLOCK, 4)
        assert np.abs(b[:, :2]).max() > 0.05 and not b[:, 2:].any()
    finally:
        src.stop()


def test_device_that_rejects_the_format_is_retried(fast):
    sd = FakeSD(plugged=True, rate_ok=False)
    src = DeviceSource("TF", BLOCK, sd_module=sd)
    src.start(lambda b: None)
    try:
        wait_until(lambda: "Invalid sample rate" in (src.stats()["error"] or ""))
        assert not src.stats()["connected"]
        sd.rate_ok = True  # e.g. the console was switched back to 48 kHz
        wait_until(lambda: src.stats()["connected"])
    finally:
        src.stop()


def test_app_starts_without_the_audio_device(fast, monkeypatch):
    """The real crash we hit: no TF5 at startup must not stop the server."""
    import tf_daemon.app as app_mod

    sd = FakeSD(plugged=False)
    real = app_mod.DeviceSource
    monkeypatch.setattr(app_mod, "DeviceSource", lambda *a, **kw: real(*a, sd_module=sd, **kw))
    cfg = make_config(audio={"source": "device"})
    modules = build_modules(cfg)
    app = create_app(cfg, modules)
    try:
        client = app.test_client()
        assert client.get("/").status_code == 200
        st = client.get("/api/audio/status").get_json()
        assert st["source_channels"] == 34 and not st["source_stats"]["connected"]
        assert len(client.get("/api/audio/meta").get_json()["channels"]) == 34
    finally:
        app.extensions["tf_stop"]()


def test_analysis_survives_an_exception():
    cfg = make_config()
    modules = build_modules(cfg)
    engine = modules[0].engine
    calls = {"n": 0}
    real = engine._publish

    def flaky(active):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("boom")
        real(active)

    engine._publish = flaky
    app = create_app(cfg, modules)
    try:
        wait_until(lambda: calls["n"] >= 3)
        assert frames_flowing(engine)  # still analyzing after the error
    finally:
        app.extensions["tf_stop"]()
