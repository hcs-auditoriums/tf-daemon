import numpy as np
import pytest

from tf_daemon.dsp.spectrum import DisplayBins, SpectrumAnalyzer, blackman_harris

FS = 48000
N = 32768


def make(channels=1, **kw):
    args = dict(channels=channels, samplerate=FS, n_fft=N, hop=2400, average_tau=3.0,
                fmin=50.0, fmax=16000.0, bands_per_octave=48)
    args.update(kw)
    return SpectrumAnalyzer(**args)


def sine(freq, amp=1.0, n=N):
    t = np.arange(n) / FS
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_window_shape():
    w = blackman_harris(N)
    assert w.dtype == np.float32 and w.shape == (N,)
    assert w[0] == pytest.approx(6e-5, abs=1e-4)
    assert w[N // 2] == pytest.approx(1.0, abs=1e-6)


def test_display_bins_count_and_mapping():
    b = DisplayBins(FS, N, 50.0, 16000.0, 48)
    assert 395 <= b.count <= 405
    assert np.all(np.diff(b.starts) >= 0)
    assert b.starts[0] >= 1 and b.end <= N // 2 + 1
    assert b.centers[0] > 50 and b.centers[-1] < 16000


@pytest.mark.parametrize("amp_db", [0.0, -20.0, -60.0])
def test_bin_centered_sine_reads_dbfs(amp_db):
    a = make()
    f = 64 * FS / N  # exactly on bin 64 -> no scalloping
    freq = f * 40    # 2343.75 Hz, still bin-centered
    a.process(sine(freq, 10 ** (amp_db / 20))[None, :])
    i = int(np.argmin(np.abs(a.bins.centers - freq)))
    assert a.avg_db[0, i] == pytest.approx(amp_db, abs=0.05)
    assert a.peak_db[0].max() == pytest.approx(amp_db, abs=0.05)


def test_narrow_peak_survives_display_reduction():
    a = make()
    x = sine(10007.3, 0.01) + (np.random.default_rng(0).standard_normal(N) * 1e-4).astype(np.float32)
    a.process(x[None, :])
    # Scalloping loss for Blackman-Harris is < 0.9 dB.
    assert a.avg_db[0].max() == pytest.approx(-40.0, abs=1.0)


def test_averaging_time_constant_and_peak_hold():
    a = make(average_tau=1.0)
    loud, quiet = sine(1000, 1.0)[None, :], np.zeros((1, N), np.float32)
    a.process(loud)
    i = int(np.argmax(a.avg_db[0]))
    for _ in range(20):  # 20 hops = 1 s = one time constant
        a.process(quiet)
    # After one tau the averaged power has decayed by 1/e (-4.34 dB).
    assert a.avg_db[0, i] - a.peak_db[0, i] == pytest.approx(-4.34, abs=0.1)
    a.reset_peak()
    a.process(quiet)
    assert a.peak_db[0].max() == pytest.approx(a.avg_db.min(), abs=1.0) or a.peak_db[0].max() < -100


def test_reset_single_channel():
    a = make(channels=2)
    x = np.stack([sine(1000), sine(1000)])
    a.process(x)
    a.reset_peak(1)
    a.process(np.zeros_like(x))
    assert a.peak_db[0].max() > -1
    assert a.peak_db[1].max() < -100


def test_process_is_allocation_stable():
    a = make(channels=4)
    x = np.random.default_rng(1).standard_normal((4, N)).astype(np.float32)
    ids = [id(arr) for arr in (a.avg_db, a.peak_db, a.avg, a.peak, a.power)]
    for _ in range(3):
        a.process(x)
    assert ids == [id(arr) for arr in (a.avg_db, a.peak_db, a.avg, a.peak, a.power)]
    assert a._spec.dtype == np.complex64
