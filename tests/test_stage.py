"""The simulated stage (audio/stage.py): the desk shapes the mains, the loop rings, the room is heard."""

import numpy as np
import pytest
from helpers import make_config, wait_until

from tf_daemon.app import build_modules, build_source, create_app, stage_settings
from tf_daemon.audio.stage import FlatDesk, StageSettings, StageSource
from tf_daemon.dsp.eq import eq_curve

FS, N = 48000, 1200


class Desk(FlatDesk):
    def __init__(self, fader=0.0, eq=None):
        self.fader, self.eq = fader, eq

    def channel_eq(self, x):
        return self.eq

    def fader_db(self, x):
        return self.fader


def bell(f, g, q):
    flat = {"type": "bell", "on": True, "gain": 0.0, "q": 1.0}
    return {
        "on": True,
        "bank": "PRECISE",
        "one_knob": False,
        "hpf": None,
        "bands": [
            {**flat, "label": "L", "freq": 100.0},
            {**flat, "label": "LM", "freq": f, "gain": g, "q": q},
            {**flat, "label": "HM", "freq": 3000.0},
            {**flat, "label": "H", "freq": 8000.0},
        ],
    }


def run(desk, seconds, leak=-40.0, talkers=(0,), house=31):
    st = StageSettings(talkers=list(talkers), mains=(32, 33), house_mic=house, pa_leak_db=leak, turn_s=(60, 60))
    src = StageSource(34, N, st, desk)
    b = np.zeros((N, 34), np.float32)
    out = []
    for _ in range(int(seconds * FS / N)):
        src._fill(b)
        out.append(b.copy())
    return src, np.concatenate(out)


def level_at(x, f, nfft=1 << 15):
    spec = np.mean(
        [np.abs(np.fft.rfft(x[i : i + nfft] * np.hanning(nfft))) ** 2 for i in range(0, len(x) - nfft + 1, nfft // 2)],
        axis=0,
    )
    k = int(round(f * nfft / FS))
    return 10 * np.log10(spec[k - 30 : k + 31].mean() + 1e-30)


def test_the_mains_carry_the_desk_eq():
    eq = bell(1000.0, 10.0, 1.0)
    _, x = run(Desk(0.0, eq), 6.0)
    for f in (300.0, 1000.0, 3000.0):
        measured = level_at(x[:, 32], f) - level_at(x[:, 0], f)
        assert measured == pytest.approx(float(eq_curve(eq, np.array([f]))[0]), abs=1.0), f
    assert np.array_equal(x[:, 32], x[:, 33])  # mono mains on both stereo-bus channels


def test_a_channel_that_is_off_is_not_in_the_mains():
    _, x = run(Desk(None), 2.0)
    assert np.max(np.abs(x[:, 32])) < 1e-9 < np.max(np.abs(x[:, 0]))


@pytest.mark.parametrize("fader, grows", [(-10.0, False), (14.0, True)])
def test_too_much_gain_rings_at_the_leak_resonance(fader, grows):
    _, x = run(Desk(fader), 4.0, leak=-18.0)
    first, last = level_at(x[:FS, 32], 2480.0), level_at(x[-FS:, 32], 2480.0)
    assert bool(last - first > 6) == grows
    assert np.max(np.abs(x[:, 32])) <= 0.9 + 1e-6  # the limiter keeps it bounded


def test_the_house_mic_hears_the_mains_after_the_room_delay():
    src, x = run(Desk(0.0), 3.0)
    mains, house = x[:, 32], x[:, 31]
    n = 1 << 18
    xc = np.fft.irfft(np.fft.rfft(house[-n:]) * np.conj(np.fft.rfft(mains[-n:])))
    lag = int(np.argmax(xc[: FS // 10]))
    assert abs(lag - round(12.0 / 343.0 * FS + 256)) <= 2  # 12 m at 343 m/s, + the room colour's latency


def test_talkers_take_turns():
    st = StageSettings(talkers=[0, 1], mains=(32, 33), house_mic=None, turn_s=(1.0, 2.0))
    src = StageSource(34, N, st, Desk(0.0))
    b = np.zeros((N, 34), np.float32)
    seen = set()
    for _ in range(int(20 * FS / N)):
        src._fill(b)
        seen.add(src.speaking)
    assert {0, 1} <= seen


def test_cheap_enough_to_run_live():
    import time

    st = StageSettings(talkers=[0, 1], mains=(32, 33), house_mic=31)
    src = StageSource(34, N, st, Desk(0.0))
    b = np.zeros((N, 34), np.float32)
    t0 = time.perf_counter()
    for _ in range(40):  # one second of audio
        src._fill(b)
    assert time.perf_counter() - t0 < 0.5


def test_config_builds_the_stage_from_the_patch():
    cfg = make_config(audio={"source": "stage"}, channels=[{"index": 0}, {"index": 1}, {"index": 32}, {"index": 33}])
    s = stage_settings(cfg)
    assert (s.talkers, s.mains, s.house_mic) == ([0, 1], (32, 33), 31)
    assert isinstance(build_source(cfg), StageSource)


def test_demo_desk_eq_changes_reach_the_simulated_mains():
    """In demo mode the stage and the TF5 module share one simulated console."""
    cfg = make_config(
        audio={"source": "stage"},
        tf5={"source": "demo"},
        channels=[{"index": 0, "label": "Pastor"}, {"index": 32}, {"index": 33}],
    )
    modules = build_modules(cfg)
    app = create_app(cfg, modules)
    try:
        tf5 = next(m for m in modules if m.name == "tf5")
        stage = modules[0].engine.source
        wait_until(lambda: tf5.eq_client.synced)
        assert stage.desk is tf5.simulator
        before = stage.desk.channel_eq(0)
        tf5.write_eq(0, [("PEQ/Bank/Band:0/Gain", -900)])
        wait_until(lambda: stage.desk.channel_eq(0) != before)
        assert stage.desk.channel_eq(0)["bands"][1]["gain"] == -9.0
    finally:
        app.extensions["tf_stop"]()
