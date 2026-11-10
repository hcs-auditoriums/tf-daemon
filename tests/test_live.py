"""End-to-end checks against a running server with simulated hardware.

tests/conftest.py starts a private demo server for these (``--demo --config
config.dev.toml``) and sets TF_DAEMON_URL; set it yourself to use another server
started with ``--demo``. Read-only: nothing here changes the server.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

import pytest

URL = os.environ.get("TF_DAEMON_URL", "").rstrip("/")

pytestmark = pytest.mark.skipif(not URL, reason="TF_DAEMON_URL not set (no live server)")


def get(path: str):
    with urllib.request.urlopen(URL + path, timeout=5) as r:
        body = r.read()
        return r.status, (json.loads(body) if r.headers.get_content_type() == "application/json" else body)


def wait_for(path: str, pred, timeout: float = 15.0):
    """The server may be restarting after a code change: retry until it answers ``pred``."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            _, last = get(path)
            if pred(last):
                return last
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last = e
        time.sleep(0.2)  # poll
    raise AssertionError(f"{path} never satisfied the check; last: {str(last)[:300]}")


@pytest.fixture(scope="module")
def status():
    def all_up(s):
        m = s["modules"]
        return m["audio"]["frames"] > 0 and m["rf"]["connected"] and m["qlxd"]["online"] >= 4 and m["tf5"]["connected"]

    return wait_for("/api/status", all_up)


def test_every_simulated_device_is_up(status):
    m = status["modules"]
    assert m["audio"]["source"] == "stage" and m["audio"]["skipped_hops"] == 0
    assert m["rf"]["device"] == "demo"
    assert m["qlxd"]["receivers"] == m["qlxd"]["online"] == 4


@pytest.mark.parametrize("page", ["/", "/rf", "/qlxd", "/mix", "/desk", "/config"])
def test_pages_load(status, page):
    code, body = get(page)
    assert code == 200 and b"<html" in body.lower()


def test_channel_names_come_from_the_simulated_console(status):
    chans = wait_for("/api/audio/meta", lambda m: len(m["channels"]) == 34)["channels"]
    labels = {c["index"]: c["label"] for c in chans}
    assert labels[0] == "Pastor" and labels[32] == "Main L"  # config labels win
    assert labels[2] == "BGV 1"  # read from the console


def test_the_desks_buses_dcas_and_geq_are_read(status):
    """The Mix page's data (console/buses.py, busproc.py) and the SEND tab's sends from the desk."""
    mix = wait_for("/api/tf5/mix", lambda d: d["synced"] and d["mute"][0]["name"] == "INPUT")
    assert len(mix["masters"]) == 28 and mix["writable"]["mix"] and len(mix["sources"]) == 48
    assert len(get("/api/tf5/mix/geq/st")[1]["bands"]) == 31 and get("/api/tf5/mix/eq/mtx1")[1]["eq"]
    assert wait_for("/api/tf5/strip/1", lambda d: d["sends"].get("source") == "editor")["sends"]["pre_pan"]


def test_desk_coverage_is_served(status):
    from tf_daemon.console.coverage import FEATURES

    assert [f["key"] for f in get("/api/tf5/coverage")[1]["features"]] == [f.key for f in FEATURES]


def test_console_eq_is_read(status):
    eq = wait_for("/api/tf5/eq/0", lambda e: e.get("synced"))
    assert eq["connected"] and eq["eq"]["hpf"]["freq"] > 0 and len(eq["curve"]) > 0


def test_desk_meters_are_read(status):
    m = wait_for("/api/tf5/meters", lambda m: m["scale"] == "dB" and m["levels"][1] is not None)
    assert len(m["levels"]) == 34 and -60 < m["levels"][1] <= 0  # "Lead Vox" on the simulated desk
    assert m["status"]["meters"]["MIXER:Current/InCh/PreFader"]["state"] == "started"


def test_vocal_assist_can_write_to_the_simulated_desk(status):
    _, s = get("/api/vocal/status")
    assert s["enabled"] and s["can_write"]


def test_ring_out_is_offered_with_the_simulated_desks_faders(status):
    _, s = get("/api/vocal/status")
    assert s["ringout_allowed"] and {m["id"] for m in s["mic_types"]} >= {"wig", "lav"}
    _, ro = get("/api/vocal/ringout")
    assert ro["allowed"] and not ro["running"]  # read-only: never started here
    _, tf5 = get("/api/tf5/status")
    assert tf5["eq"]["fader_writes"]


