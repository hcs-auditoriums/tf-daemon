"""A channel's gain, gate, comp and sends (tf_daemon/console/strip.py, /api/tf5/strip/<usb>):
simulated and editable in demo mode, sends set on the simulated desk and read back over RCP;
with a real desk nothing that isn't read yet is shown or written."""

import pytest
from editor_fakes import wait_until
from helpers import make_config

from tf_daemon.app import build_modules, create_app
from tf_daemon.console.strip import NOT_READ, SPECS, Strip


@pytest.fixture(scope="module")
def demo():
    cfg = make_config(tf5={"source": "demo"})
    modules = build_modules(cfg)
    tf5 = next(m for m in modules if m.name == "tf5")
    app = create_app(cfg, modules)
    wait_until(lambda: tf5.client.connected and tf5.client.raw(0, "ToMix/Level", 0) is not None)
    yield app.test_client(), tf5
    app.extensions["tf_stop"]()


def put(c, usb, **body):
    return c.put(f"/api/tf5/strip/{usb}", json={"confirm": True, **body})


def test_demo_strip_has_every_section(demo):
    c, _ = demo
    d = c.get("/api/tf5/strip/0").get_json()
    assert d["source"] == "InCh 1" and d["simulated"]
    for section in ("input", "gate", "comp"):
        assert d[section]["available"] and set(d[section]["values"]) == {s["key"] for s in d["specs"][section]}
    sends = d["sends"]
    assert sends["available"] and sends["writable"] and sends["mixes"][0]["name"]


def test_values_are_held_to_their_specs(demo):
    c, _ = demo
    r = put(c, 1, section="gate", changes={"on": True, "threshold": -99, "attack": 7.4})
    assert r.get_json() == {"ok": True, "applied": {"on": True, "threshold": -72.0, "attack": 7.0}}
    assert c.get("/api/tf5/strip/1").get_json()["gate"]["values"]["threshold"] == -72
    assert put(c, 1, section="comp", changes={"ratio": 7}).status_code == 400  # not one of the TF's ratios
    assert put(c, 1, section="comp", changes={"ratio": "inf", "knee": "hard"}).get_json()["ok"]
    assert put(c, 1, section="input", changes={"phantom": "yes"}).status_code == 400
    assert put(c, 1, section="gate", changes={"nonsense": 1}).status_code == 400
    assert c.put("/api/tf5/strip/1", json={"section": "gate", "changes": {"on": False}}).status_code == 400  # confirm


def test_a_send_is_set_on_the_simulated_desk(demo):
    c, tf5 = demo
    assert put(c, 2, section="sends", mix=3, changes={"level": -12.5, "on": False, "pre": True}).get_json()["ok"]
    wait_until(lambda: tf5.client.raw(2, "ToMix/Level", 3) == "-1250" and tf5.client.raw(2, "ToMix/On", 3) == "0")
    row = next(m for m in c.get("/api/tf5/strip/2").get_json()["sends"]["mixes"] if m["mix"] == 3)
    assert row["level"] == -12.5 and row["on"] is False and row["pre"] is True
    assert put(c, 2, section="sends", mix=3, changes={"level": None}).get_json()["ok"]  # -inf
    wait_until(lambda: tf5.client.raw(2, "ToMix/Level", 3) == "-32768")
    assert put(c, 2, section="sends", mix=99, changes={"level": 0}).status_code == 400


def test_with_a_real_desk_nothing_unread_is_shown_or_written():
    strip = Strip([("InCh", 0), ("InCh", 35), ("St", 0)], rcp=None)
    d = strip.get(0)
    assert d["gate"] == {"available": False, "reason": NOT_READ} and not d["sends"]["available"]
    with pytest.raises(PermissionError):
        strip.put(0, "comp", {"on": True})
    assert "isn't on this channel" in strip.get(1)["gate"]["reason"]  # no gate on CH 33-40
    assert not strip.get(2)["input"]["available"]  # the stereo bus has no head amp


def test_specs_are_sane():
    for section, specs in SPECS.items():
        for s in specs:
            if s.kind == "number" and s.default is not None:
                assert s.lo <= s.default <= s.hi, (section, s.key)
            if s.kind == "choice":
                assert s.default in s.choices, (section, s.key)
