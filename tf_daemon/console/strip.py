"""A channel's processing as TF StageMix shows it, beyond the EQ: input (gain, +48V, phase,
digital gain), gate, compressor and sends to the AUX mixes (routes /api/tf5/strip/<usb>,
registered by console/module.py).

What the desk gives today (docs/desk-coverage.md): send levels and ONs are read over RCP;
gain, gate, compressor, send pre / pan aren't read yet. Until they are, the application keeps
them in its own store, *simulated*: in demo mode it is seeded and editable (and a send level or
ON is set on the simulated desk itself, so it comes back over RCP like a desk change); with a
real desk those sections say they aren't available, and nothing is written.

SPECS holds every parameter's range, step, default and unit, in one place. The ranges are
those Yamaha's consoles share (TF Series manuals don't print them): confirm them on the TF5
when each protocol is decoded.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from typing import Any

from .patch import source_label
from .rcp import NEG_INF, TF_MIXES

NOT_READ = "not read from the desk yet (docs/desk-coverage.md)"
NO_WRITE = "can't be changed on the desk from here yet (docs/desk-coverage.md)"


@dataclass(frozen=True)
class Spec:
    key: str
    label: str
    kind: str  # "number", "switch", "choice"
    unit: str = ""
    lo: float = 0
    hi: float = 0
    step: float = 1
    default: Any = 0
    log: bool = False  # a slider moves along a log scale (times)
    choices: tuple[Any, ...] = ()


INF = "inf"  # the ratio ∞:1
SPECS: dict[str, list[Spec]] = {
    "input": [
        Spec("gain", "Gain", "number", "dB", -6, 66, 1, 0),
        Spec("phantom", "+48V", "switch", default=False),
        Spec("phase", "Phase", "switch", default=False),
        Spec("digital_gain", "Digital gain", "number", "dB", -96, 24, 0.1, 0),
    ],
    "gate": [
        Spec("on", "Gate", "switch", default=False),
        Spec("threshold", "Threshold", "number", "dB", -72, 0, 1, -26),
        Spec("range", "Range", "number", "dB", -72, 0, 1, -56),
        Spec("attack", "Attack", "number", "ms", 0, 120, 1, 0),
        Spec("hold", "Hold", "number", "ms", 0.02, 2130, 0.01, 2.56, log=True),
        Spec("decay", "Decay", "number", "ms", 5, 42300, 1, 331, log=True),
    ],
    "comp": [
        Spec("on", "Comp", "switch", default=False),
        Spec("one_knob", "1-knob", "switch", default=False),
        Spec("level", "Amount", "number", "%", 0, 100, 1, 50),
        Spec("threshold", "Threshold", "number", "dB", -54, 0, 1, -8),
        Spec(
            "ratio",
            "Ratio",
            "choice",
            default=2.5,
            choices=(1, 1.1, 1.3, 1.5, 1.7, 2, 2.5, 3, 3.5, 4, 5, 6, 8, 10, 20, INF),
        ),
        Spec("attack", "Attack", "number", "ms", 0, 120, 1, 30),
        Spec("release", "Release", "number", "ms", 5, 42300, 1, 250, log=True),
        Spec("gain", "Out gain", "number", "dB", 0, 18, 0.5, 0),
        Spec("knee", "Knee", "choice", default="soft", choices=("hard", "medium", "soft")),
    ],
    "send": [
        Spec("level", "Level", "number", "dB", -60, 10, 0.5, None),  # None = -inf (below -60)
        Spec("on", "On", "switch", default=True),
        Spec("pre", "Pre", "switch", default=False),
        Spec("pan", "Pan", "number", "", -63, 63, 1, 0),
    ],
}
GATE_CHANNELS = 32  # the gate is on input channels 1-32 only (CH 33-40 have none)


def _limit(spec: Spec, v: Any) -> Any:
    """``v`` held to the spec (ValueError when it can't be)."""
    if spec.kind == "switch":
        if not isinstance(v, bool):
            raise ValueError(f"{spec.key}: true or false")
        return v
    if spec.kind == "choice":
        if v not in spec.choices:
            raise ValueError(f"{spec.key}: one of {list(spec.choices)}")
        return v
    if v is None and spec.default is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise ValueError(f"{spec.key}: a number")
    v = min(spec.hi, max(spec.lo, float(v)))
    return round(round(v / spec.step) * spec.step, 4)


def _seed(section: str, x: int) -> dict[str, Any]:
    """Demo values: the defaults, varied a little per channel so the strips don't all look alike."""
    out = {s.key: s.default for s in SPECS[section]}
    if section == "input":
        out.update(gain=float(24 + (x * 7) % 26), phantom=x % 3 != 2)
    elif section == "gate":
        out.update(on=x in (5, 6), threshold=-40.0)
    elif section == "comp":
        out.update(on=x < 5, threshold=float(-18 + x % 6), ratio=3)
    return out


class Strip:
    """Per channel: what the desk gives (RCP sends) and the application's simulated store."""

    def __init__(self, patch: list, rcp: Any, simulator: Any = None) -> None:
        self.patch, self.rcp, self.simulator = patch, rcp, simulator
        self._lock = threading.Lock()
        self._store: dict[tuple[str, str, int], dict[str, Any]] = {}  # (section, kind, x) -> values
        self._sends: dict[tuple[str, int, int], dict[str, Any]] = {}  # (kind, x, mix) -> pre / pan

    @property
    def simulated(self) -> bool:
        return self.simulator is not None

    def _src(self, usb: int) -> tuple[str, int] | None:
        return self.patch[usb] if 0 <= usb < len(self.patch) else None

    def _has(self, section: str, src: tuple[str, int] | None) -> bool:
        if src is None or src[0] != "InCh":
            return False
        return section != "gate" or src[1] < GATE_CHANNELS

    def _values(self, section: str, src: tuple[str, int]) -> dict[str, Any]:
        key = (section, src[0], src[1])
        if key not in self._store:
            self._store[key] = _seed(section, src[1])
        return self._store[key]

    def get(self, usb: int) -> dict[str, Any]:
        src = self._src(usb)
        out: dict[str, Any] = {
            "index": usb,
            "source": source_label(src) if src is not None else None,
            "simulated": self.simulated,
            "specs": {k: [asdict(s) for s in v] for k, v in SPECS.items()},
        }
        with self._lock:
            for section in ("input", "gate", "comp"):
                if not self._has(section, src):
                    out[section] = {"available": False, "reason": f"{section} isn't on this channel"}
                elif not self.simulated:
                    out[section] = {"available": False, "reason": NOT_READ}
                else:
                    assert src is not None
                    out[section] = {"available": True, "writable": True, "values": dict(self._values(section, src))}
            out["sends"] = self._get_sends(usb, src)
        return out

    def _get_sends(self, usb: int, src: tuple[str, int] | None) -> dict[str, Any]:
        if src is None or src[0] != "InCh" or self.rcp is None:
            return {"available": False, "reason": "no sends on this channel"}
        mixes = self.rcp.mixes()
        rows = []
        for m in range(TF_MIXES):
            level, on = self.rcp.raw(usb, "ToMix/Level", m), self.rcp.raw(usb, "ToMix/On", m)
            if level is None and on is None:
                continue
            row = {"mix": m, "name": mixes[m] if m < len(mixes) and mixes[m] else f"Mix {m + 1}"}
            row["level"] = None if level is None or int(level) <= NEG_INF else int(level) / 100
            row["on"] = on != "0"
            extra = self._sends.get((src[0], src[1], m))
            row.update(extra or ({"pre": False, "pan": 0} if self.simulated else {}))
            rows.append(row)
        return {
            "available": bool(rows),
            "writable": self.simulated,
            "pre_pan": self.simulated,  # pre / pan aren't read from a real desk yet
            "reason": None if self.simulated else NO_WRITE,
            "mixes": rows,
        }

    def put(self, usb: int, section: str, changes: dict[str, Any], mix: int | None = None) -> dict[str, Any]:
        """Apply ``changes`` (by parameter key) to a section: in the simulated store, or for a send's
        level / ON on the simulated desk. ValueError / PermissionError when it can't be done."""
        src = self._src(usb)
        if section == "sends":
            return self._put_send(usb, src, mix, changes)
        if section not in ("input", "gate", "comp") or not self._has(section, src):
            raise ValueError(f"no {section} on this channel")
        if not self.simulated:
            raise PermissionError(f"{section} {NO_WRITE}")
        specs = {s.key: s for s in SPECS[section]}
        bad = [k for k in changes if k not in specs]
        if bad:
            raise ValueError(f"unknown {section} parameter(s): {bad}")
        clean = {k: _limit(specs[k], v) for k, v in changes.items()}
        assert src is not None
        with self._lock:
            self._values(section, src).update(clean)
        return clean

    def _put_send(self, usb: int, src: tuple[str, int] | None, mix: Any, changes: dict[str, Any]) -> dict[str, Any]:
        if src is None or src[0] != "InCh":
            raise ValueError("no sends on this channel")
        if not isinstance(mix, int) or not 0 <= mix < TF_MIXES:
            raise ValueError(f"mix: 0-{TF_MIXES - 1}")
        if not self.simulated:
            raise PermissionError(f"sends {NO_WRITE}")
        specs = {s.key: s for s in SPECS["send"]}
        bad = [k for k in changes if k not in specs]
        if bad:
            raise ValueError(f"unknown send parameter(s): {bad}")
        clean = {k: _limit(specs[k], v) for k, v in changes.items()}
        rcp, x = self.simulator.rcp, src[1]
        if "level" in clean:  # on the simulated desk: it comes back over RCP like a change at the desk
            raw = (
                NEG_INF
                if clean["level"] is None or clean["level"] <= specs["level"].lo
                else round(clean["level"] * 100)
            )
            rcp.set("MIXER:Current/InCh/ToMix/Level", x, mix, str(raw))
        if "on" in clean:
            rcp.set("MIXER:Current/InCh/ToMix/On", x, mix, "1" if clean["on"] else "0")
        with self._lock:
            extra = self._sends.setdefault((src[0], x, mix), {"pre": False, "pan": 0})
            extra.update({k: v for k, v in clean.items() if k in ("pre", "pan")})
        return clean
