"""The Desk page's reads (console/desk.py, scenes.py, recorder.py, effects.py) against the simulated
desk: scenes and recall, USB recorder (read-only), FX and delay (written only through the guarded
POST /api/tf5/param), the selected channel; and a recall reaching RCP clients as sscurrent."""

import socket

import pytest
from editor_fakes import assert_read_only, wait_until
from helpers import make_config

from tf_daemon.app import build_modules, create_app
from tf_daemon.console import recorder
from tf_daemon.console.desk import CH1_ID, CH_STEP, channel_of
from tf_daemon.console.editor.client import TFEditorClient
from tf_daemon.console.editor.codec import event
from tf_daemon.console.editor.guard import ReadOnlyViolation, check_outbound
from tf_daemon.console.sim import SimConsole


def _closed_port():
    with socket.create_server(("127.0.0.1", 0)) as s:
        return s.getsockname()[1]


@pytest.fixture
def desk(console):
    """``desk(**tf5 switches)``: a test client of an app on the simulated desk, every function synced."""
    apps = []

    def make(**tf5):
        cfg = make_config(tf5={"host": "127.0.0.1", "port": _closed_port(), "eq": True, "eq_port": console.port, **tf5})
        app = create_app(cfg, build_modules(cfg))
        apps.append(app)
        c = app.test_client()
        wait_until(lambda: all(f["synced"] for f in c.get("/api/tf5/status").get_json()["eq"]["functions"].values()))
        return c

    yield make
    for app in apps:
        app.extensions["tf_stop"]()


def test_scenes_list_the_stored_slots_and_the_current_one(desk, console):
    c = desk()
    d = c.get("/api/tf5/scenes").get_json()
    assert d["synced"] and d["stored"] == 5 and d["slots"] == 200
    assert [s["number"] for s in d["scenes"]] == ["A00", "A01", "A02", "A05", "B00"]
    a02 = d["scenes"][2]
    assert a02 == {
        "number": "A02",
        "bank": "A",
        "slot": 2,
        "stored": True,
        "data_type": 1,
        "title": "Sunday PM",
        "comment": "Evening, band",
        "protect": False,
        "time_stamp": "2026-11-03 09:30:00",
        "fade": {"on": True, "time_s": 2.5},
        "file": "SCENE_A02.tfs",
    }
    assert d["scenes"][0]["protect"] and d["scenes"][4]["title"] == "Rehearsal"
    assert d["current"] == {"number": "A00", "edited": False, "selected": "A00", "last_recalled": None}
    assert d["recall"] == {"available": False, "reason": "recalling scenes is off (tf5.scene_recall = false)"}
    assert not d["store"]["available"] and "unverified" in d["store"]["reason"]
    assert not d["undo"]["available"] and d["undo"]["reason"]
    every = c.get("/api/tf5/scenes?all=1").get_json()["scenes"]
    assert len(every) == 200 and every[3] == {**every[3], "number": "A03", "stored": False, "title": "", "file": None}
    assert every[199]["number"] == "B99"
    unchanged = c.get(f"/api/tf5/scenes?since={d['version']}").get_json()
    assert unchanged == {"version": d["version"], "unchanged": True}
    assert_read_only(console)  # reading the scenes sends nothing but the handshake


def test_a_recall_updates_the_current_scene(desk, console):
    c = desk(scene_recall=True, mix_write=True)
    before = c.get("/api/tf5/scenes").get_json()
    assert before["recall"]["available"]
    r = c.post("/api/tf5/event/recall_scene", json={"number": "A02", "confirm": True}).get_json()
    assert r == {"ok": True, "number": "A02", "confirmed": True, "status": "OK"}
    wait_until(lambda: c.get("/api/tf5/scenes").get_json()["current"]["number"] == "A02")
    cur = c.get(f"/api/tf5/scenes?since={before['version']}").get_json()["current"]
    assert cur["edited"] is False and cur["last_recalled"]["number"] == "A02" and cur["last_recalled"]["status"] == "OK"
    assert console.recalls == ["A02"]
    # A recall from elsewhere (the desk itself) is noticed too.
    console.push_change("Scene/LastScene", "B00", func="Setup")
    console.push_event("onRecalledScene", "STATUS:OK NUMBER:B00")
    wait_until(lambda: (c.get("/api/tf5/scenes").get_json()["current"]["last_recalled"] or {}).get("number") == "B00")
    # Changing the desk after a recall marks the scene edited (as the simulated desk does).
    body = {"func": "Mixing", "changes": [["DCA:0/Fader/Level", -1000]], "confirm": True}
    assert c.post("/api/tf5/param", json=body).get_json()["ok"]
    wait_until(lambda: c.get("/api/tf5/scenes").get_json()["current"]["edited"] is True)


