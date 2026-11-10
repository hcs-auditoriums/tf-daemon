"""A channel's input, 1-knob EQ, gate, comp and sends (tf_daemon/console/strip.py, strip_desk.py,
/api/tf5/strip/<usb>): input / EQ / dynamics read from and written to the desk over the TF Editor
protocol (the simulated desk in demo mode), each value with its lock reason, writes echoed; sends
set on the simulated desk and read back over RCP; with no editor protocol nothing is shown or
written."""

import json

import pytest
from editor_fakes import assert_read_only, wait_until
from helpers import make_config

from tf_daemon.app import build_modules, create_app
from tf_daemon.audio.module import _send_strip
from tf_daemon.console.editor import tree
from tf_daemon.console.editor.guard import WriteNotAllowed
from tf_daemon.console.strip import INF, SPECS, Strip, limit, to_raw, to_value
from tf_daemon.console.strip_desk import DeskStrip, input_port, port_label


@pytest.fixture(scope="module")
def demo():
    cfg = make_config(tf5={"source": "demo"})
    modules = build_modules(cfg)
    tf5 = next(m for m in modules if m.name == "tf5")
    app = create_app(cfg, modules)
    ec = tf5.eq_client
    wait_until(lambda: tf5.client.connected and tf5.client.raw(0, "ToMix/Level", 0) is not None)
    wait_until(lambda: ec.synced and ec.get("PhantomMaster/On", "Setup") is not None)
    yield app.test_client(), tf5
    app.extensions["tf_stop"]()


def put(c, usb, **body):
    return c.put(f"/api/tf5/strip/{usb}", json={"confirm": True, **body})


def spec(section, key):
    return next(s for s in SPECS[section] if s.key == key)


def test_demo_strip_reads_every_section_from_the_desk(demo):
    c, _ = demo
    d = c.get("/api/tf5/strip/0").get_json()
    assert d["source"] == "InCh 1" and d["simulated"]
    for section in ("input", "eq", "gate", "comp"):
        sec = d[section]
        assert sec["available"] and sec["writable"], section
        assert set(sec["values"]) == {s["key"] for s in d["specs"][section]}, section
    inp = d["input"]
    assert inp["values"]["source"] == "INPUT" and inp["port"] == "INPUT 1"
    assert inp["values"]["gain"] == 45 and inp["values"]["phantom_master"] is True  # INPUT 1 as captured
    assert d["gate"]["values"]["type"] == "GATE" and d["comp"]["values"]["knee"] == "SOFT-2"
    assert d["comp"]["values"]["ratio"] == 3 and d["eq"]["values"]["type"] == "BOLD"
    assert inp["locked"] == {"source": "can't be changed from here"}  # the patch isn't a write family
    assert d["gate"]["locked"] == {"type": d["gate"]["locked"]["type"]}  # only the type is shown-only
    sends = d["sends"]
    assert sends["available"] and sends["writable"] and sends["mixes"][0]["name"]


def test_an_edit_is_held_to_the_schema_written_and_echoed(demo):
    c, tf5 = demo
    sim = tf5.simulator.editor
    r = put(c, 1, section="gate", changes={"on": True, "threshold": -99, "attack": 7.4}).get_json()
    assert r == {
        "ok": True,
        "applied": {"on": True, "threshold": -72.0, "attack": 7},
        "confirmed": {"on": True, "threshold": True, "attack": True},
    }
    assert ("Mixing", "InputChannel:1/Gate/Bank/Gate/Threshold", -7200) in sim.all_writes
    assert c.get("/api/tf5/strip/1").get_json()["gate"]["values"]["threshold"] == -72
    assert put(c, 1, section="comp", changes={"ratio": 7}).status_code == 400  # not one of the TF's ratios
    assert put(c, 1, section="comp", changes={"ratio": INF, "knee": "HARD"}).get_json()["ok"]
    assert ("Mixing", "InputChannel:1/Comp/Bank/Compressor/Ratio", 255) in sim.all_writes
    assert put(c, 1, section="comp", changes={"knee": "soft"}).status_code == 400  # not a verified knee
    assert put(c, 1, section="comp", changes={"type": "DUCKING"}).status_code == 400  # types: shown only
    assert put(c, 1, section="input", changes={"phantom": "yes"}).status_code == 400
    assert put(c, 1, section="gate", changes={"nonsense": 1}).status_code == 400
    assert c.put("/api/tf5/strip/1", json={"section": "gate", "changes": {"on": False}}).status_code == 400  # confirm


