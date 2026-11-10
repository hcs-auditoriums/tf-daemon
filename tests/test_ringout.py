"""Ring-out (vocal/ringout.py) in a closed loop with the simulated stage, offline: it finds where
the mic feeds back, notches it, gains headroom, and leaves the fader a margin under the limit;
Mute & cancel mutes and puts everything back; an operator's move on the desk stops it."""

import math

import numpy as np
import pytest
from helpers import wait_until
from offline import BLOCK, FS, OfflineEngine, RawDesk

from tf_daemon.audio.stage import LEAK_RESONANCES, StageSettings, StageSource
from tf_daemon.dsp.eq import eq_curve
from tf_daemon.vocal import ringout as ro
from tf_daemon.vocal.auto import PATHS, AutoEQ
from tf_daemon.vocal.ringout import RingOut
from tf_daemon.vocal.ringwatch import MainsWatch
from tf_daemon.vocal.session import MicChecks

RING_HZ = LEAK_RESONANCES[1][0]  # the stage's leak path peaks here first (2480 Hz, +8 dB)


class FaderDesk(RawDesk):
    """RawDesk (channel 0's EQ) with its fader, readable over "both protocols" and writable
    (DeskFaders), and heard by the stage (DeskMix)."""

    fader_writable = True

    def __init__(self, level=-1000, on=1):
        super().__init__()
        self.level, self.on = level, on
        self.fader_writes = []

    def fader_db(self, x):
        return None if x != 0 or not self.on or self.level <= -32768 else self.level / 100

    def fader(self, usb):
        return {"level": self.level, "on": self.on, "rcp_level": self.level, "rcp_on": self.on}

    def write_fader(self, usb, level=None, on=None):
        out = []
        if on == 0:
            self.on = 0
            out.append({"path": "Fader/On", "value": 0, "confirmed": True, "actual": 0})
        if level is not None:
            self.level = level
            out.append({"path": "Fader/Level", "value": level, "confirmed": True, "actual": level})
        if on is not None and on != 0:
            self.on = on
            out.append({"path": "Fader/On", "value": on, "confirmed": True, "actual": on})
        self.fader_writes.append((level, on))
        return out


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    # The same steps, closer together (stream time: the stage runs as fast as it can).
    monkeypatch.setattr(ro, "RAMP_EVERY_S", 0.4)
    monkeypatch.setattr(ro, "CLEAR_S", 0.4)


def rig(pa_leak_db=-12.0, tone_db=-120.0, tone_hz=2500.0, level=-1000, colour=()):
    desk = FaderDesk(level=level)
    st = StageSettings(
        talkers=[0],
        mains=(32, 33),
        house_mic=None,
        pa_leak_db=pa_leak_db,
        turn_s=(600.0, 600.0),
        tone_db=tone_db,
        tone_hz=tone_hz,
        speech_db=-30.0,
        colours=[list(colour)],
    )
    eng = OfflineEngine(StageSource(34, BLOCK, st, desk), analyzed=[0, 32, 33])
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
    r = None

    def sleep(seconds):  # the loop's waits move the stage on by as much stream time
        second = int(eng.t)
        eng.run(max(seconds, 0.05))
        r.touch()
        if int(eng.t) != second:
            auto.tick(eng.t)  # auto EQ's loop, on the same clock

    r = RingOut(
        eng,
        checks,
        desk,
        auto,
        mains=(32, 33),
        ceiling_db=10.0,
        margin_db=6.0,
        samplerate=FS,
        clock=lambda: eng.t,
        sleep=sleep,
    )
    return desk, eng, r


def finished(r):
    return lambda: r.phase not in ("ringing out", "together", "tone")


def notch_freqs(desk):
    return [
        desk.raw[f"{b}/Frequency"] / 10
        for b in ("PEQ/Bank/BandL", "PEQ/Bank/Band:0", "PEQ/Bank/Band:1", "PEQ/Bank/BandH")
        if desk.raw[f"{b}/Gain"] < 0 and desk.raw[f"{b}/Q"] >= 5000
    ]


@pytest.mark.slow
def test_rings_out_a_mic_notches_the_ring_and_gains_headroom():
    desk, eng, r = rig()
    r.start([(0, "wig", "speech")], tone=False)
    wait_until(finished(r), timeout=120)
    st = r.status()
    assert st["phase"] == "finished", st["message"]
    m = st["mics"][0]
    # The first ring: at the stage's leak resonance (nothing else in this loop rings), with the
    # fader somewhere under the ceiling.
    first = m["rings"][0]
    assert abs(math.log2(first["freq"] / RING_HZ)) < 1 / 24, m["rings"]
    assert all(min(abs(math.log2(r["freq"] / f)) for f in (RING_HZ, 820.0)) < 1 / 6 for r in m["rings"])
    assert -10.0 < m["first_ring_db"] < 10.0, m
    # Notched on the desk (narrow, the wig type's Q 10), and the mic now goes louder before it rings.
    assert any(abs(math.log2(f / RING_HZ)) < 1 / 24 for f in notch_freqs(desk))
    assert desk.raw[next(f"{b}/Q" for b in ("PEQ/Bank/Band:1", "PEQ/Bank/BandH") if desk.raw[f"{b}/Gain"] < 0)] == 10000
    assert m["limit_db"] > m["first_ring_db"]
    # Ends a margin under the limit, never above where the operator had it, on again.
    assert desk.level <= round((m["limit_db"] - 6.0) * 100) and desk.level <= -1000
    assert desk.on == 1
    # Never pushed past the ceiling, and every rise was one step.
    levels = [lv for lv, _ in desk.fader_writes if lv is not None]
    assert max(levels) <= 1000
    rises = [b - a for a, b in zip(levels, levels[1:], strict=False) if b > a]
    assert all(d <= 100 for d in rises[:5])  # the ramp itself (later rises go back to under a ring)


