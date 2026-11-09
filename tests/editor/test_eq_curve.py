"""The EQ curve (dsp/eq.py), checked against TF Editor's own maths (tests/tf_editor_reference.py)."""

import numpy as np
import pytest
import tf_editor_reference as ref

from tf_daemon.console.editor.dump import channel_eq
from tf_daemon.dsp.eq import CURVE_FREQS, biquad, eq_curve, section_db

KINDS = ["bell", "low_shelf", "high_shelf", "lpf", "hpf", "lpf1", "hpf1"]


def _eq(*bands, on=True, hpf=None):
    return {
        "on": on,
        "bank": "BOLD",
        "one_knob": False,
        "hpf": hpf,
        "bands": [dict(label=str(i), **{"on": True, **b}) for i, b in enumerate(bands)],
    }


def _at(curve_db, freqs, f):
    return float(np.interp(np.log(f), np.log(freqs), curve_db))


def _ref_sections(eq):
    """The 6-section table TF Editor builds for this EQ (FUN_0051a080), for the reference."""
    out = []
    if eq["hpf"]:
        out.append(
            ("hpf1" if eq["hpf"]["slope"] == 6 else "hpf", eq["hpf"]["freq"], 1.0 if eq["hpf"]["on"] else 0.0, 0.707106)
        )
    for b in eq["bands"]:
        if b["type"] in ("hpf", "lpf"):
            out.append((b["type"], b["freq"], 1.0 if b["on"] else 0.0, 0.707106))
        else:
            out.append((b["type"], b["freq"], b["gain"], b["q"]))
    return out


@pytest.mark.parametrize("seed", range(40))
def test_section_matches_tf_editors_designer(seed):
    """Every section type against a line-by-line transcription of the decompiled code."""
    rng = np.random.default_rng(seed)
    for kind in KINDS:
        f0, q = float(10 ** rng.uniform(1.2, 4.3)), float(10 ** rng.uniform(-1.2, 1.3))
        g = float(rng.uniform(-18, 18))
        filt = kind in ("lpf", "hpf", "lpf1", "hpf1")
        mine = section_db(kind, f0, 0.0 if filt else g, q)
        theirs = [
            ref.response_db(ref.design(48000, q, 1.0 if filt else g, f0, ref.TYPE[kind]), f, 48000) for f in CURVE_FREQS
        ]
        assert np.allclose(mine, theirs, atol=1e-6), kind


def test_curve_matches_tf_editor_for_board_like_settings(mixing):
    for x in (0, 1, 2, 3):
        eq = channel_eq(mixing, "InCh", x)
        expect = ref.curve(_ref_sections(eq), CURVE_FREQS) if eq["on"] else [0.0] * len(CURVE_FREQS)
        assert np.allclose(eq_curve(eq), expect, atol=1e-6)
    for x in (0, 1):
        eq = channel_eq(mixing, "St", x)
        assert np.allclose(eq_curve(eq), ref.curve(_ref_sections(eq), CURVE_FREQS), atol=1e-6)


def test_curve_flat_when_off_missing_or_zero_gain():
    freqs = CURVE_FREQS
    loud = _eq({"type": "bell", "freq": 1000, "gain": 12, "q": 1}, hpf={"on": True, "freq": 100, "slope": 12})
    assert np.all(eq_curve({**loud, "on": False}) == 0)
    assert np.all(eq_curve(None) == 0)
    flat = _eq(*({"type": t, "freq": 1000, "gain": 0, "q": 0.7} for t in ("bell", "low_shelf", "high_shelf")))
    assert np.all(eq_curve(flat) == 0)
    assert np.all(eq_curve(_eq(hpf={"on": False, "freq": 500, "slope": 12})) == 0)
    assert np.all(eq_curve(_eq({"type": "lpf", "on": False, "freq": 500, "gain": 0, "q": 0.7})) == 0)
    assert len(eq_curve(loud)) == len(freqs) == 241 and freqs[0] == 20 and freqs[-1] == 20000


@pytest.mark.parametrize("gain", [-18, -6, 3, 15])
@pytest.mark.parametrize("q", [0.1, 0.7, 4, 16])
def test_bell_hits_its_gain_at_its_frequency(gain, q):
    c = section_db("bell", 1000, gain, q, np.array([1000.0, 500.0, 2000.0, 20.0]))
    assert c[0] == pytest.approx(gain, abs=1e-6)  # bilinear with pre-warping: exact at f0
    assert abs(c[1]) < abs(gain) and abs(c[2]) < abs(gain)
    assert abs(c[1] - c[2]) < 0.5  # ~symmetric an octave either side
    if q >= 0.7:
        assert abs(c[3]) < 0.5


@pytest.mark.parametrize("gain", [-12, -3, 6, 15])
def test_bell_cut_mirrors_boost(gain):
    f = np.geomspace(20, 20000, 60)
    assert np.allclose(section_db("bell", 900, gain, 0.8, f), -section_db("bell", 900, -gain, 0.8, f))


