"""Writing EQ to the desk: only on request, only input-channel PEQ, only what was asked.

Everything here runs against the fake console (tests/tfeditor_fakes.py); nothing
in the test suite talks to a real TF5.
"""

import socket
import time

import pytest
from editor_fakes import FakeEditorConsole, build_dump, default_mixing_values, mixing_tree, set_param, wait_until
from helpers import make_config

from tf_daemon.app import build_modules, create_app
from tf_daemon.config import ConfigError, load_config
from tf_daemon.console.editor.client import TFEditorClient
from tf_daemon.console.editor.codec import ORIGIN_SELF, SET_PARAM, T_S32, T_U32, parse_message, set_param_message
from tf_daemon.console.editor.dump import Dump
from tf_daemon.console.editor.guard import ReadOnlyViolation, check_outbound
from tf_daemon.vocal.module import _Session
from tf_daemon.vocal.plan import eq_plan

GAIN_L = "InputChannel:3/PEQ/Bank/BandL/Gain"
FREQ_2 = "InputChannel:3/PEQ/Bank/Band:1/Frequency"


@pytest.fixture
def console():
    fake = FakeEditorConsole()
    yield fake
    fake.close()


@pytest.fixture
def client_for():
    clients = []

    def make(fake, **kw):
        c = TFEditorClient("127.0.0.1", fake.port, **kw)
        clients.append(c)
        c.start()
        assert wait_until(lambda: c.synced)
        return c
    yield make
    for c in clients:
        c.stop()


def sent_sets(fake):
    return [(t, p) for t, p in fake.raw if parse_message(p).cmd == SET_PARAM]


# ====================================================================== addressing

def test_address_is_the_inverse_of_resolve():
    dump = Dump(build_dump(mixing_tree(), default_mixing_values()))
    for path in (GAIN_L, FREQ_2, "InputChannel:0/PEQ/On", "InputChannel:39/PEQ/Bank/HPF/Frequency",
                 "InputChannel:7/PEQ/Bank/BandH/LPFOn", "InputChannel:2/Fader/Level"):
        wp, idx, prm = dump.address(path)
        name, _, prm2 = dump.resolve(wp, idx)
        assert prm2 is prm and name.replace("Bank/", "Bank/") == path
    # The guide's example: ch4 BandL gain is [0, 7, 1, 3, 2] / [3, 0, 0, 0, 0].
    assert dump.address(GAIN_L)[:2] == ([0, 7, 1, 3, 2], [3, 0, 0, 0, 0])
    for bad in ("InputChannel:40/PEQ/On", "InputChannel:0/PEQ/Nope", "Nope/PEQ/On"):
        with pytest.raises(KeyError):
            dump.address(bad)


# ====================================================================== client

def test_read_only_unless_enabled(console, client_for):
    c = client_for(console)
    assert c.status()["read_only"]
    with pytest.raises(PermissionError):
        c.write([(GAIN_L, -400)])
    assert sent_sets(console) == [] and console.writes == []


def test_write_sends_exactly_the_requested_changes_and_waits_for_the_echo(console, client_for):
    c = client_for(console, writes=True)
    assert not c.status()["read_only"]
    res = c.write([(GAIN_L, -400), (FREQ_2, 34000)])
    assert [(r["path"], r["value"], r["confirmed"], r["actual"]) for r in res] == [
        (GAIN_L, -400, True, -400), (FREQ_2, 34000, True, 34000)]
    assert console.writes == [(GAIN_L, -400), (FREQ_2, 34000)]
    assert c.get(GAIN_L) == -400 and c.get(FREQ_2) == 34000          # our copy follows the echo
    frames = sent_sets(console)
    assert len(frames) == 2
    for tag, payload in frames:
        e = parse_message(payload).elements
        assert tag == "MMIX" and e[1].value == "Mixing" and e[6].value == [ORIGIN_SELF]
        with pytest.raises(ReadOnlyViolation):    # outside write() the same frame is refused again
            check_outbound(tag, payload)
    gain = parse_message(frames[0][1]).elements
    assert gain[4].value == [0, 7, 1, 3, 2] and gain[5].value == [3, 0, 0, 0, 0]
    assert gain[7].type == T_S32 and gain[7].value == [-400]       # signed -> 0x24 (guide 4.4)
    assert parse_message(frames[1][1]).elements[7].type == T_U32   # unsigned -> 0x14


