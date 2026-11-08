import json
import time

import numpy as np
import pytest

from tf_daemon.app import build_modules, create_app
from tf_daemon.config import ConfigError, load_config
from tf_daemon.rf import engine as engine_mod
from tf_daemon.rf.devices import DemoDevice, RFDeviceError, parse_scan
from tf_daemon.rf.engine import RFEngine, SweepSettings, fit_span


def test_parse_scan_handles_firmware_bug_and_junk():
    raw = bytearray(b"-80.5\r\n-:.0\r\n  -42.25 extra\r\n\r\nscan\r\n-90\r\n")
    np.testing.assert_allclose(parse_scan(raw), [-80.5, -10.0, -42.25, -90.0])


def test_sweep_settings_validation():
    base = SweepSettings(88_000_000, 108_000_000, 290, "auto", 0.5)
    assert base.updated({"points": 450, "rbw": "100"}).points == 450
    assert base.updated({"start_hz": None}).start_hz == 88_000_000  # None keeps the current value
    for bad in ({"start_hz": 108_000_000}, {"points": 451}, {"rbw": "7"}, {"interval_s": 0}, {"points": "x"}):
        with pytest.raises(ValueError):
            base.updated(bad)


def test_config_rejects_bad_rf(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[rf]\nsource = "hackrf"\n')
    with pytest.raises(ConfigError, match="rf.source"):
        load_config(p)
    p.write_text("[rf]\npoints = 1000\n")
    with pytest.raises(ConfigError, match="points"):
        load_config(p)


def _wait_state(engine, pred, timeout=5.0):
    deadline, seq = time.time() + timeout, 0
    while time.time() < deadline:
        seq, payload = engine.hub.wait(seq, timeout=0.5)
        if payload is not None and pred(state := json.loads(payload)):
            return state
    raise AssertionError("state not reached in time")


def test_rf_module_end_to_end_with_demo():
    cfg = load_config(None)
    cfg["rf"]["interval_s"] = 0.1
    cfg["rf"]["auto_connect"] = False  # manual: nothing happens until Connect
    modules = build_modules(cfg, "synth", rf_source="demo", qlxd_source="off")
    app = create_app(cfg, modules)
    try:
        engine = next(m for m in modules if m.name == "rf").engine
        client = app.test_client()
        assert client.get("/rf").status_code == 200

        assert client.post("/api/rf/scan/start").status_code == 409  # not connected yet
        assert client.post("/api/rf/connect").get_json()["ok"]
        assert client.post("/api/rf/scan/start", json={"points": 2000}).status_code == 400

        r = client.post("/api/rf/scan/start", json={"start_hz": 400_000_000, "stop_hz": 500_000_000, "points": 101})
        assert r.get_json()["settings"]["points"] == 101
        s = _wait_state(engine, lambda s: s["sweeps"] >= 3)
        assert s["scanning"] and s["sweep"]["start_hz"] == 400_000_000
        assert len(s["sweep"]["live"]) == len(s["sweep"]["peak"]) == 101
        assert all(p >= v for p, v in zip(s["sweep"]["peak"], s["sweep"]["live"]))

        assert client.post("/api/rf/reset-peak").get_json()["ok"]
        assert client.get("/api/status").get_json()["modules"]["rf"]["sweeps"] >= 3

        client.post("/api/rf/scan/stop")
        assert not client.get("/api/rf/state").get_json()["scanning"]
        client.post("/api/rf/disconnect")
        assert not client.get("/api/rf/status").get_json()["connected"]
    finally:
        app.extensions["tf_stop"]()


def test_points_limited_to_device_maximum():
    cfg = load_config(None)
    dev = DemoDevice()
    dev.max_points = 290  # what an original (non-Ultra) tinySA reports
    engine = RFEngine(cfg, dev)
    engine.connect()
    try:
        with pytest.raises(ValueError, match="at most 290"):
            engine.start_scan({"points": 450})
        assert engine.snapshot()["max_points"] == 290
        engine.start_scan({"points": 290})
    finally:
        engine.stop()


def test_parse_scan_none_is_empty():
    assert parse_scan(None).size == 0


def test_rf_off_and_sweep_failure():
    cfg = load_config(None)
    assert [m.name for m in build_modules(cfg, "synth", rf_source="off", qlxd_source="off")] == ["audio"]

    class Flaky(DemoDevice):
        def sweep(self, *a):
            raise RFDeviceError("USB unplugged")

    engine = RFEngine(cfg, Flaky())
    engine.connect()
    engine.start_scan()
    s = _wait_state(engine, lambda s: s["error"] is not None)
    assert "USB unplugged" in s["error"] and not s["scanning"]
    engine.stop()


def test_fit_span():
    assert fit_span([]) is None
    assert fit_span([482.425, 607.7]) == (480_000_000, 610_000_000)
    assert fit_span([578.35]) == (573_000_000, 584_000_000)  # one receiver: at least 10 MHz wide
    assert fit_span([1.0])[0] == 0


@pytest.fixture
def auto_cfg(monkeypatch):
    monkeypatch.setattr(engine_mod, "SUPERVISE_INTERVAL_S", 0.05)
    cfg = load_config(None)
    cfg["rf"].update(auto_connect=True, interval_s=0.1)
    return cfg


def _until(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def test_auto_mode_connects_and_sweeps_the_receiver_range(auto_cfg):
    hint = [(480_000_000, 610_000_000)]
    dev = DemoDevice()
    dev.max_points = 290
    engine = RFEngine(auto_cfg, dev, range_hint=lambda: hint[0])
    engine.start()
    try:
        _until(lambda: engine.status()["sweeps"] >= 2)
        st = engine.status()
        assert st["connected"] and st["scanning"] and st["auto"]
        assert (st["settings"]["start_hz"], st["settings"]["stop_hz"], st["settings"]["points"]) == \
            (480_000_000, 610_000_000, 290)

        hint[0] = (470_000_000, 610_000_000)  # a receiver was added: auto range follows
        _until(lambda: engine.status()["settings"]["start_hz"] == 470_000_000)

        engine.stop_scan(by_user=True)  # a user's Stop is respected...
        time.sleep(0.3)
        assert not engine.status()["scanning"] and engine.status()["paused"]

        engine.start_scan({"start_hz": 500_000_000, "stop_hz": 520_000_000}, by_user=True)  # ...until Start
        hint[0] = (460_000_000, 610_000_000)
        time.sleep(0.3)
        st = engine.status()
        assert st["scanning"] and not st["paused"]
        assert st["settings"]["start_hz"] == 500_000_000  # a hand-picked range is kept

        engine.disconnect(by_user=True)
        time.sleep(0.3)
        assert not engine.status()["connected"]
        engine.connect(by_user=True)
        _until(lambda: engine.status()["scanning"])
    finally:
        engine.stop()


def test_auto_mode_waits_for_and_recovers_the_device(auto_cfg):
    class Unpluggable(DemoDevice):
        present, fail_next = False, False

        def open(self):
            if not self.present:
                raise RFDeviceError("tinySA not found. Is it plugged in?")
            return super().open()

        def sweep(self, *a):
            if self.fail_next:
                self.fail_next = False
                raise RFDeviceError("USB unplugged")
            return super().sweep(*a)

    dev = Unpluggable()
    engine = RFEngine(auto_cfg, dev)  # no receivers: falls back to the configured range
    engine.start()
    try:
        _until(lambda: engine.status()["error"] is not None)
        assert not engine.status()["connected"]  # keeps retrying while absent

        dev.present = True
        _until(lambda: engine.status()["sweeps"] >= 1)
        assert engine.status()["settings"]["start_hz"] == auto_cfg["rf"]["start_hz"]

        n = engine.status()["sweeps"]
        dev.fail_next = True  # yanked mid-sweep: closed, reopened, sweeping again
        _until(lambda: engine.status()["sweeps"] > n + 1 and engine.status()["error"] is None)
    finally:
        engine.stop()
