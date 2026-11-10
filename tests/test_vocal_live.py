"""Live listening (vocal/session.py) against the simulated stage, run offline: the
rolling suggestion finds the talker's colour, and the mains check tells whether
the voice reaches the mains."""

import numpy as np
import pytest
from offline import BLOCK, FS, OfflineEngine

from tf_daemon.audio.stage import FlatDesk, StageSettings, StageSource
from tf_daemon.vocal.session import MAINS_MISMATCH_DB, MicChecks


class Desk(FlatDesk):
    def __init__(self, fader=0.0):
        self.fader = fader

    def fader_db(self, x):
        return self.fader


def stage(fader=0.0, colours=None):
    st = StageSettings(talkers=[0], mains=(32, 33), house_mic=None, pa_leak_db=-30.0, turn_s=(600.0, 600.0))
    if colours is not None:
        st.colours = colours
    return StageSource(34, BLOCK, st, Desk(fader))


def listen(fader=0.0, seconds=30.0, colours=None):
    eng = OfflineEngine(stage(fader, colours), analyzed=[0, 32, 33])
    checks = MicChecks(eng, FS, inputs=None, target_s=20.0, max_cut_db=6.0, max_boost_db=3.0, mains=(32, 33))
    checks.start(0, live=True)
    eng.run(seconds)
    return checks


@pytest.mark.slow
def test_live_suggestion_finds_a_boomy_voice_and_keeps_listening():
    checks = listen(colours=[[(240.0, 8.0, 1.2)]])
    s = dict(checks.sessions())[0]
    assert s.state == "live" and s.result is not None and s.result["ok"]
    cuts = [b for b in s.result["bands"] if b["gain"] < 0]
    assert any(150 <= b["freq"] <= 400 for b in cuts), s.result["bands"]
    assert checks.result(0) is s.result  # appliable while live
    checks.stop(0)
    assert dict(checks.sessions())[0].state == "done" and checks.result(0)["ok"]


@pytest.mark.slow
def test_mains_check_measures_the_channel_gain():
    checks = listen(fader=-6.0)
    mains = checks.result(0)["mains"]
    assert mains["verdict"] == "ok" and mains["coherence"] > 0.8
    assert mains["gain_db"] == pytest.approx(-6.0, abs=1.5)
    assert mains["delay_ms"] == pytest.approx(256 / FS * 1000, abs=0.5)  # the desk EQ's latency


@pytest.mark.slow
def test_mains_check_notices_a_channel_that_is_off():
    checks = listen(fader=None)
    assert checks.result(0)["mains"]["verdict"] in ("weak", "unknown")


def test_a_mains_ring_this_mic_feeds_becomes_a_notch_candidate():
    eng = OfflineEngine(stage(), analyzed=[0])
    checks = MicChecks(eng, FS, inputs=None, target_s=20.0, max_cut_db=6.0, max_boost_db=3.0)
    eng.rings.update(32, "Main L", [{"freq": 2480.0, "kind": "sustained", "prominence": 15, "level": -20}], 0.0)
    eng.rings.set_sources(32, 2480.0, [(0, "In 1", -20.0), (5, "In 6", -40.0)], "level")
    assert any(abs(r["freq"] - 2480.0) < 20 for r in checks.rings_for(0))
    assert checks.rings_for(5) == []  # not the strongest source


def _room_run(house, leak=-30.0, gain=None):
    st = StageSettings(
        talkers=[0], mains=(32, 33), house_mic=31, pa_leak_db=leak, turn_s=(600.0, 600.0), colours=[[]]
    )  # a neutral voice: whatever needs fixing is the room's
    src = StageSource(34, BLOCK, st, Desk(0.0))
    eng = OfflineEngine(src, analyzed=[0, 31, 32, 33])
    if gain is not None:  # what reaches the house mic (e.g. nearly nothing)
        orig = src._fill

        def fill(block):
            orig(block)
            block[:, 31] *= gain
            block[:, 31] += np.random.default_rng(0).standard_normal(len(block)) * 0.05

        src._fill = fill
    checks = MicChecks(
        eng,
        FS,
        inputs=None,
        target_s=20.0,
        max_cut_db=6.0,
        max_boost_db=3.0,
        mains=(32, 33),
        house_mic=31 if house else None,
    )
    checks.start(0, live=True)
    eng.run(40.0)
    return checks.result(0)