@pytest.mark.parametrize("path,value", [
    ("InputChannel:3/Fader/Level", -1000),          # not EQ
    ("InputChannel:3/Label/Name", 0),               # not EQ
    ("Stereo:0/PEQ/Bank/BandL/Gain", -400),         # not an input channel
    ("InputChannel:3/PEQ/Bank/OneKnob/On", 0),      # not on the list
    (GAIN_L, -2400),                                # beyond +-18 dB
    ("InputChannel:3/PEQ/Bank/BandL/Q", 0),         # Q below 0.1
    ("InputChannel:3/PEQ/Bank/HPF/Frequency", 20000),   # HPF above 600 Hz
    ("InputChannel:3/PEQ/On", True),                # not an int
    ("InputChannel:3/PEQ/On", 2),
    ("InputChannel:40/PEQ/On", 1),                  # no such channel
])
def test_write_refuses_anything_else(console, client_for, path, value):
    c = client_for(console, writes=True)
    with pytest.raises(ValueError):
        c.write([(GAIN_L, -300), (path, value)])     # all or nothing: the valid one isn't sent either
    assert sent_sets(console) == [] and console.writes == []


def test_unconfirmed_write_is_reported():
    fake = FakeEditorConsole(echo_writes=False)
    c = TFEditorClient("127.0.0.1", fake.port, writes=True)
    c.start()
    try:
        assert wait_until(lambda: c.synced)
        t0 = time.monotonic()
        res = c.write([(GAIN_L, -400)], timeout=0.5)
        assert res[0]["confirmed"] is False and res[0]["actual"] is None
        assert 0.4 < time.monotonic() - t0 < 3
        assert fake.writes == [(GAIN_L, -400)]
    finally:
        c.stop()
        fake.close()


def test_guard_passes_only_the_authorized_frames(console, client_for):
    c = client_for(console, writes=True)
    with c._lock:
        wp, idx, prm = c._mixing.address(GAIN_L)
    c._authorized = {(tuple(wp), tuple(idx)): (prm.type, -400)}
    try:
        check_outbound("MMIX", set_param_message(wp, idx, prm, -400), c._allow_set)
        for payload in (set_param_message(wp, idx, prm, -500),                       # other value
                        set_param_message(wp, [4, 0, 0, 0, 0], prm, -400),          # other channel
                        set_param("Mixing", wp, idx, -400, origin=0xA1, vtype=T_S32)):   # other origin
            with pytest.raises(ReadOnlyViolation):
                check_outbound("MMIX", payload, c._allow_set)
        with pytest.raises(ReadOnlyViolation):                                       # other channel tag
            check_outbound("MPRO", set_param_message(wp, idx, prm, -400), c._allow_set)
    finally:
        c._authorized = {}
    with pytest.raises(ReadOnlyViolation):
        check_outbound("MMIX", set_param_message(wp, idx, prm, -400))


def test_write_needs_a_connection():
    c = TFEditorClient("127.0.0.1", 9, writes=True)
    with pytest.raises(ConnectionError):
        c.write([(GAIN_L, -400)])


# ====================================================================== config

def test_config(tmp_path):
    assert load_config(None).tf5.eq_write is False
    p = tmp_path / "c.toml"
    for bad in ('[tf5]\neq_write = 1\n', '[tf5]\neq_write = true\n'):   # not a bool / without eq
        p.write_text(bad)
        with pytest.raises(ConfigError, match="eq_write"):
            load_config(p)
    p.write_text('[tf5]\neq = true\neq_write = true\n')
    assert load_config(p).tf5.eq_write is True


# ====================================================================== vocal assist: plan

def _current(on=True, one_knob=False, h_type="high_shelf"):
    return {"on": on, "bank": "BOLD", "one_knob": one_knob, "hpf": {"on": False, "freq": 100.0, "slope": 12},
            "bands": [{"label": "L", "type": "low_shelf", "on": True, "freq": 125.0, "gain": 2.0, "q": 0.7},
                      {"label": "1", "type": "bell", "on": True, "freq": 1000.0, "gain": 0.0, "q": 0.7},
                      {"label": "2", "type": "bell", "on": True, "freq": 4000.0, "gain": -3.0, "q": 0.7},
                      {"label": "H", "type": h_type, "on": True, "freq": 10000.0, "gain": 4.6, "q": 0.7}]}


RESULT = {"ok": True, "hpf": {"band": "HPF", "freq": 80.0},
          "bands": [{"band": "L", "type": "bell", "freq": 340.0, "gain": -4.0, "q": 1.4},
                    {"band": "2", "type": "bell", "freq": 3400.0, "gain": -3.0, "q": 12.0}]}


def test_plan_makes_the_desk_match_the_suggestion():
    changes, lines = eq_plan(RESULT, _current(on=False))
    assert changes == [
        ("PEQ/On", 1),
        ("PEQ/Bank/HPF/On", 1), ("PEQ/Bank/HPF/Frequency", 800),
        ("PEQ/Bank/BandL/Type", 0), ("PEQ/Bank/BandL/Frequency", 3400), ("PEQ/Bank/BandL/Gain", -400),
        ("PEQ/Bank/BandL/Q", 1400),
        ("PEQ/Bank/Band:1/Frequency", 34000), ("PEQ/Bank/Band:1/Gain", -300), ("PEQ/Bank/Band:1/Q", 10000),  # Q <= 10
        ("PEQ/Bank/BandH/Gain", 0),                       # unused band flattened; band 1 already flat
    ]
    assert lines[0] == "EQ: on" and "H: flat (was +4.6 dB)" in lines