def test_bell_q_sets_width():
    f = np.array([1000.0, 2000.0])
    wide = section_db("bell", 1000, 12, 0.5, f)
    narrow = section_db("bell", 1000, 12, 8, f)
    assert wide[0] == pytest.approx(narrow[0]) and wide[1] > 6 > narrow[1]


@pytest.mark.parametrize("gain", [-12, 6])
def test_shelves_are_two_first_order_sections(gain):
    f = np.array([10.0, 1000.0, 23000.0])
    v = 10 ** (abs(gain) / 20)
    corner = 20 * np.log10((1 + v) / 2) * np.sign(gain)  # |(j + sqrt V) / (j + 1)|^2, twice
    low = section_db("low_shelf", 1000, gain, 0.7, f)
    high = section_db("high_shelf", 1000, gain, 0.7, f)
    assert low[0] == pytest.approx(gain, abs=0.05) and abs(low[2]) < 0.3
    assert high[2] == pytest.approx(gain, abs=0.3) and abs(high[0]) < 0.05
    assert low[1] == pytest.approx(corner, abs=1e-6) and high[1] == pytest.approx(corner, abs=1e-6)
    assert np.allclose(section_db("low_shelf", 1000, gain, 0.2, f), low)  # Q isn't used
    assert np.allclose(section_db("low_shelf", 1000, -gain, 0.7, f), -low)  # cut mirrors boost


def test_hpf_and_lpf_slopes():
    f = np.array([25.0, 50.0, 100.0, 200.0, 400.0, 10000.0])
    c = section_db("hpf", 100, 0, 0.707106, f)
    assert c[2] == pytest.approx(-3.01, abs=0.01)  # -3 dB at the corner
    assert c[0] - c[1] == pytest.approx(-12, abs=0.3)  # 12 dB/oct below it
    assert c[5] == pytest.approx(0, abs=0.01)
    assert np.allclose(eq_curve(_eq(hpf={"on": True, "freq": 100, "slope": 12}), f), c)
    band_hpf = eq_curve(_eq({"type": "hpf", "freq": 100, "gain": 9, "q": 3}), f)
    assert np.allclose(band_hpf, c)  # band gain / Q don't matter for filters
    lpf = section_db("lpf", 100, 0, 0.707106, f)
    assert lpf[2] == pytest.approx(-3.01, abs=0.01) and lpf[3] - lpf[4] == pytest.approx(12, abs=0.3)
    f1 = np.array([5.0, 10.0, 1000.0, 2000.0, 4000.0])  # first order: asymptotic further from the corner
    first = eq_curve(_eq(hpf={"on": True, "freq": 1000, "slope": 6}), f1)
    assert first[2] == pytest.approx(-3.01, abs=0.01) and first[1] - first[0] == pytest.approx(6, abs=0.1)
    lpf1 = section_db("lpf1", 100, 0, 0.7, np.array([100.0, 2000.0, 4000.0]))
    assert lpf1[0] == pytest.approx(-3.01, abs=0.01) and lpf1[1] - lpf1[2] == pytest.approx(6, abs=0.3)


def test_bands_add_up_in_db():
    f = np.geomspace(20, 20000, 50)
    a = {"type": "bell", "freq": 100, "gain": -6, "q": 2}
    b = {"type": "high_shelf", "freq": 8000, "gain": 4, "q": 0.7}
    h = {"on": True, "freq": 40, "slope": 12}
    assert np.allclose(
        eq_curve(_eq(a, b, hpf=h), f), eq_curve(_eq(a), f) + eq_curve(_eq(b), f) + eq_curve(_eq(hpf=h), f)
    )


def test_curve_of_the_captured_channel(mixing):
    c = eq_curve(channel_eq(mixing, "InCh", 0))
    assert np.all(np.isfinite(c))
    assert _at(c, CURVE_FREQS, 20000) > _at(c, CURVE_FREQS, 3350) + 6  # the high shelf lifts the top
    assert _at(c, CURVE_FREQS, 800) < -14.1  # the cut, deepened by its neighbours
    assert _at(c, CURVE_FREQS, 50) < -20  # HPF at 200 Hz


def test_parameters_are_clamped_like_tf_editor():
    f = np.array([100.0, 1000.0])
    assert np.allclose(section_db("bell", 1000, 6, 0.0, f), section_db("bell", 1000, 6, 0.1, f))  # Q >= 0.1
    assert np.allclose(section_db("bell", 5, 6, 1, f), section_db("bell", 16, 6, 1, f))  # f >= 16 Hz
    assert np.allclose(section_db("bell", 1000, 6, 1, f, fs=8000), section_db("bell", 1000, 6, 1, f, fs=44100))
    with pytest.raises(ValueError):
        biquad("notch", 1000, 0, 1)