@pytest.mark.slow
def test_a_house_mic_aims_the_suggestion_at_the_room():
    """The simulated room is boomy around 300 Hz; the voice itself is neutral."""
    low_cut = lambda r: any(b["gain"] < 0 and 150 <= b["freq"] <= 450 for b in r["bands"])  # noqa: E731
    mic_only, room = _room_run(False), _room_run(True)
    assert not low_cut(mic_only) and "room" not in mic_only
    assert room["room"]["used"] and room["room"]["coherence"] > 0.5
    assert room["room"]["delay_ms"] == pytest.approx((12.0 / 343.0 + 2 * 256 / FS) * 1000, abs=1.0)
    assert low_cut(room), room["bands"]


@pytest.mark.slow
def test_a_house_mic_that_barely_hears_the_voice_is_not_used():
    r = _room_run(True, gain=0.001)
    assert r["ok"] and not r["room"]["used"]


class EqDesk(Desk):
    """A desk whose channel EQ the test sets (as Apply would); the stage hears it."""

    def __init__(self):
        super().__init__(0.0)
        self.eq = None
        self.hidden = None  # EQ the stage applies but the desk doesn't report (e.g. a bus EQ)

    def channel_eq(self, x):
        return self.hidden or self.eq


def eq_from(result):
    """The desk EQ a suggestion asks for (what Apply sets)."""
    flat = {"type": "bell", "on": True, "gain": 0.0, "q": 1.0}
    homes = (("L", 125.0), ("LM", 800.0), ("HM", 2500.0), ("H", 8000.0))
    bands = {lbl: {**flat, "label": lbl, "freq": f} for lbl, f in homes}
    for b in result["bands"]:
        bands[b["band"]] = {**flat, "label": b["band"], "freq": b["freq"], "gain": b["gain"], "q": b["q"]}
    hpf = result.get("hpf")
    return {
        "on": True,
        "bank": "PRECISE",
        "one_knob": False,
        "bands": list(bands.values()),
        "hpf": {"on": True, "freq": hpf["freq"], "slope": 12} if hpf else None,
    }


def _closed_loop(desk):
    from tf_daemon.dsp.eq import eq_curve

    st = StageSettings(
        talkers=[0],
        mains=(32, 33),
        house_mic=31,
        pa_leak_db=-30.0,
        turn_s=(600.0, 600.0),
        colours=[[(240.0, 6.0, 1.2)]],
    )
    eng = OfflineEngine(StageSource(34, BLOCK, st, desk), analyzed=[0, 31, 32, 33])
    checks = MicChecks(
        eng,
        FS,
        inputs=None,
        target_s=20.0,
        max_cut_db=6.0,
        max_boost_db=3.0,
        mains=(32, 33),
        house_mic=31,
        eq_curve=lambda i, f: eq_curve(desk.eq, f) if desk.eq else None,
    )
    checks.start(0, live=True)
    return eng, checks


@pytest.mark.slow
def test_the_live_model_settles_then_converges_once_applied():
    desk = EqDesk()
    eng, checks = _closed_loop(desk)
    eng.run(45.0)
    before = checks.result(0)
    m = before["model"]
    assert m["revision"] >= 5 and m["settled"], m
    assert m["off_after_db"] < m["off_now_db"]
    desk.eq = eq_from(before)  # Apply
    eng.run(70.0)  # past the 60 s memory: the model now only knows the EQ'd sound
    after = checks.result(0)["model"]
    assert after["off_now_db"] < m["off_now_db"]  # the voice got closer to the target
    assert after["off_now_db"] == pytest.approx(m["off_after_db"], abs=1.5)  # about as predicted
    assert after["settled"]  # and it doesn't keep chasing its own change


