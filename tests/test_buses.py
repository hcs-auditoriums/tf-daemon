"""The Mix page's desk data (console/buses.py, busproc.py) and the SEND tab's desk-backed sends
(console/sends.py), against the demo's simulated desk: read over the TF Editor protocol, written
only with confirm, within the write families, stereo strips on both halves, the GEQ's 12-band rule."""

from types import SimpleNamespace

import pytest
from editor_fakes import wait_until
from helpers import make_config

from tf_daemon.app import build_modules, create_app
from tf_daemon.console.buses import MASTERS, Buses, aux_bus, aux_halves, aux_label, aux_send
from tf_daemon.console.editor.guard import WriteNotAllowed
from tf_daemon.dsp.ring import GEQ_BANDS


@pytest.fixture(scope="module")
def demo():
    cfg = make_config(tf5={"source": "demo"})
    modules = build_modules(cfg)
    tf5 = next(m for m in modules if m.name == "tf5")
    app = create_app(cfg, modules)
    wait_until(lambda: tf5.eq_client.synced and "Volatile" in tf5.eq_client.status()["functions"])
    wait_until(lambda: all(f["synced"] for f in tf5.eq_client.status()["functions"].values()))
    yield app.test_client(), tf5
    app.extensions["tf_stop"]()


def put(c, path, **body):
    return c.put(f"/api/tf5/mix/{path}", json={"confirm": True, **body})


def writes_since(tf5, n):
    return tf5.simulator.editor.all_writes[n:]


def test_aux_numbering_is_one_helper():
    assert [aux_bus(a) for a in (0, 7, 8, 9, 19)] == ["Mix:0", "Mix:7", "StMix:0", "StMix:1", "StMix:11"]
    assert [aux_send(a) for a in (0, 8, 19)] == ["ToMix:0", "ToStMix:0", "ToStMix:11"]
    assert aux_halves(3) == [3] and aux_halves(8) == aux_halves(9) == [8, 9] and aux_halves(19) == [18, 19]
    assert aux_label(9) == "AUX 9/10" and aux_label(0) == "AUX 1"
    with pytest.raises(ValueError):
        aux_bus(20)
    assert [m.key for m in MASTERS][:14] == [f"aux{a}" for a in (1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 13, 15, 17, 19)]


def test_the_mix_snapshot_reads_the_desk(demo):
    c, _ = demo
    d = c.get("/api/tf5/mix").get_json()
    assert d["synced"] and d["writable"] == {"mix": True, "processing": True} and d["simulated"]
    m = {s["key"]: s for s in d["masters"]}
    assert m["aux1"]["name"] == "Wedge DS" and m["aux9"]["name"] == "Aux 9/10" and m["aux9"]["stereo"]
    assert m["st"]["geq"] and m["aux1"]["geq"] and not m["aux9"]["geq"] and m["mono"]["balance"] is None
    assert m["dca1"]["name"] == "VOX" and len([s for s in d["masters"] if s["group"] == "dca"]) == 8
    src = {s["key"]: s for s in d["sources"]}
    assert src["in1"]["dca"][0] == 1 and src["in6"]["dca"][1] == 1 and src["in40"]["dca"] == [0] * 8
    assert [g["name"] for g in d["mute"]][:2] == ["INPUT", "FX"] and d["mute_map"] == {
        "channel": [0, 1, 2, 3],
        "fx": [4, 5],
        "verified": False,
    }
    assert len(d["fx"]) == 8 and d["fx"][0]["plugin"] == "Echo"
    assert c.get(f"/api/tf5/mix?since={d['version']}").get_json() == {"version": d["version"], "unchanged": True}


def test_a_stereo_master_is_set_on_both_halves(demo):
    c, tf5 = demo
    n = len(tf5.simulator.editor.all_writes)
    r = put(c, "strip/aux10", level=-1000, on=0, balance=-20, cue=1).get_json()
    assert r["ok"] and all(x["confirmed"] for x in r["results"])
    got = set(writes_since(tf5, n))
    for half in ("StMix:0", "StMix:1"):
        assert {("Mixing", f"{half}/Fader/Level", -1000), ("Mixing", f"{half}/Fader/On", 0)} <= got
        assert ("Mixing", f"{half}/Output/Balance", -20) in got and ("Volatile", f"Cue/{half}/On", 1) in got
    m = {s["key"]: s for s in c.get("/api/tf5/mix").get_json()["masters"]}
    assert m["aux9"]["level"] == -1000 and m["aux9"]["on"] == 0 and m["aux9"]["cue"] == 1
    assert put(c, "strip/aux10", on=1, cue=0).get_json()["ok"]