def test_mute_and_cancel_mutes_at_once_and_puts_everything_back():
    desk, eng, r = rig(pa_leak_db=-40.0)  # no feedback in reach: it just ramps
    before_eq = {p: desk.raw[p] for p in PATHS}
    r.start([(0, "generic", "speech")], tone=False)
    wait_until(lambda: desk.level > -1500 and r.status()["mics"][0]["state"] == "ringing out", timeout=60)
    desk.raw["PEQ/Bank/Band:0/Gain"] = -300  # as if a notch had gone on
    r.mics[0].known_eq["PEQ/Bank/Band:0/Gain"] = -300
    st = r.abort()
    assert st["phase"] == "aborted"
    assert desk.on == 0  # muted, and it stays muted
    assert desk.level == -1000  # the fader where it was
    assert {p: desk.raw[p] for p in PATHS} == before_eq  # the EQ as it was
    n = len(desk.fader_writes)
    eng.run(2.0)
    assert len(desk.fader_writes) == n  # nothing moves after the cancel
    r.unmute()
    assert desk.on == 1


def test_a_fader_moved_on_the_desk_stops_it_and_leaves_the_desk_alone():
    desk, eng, r = rig(pa_leak_db=-40.0)
    r.start([(0, "generic", "speech")], tone=False)
    wait_until(lambda: r.status()["mics"][0]["state"] == "ringing out" and desk.level > -2000, timeout=60)
    desk.level = -500  # the operator grabs the fader
    wait_until(finished(r), timeout=30)
    assert r.phase == "stopped" and "moved on the desk" in r.message
    assert desk.level == -500


def test_the_page_going_away_pulls_the_faders_back(monkeypatch):
    desk, eng, r = rig(pa_leak_db=-40.0)
    r._sleep = lambda s: eng.run(max(s, 0.05))  # nobody touches the heartbeat
    r.start([(0, "generic", "speech")], tone=False)
    wait_until(finished(r), timeout=60)
    assert r.phase == "stopped" and "stopped watching" in r.message
    assert desk.level == -1000 and desk.on == 1


def test_a_steady_tone_is_not_taken_for_feedback():
    desk, eng, r = rig(pa_leak_db=-40.0, tone_db=-5.0, tone_hz=1500.0)
    r.start([(0, "generic", "speech")], tone=False)
    # The ring detector reports the steady tone (it holds its frequency like a ring would).
    wait_until(lambda: desk.level > -1900, timeout=60)
    eng.rings.update(0, "In 1", [{"freq": 1500.0, "kind": "sustained", "level": -30.0, "prominence": 20.0}], 1e12)
    wait_until(finished(r), timeout=120)
    assert r.phase == "finished", r.message
    assert not notch_freqs(desk)
    assert any(e["action"] == "tone" for e in r.status()["log"])


def test_silent_mains_stop_it_before_any_fader_moves():
    desk, eng, r = rig()
    desk.master_db = lambda: -138.0  # the main bus muted: nothing reaches the PA (or the mains USB)
    r.start([(0, "generic", "speech")], tone=False)
    wait_until(finished(r), timeout=60)
    assert r.phase == "stopped" and "mains are silent" in r.message
    assert desk.fader_writes == []


def test_refuses_when_the_two_protocols_disagree_about_the_fader():
    desk, eng, r = rig()
    desk.fader = lambda usb: {"level": -1000, "on": 1, "rcp_level": -1100, "rcp_on": 1}
    with pytest.raises(ValueError, match="disagree"):
        r.start([(0, "generic", "speech")], tone=False)
    assert desk.fader_writes == []


def test_shared_rings_are_named():
    a = ro._Mic(0, "A", "wig", "speech", 0, 1, {})
    b = ro._Mic(1, "B", "wig", "speech", 0, 1, {})
    a.rings = [{"freq": 2480.0}]
    b.rings = [{"freq": 2500.0}, {"freq": 800.0}]
    assert ro._shared_rings([a, b]) == [{"freq": 2480.0, "mics": ["A", "B"]}]


