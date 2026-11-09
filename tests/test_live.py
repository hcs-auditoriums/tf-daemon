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
        time.sleep(0.2)
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


@pytest.mark.parametrize("page", ["/", "/rf", "/qlxd", "/config"])
def test_pages_load(status, page):
    code, body = get(page)
    assert code == 200 and b"<html" in body.lower()


def test_channel_names_come_from_the_simulated_console(status):
    chans = wait_for("/api/audio/meta", lambda m: len(m["channels"]) == 34)["channels"]
    labels = {c["index"]: c["label"] for c in chans}
    assert labels[0] == "Pastor" and labels[32] == "Main L"  # config labels win
    assert labels[2] == "BGV 1"  # read from the console


def test_console_eq_is_read(status):
    eq = wait_for("/api/tf5/eq/0", lambda e: e.get("synced"))
    assert eq["connected"] and eq["eq"]["hpf"]["freq"] > 0 and len(eq["curve"]) > 0


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


def test_audio_websocket_streams_metadata_console_and_spectra(status):
    from simple_websocket import Client

    ws = Client.connect(URL.replace("http", "ws", 1) + "/ws/audio")
    try:
        kinds, binary = set(), 0
        deadline = time.time() + 5
        while time.time() < deadline and not ({"tf5", "rings"} <= kinds and binary >= 3):
            m = ws.receive(timeout=0.5)
            if isinstance(m, str):
                kinds.add(json.loads(m).get("type"))
            elif m is not None:
                binary += 1
        assert {"tf5", "rings"} <= kinds, kinds
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
