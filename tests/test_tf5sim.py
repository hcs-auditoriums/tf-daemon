"""The simulated TF5 (tf_daemon.console.sim) and demo mode (--tf5-source demo / --demo)."""

import pytest
from editor_fakes import wait_until
from helpers import make_config

from tf_daemon.app import build_modules, create_app
from tf_daemon.config import ConfigError
from tf_daemon.console.patch import default_usb_patch, parse_patch_entry
from tf_daemon.console.rcp import TF5Client
from tf_daemon.console.sim import DEMO_INPUTS, RCP_PREFIX, SimConsole, SimRCP

PATCH = [parse_patch_entry(e) for e in default_usb_patch()]


def test_demo_mode_runs_the_tf5_module_against_the_simulator():
    cfg = make_config(tf5={"source": "demo"})
    assert cfg.tf5.host == ""  # demo needs no console address, and turns EQ read / write on
    modules = build_modules(cfg)
    by_name = {m.name: m for m in modules}
    assert list(by_name) == ["audio", "tf5"]  # the simulator belongs to the tf5 module
    app = create_app(cfg, modules)
    try:
        tf5 = by_name["tf5"]
        wait_until(lambda: tf5.client.connected and tf5.eq_client.synced)
        wait_until(lambda: tf5.client.names().get(32) == "ST L")
        assert tf5.client.names()[1] == DEMO_INPUTS[1][0]
        assert tf5.eq_writable
        eq = app.test_client().get("/api/tf5/eq/0").get_json()
        assert eq["synced"] and eq["eq"]["hpf"]["freq"] == 200  # ch1: the EQ captured from a real TF5

        results = tf5.write_eq(0, [("PEQ/Bank/Band:0/Gain", -300)])
        assert results == [{"path": "PEQ/Bank/Band:0/Gain", "value": -300, "confirmed": True, "actual": -300}]
        assert tf5.simulator.editor.writes == [("InputChannel:0/PEQ/Bank/Band:0/Gain", -300)]
        assert tf5.eq(0)["eq"]["bands"][1]["gain"] == -3.0
    finally:
        app.extensions["tf_stop"]()


def test_tf5_source_choices():
    assert [m.name for m in build_modules(make_config())] == ["audio"]
    # A real console needs its address; without one the module stays off.
    assert [m.name for m in build_modules(make_config(tf5={"source": "device"}))] == ["audio"]
    assert [m.name for m in build_modules(make_config(tf5={"source": "off", "host": "10.0.0.5"}))] == ["audio"]
    with pytest.raises(ConfigError, match="tf5.source"):
        make_config(tf5={"source": "console"})


def test_desk_changes_are_pushed():
    sim = SimRCP(drift_s=0.05)
    client = TF5Client("127.0.0.1", sim.port, PATCH, 30.0, lambda names: None)
    client.start()
    try:
        wait_until(lambda: client.gains().get(0) is not None)
        start = dict(client.gains())
        wait_until(lambda: client.gains() != start)
    finally:
        client.stop()
        sim.close()


def test_rcp_replies():
    sim = SimRCP()
    try:
        name = RCP_PREFIX["InCh"] + "Label/Name"
        assert sim.reply(f"get {name} 1 0") == f'OK get {name} 1 0 "{DEMO_INPUTS[1][0]}"'
        assert sim.reply(f"get {name} 99 0") == f"ERROR get {name} InvalidArgument"
        assert sim.reply(f"get {name} x 0") == "ERROR get UnknownCommand"
        assert sim.reply("prminfo 0").startswith(f'OK prminfo 0 "{name}" 40 0 ')
        assert sim.reply("prminfo 999") == "ERROR prminfo 999 UnknownIndex"
        assert sim.reply(f'set {name} 0 0 "x"') == "ERROR set UnknownCommand"  # never changes the "desk"
    finally:
        sim.close()


def test_sim_console_listens_on_the_given_ports():
    sim = SimConsole(drift_s=0)
    try:
        assert sim.rcp_port and sim.eq_port and sim.rcp_port != sim.eq_port
        assert sim.status()["eq_writes"] == 0
    finally:
        sim.close()
