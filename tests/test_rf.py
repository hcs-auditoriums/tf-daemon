import json
import time

import numpy as np
import pytest

from tf_daemon.app import build_modules, create_app
from tf_daemon.config import ConfigError, load_config
from tf_daemon.rf.devices import DemoDevice, RFDeviceError, parse_scan
from tf_daemon.rf.engine import RFEngine, SweepSettings


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
