import json
import time

import pytest

from tf_daemon.app import build_modules, create_app
from tf_daemon.config import ConfigError, load_config
from tf_daemon.qlxd import engine as engine_mod
from tf_daemon.qlxd import protocol as p
from tf_daemon.qlxd.engine import QLXDEngine
from tf_daemon.qlxd.simulator import SimReceiver


def test_reader_splits_stream_across_chunks():
    r = p.MessageReader()
    assert r.feed(b"< REP 1 BATT_BARS 004 >< REP 1 CHAN_NA") == ["REP 1 BATT_BARS 004"]
    assert r.feed(b"ME {Lead Vox     } >") == ["REP 1 CHAN_NAME {Lead Vox     }"]


def test_parse_and_decode():
    assert p.parse("REP 1 CHAN_NAME {Lead Vox      }") == ("REP", 1, "CHAN_NAME", ["Lead Vox"])
    assert p.parse("REP DEVICE_ID {QLXD4-1 }") == ("REP", None, "DEVICE_ID", ["QLXD4-1"])
    assert p.parse("REP ERR") is not None and p.parse("GET 1 ALL") is None
    assert p.decode("FREQUENCY", ["578350"]) == {"frequency_mhz": 578.35}
    assert p.decode("BATT_BARS", ["255"]) == {"batt_bars": None}
    assert p.decode("BATT_RUN_TIME", ["00185"]) == {"batt_run_min": 185, "batt_calculating": False}
    assert p.decode("BATT_RUN_TIME", ["65534"]) == {"batt_run_min": None, "batt_calculating": True}
    assert p.decode("RX_RF_LVL", ["070"]) == {"rf_dbm": -58}
    assert p.decode("RF_ANTENNA", ["XB"]) == {"ant_a": False, "ant_b": True}
    assert p.decode("ALL", ["AX", "100", "030"]) == {"ant_a": True, "ant_b": False, "rf_dbm": -28, "audio_dbfs": -20}
    assert p.decode("TX_TYPE", ["UNKN"]) == {"tx_type": None}
    assert p.decode("GROUP_CHAN", ["--,--"]) == {"group_chan": None}
    assert p.decode("SOMETHING_NEW", ["1"]) == {}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    # No real NIC in tests: receivers connect unbound, no SLP listener.
    monkeypatch.setattr(engine_mod, "find_interface", lambda name: None)
    c = load_config(None)
    c["qlxd"].update(state_file=str(tmp_path / "qlxd_state.json"), meter_poll_s=0.2, full_poll_s=1.0)
    return c


def _wait(pred, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def test_manual_host_monitoring_persistence_and_offline(cfg):
    sim = SimReceiver("Lead Vox", 578350, "1,2", run_min=185, seed=1)
    sim.start()
    eng = QLXDEngine(cfg)
    eng.start()
    try:
        with pytest.raises(ValueError):
            eng.update_settings({"hosts": ["not a host!"]})
        eng.update_settings({"hosts": f"127.0.0.1:{sim.port}, 127.0.0.1:{sim.port}"})  # duplicates collapse
        assert eng.settings["hosts"] == [f"127.0.0.1:{sim.port}"]

        def rx():
            return eng.snapshot()["receivers"]
        _wait(lambda: rx() and rx()[0]["online"] and rx()[0]["rf_dbm"] is not None)
        r = rx()[0]
        assert r["source"] == "manual" and r["name"] == "Lead Vox" and r["model"] == "QLXD4"
        assert r["frequency_mhz"] == 578.35 and r["group_chan"] == "1 / 2"
        assert 180 <= r["batt_run_min"] <= 185 and r["batt_type"] == "LION" and r["tx_type"] == "QLXD2_SM58"

        # Persisted and reloaded by a fresh engine.
        saved = json.loads(open(cfg["qlxd"]["state_file"]).read())
        assert saved == {"interface": "", "hosts": [f"127.0.0.1:{sim.port}"]}
        assert QLXDEngine(cfg).settings["hosts"] == saved["hosts"]

        sim.stop()
        _wait(lambda: not rx()[0]["online"])
        assert rx()[0]["error"]  # still listed (manual), with the reason
    finally:
        eng.stop()
        sim.stop()


def test_found_host_that_never_answers_is_dropped(cfg, monkeypatch):
    monkeypatch.setattr(engine_mod, "PROBE_ATTEMPTS", 1)
    monkeypatch.setattr(engine_mod.protocol, "PORT", 1)  # nothing listens on 127.0.0.1:1
    eng = QLXDEngine(cfg)
    eng.start()
    try:
        eng._on_announce("127.0.0.1", eng._generation)
        assert eng.snapshot()["network"]["probing"] == 1
        assert eng.snapshot()["receivers"] == []  # never shown while unproven
        _wait(lambda: eng.snapshot()["network"]["probing"] == 0)
        assert "127.0.0.1:1" in eng._rejected
        eng._on_announce("127.0.0.1", eng._generation)  # ignored while rejected
        assert eng.snapshot()["network"]["probing"] == 0
    finally:
        eng.stop()


def test_demo_module_routes(cfg):
    modules = build_modules(cfg, "synth", rf_source="off", qlxd_source="demo")
    app = create_app(cfg, modules)
    try:
        client = app.test_client()
        assert client.get("/qlxd").status_code == 200
        _wait(lambda: client.get("/api/qlxd/status").get_json()["online"] == 4)
        state = client.get("/api/qlxd/state").get_json()
        spare = next(r for r in state["receivers"] if r["name"] == "Spare")
        assert spare["tx_type"] is None and spare["batt_bars"] is None
        assert isinstance(client.get("/api/qlxd/interfaces").get_json(), list)
        assert client.post("/api/qlxd/settings", json={"interface": "nope0"}).status_code == 400
        assert client.post("/api/qlxd/settings", data="x").status_code == 400
        assert client.post("/api/qlxd/rescan").get_json()["ok"]
    finally:
        app.extensions["tf_stop"]()


def test_config_rejects_bad_qlxd(tmp_path):
    p_ = tmp_path / "c.toml"
    p_.write_text('[qlxd]\nsource = "axient"\n')
    with pytest.raises(ConfigError, match="qlxd.source"):
        load_config(p_)
