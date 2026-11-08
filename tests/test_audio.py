import struct
import time
import wave

import numpy as np
import pytest

from tf_daemon.app import build_modules, create_app
from tf_daemon.audio.engine import MAGIC
from tf_daemon.audio.ringbuffer import RingBuffer
from tf_daemon.audio.sources import SourceError, SynthSource, WavSource
from tf_daemon.config import DEFAULTS, ConfigError, load_config
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
    assert cfg["dsp"]["fft_size"] == 16384
    assert cfg["dsp"]["hop"] == DEFAULTS["dsp"]["hop"]
    assert cfg["channels"] == [{"index": 5, "label": "Vox"}]
    assert load_config(tmp_path / "missing.toml")["audio"]["samplerate"] == 48000


def test_config_rejects_other_samplerates(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[audio]\nsamplerate = 44100\n")
    with pytest.raises(ConfigError):
        load_config(p)


def _run_synth(tone_db, seconds=6):
    fs, n = 48000, 32768
    src = SynthSource(channels=2, active=[0, 1], blocksize=1200, speech_db=-20, noise_db=-50,
                      tone_hz=2500, tone_db=tone_db, seed=3)
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
        w.setnchannels(2); w.setsampwidth(3); w.setframerate(48000)
        v = (np.array([[1 << 22, -(1 << 22)]] * 2400, dtype=np.int32))
        b = v.astype("<i4").tobytes()
        w.writeframes(b"".join(b[i:i + 3] for i in range(0, len(b), 4)))
    src = WavSource(good, 1200)
    blk = np.zeros((1200, 2), np.float32)
    src._fill(blk)
    np.testing.assert_allclose(blk[0], [0.5, -0.5])

    bad = tmp_path / "b.wav"
    with wave.open(str(bad), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
        w.writeframes(b"\0\0" * 4410)
    with pytest.raises(SourceError, match="48000"):
        WavSource(bad, 1200)


def test_engine_end_to_end_with_synth():
    cfg = load_config(None)
    modules = build_modules(cfg, "synth")
    app = create_app(cfg, modules)
    try:
        engine = modules[0].engine
        deadline = time.time() + 5
        seq, payload = 0, None
        while time.time() < deadline and payload is None:
            seq, payload = engine.hub.wait(seq, timeout=0.5)
        assert payload is not None, "no frame within 5 s"
        magic, s, nch, nb = struct.unpack_from("<4sIHH", payload)
        assert magic == MAGIC and nch == 2 and nb == engine.analyzer.bins.count
        assert len(payload) == 12 + nch * 2 * nb * 2

        client = app.test_client()
        meta = client.get("/api/audio/meta").get_json()
        assert [c["label"] for c in meta["channels"]] == ["Main L", "Main R"]
        assert client.post("/api/audio/reset-peak", json={"channel": 1}).get_json() == {"ok": True}
        assert client.post("/api/audio/reset-peak", json={"channel": 9}).status_code == 400
        assert client.get("/api/status").get_json()["modules"]["audio"]["frames"] >= 1
    finally:
        app.extensions["tf_stop"]()


def test_engine_rejects_missing_channel():
    cfg = load_config(None)
    cfg["channels"] = [{"index": 0, "label": "A"}]
    from tf_daemon.audio.engine import AudioEngine
    src = SynthSource(channels=1, active=[0], blocksize=1200, speech_db=-20, noise_db=-50, tone_hz=2500, tone_db=-15)
    src.channels = 1
    cfg["channels"].append({"index": 3, "label": "B"})
    with pytest.raises(SourceError, match="3 \\(B\\)"):
        AudioEngine(cfg, src)