def test_dca_mute_and_cue_writes(demo):
    c, tf5 = demo
    n = len(tf5.simulator.editor.all_writes)
    assert put(c, "strip/dca2", level=-500).get_json()["ok"]
    assert put(c, "assign", source="in12", dca=3, on=1).get_json()["ok"]
    assert put(c, "assign", source="stin2", mute=1, on=1).get_json()["ok"]
    assert put(c, "assign", source="ins3", mute=1, on=1).get_json()["ok"]
    assert put(c, "mute/5", on=1).get_json()["ok"]
    assert put(c, "strip/in3", cue=1).get_json()["ok"]
    assert writes_since(tf5, n) == [
        ("Mixing", "DCA:1/Fader/Level", -500),
        ("Mixing", "InputChannel:11/DCAGroup:3/Assign", 1),
        ("Mixing", "StInChannel:1/MuteGroup:1/Assign", 1),
        ("FX", "InsFX:2/MuteGroup:1/Assign", 1),
        ("Setup", "MuteMaster:5/On", 1),
        ("Volatile", "Cue/InputChannel:2/On", 1),
    ]
    d = c.get("/api/tf5/mix").get_json()
    assert d["mute"][5]["on"] == 1 and {s["key"]: s for s in d["sources"]}["in12"]["dca"][3] == 1


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("strip/mono", {"balance": 3}),  # MONO has no balance
        ("strip/in3", {"level": 0}),  # input faders: the audio page (fader family)
        ("strip/nope", {"on": 1}),
        ("strip/aux1", {"level": 2000}),  # past +10 dB
        ("assign", {"source": "in1", "dca": 8, "on": 1}),
        ("assign", {"source": "fx1", "dca": 0, "on": 1}),
        ("mute/6", {"on": 1}),
        ("mute/0", {"on": 2}),
    ],
)
def test_bad_changes_are_refused(demo, path, body):
    c, tf5 = demo
    n = len(tf5.simulator.editor.all_writes)
    assert put(c, path, **body).status_code == 400
    assert c.put(f"/api/tf5/mix/{path}", json=body).status_code == 400  # no confirm
    assert writes_since(tf5, n) == []


def test_geq_reads_writes_and_keeps_the_12_band_rule(demo):
    c, tf5 = demo
    g = c.get("/api/tf5/mix/geq/aux1").get_json()
    assert [b["freq"] for b in g["bands"]] == GEQ_BANDS and g["adjusted"] == 3 and g["max_adjusted"] == 12
    assert g["bands"][9]["gain"] == -3.0
    assert put(c, "geq/aux1", gains={str(b): -100 for b in range(19, 29)}).get_json()["ok"]  # 12 now
    n = len(tf5.simulator.editor.all_writes)
    r = put(c, "geq/aux1", gains={"0": 200})
    assert r.status_code == 400 and "12" in r.get_json()["error"] and writes_since(tf5, n) == []
    assert put(c, "geq/aux1", gains={"0": 200, "28": 0}).get_json()["ok"]  # one out, one in: still 12
    assert c.get("/api/tf5/mix/geq/aux1").get_json()["adjusted"] == 12
    assert put(c, "geq/aux1", gains={str(b): 0 for b in range(31)}).get_json()["ok"]
    assert c.get("/api/tf5/mix/geq/aux9").status_code == 404  # AUX 9-20 have no GEQ
    n = len(tf5.simulator.editor.all_writes)
    assert put(c, "geq/st", lr_link=1, gains={"12": -400}).get_json()["ok"]  # linked: both sides
    assert {w[1] for w in writes_since(tf5, n)} >= {"Stereo:0/GEQ/Band:12/Gain", "Stereo:1/GEQ/Band:12/Gain"}
    n = len(tf5.simulator.editor.all_writes)
    assert put(c, "geq/st", lr_link=0, side=1, gains={"12": 0}).get_json()["ok"]  # unlinked: that side
    assert [w[1] for w in writes_since(tf5, n)] == [
        "Stereo:0/GEQ/LRLink",
        "Stereo:1/GEQ/LRLink",
        "Stereo:1/GEQ/Band:12/Gain",
    ]


