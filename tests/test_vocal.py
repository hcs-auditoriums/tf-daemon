"""Vocal EQ assist: suggestions from a mic check (never applied to the desk)."""

import time

import numpy as np
import pytest

from tf_daemon.app import build_modules, create_app
from tf_daemon.config import ConfigError, load_config
from tf_daemon.dsp.vocal import ANSI_FREQS, ANSI_NORMAL, VoiceCollector, analyze, bell_db
from tf_daemon.vocal import VocalModule

FS, HOP, N = 48000, 2400, 4096


def shape(x, gain_db):
    """Apply a magnitude response (dB as a function of Hz) to the whole signal."""
    f = np.maximum(np.fft.rfftfreq(len(x), 1 / FS), 1.0)
    return np.fft.irfft(np.fft.rfft(x) * 10 ** (gain_db(f) / 20), len(x))


def talker(seconds=22.0, f0_lo=95, f0_hi=180, seed=1):
    """A test voice: glottal pulses with gliding pitch, a little breath noise, shaped
    to the ANSI standard speech spectrum, in syllables; -20 dBFS while talking."""
    rng = np.random.default_rng(seed)
    n = int(seconds * FS)
    env, t = np.zeros(n), 0
    while t < n:
        on = int(rng.uniform(0.12, 0.4) * FS)
        env[t:t + on] = 1
        t += on + int(rng.uniform(0.05, 0.3) * FS)
    env = np.convolve(env, np.ones(480) / 480, "same")
    # Intonation: a new pitch target every ~70 ms, like running speech (a slower,
    # lumpier pitch track leaves harmonic ripple in the long-term spectrum).
    k = int(seconds * 14)
    f0 = np.interp(np.arange(n), np.linspace(0, n, k), rng.uniform(f0_lo, f0_hi, k))
    pulses = np.diff(np.floor(np.cumsum(f0 / FS)), prepend=0)
    voice = pulses + 0.02 * rng.standard_normal(n)
    voice = shape(voice, lambda f: np.interp(np.log(f), np.log(ANSI_FREQS), ANSI_NORMAL,
                                             left=ANSI_NORMAL[0], right=ANSI_NORMAL[-1]) - 30)
    sig = voice * env
    sig *= 0.1 / np.sqrt(np.mean(sig[env > 0.5] ** 2))
    return sig + 10 ** (-75 / 20) * rng.standard_normal(n)


def run(x):
    c = VoiceCollector(FS)
    for end in range(N, len(x) + 1, HOP):
        c.process(x[end - N:end], end / FS)
    return analyze(c)


@pytest.fixture(scope="module")
def voice():
    return talker()


def only_move(r):
    assert r["ok"], r.get("error")
    assert len(r["bands"]) == 1, r["bands"]
    return r["bands"][0]


# ---------------------------------------------------------------- analysis

def test_neutral_voices_get_no_eq_moves(voice):
    for r in (run(voice), run(talker(f0_lo=170, f0_hi=290, seed=4))):
        assert r["ok"] and r["bands"] == [], r["bands"]
        assert r["confidence"] == "good"


def test_hpf_sits_under_the_voice(voice):
    low, high = run(voice), run(talker(f0_lo=170, f0_hi=290, seed=4))
    assert 60 <= low["hpf"]["freq"] < 100 and low["voice"] == "lower voice"
    assert 120 <= high["hpf"]["freq"] < 180 and high["voice"] == "higher voice"


@pytest.mark.parametrize("freq,q,band,issue", [
    (350, 1.4, "L", "boxy"),
    (1000, 2.0, "1", "nasal / honky"),
    (3500, 1.5, "2", "harsh"),
])
def test_build_ups_are_found(voice, freq, q, band, issue):
    m = only_move(run(shape(voice, lambda f: bell_db(f, freq, 6, q))))
    assert m["band"] == band and m["issue"] == issue and m["kind"] == "cut"
    assert abs(np.log2(m["freq"] / freq)) < 0.25          # within a quarter octave
    assert -6 <= m["gain"] <= -3 and 1.0 <= m["q"] <= 3.0


def test_two_problems_at_once(voice):
    r = run(shape(voice, lambda f: bell_db(f, 400, 5, 1.2) + bell_db(f, 3200, 5, 1.5)))
    assert sorted((b["band"], b["issue"]) for b in r["bands"]) == [("2", "harsh"), ("L", "boxy")]


def test_dull_voice_gets_a_presence_boost(voice):
    m = only_move(run(shape(voice, lambda f: -6 / (1 + (3000 / f) ** 2))))
    assert m["kind"] == "boost" and 0 < m["gain"] <= 3 and m["freq"] >= 3000


def test_narrow_resonance_gets_a_notch(voice):
    m = only_move(run(shape(voice, lambda f: bell_db(f, 2200, 12, 20))))
    assert m["kind"] == "notch" and m["q"] >= 6 and abs(m["freq"] - 2200) < 30 and m["band"] == "2"


def test_rings_on_the_input_become_notches(voice):
    c = VoiceCollector(FS)
    for end in range(N, len(voice) + 1, HOP):
        c.process(voice[end - N:end], end / FS)
    r = analyze(c, rings=[{"freq": 2510.0, "count": 3}])
    assert [(b["issue"], b["freq"], b["kind"]) for b in r["bands"]] == [("ring (3× on this input)", 2510.0, "notch")]


