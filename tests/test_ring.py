"""Ring detector: real rings must be found (in time), musical/speech material must not be.

Signals are synthesized at 48 kHz and fed hop by hop, as the engine does.
"""

import numpy as np
import pytest
from helpers import make_config, wait_until

from tf_daemon.app import build_modules, create_app
from tf_daemon.audio.sources import SynthSource, _PacedSource, make_pink_noise
from tf_daemon.config import ConfigError, load_config
from tf_daemon.dsp.ring import RingDetector, RingLog, RingSettings, geq_band

FS, HOP, BLOCK = 48000, 2400, 1200


def speech(seconds, tone_db=-200.0, tone_hz=2500.0, seed=3):
    """The synth's voice: gliding harmonics in syllables (plus an optional steady tone)."""
    src = SynthSource(
        channels=1,
        active=[0],
        blocksize=BLOCK,
        speech_db=-20,
        noise_db=-60,
        tone_hz=tone_hz,
        tone_db=tone_db,
        seed=seed,
    )
    blk = np.zeros((BLOCK, 1), np.float32)
    out = []
    for _ in range(int(seconds * FS / BLOCK)):
        src._fill(blk)
        out.append(blk[:, 0].copy())
    return np.concatenate(out).astype(np.float64)


def noise(seconds, db, seed=1):
    return make_pink_noise(1 << 20, np.random.default_rng(seed))[: int(seconds * FS)] * 10 ** (db / 20) / np.sqrt(2)


def tone(seconds, f, db):
    t = np.arange(int(seconds * FS)) / FS
    return 10 ** (np.asarray(db(t) if callable(db) else db) / 20) * np.sin(2 * np.pi * f * t)


def feedback_loop(x, f, g, delay_s=0.03, q=2.0):
    """A sound system just below feedback: y = x + g * bandpass(y delayed); loop gain g at f."""
    d = int(delay_s * FS)
    w = 2 * np.pi * f / FS
    alpha = np.sin(w) / (2 * q)
    a0 = 1 + alpha
    b0, b2, a1, a2 = alpha / a0, -alpha / a0, -2 * np.cos(w) / a0, (1 - alpha) / a0
    y = np.zeros(len(x) + d)
    u1 = u2 = v1 = v2 = 0.0
    for i in range(len(x)):
        u = y[i]
        v = b0 * u + b2 * u2 - a1 * v1 - a2 * v2
        u2, u1, v2, v1 = u1, u, v1, v
        y[i + d] = x[i] + g * v
    return y[d:]


def reverb(x, rt60=1.0, seed=7):
    n = int(rt60 * FS)
    t = np.arange(n) / FS
    ir = np.random.default_rng(seed).standard_normal(n) * 10 ** (-3 * t / rt60)
    ir[0] = 1.0
    ir /= np.sqrt(np.sum(ir**2))
    size = 1 << (len(x) + n - 1).bit_length()
    return np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(ir, size), size)[: len(x)]


def detect(x, settings=RingSettings()):
    """{(freq rounded to 10 Hz, kind): first time seen (s)}."""
    det = RingDetector(FS, HOP, settings)
    first = {}
    for end in range(settings.n_fft, len(x) + 1, HOP):
        for r in det.process(x[end - settings.n_fft : end], end / FS):
            first.setdefault((round(r["freq"] / 10) * 10, r["kind"]), end / FS)
    return first


def found_near(first, f, within_s, kinds=("tail", "sustained", "growing")):
    return any(abs(k[0] - f) <= 0.02 * f and k[1] in kinds and t <= within_s for k, t in first.items())


# ---------------------------------------------------------------- must detect


@pytest.mark.parametrize("tone_db", [-5, -15, -25])
def test_steady_ring_quieter_than_the_voice(tone_db):
    first = detect(speech(6, tone_db=tone_db))
    assert found_near(first, 2500, 2.0)
    assert found_near(first, 2500, 2.5, kinds=("sustained",))
    assert all(abs(k[0] - 2500) < 50 for k in first)  # nothing else


