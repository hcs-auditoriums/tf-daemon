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


def test_the_page_gets_the_desks_eq_ranges(rig):
    c, _ = rig()  # the TF5's: HPF 20-600 Hz, bands 20 Hz - 20 kHz, +-18 dB, Q 0.1-10
    assert c.get("/api/tf5/eq/0").get_json()["limits"] == {
        "hpf_freq": [20, 600],
        "freq": [20, 20000],
        "gain": [-18, 18],
        "q": [0.1, 10],
    }


def test_a_person_edit_reaches_the_desk(rig):
    c, tf5 = rig()
    assert c.get("/api/tf5/eq/0").get_json()["writable"] is True
    r = c.put(
        "/api/tf5/eq/0", json={"changes": [[GAIN, -450], ["PEQ/Bank/Band:1/Frequency", 31500]], "confirm": True}
    ).get_json()
    assert r["ok"] and all(x["confirmed"] for x in r["results"])
    wait_until(lambda: tf5.eq_raw(0, [GAIN])[GAIN] == -450)
    band = c.get("/api/tf5/eq/0").get_json()["eq"]["bands"][2]
    assert (band["label"], band["gain"], band["freq"]) == ("HM", -4.5, 3150.0)


@pytest.mark.parametrize(
    "body, error",
    [
        ({"changes": [[GAIN, -450]]}, "confirm"),
        ({"changes": [[GAIN, "-450"]], "confirm": True}, "changes"),
        ({"changes": [], "confirm": True}, "changes"),
        ({"changes": [[GAIN, 5000]], "confirm": True}, "outside"),  # past +-18 dB
        ({"changes": [["Fader/Level", 0]], "confirm": True}, "writable"),  # not an EQ parameter
    ],
)
def test_refused_edits(rig, body, error):
    c, tf5 = rig()
    before = tf5.eq_raw(0, [GAIN])
    r = c.put("/api/tf5/eq/0", json=body)
    assert r.status_code == 400 and error in r.get_json()["error"]
    assert tf5.eq_raw(0, [GAIN]) == before


def test_no_editing_without_eq_write(rig):
    c, tf5 = rig()
    tf5.eq_client.writes = False  # as with tf5.eq_write = false
    assert c.get("/api/tf5/eq/0").get_json()["writable"] is False
    r = c.put("/api/tf5/eq/0", json={"changes": [[GAIN, -450]], "confirm": True})
    assert r.status_code == 400 and "eq_write" in r.get_json()["error"]


# ---------------------------------------------------------------- the sidebar's fader and ON (PUT /api/tf5/fader)


def test_a_person_sets_the_fader_and_on_switch(rig):
    c, tf5 = rig()
    assert c.get("/api/tf5/eq/0").get_json()["fader_writable"] is True  # demo: tf5.fader_write on
    r = c.put("/api/tf5/fader/0", json={"level": -1250, "confirm": True}).get_json()
    assert r["ok"] and all(x["confirmed"] for x in r["results"])
    wait_until(lambda: tf5.fader(0)["level"] == -1250 and tf5.fader(0)["rcp_level"] == -1250)  # RCP follows
    r = c.put("/api/tf5/fader/0", json={"on": 0, "confirm": True}).get_json()
    assert r["ok"]
    wait_until(lambda: tf5.fader(0)["on"] == 0 and tf5.fader(0)["rcp_on"] == 0)
    assert c.put("/api/tf5/fader/0", json={"on": 1, "level": -32768, "confirm": True}).get_json()["ok"]
    wait_until(lambda: tf5.fader(0) == {"level": -32768, "on": 1, "rcp_level": -32768, "rcp_on": 1})


@pytest.mark.parametrize(
    "usb, body, error",
    [
        (0, {"level": -1000}, "confirm"),
        (0, {"confirm": True}, "level"),
        (0, {"level": "-10", "confirm": True}, "level"),
        (0, {"on": 2, "confirm": True}, "level"),
        (0, {"on": True, "confirm": True}, "level"),
        (0, {"level": 1100, "confirm": True}, "outside"),  # above +10 dB
        (32, {"level": 0, "confirm": True}, "input channel"),  # the stereo bus: never
    ],
)
def test_refused_fader_sets(rig, usb, body, error):
    c, tf5 = rig()
    before = tf5.fader(0)
    r = c.put(f"/api/tf5/fader/{usb}", json=body)
    assert r.status_code == 400 and error in r.get_json()["error"], r.get_json()
    assert tf5.fader(0) == before


def test_no_fader_from_the_page_without_fader_write(rig):
    c, tf5 = rig()
    tf5.eq_client.fader_writes = False  # as with tf5.fader_write = false
    assert c.get("/api/tf5/eq/0").get_json()["fader_writable"] is False
    r = c.put("/api/tf5/fader/0", json={"level": -1000, "confirm": True})
    assert r.status_code == 400 and "fader_write" in r.get_json()["error"]