def test_limits_are_respected(voice):
    r = run(shape(voice, lambda f: bell_db(f, 350, 15, 1.4)))
    assert min(b["gain"] for b in r["bands"]) >= -6.0
    c = VoiceCollector(FS)
    x = shape(voice, lambda f: bell_db(f, 350, 15, 1.4))
    for end in range(N, len(x) + 1, HOP):
        c.process(x[end - N:end], end / FS)
    assert min(b["gain"] for b in analyze(c, max_cut_db=3.0)["bands"]) >= -3.0


def test_not_enough_speech_or_not_a_voice(voice):
    assert not run(voice[: 3 * FS])["ok"]
    r = run(np.random.default_rng(3).standard_normal(20 * FS) * 0.05)
    assert not r["ok"] and "speech" in r["error"]


# ---------------------------------------------------------------- module

def _engine_and_module(**vocal):
    cfg = load_config(None)
    cfg["vocal"].update(enabled=True, listen_s=10.0, **vocal)
    modules = build_modules(cfg, "synth", rf_source="off", qlxd_source="off")
    return cfg, modules, modules[0].engine, next(m for m in modules if m.name == "vocal")


def feed(module, index, x, t0=0.0):
    for end in range(N, len(x) + 1, HOP):
        module._tap(index, x[end - N:end].astype(np.float32), t0 + end / FS)


def test_session_listens_then_suggests(voice):
    _, _, engine, vm = _engine_and_module()
    vm.start_session(5)
    assert 5 in engine.active_inputs()                       # kept analyzed for the check
    assert vm.snapshot()["sessions"]["5"]["state"] == "listening"
    feed(vm, 5, shape(voice, lambda f: bell_db(f, 350, 6, 1.4)))
    s = vm.snapshot()["sessions"]["5"]
    assert s["state"] == "done" and s["result"]["ok"]            # finished itself at listen_s of speech
    assert s["speech_s"] >= 10 and [b["issue"] for b in s["result"]["bands"]] == ["boxy"]
    assert s["current_eq"] is None                               # no TF5 EQ reader configured
    assert 5 not in engine.active_inputs()                       # released when done
    vm.clear_session(5)
    assert vm.snapshot()["sessions"] == {}


def test_stop_early_and_cancel(voice):
    _, _, engine, vm = _engine_and_module()
    vm.start_session(2)
    feed(vm, 2, voice[: 8 * FS])
    vm.stop_session(2)                                           # analyze what was heard
    assert vm.snapshot()["sessions"]["2"]["state"] in ("done", "error")
    vm.start_session(3)
    vm.clear_session(3)                                          # cancel
    assert "3" not in vm.snapshot()["sessions"] and 3 not in engine.active_inputs()


def test_only_listed_inputs():
    _, _, _, vm = _engine_and_module(inputs=[4, 5])
    with pytest.raises(ValueError, match="vocal.inputs"):
        vm.start_session(0)
    vm.start_session(4)
    assert vm.snapshot()["inputs"] == [4, 5]


def test_routes_and_off_by_default():
    cfg = load_config(None)
    assert cfg["vocal"]["enabled"] is False
    modules = build_modules(cfg, "synth", rf_source="off", qlxd_source="off")
    assert "vocal" not in [m.name for m in modules]
    app = create_app(cfg, modules)
    try:
        assert app.test_client().get("/api/vocal/status").status_code == 404
    finally:
        app.extensions["tf_stop"]()

    cfg, modules, _, _ = _engine_and_module()
    app = create_app(cfg, modules)
    try:
        c = app.test_client()
        assert c.get("/api/vocal/status").get_json()["enabled"]
        assert c.post("/api/vocal/start", json={"channel": "x"}).status_code == 400
        assert c.post("/api/vocal/start", json={"channel": 1}).get_json()["ok"]
        assert c.get("/api/vocal/status").get_json()["sessions"]["1"]["state"] == "listening"
        assert c.post("/api/vocal/stop", json={"channel": 1}).get_json()["ok"]
        assert c.post("/api/vocal/clear", json={"channel": 1}).get_json()["ok"]
    finally:
        app.extensions["tf_stop"]()


def test_config_validation(tmp_path):
    p = tmp_path / "c.toml"
    for bad in ("[vocal]\nenabled = 1\n", "[vocal]\ninputs = [-1]\n", "[vocal]\nlisten_s = 2\n",
                "[vocal]\nmax_boost_db = 9\n"):
        p.write_text(bad)
        with pytest.raises(ConfigError, match="vocal"):
            load_config(p)


def test_engine_taps_see_analyzed_inputs():
    cfg = load_config(None)
    modules = build_modules(cfg, "synth", rf_source="off", qlxd_source="off")
    seen = set()
    modules[0].engine.add_tap(lambda i, samples, t: seen.add((i, len(samples))))
    app = create_app(cfg, modules)
    try:
        deadline = time.time() + 5
        while not seen and time.time() < deadline:
            time.sleep(0.05)
        assert {i for i, _ in seen} <= {32, 33} and seen and all(n == 32768 for _, n in seen)
    finally:
        app.extensions["tf_stop"]()