def test_watch_finds_a_growing_tone_but_not_a_steady_one():
    w = MainsWatch(FS)
    t = np.arange(MainsWatch.N) / FS
    noise = np.random.default_rng(1).standard_normal(MainsWatch.N) * 1e-3
    for i in range(60):  # 3 s steady
        w.feed(noise + 0.01 * np.sin(2 * np.pi * 1000 * t), i * 0.05)
    assert w.take() is None
    for i in range(60, 80):  # 2000 Hz growing 3 dB a hop
        amp = 1e-3 * 10 ** (3 * (i - 60) / 20)
        w.feed(noise + 0.01 * np.sin(2 * np.pi * 1000 * t) + min(amp, 0.3) * np.sin(2 * np.pi * 2000 * t), i * 0.05)
    c = w.take()
    assert c is not None and abs(c[1] - 2000) < 10


def in_order(desk):
    used = [
        desk.raw[f"{b}/Frequency"]
        for b in ("PEQ/Bank/BandL", "PEQ/Bank/Band:0", "PEQ/Bank/Band:1", "PEQ/Bank/BandH")
        if desk.raw[f"{b}/Gain"] != 0
    ]
    return used == sorted(used)


@pytest.mark.slow
def test_sets_the_high_pass_and_a_vocal_curve_then_live_tone_eq_leaves_the_notches_alone():
    desk, eng, r = rig(colour=[(240.0, 8.0, 1.2)])  # a boomy talker: the vocal curve has something to cut
    r.start([(0, "wig", "speech")], tone=True)
    wait_until(lambda: r.phase == "tone", timeout=120)
    assert desk.raw["PEQ/Bank/HPF/On"] == 1 and desk.raw["PEQ/Bank/HPF/Frequency"] >= 1000  # the wig floor
    m = r.status()["mics"][0]
    assert m["shaped"], r.status()["log"]  # the voice heard while it rang out gave a curve
    boom = [
        b
        for b in ("PEQ/Bank/BandL", "PEQ/Bank/Band:0")
        if desk.raw[f"{b}/Gain"] < 0 and 150 <= desk.raw[f"{b}/Frequency"] / 10 <= 400
    ]
    assert boom, desk.raw  # the boom, cut
    assert in_order(desk)
    notches = {b: (desk.raw[f"{b}/Frequency"], desk.raw[f"{b}/Gain"], desk.raw[f"{b}/Q"]) for b in r.mics[0].notches}
    assert notches
    session = dict(r.checks.sessions())[0]
    assert session.live and session.mic == "wig" and session.reserved and session.hpf_floor >= 100
    eng.run(30.0)  # live tone EQ: whatever it does, the notches stay
    for b, v in notches.items():
        assert (desk.raw[f"{b}/Frequency"], desk.raw[f"{b}/Gain"], desk.raw[f"{b}/Q"]) == v
    st = r.finish()
    assert st["phase"] == "finished" and not r.auto.state(0)["on"]


def _mic_on(desk, r):
    raw = desk.eq_raw(0, PATHS)
    m = ro._Mic(0, "In 1", "wig", "speech", -1000, 1, dict(raw), level=-1000, on=1, known_eq=dict(raw))
    r.mics = [m]
    return m


def test_notches_keep_the_bands_in_frequency_order():
    desk, eng, r = rig()
    desk.raw.update({"PEQ/Bank/Band:1/Frequency": 30000, "PEQ/Bank/Band:1/Gain": 200})  # the desk's own +2 dB at 3 kHz
    m = _mic_on(desk, r)
    for f in (620.0, 320.0, 1800.0):  # rung in this order, as on stage
        assert r._notch(m, f, -3.0)
        assert in_order(desk), desk.raw
    assert sorted(n["freq"] for n in m.notches.values()) == [320.0, 620.0, 1800.0]
    # The desk's 3 kHz band moved along (to H) instead of being lost, still a bell.
    assert desk.raw["PEQ/Bank/BandH/Frequency"] == 30000 and desk.raw["PEQ/Bank/BandH/Gain"] == 200
    assert desk.raw["PEQ/Bank/BandH/Type"] == 0


def test_a_low_ring_raises_the_high_pass_instead_of_a_notch():
    desk, eng, r = rig()
    m = _mic_on(desk, r)
    r._lowest_pitch = lambda index: 180.0  # a higher voice: room under it
    r._hpf_on(m)
    assert desk.raw["PEQ/Bank/HPF/On"] == 1 and desk.raw["PEQ/Bank/HPF/Frequency"] == 1000
    assert r._notch(m, 120.0, -2.0)
    assert desk.raw["PEQ/Bank/HPF/Frequency"] == 1550 and not m.notches  # 1.3 x the ring
    assert m.hpf_floor == 155.0 and m.rings[-1]["band"] == "HPF"
    r._lowest_pitch = lambda index: 100.0  # a low voice: the high-pass can't go over its fundamental
    m2 = _mic_on(desk, r)
    assert r._notch(m2, 125.0, -2.0) and m2.notches  # so a notch instead
