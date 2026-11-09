"""Live listening (vocal/session.py) against the simulated stage, run offline: the
rolling suggestion finds the talker's colour, and the mains check tells whether
the voice reaches the mains."""

import numpy as np
import pytest

from tf_daemon.audio.stage import FlatDesk, StageSettings, StageSource
from tf_daemon.config import Channel
from tf_daemon.dsp.ring import RingLog
from tf_daemon.vocal.session import MicChecks

FS, BLOCK, HOP, FRAME = 48000, 1200, 2400, 32768


class Desk(FlatDesk):
    def __init__(self, fader=0.0):
        self.fader = fader

    def fader_db(self, x):
        return self.fader


class OfflineEngine:
    """Just enough of the audio engine for MicChecks: frames from the stage, hop by hop."""

    def __init__(self, source, analyzed):
        self.source, self.analyzed = source, analyzed
        self.inputs = [Channel(i, f"In {i + 1}") for i in range(source.channels)]
        self.rings = RingLog()
        self.frame_taps = []
        self.buf = np.zeros((FRAME, source.channels), np.float32)
        self.t = 0.0

    def add_tap(self, tap):
        pass

    def add_frame_tap(self, tap):
        self.frame_taps.append(tap)

    def acquire(self, index):
        pass

    def release(self, index):
        pass

    def run(self, seconds):
        block = np.zeros((BLOCK, self.source.channels), np.float32)
        for _ in range(int(seconds * FS / HOP)):
            for _ in range(HOP // BLOCK):
                self.source._fill(block)
                self.buf = np.concatenate((self.buf[BLOCK:], block))
            self.t += HOP / FS
            frames = {i: self.buf[:, i] for i in self.analyzed}
            for tap in self.frame_taps:
                tap(frames, self.t)


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


def test_live_suggestion_finds_a_boomy_voice_and_keeps_listening():
    checks = listen(colours=[[(240.0, 8.0, 1.2)]])
    s = dict(checks.sessions())[0]
    assert s.state == "live" and s.result is not None and s.result["ok"]
    cuts = [b for b in s.result["bands"] if b["gain"] < 0]
    assert any(150 <= b["freq"] <= 400 for b in cuts), s.result["bands"]
    assert checks.result(0) is s.result          # appliable while live
    checks.stop(0)
    assert dict(checks.sessions())[0].state == "done" and checks.result(0)["ok"]


def test_mains_check_measures_the_channel_gain():
    checks = listen(fader=-6.0)
    mains = checks.result(0)["mains"]
    assert mains["verdict"] == "ok" and mains["coherence"] > 0.8
    assert mains["gain_db"] == pytest.approx(-6.0, abs=1.5)
    assert mains["delay_ms"] == pytest.approx(256 / FS * 1000, abs=0.5)   # the desk EQ's latency


def test_mains_check_notices_a_channel_that_is_off():
    checks = listen(fader=None)
    assert checks.result(0)["mains"]["verdict"] in ("weak", "unknown")


def test_a_mains_ring_this_mic_feeds_becomes_a_notch_candidate():
    eng = OfflineEngine(stage(), analyzed=[0])
    checks = MicChecks(eng, FS, inputs=None, target_s=20.0, max_cut_db=6.0, max_boost_db=3.0)
    eng.rings.update(32, "Main L", [{"freq": 2480.0, "kind": "sustained", "prominence": 15, "level": -20}], 0.0)
    eng.rings.set_sources(32, 2480.0, [(0, "In 1", -20.0), (5, "In 6", -40.0)], "level")
    assert any(abs(r["freq"] - 2480.0) < 20 for r in checks.rings_for(0))
    assert checks.rings_for(5) == []   # not the strongest source