def test_head_amp_and_phantom_are_written_on_the_input_port(demo):
    c, tf5 = demo
    sim = tf5.simulator.editor
    r = put(c, 2, section="input", changes={"gain": 31.4, "phantom": False, "digital_gain": -3.25}).get_json()
    assert r["ok"] and r["applied"] == {"gain": 31, "phantom": False, "digital_gain": -3.2}
    assert sim.dumps["Processing"].get("BuiltIn/InputPort:2/HeadAmp/Gain") == 3100
    assert sim.dumps["Processing"].get("BuiltIn/InputPort:2/HeadAmp/48V") == 0
    assert sim.mixing.get("InputChannel:2/Input/Gain") == -320
    assert put(c, 2, section="input", changes={"phantom_master": True}).get_json()["confirmed"] == {
        "phantom_master": True
    }


def test_a_change_on_the_desk_reaches_the_strip_and_the_page_push(demo):
    _, tf5 = demo
    sim, strip = tf5.simulator.editor, tf5.strip
    before = strip.version()
    sim.push_change("InputChannel:7/Comp/Bank/OneKnob/Depth", 77)
    sim.push_change("BuiltIn/InputPort:7/HeadAmp/Gain", 5200, func="Processing")
    wait_until(lambda: strip.get(7)["input"]["values"]["gain"] == 52)
    assert strip.version() != before and strip.get(7)["comp"]["values"]["level"] == 77

    class WS:
        def __init__(self):
            self.sent = []

        def send(self, m):
            self.sent.append(json.loads(m))

    ws, view = WS(), [7]
    sent = _send_strip(ws, strip, view, None)
    assert [m["type"] for m in ws.sent] == ["tf5-strip"] and ws.sent[0]["index"] == 7
    sent = _send_strip(ws, strip, view, sent)
    assert len(ws.sent) == 1  # nothing changed: nothing sent
    sim.push_change("InputChannel:7/Gate/On", 1)
    wait_until(lambda: strip.get(7)["gate"]["values"]["on"])
    _send_strip(ws, strip, view, sent)
    assert ws.sent[-1]["gate"]["values"]["on"] is True


def test_a_usb_channel_has_no_head_amp(demo):
    _, tf5 = demo
    desk = tf5.strip.desk
    sec = desk.section("input", 35)  # CH 36: Patch/Select 1 (USB) on the demo desk, as on the auditorium's
    assert sec["values"]["source"] == "USB" and sec["port"] is None
    assert set(sec["absent"]) == {"gain", "phantom"} and "phase" in sec["values"]
    with pytest.raises(ValueError, match="no head amp"):
        desk.put(35, "input", {"gain": 20})
    assert desk.section("gate", 35)["available"]  # the V4.55 tree has a gate on every input channel


def test_a_switched_off_family_locks_and_refuses(console, client_for):
    ec = client_for(console, functions=tree.FUNCTIONS, families=["processing"])
    wait_until(lambda: ec.synced and ec.get("PhantomMaster/On", "Setup") is not None)
    desk = DeskStrip(ec)
    sec = desk.section("input", 0)
    assert sec["locked"]["gain"] == "writing it is off (tf5.headamp_write)"
    assert sec["locked"]["phantom"] == sec["locked"]["phantom_master"] == "writing it is off (tf5.phantom_write)"
    assert "phase" not in sec["locked"]
    with pytest.raises(WriteNotAllowed):
        desk.put(0, "input", {"gain": 20})
    assert desk.put(0, "input", {"phase": True})["confirmed"] == {"phase": True}


