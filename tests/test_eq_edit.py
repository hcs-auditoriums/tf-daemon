"""Editing the console's EQ from the page: PUT /api/tf5/eq/<usb> against the simulated TF5."""

import pytest
from helpers import make_config, wait_until

from tf_daemon.app import build_modules, create_app

GAIN = "PEQ/Bank/Band:1/Gain"


@pytest.fixture
def rig():
    apps = []

    def make():
        cfg = make_config(tf5={"source": "demo"}, channels=[{"index": 0, "label": "Pastor"}])
        modules = build_modules(cfg)
        app = create_app(cfg, modules)
        apps.append(app)
        tf5 = next(m for m in modules if m.name == "tf5")
        wait_until(lambda: tf5.eq_client.synced)
        return app.test_client(), tf5

    yield make
    for a in apps:
        a.extensions["tf_stop"]()


def test_a_person_edit_reaches_the_desk(rig):
    c, tf5 = rig()
    assert c.get("/api/tf5/eq/0").get_json()["writable"] is True
    r = c.put("/api/tf5/eq/0", json={"changes": [[GAIN, -450], ["PEQ/Bank/Band:1/Frequency", 31500]],
                                      "confirm": True}).get_json()
    assert r["ok"] and all(x["confirmed"] for x in r["results"])
    wait_until(lambda: tf5.eq_raw(0, [GAIN])[GAIN] == -450)
    band = c.get("/api/tf5/eq/0").get_json()["eq"]["bands"][2]
    assert (band["label"], band["gain"], band["freq"]) == ("HM", -4.5, 3150.0)


@pytest.mark.parametrize("body, error", [
    ({"changes": [[GAIN, -450]]}, "confirm"),
    ({"changes": [[GAIN, "-450"]], "confirm": True}, "changes"),
    ({"changes": [], "confirm": True}, "changes"),
    ({"changes": [[GAIN, 5000]], "confirm": True}, "outside"),              # past +-18 dB
    ({"changes": [["Fader/Level", 0]], "confirm": True}, "writable"),       # not an EQ parameter
])
def test_refused_edits(rig, body, error):
    c, tf5 = rig()
    before = tf5.eq_raw(0, [GAIN])
    r = c.put("/api/tf5/eq/0", json=body)
    assert r.status_code == 400 and error in r.get_json()["error"]
    assert tf5.eq_raw(0, [GAIN]) == before


def test_no_editing_without_eq_write(rig):
    c, tf5 = rig()
    tf5.eq_client.writes = False          # as with tf5.eq_write = false
    assert c.get("/api/tf5/eq/0").get_json()["writable"] is False
    r = c.put("/api/tf5/eq/0", json={"changes": [[GAIN, -450]], "confirm": True})
    assert r.status_code == 400 and "eq_write" in r.get_json()["error"]
