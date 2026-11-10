"""Auto EQ (vocal/auto.py), in a closed loop with the simulated stage, offline."""

import pytest
from offline import BLOCK, FS, OfflineEngine, RawDesk

from tf_daemon.audio.stage import StageSettings, StageSource
from tf_daemon.vocal.auto import PATHS, STEP_DB, AutoEQ
from tf_daemon.vocal.session import MicChecks


def rig(colour=None, talkers=(0,)):
    desk = RawDesk()
    st = StageSettings(
        talkers=list(talkers),
        mains=(32, 33),
        house_mic=None,
        pa_leak_db=-30.0,
        turn_s=(600.0, 600.0),
        tone_db=-120.0,
        colours=[colour or [(240.0, 8.0, 1.2)], []],
    )
    eng = OfflineEngine(StageSource(34, BLOCK, st, desk), analyzed=[0, 32, 33])
    from tf_daemon.dsp.eq import eq_curve

    checks = MicChecks(
        eng,
        FS,
        inputs=None,
        target_s=20.0,
        max_cut_db=6.0,
        max_boost_db=3.0,
        mains=(32, 33),
        eq_curve=lambda i, f: eq_curve(desk._eq(), f),
    )
    auto = AutoEQ(checks, desk, max_cut_db=6.0, max_boost_db=3.0)
    return desk, eng, checks, auto


def run(eng, auto, seconds):
    for _ in range(int(seconds)):
        eng.run(1.0)
        auto.tick(eng.t)


@pytest.mark.slow
def test_auto_walks_the_eq_to_the_suggestion_gently_and_undo_puts_it_back():
    desk, eng, checks, auto = rig()
    before = dict(desk.raw)
    auto.arm(0)  # starts live listening too
    assert dict(checks.sessions())[0].live
    run(eng, auto, 150)
    low = desk.raw["PEQ/Bank/BandL/Gain"] / 100
    assert low <= -4.0 and 150 <= desk.raw["PEQ/Bank/BandL/Frequency"] / 10 <= 400  # the boom, cut
    assert desk.raw["PEQ/Bank/BandL/Type"] == 0  # made a bell first, while flat
    # Never more than STEP_DB per band per write, and no boosts (headroom unknown here).
    gains = {p: 0 for p in PATHS if p.endswith("/Gain")}
    for w in desk.writes:
        for p, v in w:
            if p.endswith("/Gain"):
                assert abs(v - gains[p]) <= STEP_DB * 100 and v <= 0, (p, gains[p], v)
                gains[p] = v
    assert auto.state(0)["on"] and auto.state(0)["log"][-1]["action"] == "eq"
    auto.undo(0)  # one long run serves both checks
    assert desk.raw == before and not auto.state(0)["on"]


@pytest.mark.slow
def test_auto_stops_when_someone_touches_the_eq():
    desk, eng, checks, auto = rig()
    auto.arm(0)
    run(eng, auto, 60)
    desk.raw["PEQ/Bank/Band:0/Gain"] = 300  # the operator turns a knob on the desk
    run(eng, auto, 2)
    st = auto.state(0)
    assert not st["on"] and "changed on the desk" in st["stopped"]
    n = len(desk.writes)
    run(eng, auto, 10)
    assert len(desk.writes) == n  # and it keeps its hands off


def test_a_ring_gets_a_notch_at_once_and_deeper_while_it_lasts():
    desk, eng, checks, auto = rig()
    auto.arm(0)
    eng.rings.update(32, "Main L", [{"freq": 2480.0, "kind": "sustained", "prominence": 15, "level": -20}], 0.0)
    eng.rings.set_sources(32, 2480.0, [(0, "In 1", -20.0)], "level")
    auto.tick(eng.t + 1)
    band = "PEQ/Bank/Band:1"  # HM: its home range holds 2.48 kHz
    assert desk.raw[f"{band}/Frequency"] == 24800 and desk.raw[f"{band}/Q"] == 8000
    assert desk.raw[f"{band}/Gain"] == -300
    auto.tick(eng.t + 2)
    assert desk.raw[f"{band}/Gain"] == -600
    assert "ring" in auto.state(0)["log"][-1]["why"]


def test_turning_on_needs_a_writable_desk_and_the_eq_on():
    desk, eng, checks, auto = rig()
    desk.raw["PEQ/On"] = 0
    with pytest.raises(ValueError, match="EQ on"):
        auto.arm(0)
    desk.raw["PEQ/On"] = 1
    desk.eq_writable = False
    with pytest.raises(PermissionError):
        auto.arm(0)