def test_read_only_client_writes_nothing(console, client_for):
    ec = client_for(console, functions=tree.FUNCTIONS)
    wait_until(lambda: ec.synced)
    desk = DeskStrip(ec)
    assert not desk.section("gate", 0)["writable"]
    with pytest.raises(WriteNotAllowed):
        desk.put(0, "gate", {"on": True})
    assert_read_only(console)


def test_without_the_editor_protocol_nothing_is_shown_or_written():
    strip = Strip([("InCh", 0), ("St", 0)], rcp=None)
    d = strip.get(0)
    assert not d["gate"]["available"] and "tf5.eq" in d["gate"]["reason"] and not d["sends"]["available"]
    with pytest.raises(PermissionError):
        strip.put(0, "comp", {"on": True})
    assert not strip.get(1)["input"]["available"]  # the stereo bus has no head amp


def test_the_channel_to_port_mapping():
    assert input_port(0, 4) == "BuiltIn/InputPort:4" and port_label(input_port(0, 4)) == "INPUT 5"
    assert input_port(2, 0) == "Slot/InputPort:0" and port_label("Slot/InputPort:0") == "SLOT 1"
    assert input_port(1, 4) is None and input_port(None, 4) is None  # USB / unknown: no head amp
    assert input_port(0, 35) is None  # no local jack for CH 36


def test_specs_come_from_the_desk_schema():
    for section in ("input", "eq", "gate", "comp"):
        for s in SPECS[section]:
            if s.kind == "number":
                t = tree.spec(s.func, {"channel": "InputChannel:0/", "port": "BuiltIn/InputPort:0/"}[s.at] + s.path)
                assert s.hi == t.max / (t.scaling or 1) and s.default == t.default / (t.scaling or 1), s.key
                assert s.inf or s.lo == t.min / (t.scaling or 1), s.key
            if s.kind == "choice":
                assert s.default is None or s.default in s.choices, (section, s.key)
    assert spec("input", "gain").lo == -6 and spec("input", "gain").hi == 66
    assert spec("gate", "hold").hi == 1960 and spec("gate", "range").inf
    assert spec("comp", "knee").choices == ("HARD", "SOFT-2")  # the guard's verified values


def test_raw_and_value_convert_in_one_place():
    hold, rng, ratio, src = spec("gate", "hold"), spec("gate", "range"), spec("comp", "ratio"), spec("input", "source")
    assert to_value(hold, 2360) == 2.36 and to_raw(hold, limit(hold, 2.364)) == 2360
    assert to_value(rng, -32768) is None and to_raw(rng, limit(rng, None)) == -32768
    assert to_value(rng, -5600) == -56 and to_raw(rng, limit(rng, -99)) == -7200  # held to the slider's floor
    assert to_value(ratio, 255) == INF and to_value(ratio, 12) == 1.2 and to_raw(ratio, 2.5) == 25
    assert to_value(src, 2) == "SLOT" and to_raw(src, "USB") == 1
    with pytest.raises(ValueError):
        limit(spec("gate", "type"), "GATE")


def test_a_send_is_set_on_the_simulated_desk(demo):
    c, tf5 = demo
    assert put(c, 2, section="sends", mix=3, changes={"level": -12.5, "on": False, "pre": True}).get_json()["ok"]
    wait_until(lambda: tf5.client.raw(2, "ToMix/Level", 3) == "-1250" and tf5.client.raw(2, "ToMix/On", 3) == "0")
    row = next(m for m in c.get("/api/tf5/strip/2").get_json()["sends"]["mixes"] if m["mix"] == 3)
    assert row["level"] == -12.5 and row["on"] is False and row["pre"] is True
    assert put(c, 2, section="sends", mix=3, changes={"level": None}).get_json()["ok"]  # -inf
    wait_until(lambda: tf5.client.raw(2, "ToMix/Level", 3) == "-32768")
    assert put(c, 2, section="sends", mix=99, changes={"level": 0}).status_code == 400
