"""A literal transcription of TF Editor's EQ maths (decompiled tf_editor.exe, guides/tf5_api.md),
kept deliberately close to the decompiler output so tfeditor.eq_curve can be checked against it.

FUN_005196e0(coef, fs, Q, gain, freq, type) designs one section into five floats:
    coef[0] = -a2, coef[1] = -a1, coef[2] = b2, coef[3] = b1, coef[4] = b0      (a0 = 1)
FUN_00546ae0 evaluates 10*log10(|H|^2) per section with phi = 4 sin^2(w/2) and sums them;
a section whose gain / on value is 0 contributes 0 dB.
Constants (from .rdata): fs >= 44100, freq >= 16, Q >= 0.1, V = 10^(|gain|/20).
"""

import math

TYPE = {"bell": 0, "low_shelf": 2, "high_shelf": 3, "lpf": 4, "hpf": 5, "lpf1": 6, "hpf1": 7}


def design(fs, q, gain, freq, typ):
    fs = max(fs, 44100)
    freq = max(freq, 16.0)
    q = max(q, 0.1)
    f8 = f11 = f4 = f6 = 0.0
    f5 = 1.0
    if typ == 0:
        if gain != 0.0:
            k = math.tan(freq * math.pi / fs)
            f9 = k / q
            v = math.exp(abs(gain) / 20.0 * math.log(10))
            f8 = v * f9
            f7 = k * k + 1.0
            f11 = f9
            if gain > 0.0:
                f11, f8 = f8, f9
            f9 = f7 + f8
            f5 = (f7 + f11) / f9
            f11 = (f7 - f11) / f9
            f8 = (f8 - f7) / f9
            f6 = ((k * k - 1.0) * 2.0) / f9
            f4 = -f6
    elif typ in (2, 3):
        if gain != 0.0:
            k = math.tan(freq * math.pi / fs)
            f7 = k
            v = math.exp(abs(gain) / 20.0 * math.log(10))
            f11 = v
            f8 = k * 2.0 * k
            f3 = math.sqrt(v)
            if typ == 2:
                d12 = 2.0 - f11 * f8
                f9 = f7 * f3 + 1.0
                f3 = 1.0 - f7 * f3
            else:
                f9 = f7 + f3
                f3 = f3 - f7
                d12 = f11 * 2.0 - f8
            f4 = d12
            if gain <= 0.0:
                f5 = f7 + 1.0
                f6 = f8 - 2.0
                f11 = 1.0 - f7
                f10 = f9
            else:
                f6 = -f4
                f4 = 2.0 - f8
                f11 = f3
                f10 = f7 + 1.0
                f5 = f9
                f3 = 1.0 - f7
            f10 = f10 * f10
            f6 = f6 / f10
            f11 = (f11 * f11) / f10
            f4 = f4 / f10
            f8 = (-f3 * f3) / f10
            f5 = (f5 * f5) / f10
    elif typ in (4, 5):
        if gain >= 0.0:
            k = math.tan(freq * math.pi / fs)
            f7 = k * k
            f3 = k / q + 1.0 + f7
            if typ == 4:
                f11 = f7 / f3
                d12 = f11 * 2.0
            else:
                f11 = 1.0 / f3
                d12 = f11 * -2.0
            f8 = ((k / q - 1.0) - f7) / f3
            f4 = ((1.0 - f7) * 2.0) / f3
            f6 = d12
            f5 = f11
    elif typ in (6, 7):
        if gain >= 0.0:
            k = math.tan(freq * math.pi / fs)
            f8_ = k + 1.0
            if typ == 6:
                f5 = k / f8_
                f6 = f5
            else:
                f5 = 1.0 / f8_
                f6 = -f5
            f4 = (1.0 - k) / f8_
            f8 = f11 = 0.0
    return [f8, f4, f11, f6, f5]


def response_db(coef, f, fs):
    """FUN_00546ae0 for one section at frequency f."""
    c0, c1, c2, c3, c4 = coef
    phi = math.sin((f * 2 * math.pi / fs) * 0.5)
    phi = phi * 4.0 * phi
    n = c2 + c3 + c4
    d = -c1 + 1.0 + -c0
    num = (c2 * c4 * phi - ((c2 + c4) * c3 + c4 * 4.0 * c2)) * phi + n * n
    den = (phi * -c0 - ((-c0 + 1.0) * -c1 + -c0 * 4.0)) * phi + d * d
    return 10.0 * math.log10(num / den)


def curve(sections, freqs, fs=48000):
    """sections: [(kind, freq, gain_or_on, q)] as TF Editor's table holds them."""
    out = []
    for f in freqs:
        total = 0.0
        for kind, freq, gain, q in sections:
            if gain == 0.0:
                continue
            total += response_db(design(fs, q, gain, freq, TYPE[kind]), f, fs)
        out.append(total)
    return out