def test_plan_leaves_a_filter_band_alone():
    changes, lines = eq_plan({"ok": True, "hpf": None, "bands": []}, _current(h_type="lpf"))
    assert not any(p.startswith("PEQ/Bank/BandH") for p, _ in changes) and "H: LPF left as it is" in lines


# ====================================================================== vocal assist: module and routes

def _closed_port():
    with socket.create_server(("127.0.0.1", 0)) as s:
        return s.getsockname()[1]


def _vocal_app(fake, eq_write=True):
    cfg = make_config(tf5={"host": "127.0.0.1", "port": _closed_port(), "eq": True, "eq_port": fake.port,
                           "eq_write": eq_write}, vocal={"enabled": True})
    modules = build_modules(cfg)
    app = create_app(cfg, modules)
    vm = next(m for m in modules if m.name == "vocal")
    tf5 = next(m for m in modules if m.name == "tf5")
    assert wait_until(lambda: tf5.eq_client.synced)
    s = _Session(48000, 20.0)
    s.state, s.result = "done", RESULT
    vm._sessions[3] = s                       # a finished mic check on USB 4 = InCh 4
    return app, vm, tf5


def test_apply_and_undo_only_on_confirmed_requests(console):
    app, vm, tf5 = _vocal_app(console)
    try:
        c = app.test_client()
        before = tf5.eq_raw(3, ["PEQ/Bank/BandL/Gain", "PEQ/Bank/BandL/Frequency", "PEQ/Bank/HPF/Frequency"])
        st = c.get("/api/vocal/status").get_json()
        assert st["can_write"] and st["sessions"]["3"]["apply"]["possible"]
        assert "L: bell -4.0 dB @ 340 Hz, Q 1.4" in st["sessions"]["3"]["apply"]["changes"]
        assert console.writes == []                                      # showing it changes nothing

        assert c.post("/api/vocal/apply", json={"channel": 3}).status_code == 400   # no confirm
        assert c.post("/api/vocal/apply", json={"channel": 3, "confirm": "yes"}).status_code == 400
        assert console.writes == []

        d = c.post("/api/vocal/apply", json={"channel": 3, "confirm": True}).get_json()
        assert d["ok"] and d["sent"] == d["confirmed"] > 0
        assert ("InputChannel:3/PEQ/Bank/BandL/Gain", -400) in console.writes
        assert all(p.startswith("InputChannel:3/PEQ/") for p, _ in console.writes)
        assert tf5.eq_raw(3, ["PEQ/Bank/BandL/Gain"]) == {"PEQ/Bank/BandL/Gain": -400}
        st = c.get("/api/vocal/status").get_json()
        assert st["undo"] == {"3": True} and st["applied"]["3"]["action"] == "apply"

        n = len(console.writes)
        d = c.post("/api/vocal/undo", json={"channel": 3, "confirm": True}).get_json()
        assert d["ok"] and d["sent"] == d["confirmed"] == n
        assert tf5.eq_raw(3, list(before)) == before                     # back as it was
        assert c.post("/api/vocal/undo", json={"channel": 3, "confirm": True}).get_json()["error"] == "nothing to undo"
    finally:
        app.extensions["tf_stop"]()


def test_no_writes_when_eq_write_is_off(console):
    app, vm, _ = _vocal_app(console, eq_write=False)
    try:
        c = app.test_client()
        st = c.get("/api/vocal/status").get_json()
        assert not st["can_write"] and "eq_write" in st["sessions"]["3"]["apply"]["reason"]
        r = c.post("/api/vocal/apply", json={"channel": 3, "confirm": True})
        assert r.status_code == 400 and "eq_write" in r.get_json()["error"]
        assert console.writes == [] and sent_sets(console) == []
    finally:
        app.extensions["tf_stop"]()


def test_not_applied_with_one_knob_on_or_to_a_bus():
    fake = FakeEditorConsole({**default_mixing_values(), "InputChannel:3/PEQ/Bank/OneKnob/On": 1})
    app, vm, _ = _vocal_app(fake)
    try:
        assert "1-knob" in vm.apply_blocker(3)
        s = _Session(48000, 20.0)
        s.state, s.result = "done", RESULT
        vm._sessions[32] = s                  # USB 33 = St L: a bus, not an input channel
        assert "input channel" in vm.apply_blocker(32)
        c = app.test_client()
        for ch in (3, 32):
            assert c.post("/api/vocal/apply", json={"channel": ch, "confirm": True}).status_code == 400
        assert fake.writes == []
    finally:
        app.extensions["tf_stop"]()
        fake.close()