def test_runaway_feedback_is_growing():
    first = detect(noise(6, -50) + tone(6, 1730, lambda t: np.minimum(-60 + 12 * t, -6)))
    assert found_near(first, 1730, 2.0, kinds=("growing",))


def test_ring_left_behind_after_each_phrase():
    """Bursts of programme excite a resonance that decays at 20 dB/s; the room dies at 120 dB/s."""
    t = np.arange(int(6 * FS)) / FS
    on = (t % 1.5) < 0.4
    since_off = np.where(on, 0.0, (t % 1.5) - 0.4)
    programme = noise(6, -20) * np.where(on, 1.0, 10 ** (-120 * since_off / 20))
    resonance = tone(6, 3150, lambda tt: np.where(on, -30, -30 - 20 * since_off))
    first = detect(programme + resonance + noise(6, -80, 5))
    assert found_near(first, 3150, 1.0, kinds=("tail",))


def test_speech_through_a_loop_just_below_feedback():
    """The real thing: the voice through a loop with gain 0.95 (+26 dB, ~15 dB/s decay) near 2 kHz.

    The ring is excited only by the speech and comes and goes with it.
    """
    first = detect(feedback_loop(speech(6), 2000, 0.95))
    assert found_near(first, 2000, 1.5) or any(1800 <= k[0] <= 2400 and t <= 1.5 for k, t in first.items())


# ---------------------------------------------------------------- must not detect


@pytest.mark.parametrize("seed", [3, 11])
def test_speech_alone(seed):
    assert detect(speech(12, seed=seed)) == {}


def test_reverberant_speech():
    assert detect(reverb(speech(12))) == {}


def test_notes_that_die_with_the_room():
    """A new pitch each burst, decaying as fast as the (reverberant) programme."""
    t = np.arange(int(9 * FS)) / FS
    on = (t % 1.5) < 0.4
    since_off = np.where(on, 0.0, (t % 1.5) - 0.4)
    room = np.where(on, 1.0, 10 ** (-120 * since_off / 20))
    notes = np.concatenate(
        [tone(1.5, 600 + 137 * i, lambda tt: np.where(tt < 0.4, -30, -30 - 120 * (tt - 0.4))) for i in range(6)]
    )
    assert detect(noise(9, -20) * room + notes + noise(9, -80, 5)) == {}


def test_sung_note_with_vibrato():
    t = np.arange(int(8 * FS)) / FS
    vib = 0.012 * np.sin(2 * np.pi * 5.5 * t) / (2 * np.pi * 5.5) * 2 * np.pi * 330  # phase deviation, 1.2% depth
    note = sum((0.05 / k) * np.sin(2 * np.pi * k * 330 * t + k * vib * 2 * np.pi) for k in range(1, 7))
    assert detect(note + noise(8, -60)) == {}


def test_steady_organ_chord():
    t = np.arange(int(8 * FS)) / FS
    chord = sum((0.05 / k) * np.sin(2 * np.pi * k * 220 * t) for k in range(1, 6)) + sum(
        (0.04 / k) * np.sin(2 * np.pi * k * 277.2 * t) for k in range(1, 6)
    )
    assert detect(chord + noise(8, -60)) == {}


def test_mains_hum_is_skipped():
    t = np.arange(int(6 * FS)) / FS
    hum = sum(0.003 / k * np.sin(2 * np.pi * 60 * k * t) for k in (1, 2, 3, 5))
    assert detect(hum + noise(6, -70)) == {}
    unfiltered = detect(hum + noise(6, -70), RingSettings(mains_hz=0))  # the hum filter is what keeps it quiet
    assert any(found_near(unfiltered, f, 6.0) for f in (120, 180, 300))


# ---------------------------------------------------------------- event log