def test_bus_peq_is_read_with_its_curve_and_set_on_both_halves(demo):
    c, tf5 = demo
    st = c.get("/api/tf5/mix/eq/st?side=1").get_json()
    assert st["eq"]["bands"][0]["type"] == "hpf" and st["halves_differ"] and len(st["curve"]) == len(st["freqs"])
    mono = c.get("/api/tf5/mix/eq/mono").get_json()
    assert mono["eq"]["lpf"] is not None and [b["label"] for b in mono["eq"]["bands"]] == ["L", "H"]
    mtx = c.get("/api/tf5/mix/eq/mtx1").get_json()
    assert [b["label"] for b in mtx["eq"]["bands"]] == ["L", "LM", "HM", "H"]
    n = len(tf5.simulator.editor.all_writes)
    assert put(c, "eq/aux9", changes=[["Bank/Band:0/Gain", -450]]).get_json()["ok"]
    assert [w[1:] for w in writes_since(tf5, n)] == [
        ("StMix:0/PEQ/Bank/Band:0/Gain", -450),
        ("StMix:1/PEQ/Bank/Band:0/Gain", -450),
    ]
    assert c.get("/api/tf5/mix/eq/aux9").get_json()["eq"]["bands"][1]["gain"] == -4.5
    assert put(c, "eq/aux9", changes=[["Bank/Band:0/Gain", 5000]]).status_code == 400  # past +18 dB
    assert put(c, "eq/dca1", changes=[["On", 1]]).status_code == 400
    assert c.get("/api/tf5/mix/eq/dca1").status_code == 404


def test_the_send_tab_reads_and_writes_the_desk(demo):
    c, tf5 = demo
    sends = c.get("/api/tf5/strip/2").get_json()["sends"]
    assert sends["source"] == "editor" and sends["pre_pan"] and len(sends["mixes"]) == 23
    rows = {r["mix"]: r for r in sends["mixes"]}
    assert rows[8]["name"] == rows[9]["name"] == "Aux 9/10" and rows[20]["kind"] == "fx" and rows[22]["label"] == "MONO"
    n = len(tf5.simulator.editor.all_writes)
    body = {"section": "sends", "mix": 9, "changes": {"pan": -30, "pre": False, "level": -6}, "confirm": True}
    assert c.put("/api/tf5/strip/2", json=body).get_json()["ok"]
    got = set(writes_since(tf5, n))
    for s in ("ToStMix:0", "ToStMix:1"):
        assert {(f"InputChannel:2/{s}/Pan", -30), (f"InputChannel:2/{s}/Pre", 0)} <= {w[1:] for w in got}
    wait_until(lambda: tf5.client.raw(2, "ToMix/Level", 8) == "-600")  # the desk's RCP side sees it too
    rows = {r["mix"]: r for r in c.get("/api/tf5/strip/2").get_json()["sends"]["mixes"]}
    assert rows[8]["pan"] == rows[9]["pan"] == -30 and rows[9]["pre"] is False and rows[8]["level"] == -6
    fx = {"section": "sends", "mix": 21, "changes": {"pre": True}, "confirm": True}
    assert c.put("/api/tf5/strip/2", json=fx).get_json()["ok"]
    mono = {"section": "sends", "mix": 22, "changes": {"pre": True}, "confirm": True}
    assert c.put("/api/tf5/strip/2", json=mono).status_code == 400  # MONO has no pre / post


def test_nothing_is_written_without_the_mix_family(console, client_for):
    ec = client_for(console)  # read-only
    wait_until(lambda: ec.synced)
    desk = SimpleNamespace(eq_client=ec, write_params=lambda func, changes: ec.write(changes, func=func))
    buses = Buses(desk)
    assert not buses.snapshot()["writable"]["mix"]
    with pytest.raises(PermissionError):
        buses.set_mute(0, 1)
    ec2 = client_for(console, families=["processing"])
    wait_until(lambda: ec2.synced)
    desk2 = SimpleNamespace(eq_client=ec2, write_params=lambda func, changes: ec2.write(changes, func=func))
    with pytest.raises(WriteNotAllowed):
        Buses(desk2).set_strip("aux1", {"level": 0})
    assert console.all_writes == []
