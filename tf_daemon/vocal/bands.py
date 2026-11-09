"""Which TF5 PEQ band each EQ section goes on, keeping the bands in frequency order.

A TF5 input channel has four bands (L, LM, HM, H) that can each sit anywhere, but
an EQ whose HM is below its LM reads wrong on the desk and in TF Editor, and the
next person to touch it turns the wrong knob. So every section (a notch, a tone
cut or boost, a shelf) is placed so that L <= LM <= HM <= H in frequency, with:

* a low shelf only on L, a high shelf only on H (the mid bands are bells only);
* sections with a fixed ``band`` (one used as a filter, a notch that must not move)
  staying where they are;
* among the orders that work, each band nearest its home range.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from typing import Any

LABELS = ("L", "LM", "HM", "H")
HOME = {"L": (0.0, 400.0), "LM": (250.0, 1600.0), "HM": (1000.0, 5000.0), "H": (3500.0, 20000.0)}
PATH = {"L": "PEQ/Bank/BandL", "LM": "PEQ/Bank/Band:0", "HM": "PEQ/Bank/Band:1", "H": "PEQ/Bank/BandH"}
LABEL = {v: k for k, v in PATH.items()}


def _outside(label: str, f: float) -> float:
    lo, hi = HOME[label]
    return 0.0 if lo <= f <= hi else abs(math.log2(f / (lo if f < lo else hi)))


def _fits(label: str, s: dict[str, Any]) -> bool:
    kind = s.get("type", "bell")
    if s.get("band") is not None and s["band"] != label:
        return False
    if kind == "low_shelf" or kind == "hpf":
        return label == "L"
    if kind == "high_shelf" or kind == "lpf":
        return label == "H"
    return True


def layout(sections: Sequence[dict[str, Any]], labels: Sequence[str] = LABELS) -> dict[str, dict[str, Any]] | None:
    """{band label: section} with the bands in frequency order (see the module docstring),
    or None when the sections can't all be placed that way."""
    order = sorted(sections, key=lambda s: s["freq"])
    if len(order) > len(labels):
        return None
    best, best_cost = None, math.inf
    for combo in itertools.combinations(range(len(labels)), len(order)):
        if not all(_fits(labels[i], s) for i, s in zip(combo, order, strict=True)):
            continue
        cost = sum(_outside(labels[i], s["freq"]) for i, s in zip(combo, order, strict=True))
        if cost < best_cost:
            best, best_cost = combo, cost
    return None if best is None else {labels[i]: s for i, s in zip(best, order, strict=True)}


def fit(
    sections: list[dict[str, Any]], keep: Sequence[dict[str, Any]] = ()
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Place ``keep`` (all of them, or ValueError) and as many of ``sections`` as fit in order,
    dropping the ones that matter least (smallest |gain|) first. Returns the layout and what
    was dropped."""
    use = sorted(sections, key=lambda s: -abs(s.get("gain", 0.0)))
    dropped: list[dict[str, Any]] = []
    while True:
        placed = layout([*keep, *use])
        if placed is not None:
            return placed, dropped
        if not use:
            raise ValueError("these bands can't be placed in frequency order")
        dropped.append(use.pop())


def from_desk(raw: dict[str, int], skip: Sequence[str] = ()) -> list[dict[str, Any]]:
    """The channel's bands in use as sections (``band`` set for filters, which can't move):
    raw PEQ values (vocal.auto.PATHS) in, bands in ``skip`` (paths) left out."""
    out = []
    for label, path in PATH.items():
        if path in skip:
            continue
        t, f = raw[f"{path}/Type"], raw[f"{path}/Frequency"] / 10
        g, q = raw[f"{path}/Gain"] / 100, raw[f"{path}/Q"] / 1000
        if label in ("L", "H") and t == 2:
            out.append({"type": "hpf" if label == "L" else "lpf", "freq": f, "gain": 0.0, "q": q, "band": label})
        elif g != 0:
            kind = "bell" if t == 0 or label not in ("L", "H") else ("low_shelf" if label == "L" else "high_shelf")
            out.append({"type": kind, "freq": f, "gain": g, "q": q, "kind": "desk"})
    return out


def to_desk(placed: dict[str, dict[str, Any]], raw: dict[str, int]) -> list[tuple[str, int]]:
    """The desk changes that put ``placed`` on the bands (unused bands set flat), as (path,
    raw value), only the ones that differ from ``raw``."""
    changes: list[tuple[str, int]] = []
    for label, path in PATH.items():
        s = placed.get(label)
        if s is None:
            if raw[f"{path}/Gain"] != 0 and not (label in ("L", "H") and raw[f"{path}/Type"] == 2):
                changes.append((f"{path}/Gain", 0))
            continue
        if s["type"] in ("hpf", "lpf"):
            continue  # a filter stays as it is
        want = {
            "Frequency": max(200, min(200000, round(s["freq"] * 10))),
            "Gain": round(s["gain"] * 10) * 10,
            "Q": max(100, min(10000, round(s["q"] * 1000))),
        }
        if label in ("L", "H"):
            want["Type"] = 1 if s["type"] in ("low_shelf", "high_shelf") else 0
        changes += [(f"{path}/{p}", v) for p, v in want.items() if raw[f"{path}/{p}"] != v]
    return changes