def test_any_desk_parameter_is_read(status):
    """Every function of the simulated desk syncs; /api/tf5/param reads any parameter, with its schema."""
    wait_for("/api/tf5/status", lambda s: all(f["synced"] for f in s["eq"]["functions"].values()))
    _, dca = get("/api/tf5/param?func=Mixing&path=DCA:0/Fader/Level")
    assert isinstance(dca["value"], int) and dca["spec"]["min"] == -32768 and dca["family"] == "mix"
    _, ha = get("/api/tf5/param?func=Processing&path=BuiltIn/InputPort:0/HeadAmp/Gain")
    assert ha["value"] == 4500 and ha["writable"]  # demo mode: every write family is on
    _, scene = get("/api/tf5/param?func=Setup&path=Scene/LastScene")
    assert isinstance(scene["value"], str) and scene["spec"]["type"] == "str"
    _, tf5 = get("/api/tf5/status")
    assert tf5["eq"]["scene_recall"] and "phantom" in tf5["eq"]["write_families"]


def test_desk_page_reads_scenes_recorder_fx_and_delay(status):
    """The Desk page's reads on the simulated desk (console/desk.py); read-only here."""
    scenes = wait_for("/api/tf5/scenes", lambda d: d["synced"])
    assert {"A01", "B00"} <= {s["number"] for s in scenes["scenes"]} and scenes["recall"]["available"]
    assert not scenes["store"]["available"] and isinstance(scenes["current"]["number"], str)
    rec = wait_for("/api/tf5/recorder", lambda d: d["synced"])
    assert rec["read_only"] and rec["playlist"]["total"] == 3 and not any(c["available"] for c in rec["controls"])
    _, fx = get("/api/tf5/fx")
    assert fx["units"][0]["plugin"] == "Echo" and fx["units"][0]["writable"]  # demo: tf5.fx_write on
    _, delay = get("/api/tf5/delay")
    assert len(delay["strips"]) == 8 and delay["strips"][4]["writable"]
    _, sel = get("/api/tf5/selected")
    assert sel["channel"] == 11 and sel["inferred"]


def test_channel_strip_is_read_from_the_desk(status):
    """Input, 1-knob EQ, gate and comp come from the simulated desk's editor protocol (console/strip_desk.py)."""
    d = wait_for("/api/tf5/strip/0", lambda d: d["input"].get("available") and "gain" in d["input"]["values"])
    assert d["input"]["values"]["source"] == "INPUT" and d["input"]["port"] == "INPUT 1"
    assert d["gate"]["values"]["type"] == "GATE" and d["eq"]["values"]["type"] == "BOLD"
    assert d["comp"]["writable"] and "knee" not in d["comp"]["locked"]  # demo mode: every write family is on


def test_rf_sweeps_the_receiver_range(status):
    _, qlxd = get("/api/qlxd/state")
    freqs = [r["frequency_mhz"] * 1e6 for r in qlxd["receivers"]]
    # A server that just started sweeps the default range until it knows the receivers.
    rf = wait_for(
        "/api/rf/state",
        lambda s: (
            s["sweep"] is not None
            and s["settings"]["start_hz"] <= min(freqs)
            and max(freqs) <= s["settings"]["stop_hz"]
        ),
    )
    assert rf["scanning"] and len(rf["sweep"]["live"]) == rf["settings"]["points"]


def test_audio_websocket_streams_metadata_console_meters_and_spectra(status):
    from simple_websocket import Client

    ws = Client.connect(URL.replace("http", "ws", 1) + "/ws/audio")
    try:
        kinds, binary = set(), 0
        deadline = time.time() + 5
        while time.time() < deadline and not ({"tf5", "rings", "tf5-meters"} <= kinds and binary >= 3):
            m = ws.receive(timeout=0.5)
            if isinstance(m, str):
                kinds.add(json.loads(m).get("type"))
            elif m is not None:
                binary += 1
        assert {"tf5", "rings", "tf5-meters"} <= kinds, kinds
        assert binary >= 3  # spectrum frames at ~20/s
    finally:
        ws.close()


@pytest.mark.parametrize("path", ["/ws/rf", "/ws/qlxd"])
def test_other_websockets_send_a_snapshot(status, path):
    from simple_websocket import Client

    ws = Client.connect(URL.replace("http", "ws", 1) + path)
    try:
        assert ws.receive(timeout=3) is not None
    finally:
        ws.close()