@pytest.mark.slow
def test_the_mains_check_notices_eq_the_desk_does_not_report():
    desk = EqDesk()
    desk.hidden = eq_from({"bands": [{"band": "LM", "freq": 1000.0, "gain": 8.0, "q": 1.0}], "hpf": None})
    eng, checks = _closed_loop(desk)  # the desk reports a flat channel EQ...
    checks._eq_curve = lambda i, f: np.zeros(len(f))
    eng.run(30.0)
    mains = checks.result(0)["mains"]  # ...but the mains carry +8 dB at 1 kHz
    assert mains["verdict"] == "differs" and mains["eq_mismatch_db"] > MAINS_MISMATCH_DB


@pytest.mark.slow
def test_the_measurements_restart_only_when_the_desk_eq_changes():
    desk = EqDesk()
    eng, checks = _closed_loop(desk)
    eng.run(10.0)
    s = dict(checks.sessions())[0]
    frames = s.house.frames
    eng.run(3.0)
    assert s.house.frames > frames  # no EQ change: keeps averaging
    desk.eq = eq_from({"bands": [{"band": "L", "freq": 240.0, "gain": -6.0, "q": 1.2}], "hpf": None})
    eng.run(1.5)
    assert s.house.frames < frames and s.house.delay is not None  # started over, delay kept


class FaderDesk(FlatDesk):
    def __init__(self, faders):
        self.faders = faders

    def fader_db(self, x):
        return self.faders.get(x, 0.0)


@pytest.mark.slow
@pytest.mark.parametrize("mic_fader", [0.0, 6.0])
def test_feedback_headroom_matches_the_stages_known_loop(mic_fader):
    """Mic 0 at ``mic_fader``, a second talker at -6 dB keeps the mains busy while mic 0 is
    idle. The stage's loop is known: headroom = -(fader + the leak filter's response)."""
    from tf_daemon.vocal import feedback
    from tf_daemon.vocal.feedback import headroom_at

    st = StageSettings(
        talkers=[0, 1],
        mains=(32, 33),
        house_mic=None,
        pa_leak_db=-18.0,
        turn_s=(4.0, 7.0),
        tone_db=-120.0,
        colours=[[], []],
    )
    src = StageSource(34, BLOCK, st, FaderDesk({0: mic_fader, 1: -6.0}))
    eng = OfflineEngine(src, analyzed=[0, 1, 32, 33])
    checks = MicChecks(eng, FS, inputs=None, target_s=20.0, max_cut_db=6.0, max_boost_db=3.0, mains=(32, 33))
    checks.start(0, live=True)
    eng.run(90.0)
    s = dict(checks.sessions())[0]
    fb = s.feedback.headroom(s.mains)
    assert fb["known"] and fb["leak_delay_ms"] == pytest.approx((6.0 / 343.0 + 128 / FS) * 1000, abs=0.5)
    n = 1 << 16
    leak = 20 * np.log10(np.abs(np.fft.rfft(src._leak_h, n)) + 1e-12)
    for f in (820.0, 1500.0, 2480.0):
        expected = -(mic_fader + leak[int(round(f * n / FS))])
        assert headroom_at(fb, f, 1 / 12) == pytest.approx(expected, abs=1.0), f
    true_min = min(-(mic_fader + leak[int(round(f * n / FS))]) for f in feedback.POINTS)
    assert fb["min_db"] <= true_min + 1.0  # never reports more headroom than there is


def test_boosts_are_held_back_near_feedback():
    from tf_daemon.vocal.feedback import MARGIN_DB, boost_cap

    fb = {"known": True, "points": [3000.0, 6500.0], "headroom": [20.0, MARGIN_DB + 1.0]}
    cap = boost_cap(fb)
    assert cap(3000.0) == 20.0 - MARGIN_DB and cap(6500.0) == 1.0 and cap(500.0) == float("inf")
    assert boost_cap({"known": False}) is None