def test_recorder_is_read_only(desk, console):
    c = desk()
    d = c.get("/api/tf5/recorder").get_json()
    assert d["synced"] and d["setup_synced"] and d["read_only"]
    assert d["transport"] == {"state": 0, "rec_state": 0, "play_continue": 0}
    assert d["current"] == {"song_no": 1, "file": "Walk-in.mp3", "play_time": 242, "position": 37}
    pl = d["playlist"]
    assert pl["dir"] == "/YPE/SONGS" and pl["total"] == 3 and len(pl["songs"]) == 3
    assert pl["songs"][0] == {
        "index": 0,
        "file": "Prelude.wav",
        "artist": "Organ",
        "play_time": 184,
        "checked": False,
        "attribute": 0,
    }
    assert d["setup"] == {
        "record_level_db": 0.0,
        "format": "WAV",
        "source": 0,
        "playback_level_db": 0.0,
        "playback_mode": 0,
        "auto_scroll": 1,
    }
    assert [b["name"] for b in d["controls"]] == list(recorder.CONTROLS)
    assert not any(b["available"] for b in d["controls"]) and d["reason"] == recorder.TRANSPORT_UNVERIFIED
    console.push_change("Recorder/PlayList/Current/PlayPosition", 38, func="Status")
    wait_until(lambda: c.get("/api/tf5/recorder").get_json()["current"]["position"] == 38)
    assert_read_only(console)


def test_transport_commands_are_not_sent():
    """No verified OPERATE strings yet, and the guard refuses the Transport event in any form."""
    assert recorder.TRANSPORT_OPS == {}
    for args in ("OPERATE:Play", "OPERATE:Stop FILENAME:x.wav NUMBER:1", ""):
        with pytest.raises(ReadOnlyViolation):
            check_outbound("EEVT", event("Transport", args))
        with pytest.raises(ReadOnlyViolation):
            check_outbound("EEVT", event("Transport", args), allow_event=lambda name, a: True)


def test_fx_read_and_written_under_fx_write(desk, console):
    ro = desk()
    d = ro.get("/api/tf5/fx").get_json()
    assert d["synced"] and len(d["units"]) == 8
    fx1, fx2, ins1 = d["units"][0], d["units"][1], d["units"][2]
    assert (fx1["label"], fx1["plugin"], fx1["library"], fx1["bypass"], fx1["mix_balance"]) == (
        "FX 1",
        "Echo",
        "Echo Hall",
        0,
        100,
    )
    assert fx1["values"][:5] == [2600, 120, 45, 0, 8000] and len(fx1["values"]) == 64
    assert fx2["returns"] == [
        {"strip": "FxRtnChannel:2", "name": "FX2 L"},
        {"strip": "FxRtnChannel:3", "name": "FX2 R"},
    ]
    assert (
        ins1["label"] == "Insert FX 1"
        and ins1["plugin"] == "ReverbStage"
        and ins1["bypass"] == 1
        and "returns" not in ins1
    )
    assert not fx1["writable"] and d["limits"] == {"mix_balance": [0, 100], "value": [-(2**31), 2**31 - 1]}
    body = {"func": "FX", "changes": [["FX:0/Bypass", 1]], "confirm": True}
    assert ro.post("/api/tf5/param", json=body).status_code == 403
    assert console.all_writes == []

    c = desk(fx_write=True)
    assert c.get("/api/tf5/fx").get_json()["units"][0]["writable"]
    changes = [["FX:0/Bypass", 1], ["FX:0/MixBalance", 80], ["FX:0/Param/Bank/Value:3", -5]]
    r = c.post("/api/tf5/param", json={"func": "FX", "changes": changes, "confirm": True}).get_json()
    assert r["ok"] and all(x["confirmed"] for x in r["results"])
    wait_until(lambda: c.get("/api/tf5/fx").get_json()["units"][0]["values"][3] == -5)
    fx1 = c.get("/api/tf5/fx").get_json()["units"][0]
    assert fx1["bypass"] == 1 and fx1["mix_balance"] == 80
    assert ("FX", "FX:0/Param/Bank/Value:3", -5) in console.all_writes
    bad = {"func": "FX", "changes": [["FX:0/MixBalance", 101]], "confirm": True}
    assert c.post("/api/tf5/param", json=bad).status_code == 400  # the schema's range, checked by the guard


