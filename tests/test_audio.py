import struct
import time
import wave

import numpy as np
import pytest
from helpers import make_config

from tf_daemon.app import build_modules, create_app
from tf_daemon.audio.engine import MAGIC
from tf_daemon.audio.ringbuffer import RingBuffer
from tf_daemon.audio.sources import SourceError, SynthSource, WavSource
from tf_daemon.config import Channel, ConfigError, DspConfig, load_config
from tf_daemon.dsp.spectrum import SpectrumAnalyzer


def test_ringbuffer_selects_columns_and_wraps():
    rb = RingBuffer([2, 0], capacity=10)
    for start in range(0, 25, 7):
        block = np.arange(start, start + 7, dtype=np.float32)[:, None] * np.array([1, 10, 100], np.float32)
        rb.write_device_block(block)
    out = np.zeros((2, 6), np.float32)
    assert rb.read_latest(out) == 28
    np.testing.assert_array_equal(out[0], np.arange(22, 28) * 100)
    np.testing.assert_array_equal(out[1], np.arange(22, 28))


def test_config_defaults_and_partial(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[dsp]\nfft_size = 16384\n[[channels]]\nindex = 5\nlabel = "Vox"\n')
    cfg = load_config(p)
    assert cfg.dsp.fft_size == 16384
    assert cfg.dsp.hop == DspConfig().hop
    assert cfg.channels == (Channel(5, "Vox"),)
    assert load_config(tmp_path / "missing.toml").audio.samplerate == 48000


def test_config_rejects_other_samplerates(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[audio]\nsamplerate = 44100\n")
    with pytest.raises(ConfigError):
        load_config(p)


def _run_synth(tone_db, seconds=6):
    fs, n = 48000, 32768
    src = SynthSource(
        channels=2, active=[0, 1], blocksize=1200, speech_db=-20, noise_db=-50, tone_hz=2500, tone_db=tone_db, seed=3
    )
    an = SpectrumAnalyzer(2, fs, n, 2400, 3.0, 50, 16000, 48)
    block = np.zeros((1200, 2), np.float32)
    frame = np.zeros((2, n), np.float32)
    rb = RingBuffer([0, 1], fs * 2)
    for _ in range(fs * seconds // 1200):
        src._fill(block)
        rb.write_device_block(block)
        if rb.written >= n and rb.written % 2400 == 0:
            rb.read_latest(frame)
            an.process(frame)
    return an


def test_synth_tone_is_at_expected_level():
    with_tone, without = _run_synth(-15), _run_synth(-200)  # same seed -> same voice/noise
    i = int(np.argmin(np.abs(with_tone.bins.centers - 2500)))
    p_tone = 10 ** (with_tone.avg_db[0, i] / 10) - 10 ** (without.avg_db[0, i] / 10)
    # speech -20 dBFS RMS, tone -15 dB below -> -35 dBFS (within window scalloping).
    assert 10 * np.log10(p_tone) == pytest.approx(-35, abs=1.0)
    assert with_tone.avg_db[0, i] - without.avg_db[0, i] > 3


def test_wav_source_reads_and_rejects_rate(tmp_path):
    good = tmp_path / "a.wav"
    with wave.open(str(good), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(3)
        w.setframerate(48000)
        v = np.array([[1 << 22, -(1 << 22)]] * 2400, dtype=np.int32)
        b = v.astype("<i4").tobytes()
        w.writeframes(b"".join(b[i : i + 3] for i in range(0, len(b), 4)))
    src = WavSource(good, 1200)
    blk = np.zeros((1200, 2), np.float32)
    src._fill(blk)
    np.testing.assert_allclose(blk[0], [0.5, -0.5])

    bad = tmp_path / "b.wav"
    with wave.open(str(bad), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\0\0" * 4410)
    with pytest.raises(SourceError, match="48000"):
        WavSource(bad, 1200)


def test_engine_end_to_end_with_synth():
    cfg = make_config()
    modules = build_modules(cfg)
    app = create_app(cfg, modules)
    try:
        engine = modules[0].engine
        deadline = time.time() + 5
        seq, payload = 0, None
        while time.time() < deadline and payload is None:
            seq, payload = engine.hub.wait(seq, timeout=0.5)
        assert payload is not None, "no frame within 5 s"
        nb = engine.analyzer.bins.count
        assert _records(payload, nb) == [32, 33]  # the listed inputs (Main L/R) are always analyzed

        client = app.test_client()
        meta = client.get("/api/audio/meta").get_json()
        pinned = [(c["index"], c["label"]) for c in meta["channels"] if c["pinned"]]
        assert pinned == [(32, "Main L"), (33, "Main R")] and len(meta["channels"]) == 34
        assert meta["channels"][0]["label"] == "Ch 1"
        assert [c["index"] for c in meta["channels"] if c["bus"]] == [32, 33]  # the mains: rings there are a mix
        assert client.post("/api/audio/reset-peak", json={"channel": 33}).get_json() == {"ok": True}
        assert client.post("/api/audio/reset-peak", json={"channel": 5}).status_code == 400  # not analyzed
        assert client.get("/api/status").get_json()["modules"]["audio"]["frames"] >= 1
    finally:
        app.extensions["tf_stop"]()


def _records(payload, n_bins):
    """Input indices carried by a TFS2 frame (and a check of its layout)."""
    magic, _, count, nb = struct.unpack_from("<4sIHH", payload)
    assert magic == MAGIC and nb == n_bins
    rec = 4 + 6 * nb  # index, reserved, int16[3][nb]
    assert len(payload) == 12 + count * rec
    return [struct.unpack_from("<H", payload, 12 + k * rec)[0] for k in range(count)]


def _next_frame(engine, seq, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        seq, payload = engine.hub.wait(seq, timeout=0.5)
        if payload is not None:
            return seq, payload
    raise AssertionError("no frame in time")


def test_unlisted_inputs_are_analyzed_only_while_viewed():
    from tf_daemon.audio.engine import AudioEngine

    cfg = make_config(channels=[{"index": 1, "label": "Vox"}])
    src = SynthSource(
        channels=4, active=[0, 1, 2, 3], blocksize=1200, speech_db=-20, noise_db=-50, tone_hz=2500, tone_db=-15
    )
    engine = AudioEngine(cfg, src)
    nb = engine.analyzer.bins.count
    assert [(c.index, c.label) for c in engine.inputs] == [(0, "Ch 1"), (1, "Vox"), (2, "Ch 3"), (3, "Ch 4")]
    engine.start()
    try:
        seq, payload = _next_frame(engine, 0)
        assert _records(payload, nb) == [1]

        engine.acquire(3)
        engine.acquire(3)  # two viewers
        seq, payload = _next_frame(engine, seq)
        seq, payload = _next_frame(engine, seq)
        assert _records(payload, nb) == [1, 3]
        k = _records(payload, nb).index(3)
        avg = np.frombuffer(payload, "<i2", count=nb, offset=12 + k * (4 + 6 * nb) + 4) / 100
        assert avg.max() > -60  # real signal, not the floor

        engine.release(3)
        assert engine.active_inputs() == [1, 3]  # still one viewer
        engine.release(3)
        engine.release(1)  # listed inputs stay analyzed regardless
        assert engine.active_inputs() == [1]
        with pytest.raises(ValueError):
            engine.acquire(4)
        with pytest.raises(ValueError):
            engine.reset_peak(3)
    finally:
        engine.stop()


def test_engine_rejects_missing_channel():
    cfg = make_config(channels=[{"index": 0, "label": "A"}, {"index": 3, "label": "B"}])
    from tf_daemon.audio.engine import AudioEngine

    src = SynthSource(channels=1, active=[0], blocksize=1200, speech_db=-20, noise_db=-50, tone_hz=2500, tone_db=-15)
    src.channels = 1
    with pytest.raises(SourceError, match="3 \\(B\\)"):
        AudioEngine(cfg, src)
