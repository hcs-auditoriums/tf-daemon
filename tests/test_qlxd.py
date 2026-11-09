import json
from dataclasses import replace

import pytest
from helpers import make_config, wait_until

from tf_daemon.app import build_modules, create_app
from tf_daemon.config import ConfigError, load_config
from tf_daemon.qlxd import engine as engine_mod
from tf_daemon.qlxd import protocol as p
from tf_daemon.qlxd.engine import QLXDEngine
from tf_daemon.qlxd.sim import SimReceiver


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
    assert p.decode("MAC_ADDR", ["00:0e:dd:a4:88:bb"]) == {"mac": "00:0E:DD:A4:88:BB"}
    assert p.decode("SOMETHING_NEW", ["1"]) == {}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    # No real NIC in tests: receivers connect unbound, no SLP listener.
    monkeypatch.setattr(engine_mod, "find_interface", lambda name: None)
    return make_config(
        qlxd={
            "source": "device",
            "state_file": str(tmp_path / "qlxd_state.json"),
            "meter_poll_s": 0.2,
            "full_poll_s": 1.0,
        }
    )


def _demo(cfg):
    return replace(cfg, qlxd=replace(cfg.qlxd, source="demo"))


def test_manual_host_monitoring_persistence_and_offline(cfg):
    sim = SimReceiver("Lead Vox", 578350, "1,2", run_min=185, seed=1)
    sim.start()
    eng = QLXDEngine(cfg.qlxd)
    eng.start()
    try:
        with pytest.raises(ValueError):
            eng.update_settings({"hosts": ["not a host!"]})
        eng.update_settings({"hosts": f"127.0.0.1:{sim.port}, 127.0.0.1:{sim.port}"})  # duplicates collapse
        assert eng.settings["hosts"] == [f"127.0.0.1:{sim.port}"]

        def rx():
            return eng.snapshot()["receivers"]

        wait_until(lambda: rx() and rx()[0]["online"] and rx()[0]["rf_dbm"] is not None)
        r = rx()[0]
        assert r["source"] == "manual" and r["name"] == "Lead Vox" and r["model"] == "QLXD4"
        assert r["frequency_mhz"] == 578.35 and r["group_chan"] == "1 / 2"
        assert 180 <= r["batt_run_min"] <= 185 and r["batt_type"] == "LION" and r["tx_type"] == "QLXD2_SM58"

        # Persisted and reloaded by a fresh engine.
        saved = json.loads(open(cfg.qlxd.state_file).read())
        assert {k: saved[k] for k in ("interface", "hosts")} == {"interface": "", "hosts": [f"127.0.0.1:{sim.port}"]}
        assert QLXDEngine(cfg.qlxd).settings["hosts"] == saved["hosts"]

        sim.stop()
        wait_until(lambda: not rx()[0]["online"])
        assert rx()[0]["error"]  # still listed (manual), with the reason
    finally:
        eng.stop()
        sim.stop()


def test_found_host_that_never_answers_is_dropped(cfg, monkeypatch):
    monkeypatch.setattr(engine_mod, "PROBE_ATTEMPTS", 1)
    monkeypatch.setattr(engine_mod.protocol, "PORT", 1)  # nothing listens on 127.0.0.1:1
    eng = QLXDEngine(cfg.qlxd)
    eng.start()
    try:
        eng._on_announce("127.0.0.1", eng._generation)
        assert eng.snapshot()["network"]["probing"] == 1
        assert eng.snapshot()["receivers"] == []  # never shown while unproven
        wait_until(lambda: eng.snapshot()["network"]["probing"] == 0)
        assert "127.0.0.1:1" in eng._rejected
        eng._on_announce("127.0.0.1", eng._generation)  # ignored while rejected
        assert eng.snapshot()["network"]["probing"] == 0
    finally:
        eng.stop()


def test_demo_module_routes(cfg):
    cfg = _demo(cfg)
    modules = build_modules(cfg)
    app = create_app(cfg, modules)
    try:
        client = app.test_client()
        assert client.get("/qlxd").status_code == 200
        wait_until(lambda: client.get("/api/qlxd/status").get_json()["online"] == 4)
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


def test_known_receivers_persist_for_the_rf_page(cfg, monkeypatch):
    monkeypatch.setattr(engine_mod, "KNOWN_SAVE_MIN_INTERVAL_S", 0.0)
    sim = SimReceiver("Lee", 492025, seed=0xBB)
    sim.start()
    eng = QLXDEngine(cfg.qlxd)
    eng.start()
    try:
        eng.update_settings({"hosts": [f"127.0.0.1:{sim.port}"]})
        wait_until(lambda: eng.frequencies() and eng.frequencies()[0]["online"])
        f = eng.frequencies()[0]
        assert f == {
            "key": "00:0E:DD:00:00:BB",
            "name": "Lee",
            "device_id": "Lee",
            "frequency_mhz": 492.025,
            "online": True,
            "last_seen": f["last_seen"],
        }
        wait_until(lambda: "00:0E:DD:00:00:BB" in json.loads(open(cfg.qlxd.state_file).read())["known_receivers"])

        sim.stop()
        wait_until(lambda: not eng.frequencies()[0]["online"])  # offline, but still known
        assert eng.frequencies()[0]["frequency_mhz"] == 492.025
    finally:
        eng.stop()
        sim.stop()

    # A restart remembers it before the receiver is ever reached again.
    fresh = QLXDEngine(cfg.qlxd)
    assert [(r["name"], r["frequency_mhz"], r["online"]) for r in fresh.frequencies()] == [("Lee", 492.025, False)]
    assert fresh.forget_known("nope") == 0
    assert fresh.forget_known("00:0E:DD:00:00:BB") == 1
    assert QLXDEngine(cfg.qlxd).frequencies() == []


def test_frequencies_endpoint(cfg):
    cfg = _demo(cfg)
    modules = build_modules(cfg)
    app = create_app(cfg, modules)
    try:
        client = app.test_client()
        wait_until(lambda: len(client.get("/api/qlxd/frequencies").get_json()["receivers"]) == 4)
        rx = client.get("/api/qlxd/frequencies").get_json()["receivers"]
        assert [r["frequency_mhz"] for r in rx] == sorted(r["frequency_mhz"] for r in rx)
        assert {r["name"] for r in rx} == {"Lead Vox", "BGV 1", "Pastor HH", "Spare"}
        engine = next(m for m in modules if m.name == "qlxd").engine
        wait_until(lambda: len(engine._known) == 4)  # catalog fills on the next publish tick
        assert client.post("/api/qlxd/known/forget", json={"key": 5}).status_code == 400
        assert client.post("/api/qlxd/known/forget", json={}).get_json()["removed"] == 4
    finally:
        app.extensions["tf_stop"]()