def test_delay_read_and_written_under_processing_write(desk, console):
    d = desk().get("/api/tf5/delay").get_json()
    assert [s["label"] for s in d["strips"]] == ["ST IN 1L", "ST IN 1R", "ST IN 2L", "ST IN 2R"] + [
        f"MATRIX {n}" for n in range(1, 5)
    ]
    m1 = d["strips"][4]
    assert m1["strip"] == "Matrix:0" and m1["name"] == "Lobby" and m1["on"] == 1
    assert m1["time"] == 12500 and m1["time_ms"] == 12.5 and m1["offset_ms"] == 0.0 and not m1["writable"]
    assert d["limits"] == {"time": [1000, 1000000], "offset": [-1001000, 1001000]} and d["scale"] == 1000

    c = desk(processing_write=True)
    assert c.get("/api/tf5/delay").get_json()["strips"][1]["writable"]
    body = {"func": "Mixing", "changes": [["StInChannel:1/Delay/Time", 20000], ["StInChannel:1/Delay/On", 1]]}
    assert c.post("/api/tf5/param", json={**body, "confirm": True}).get_json()["ok"]
    st = c.get("/api/tf5/delay").get_json()["strips"][1]
    assert st["time_ms"] == 20.0 and st["on"] == 1
    too_long = {"func": "Mixing", "changes": [["Matrix:0/Delay/Time", 1000001]], "confirm": True}
    assert c.post("/api/tf5/param", json=too_long).status_code == 400


def test_selected_channel_is_decoded_by_the_inferred_rule(desk):
    d = desk().get("/api/tf5/selected").get_json()
    assert (d["raw"], d["hex"], d["channel"], d["label"], d["name"]) == (0x0CA316C9, "0x0CA316C9", 11, "CH 11", "ch11")
    assert d["inferred"]
    assert channel_of(CH1_ID) == 1 and channel_of(CH1_ID + 39 * CH_STEP) == 40
    assert (
        channel_of(CH1_ID + 40 * CH_STEP) is None and channel_of(0x140F7609) is None and channel_of(CH1_ID + 1) is None
    )


def test_desk_routes_are_404_without_the_editor_protocol():
    cfg = make_config(tf5={"host": "127.0.0.1", "port": _closed_port(), "eq": False})
    app = create_app(cfg, build_modules(cfg))
    try:
        c = app.test_client()
        for area in ("scenes", "recorder", "fx", "delay", "selected"):
            assert c.get(f"/api/tf5/{area}").status_code == 404
        assert c.get("/desk").status_code == 200
    finally:
        app.extensions["tf_stop"]()


def test_a_recall_over_the_editor_protocol_reaches_rcp_clients():
    """As on the desk: an RCP client hears sscurrent_ex (and tf5's RCP client re-reads on it, test_tf5.py)."""
    sim = SimConsole(drift_s=0)
    ec = TFEditorClient(sim.host, sim.eq_port, scene_recall=True)
    try:
        with socket.create_connection((sim.host, sim.rcp_port), timeout=5) as rcp:
            wait_until(lambda: len(sim.rcp.conns) == 1)
            ec.start()
            wait_until(lambda: ec.synced)
            assert ec.recall_scene("B07")["confirmed"]
            got = b""
            while b"sscurrent" not in got:
                got += rcp.recv(4096)
            assert b"NOTIFY sscurrent_ex scene_b 7\n" in got
    finally:
        ec.stop()
        sim.close()