def test_ringlog_counts_comebacks_and_ignores():
    log = RingLog()
    ring = [{"freq": 2500.0, "level": -30.0, "prominence": 20.0, "kind": "tail"}]
    log.update(0, "Main L", ring, 100.0)
    log.update(0, "Main L", [], 100.5)  # a flicker: still live (grace period)
    assert log.snapshot()["events"][0]["active"]
    log.update(0, "Main L", ring, 101.0)
    assert log.snapshot()["events"][0]["count"] == 1
    log.update(0, "Main L", [], 103.0)  # gone for good
    ev = log.snapshot()["events"][0]
    assert not ev["active"] and ev["geq"] == 2500
    log.update(0, "Main L", [{**ring[0], "freq": 2510.0, "kind": "sustained"}], 110.0)  # back, within 1%
    ev = log.snapshot()["events"][0]
    assert (ev["count"], ev["kind"], ev["active"]) == (2, "sustained", True)
    log.update(1, "Main R", ring, 110.0)  # same frequency, other input: its own event
    assert len(log.snapshot()["events"]) == 2

    log.ignore(2500.0)
    assert log.snapshot()["events"] == [] and log.snapshot()["ignored"] == [2500.0]
    log.update(0, "Main L", ring, 120.0)
    assert log.snapshot()["events"] == []
    log.unignore()
    log.update(0, "Main L", ring, 121.0)
    assert len(log.snapshot()["events"]) == 1
    log.end_channel(0)
    assert not log.snapshot()["events"][0]["active"]


def test_geq_band():
    assert (geq_band(2480), geq_band(1100), geq_band(90)) == (2500, 1000, 100)


def test_config(tmp_path):
    cfg = load_config(None)
    assert cfg.ring.enabled and cfg.ring.sensitivity == "normal"
    p = tmp_path / "c.toml"
    for bad in ('[ring]\nsensitivity = "max"\n', "[ring]\nfmin = 5000\nfmax = 100\n", "[ring]\nmains_hz = 55\n"):
        p.write_text(bad)
        with pytest.raises(ConfigError, match="ring"):
            load_config(p)


# ---------------------------------------------------------------- in the app


def test_engine_detects_the_synth_ring_and_routes_work():
    cfg = make_config()  # the synth carries a steady 2.5 kHz tone 15 dB under its voice
    modules = build_modules(cfg)
    app = create_app(cfg, modules)
    try:
        client = app.test_client()
        rings = lambda: client.get("/api/audio/rings").get_json()["events"]  # noqa: E731
        wait_until(lambda: any(e["active"] and abs(e["freq"] - 2500) < 10 for e in rings()), timeout=8)
        events = rings()
        assert {e["label"] for e in events} <= {"Main L", "Main R"}
        assert client.get("/api/status").get_json()["modules"]["audio"]["rings_active"] >= 1

        assert client.post("/api/audio/rings/ignore", json={"freq": "x"}).status_code == 400
        assert client.post("/api/audio/rings/ignore", json={"freq": 2500}).get_json()["ok"]
        assert client.get("/api/audio/rings").get_json()["ignored"] == [2500.0]
        assert client.post("/api/audio/rings/unignore", json={}).get_json()["ok"]
        assert client.post("/api/audio/rings/clear").get_json()["ok"]
    finally:
        app.extensions["tf_stop"]()


def test_ring_detection_can_be_turned_off():
    cfg = make_config(ring={"enabled": False})
    modules = build_modules(cfg)
    app = create_app(cfg, modules)
    try:
        # 1.5 s of analyzed audio: with detection on, the synth tone rings well before that.
        wait_until(lambda: modules[0].engine.status()["frames"] >= 30)
        assert app.test_client().get("/api/audio/rings").get_json()["events"] == []
    finally:
        app.extensions["tf_stop"]()


# ---------------------------------------------------------------- which input feeds a ring on the mains


