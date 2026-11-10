"""Auto EQ (vocal/auto.py), in a closed loop with the simulated stage, offline."""

import pytest
from test_vocal_live import BLOCK, FS, OfflineEngine

from tf_daemon.audio.stage import FlatDesk, StageSettings, StageSource
from tf_daemon.vocal.auto import PATHS, STEP_DB, AutoEQ
from tf_daemon.vocal.session import MicChecks


class RawDesk(FlatDesk):
    """Channel 0's EQ as the TF5 holds it (raw values); readable and writable like
    TF5Module (DeskEQ), and heard by the stage (DeskMix)."""

    eq_available = True
    eq_writable = True

    def __init__(self):
        self.raw = {"PEQ/On": 1, "PEQ/Bank/HPF/On": 0, "PEQ/Bank/HPF/Frequency": 800}
        for b, t, f in (("BandL", 1, 1250), ("Band:0", 0, 8000), ("Band:1", 0, 25000), ("BandH", 1, 80000)):
            self.raw.update(
                {f"PEQ/Bank/{b}/Type": t, f"PEQ/Bank/{b}/Frequency": f, f"PEQ/Bank/{b}/Gain": 0, f"PEQ/Bank/{b}/Q": 700}
            )
        self.writes = []

    def _eq(self):
        r, bands = self.raw, []
        for b, label, low, high in (
            ("BandL", "L", True, False),
            ("Band:0", "LM", False, False),
            ("Band:1", "HM", False, False),
            ("BandH", "H", False, True),
        ):
            t = r[f"PEQ/Bank/{b}/Type"]
            kind = (
                "bell"
                if t == 0 or not (low or high)
                else ("low_shelf" if low else "high_shelf")
                if t == 1
                else ("hpf" if low else "lpf")
            )
            bands.append(
                {
                    "label": label,
                    "type": kind,
                    "on": True,
                    "freq": r[f"PEQ/Bank/{b}/Frequency"] / 10,
                    "gain": r[f"PEQ/Bank/{b}/Gain"] / 100,
                    "q": r[f"PEQ/Bank/{b}/Q"] / 1000,
                }
            )
        return {
            "on": bool(r["PEQ/On"]),
            "bank": "PRECISE",
            "one_knob": False,
            "bands": bands,
            "hpf": {"on": bool(r["PEQ/Bank/HPF/On"]), "freq": r["PEQ/Bank/HPF/Frequency"] / 10, "slope": 12},
        }

    # DeskMix (the stage)
    def channel_eq(self, x):
        return self._eq() if x == 0 else None

    # DeskEQ (the vocal assist)
    def eq(self, usb):
        return {"eq": self._eq() if usb == 0 else None}

    def eq_target(self, usb):
        return "InputChannel:0" if usb == 0 else None

    def eq_raw(self, usb, paths):
        return {p: self.raw.get(p) for p in paths}

    def write_eq(self, usb, changes):
        self.writes.append(list(changes))
        for p, v in changes:
            self.raw[p] = v
        return [{"path": p, "value": v, "confirmed": True, "actual": v} for p, v in changes]


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


def test_auto_walks_the_eq_to_the_suggestion_gently():
    desk, eng, checks, auto = rig()
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


def test_undo_puts_back_the_eq_from_before():
    desk, eng, checks, auto = rig()
    before = dict(desk.raw)
    auto.arm(0)
    run(eng, auto, 120)
    assert desk.raw != before
    auto.undo(0)
    assert desk.raw == before and not auto.state(0)["on"]


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
