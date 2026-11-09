"""EQ profiles (vocal/profiles.py) against the simulated TF5: save, recall, copy, undo."""

import pytest
from helpers import make_config, wait_until

from tf_daemon.app import build_modules, create_app
from tf_daemon.vocal.auto import PATHS
from tf_daemon.vocal.profiles import ProfileStore, changes_to, clean_name, summary


@pytest.fixture
def app_for(tmp_path):
    apps = []

    def make():
        cfg = make_config(tf5={"source": "demo"}, vocal={"enabled": True, "profiles_file": str(tmp_path / "p.json")},
                          channels=[{"index": 0, "label": "Pastor"}, {"index": 1, "label": "Lead Vox"}])
        modules = build_modules(cfg)
        app = create_app(cfg, modules)
        apps.append(app)
        tf5 = next(m for m in modules if m.name == "tf5")
        wait_until(lambda: tf5.eq_client.synced)
        return app.test_client(), tf5

    yield make
    for a in apps:
        a.extensions["tf_stop"]()


def raw(tf5, ch):
    return tf5.eq_raw(ch, PATHS)


def test_save_recall_onto_another_channel_and_undo(app_for):
    c, tf5 = app_for()
    before = raw(tf5, 1)
    assert raw(tf5, 0) != before                         # the demo desk: ch1 has a real EQ, ch2 is flat
    assert c.post("/api/vocal/profiles/save", json={"channel": 0, "name": "  Pastor  Smith "}).get_json()["ok"]
    listed = c.get("/api/vocal/profiles").get_json()["profiles"]
    assert [p["name"] for p in listed] == ["Pastor Smith"] and listed[0]["from"] == "Pastor"
    no_confirm = c.post("/api/vocal/profiles/recall", json={"channel": 1, "name": "Pastor Smith"})
    assert no_confirm.status_code == 400
    r = c.post("/api/vocal/profiles/recall", json={"channel": 1, "name": "Pastor Smith", "confirm": True}).get_json()
    assert r["ok"] and r["confirmed"] == r["sent"] > 0
    wait_until(lambda: raw(tf5, 1) == raw(tf5, 0))
    assert c.post("/api/vocal/undo", json={"channel": 1, "confirm": True}).get_json()["ok"]
    wait_until(lambda: raw(tf5, 1) == before)


def test_copy_eq_between_channels(app_for):
    c, tf5 = app_for()
    r = c.post("/api/vocal/profiles/copy", json={"channel": 1, "from": 0, "confirm": True}).get_json()
    assert r["ok"] and "from Pastor" in r["changes"][0]
    wait_until(lambda: raw(tf5, 1) == raw(tf5, 0))
    assert c.post("/api/vocal/profiles/copy", json={"channel": 1, "from": 1, "confirm": True}).status_code == 400


def test_profiles_survive_a_restart_and_can_be_deleted(app_for):
    c, _ = app_for()
    c.post("/api/vocal/profiles/save", json={"channel": 0, "name": "Narrator"})
    c2, _ = app_for()                                  # a new process reading the same file
    assert [p["name"] for p in c2.get("/api/vocal/profiles").get_json()["profiles"]] == ["Narrator"]
    assert c2.post("/api/vocal/profiles/delete", json={"name": "Narrator"}).get_json()["ok"]
    assert c2.get("/api/vocal/profiles").get_json()["profiles"] == []
    assert c2.post("/api/vocal/profiles/delete", json={"name": "Narrator"}).status_code == 400


def test_names_and_helpers():
    assert clean_name("  Lady  Macbeth ") == "Lady Macbeth"
    for bad in ("", "   ", None, "x" * 41):
        with pytest.raises(ValueError):
            clean_name(bad)
    flat = {p: 0 for p in PATHS} | {"PEQ/On": 1}
    assert summary(flat) == "flat"
    cut = flat | {"PEQ/Bank/BandL/Gain": -400, "PEQ/Bank/BandL/Frequency": 2300}
    assert summary(cut) == "L -4.0 @ 230"
    assert changes_to(flat, cut) == [("PEQ/Bank/BandL/Frequency", 2300), ("PEQ/Bank/BandL/Gain", -400)]


def test_a_corrupt_profiles_file_is_ignored(tmp_path):
    p = tmp_path / "p.json"
    p.write_text("{not json")
    assert ProfileStore(p).list() == []
