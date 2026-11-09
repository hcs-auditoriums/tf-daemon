"""Mic types and EQ styles: what the tone analysis (tone.py) aims at, and how hard it pushes.

The reference is the ANSI S3.5 speech spectrum measured 1 m in front of a mouth
(tone.py). No mic on a person hears the voice that way, and not every voice
should be pushed towards average speech, so the page picks, per channel:

* a **mic type**: where the capsule sits. Its ``target`` is the part of that
  position's colour the suggestion should *accept* rather than correct, and its
  boost limits keep it away from the region where that kind of mic feeds back
  first. Head-worn mics (wig / hairline, lav) are the ones that ring at
  2-8 kHz, so they get little or no boost there.
* an **EQ style**: what the voice should end up like. Speech aims at the
  reference for intelligibility; singing keeps more warmth and the singer's
  2.5-3.5 kHz "ring", and cuts more gently.

These are starting points drawn from common placement guidance (forehead mics
sound fairly natural with a slightly softer top; chest lavs pick up a chest
resonance and lose some upper mids in the chin's shadow; close cardioids add
low end by proximity), not measurements of any particular microphone. The
ring-out session (ringout.py) and the feedback headroom (feedback.py) are what
keep the result safe; these only make the tone suggestion sensible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from ..dsp.eq import FS, section_db

Shape = tuple[tuple[str, float, float, float], ...]  # (kind, freq, gain dB, Q) sections


@dataclass(frozen=True)
class MicType:
    label: str
    note: str
    target: Shape = ()  # colour of this position to accept, not correct (dB re the reference)
    max_boost_db: float = 6.0  # cap on boosts (the config's max_boost_db still applies)
    boost_range: tuple[float, float] = (2000.0, 8000.0)  # where a boost may go
    hpf_min_hz: float = 60.0  # lowest suggested high-pass
    notch_q: float = 8.0  # ring-out notch width


@dataclass(frozen=True)
class EQStyle:
    label: str
    note: str
    target: Shape = ()  # how the voice should differ from reference speech
    cut_scale: float = 1.0  # share of an excess a cut takes out (x 0.8, tone.py)
    max_cut_db: float = 12.0  # cap on cuts (the config's max_cut_db still applies)
    hpf_ratio: float = 0.75  # high-pass at this x the lowest pitch


MIC_TYPES: dict[str, MicType] = {
    "generic": MicType("Generic", "no assumptions about the mic: aims straight at the reference"),
    "wig": MicType(
        "Wig / hairline",
        "forehead or hairline omni: accepts a softer top, keeps boosts out of the 2-8 kHz feedback range",
        target=(("high_shelf", 4000.0, -2.0, 0.7),),
        max_boost_db=1.0,
        hpf_min_hz=100.0,
        notch_q=10.0,
    ),
    "lav": MicType(
        "Lavalier (chest)",
        "chest-worn: cuts the chest resonance, puts back only part of the upper mids it loses",
        target=(("bell", 3500.0, -3.0, 1.0),),
        max_boost_db=2.0,
        hpf_min_hz=100.0,
        notch_q=10.0,
    ),
    "headset": MicType(
        "Headset / boom",
        "cheek boom near the mouth: close to the reference; breath and pops kept out by the high-pass",
        max_boost_db=2.0,
        hpf_min_hz=100.0,
    ),
    "handheld": MicType(
        "Handheld / podium",
        "close cardioid: its proximity bass is cut like any build-up",
        max_boost_db=3.0,
        hpf_min_hz=80.0,
    ),
}

EQ_STYLES: dict[str, EQStyle] = {
    "speech": EQStyle("Speech", "the reference speech spectrum: clarity first"),
    "projected": EQStyle(
        "Projected speech",
        "a stage voice: keeps the extra upper mids that projecting brings instead of cutting them",
        target=(("bell", 2500.0, 3.0, 0.7),),
    ),
    "speech_singing": EQStyle(
        "Speech & singing",
        "dialogue and songs on the same mic: a little more warmth and ring, gentler cuts",
        target=(("low_shelf", 250.0, 1.5, 0.7), ("bell", 3000.0, 1.5, 0.7)),
        cut_scale=0.75,
        max_cut_db=4.0,
        hpf_ratio=0.7,
    ),
    "singing": EQStyle(
        "Singing",
        "keeps the body of the voice and the singer's 2.5-3.5 kHz ring; cuts only clear problems",
        target=(("low_shelf", 250.0, 2.0, 0.7), ("bell", 3000.0, 2.0, 0.7)),
        cut_scale=0.6,
        max_cut_db=3.0,
        hpf_ratio=0.6,
    ),
}


def _shape_db(shape: Shape, freqs: np.ndarray) -> np.ndarray:
    out = np.zeros(len(freqs))
    for kind, f, g, q in shape:
        out += section_db(kind, f, g, q, np.asarray(freqs, dtype=float), FS)
    return out


def tone_options(mic: str, style: str, freqs: np.ndarray, *, max_cut_db: float, max_boost_db: float) -> dict[str, Any]:
    """tone.analyze's keyword arguments for ``mic`` and ``style`` (keys of MIC_TYPES / EQ_STYLES)."""
    m, s = MIC_TYPES[mic], EQ_STYLES[style]
    return {
        "target_db": _shape_db(m.target + s.target, freqs),
        "max_cut_db": min(max_cut_db, s.max_cut_db),
        "max_boost_db": min(max_boost_db, m.max_boost_db),
        "cut_scale": s.cut_scale,
        "boost_range": m.boost_range,
        "hpf_ratio": s.hpf_ratio,
        "hpf_min_hz": m.hpf_min_hz,
    }


def choices() -> dict[str, list[dict[str, str]]]:
    """For the page's pickers."""
    return {
        "mic_types": [{"id": k, "label": v.label, "note": v.note} for k, v in MIC_TYPES.items()],
        "eq_styles": [{"id": k, "label": v.label, "note": v.note} for k, v in EQ_STYLES.items()],
    }


def check(mic: Any, style: Any) -> tuple[str, str]:
    """Validate a page's choice; raises ValueError."""
    if mic not in MIC_TYPES:
        raise ValueError(f"unknown mic type {mic!r} (one of {', '.join(MIC_TYPES)})")
    if style not in EQ_STYLES:
        raise ValueError(f"unknown EQ style {style!r} (one of {', '.join(EQ_STYLES)})")
    return mic, style