class _StageSource(_PacedSource):
    """Two mics hearing a ring at different levels, a third with only noise, and a Main mix.

    The 'mains' (input 3) carry the sum, so a ring is detected there; attribution
    must rank mic A first, then mic B, and leave out the silent input.
    """

    name = "stage"

    def __init__(self, a_db=-30.0, b_db=-38.0):
        super().__init__(channels=4, blocksize=BLOCK)
        self._amps = (10 ** (a_db / 20), 10 ** (b_db / 20))
        self._noise = [make_pink_noise(1 << 19, np.random.default_rng(s)) * 10 ** (-70 / 20) for s in (1, 2, 3)]
        self._pos = 0
        self._phase = 0.0

    def _fill(self, block):
        n = block.shape[0]
        ph = self._phase + 2 * np.pi * 2000.0 / FS * np.arange(1, n + 1)
        self._phase = float(ph[-1] % (2 * np.pi))
        ring = np.sin(ph)
        sl = slice(self._pos, self._pos + n)
        self._pos = (self._pos + n) % ((1 << 19) - n)
        block[:, 0] = self._amps[0] * ring + self._noise[0][sl]
        block[:, 1] = self._amps[1] * ring + self._noise[1][sl]
        block[:, 2] = self._noise[2][sl]
        block[:, 3] = block[:, 0] + block[:, 1]


def _wait_sources(engine, pred, timeout=8.0):
    def found():
        return next(
            (
                e
                for e in engine.rings.snapshot()["events"]
                if e["active"] and e["channel"] == 3 and e["sources"] and pred(e)
            ),
            None,
        )

    wait_until(lambda: found() is not None, timeout=timeout)
    return found()


def test_ring_on_the_mains_is_traced_to_its_inputs():
    from tf_daemon.audio.engine import AudioEngine

    cfg = make_config(channels=[{"index": 3, "label": "Main"}])  # the bus: always analyzed, rings get attributed
    engine = AudioEngine(cfg, _StageSource())
    engine.set_console_names({0: "Mic A", 1: "Mic B", 2: "Spare"})
    engine.start()
    try:
        # Without fader information: ranked by level at the ring frequency.
        e = _wait_sources(engine, lambda e: len(e["sources"]) == 2)
        assert [s["label"] for s in e["sources"]] == ["Mic A", "Mic B"]
        assert e["sources"][1]["rel_db"] == pytest.approx(-8.0, abs=1.5)
        assert e["sources_basis"] == "level" and abs(e["freq"] - 2000) < 5

        # With the console's faders: level + fader. Mic B pushed up beats Mic A pulled down.
        engine.set_console_gains({0: -10.0, 1: 5.0, 2: 0.0})
        # (Contributions are smoothed over ~1 s so one measurement can't reorder the list.)
        e = _wait_sources(
            engine,
            lambda e: (
                e["sources"][0]["label"] == "Mic B" and len(e["sources"]) == 2 and e["sources"][1]["rel_db"] <= -6.0
            ),
        )
        assert e["sources_basis"] == "fader"
        assert e["sources"][1]["rel_db"] == pytest.approx(-7.0, abs=1.5)  # (-30-10) - (-38+5)

        # A channel that is off isn't in the loop at all.
        engine.set_console_gains({0: 0.0, 1: None, 2: 0.0})
        e = _wait_sources(engine, lambda e: [s["label"] for s in e["sources"]] == ["Mic A"])
    finally:
        engine.stop()


def test_narrowband_levels():
    from tf_daemon.dsp.ring import narrowband_levels
    from tf_daemon.dsp.spectrum import blackman_harris

    n = 16384
    t = np.arange(n) / FS
    frames = np.stack([0.5 * np.sin(2 * np.pi * 1000 * t), 0.05 * np.sin(2 * np.pi * 3000 * t)])
    lv = narrowband_levels(frames, [1000.0, 3000.0], FS, blackman_harris(n))
    assert lv[0, 0] == pytest.approx(-6.0, abs=0.2) and lv[1, 1] == pytest.approx(-26.0, abs=0.2)
    assert lv[0, 1] < -100 and lv[1, 0] < -100
