"""A channel's processing as TF StageMix shows it, beyond the EQ bands: input (source, head-amp
gain, +48V, phase, digital gain, the phantom master), 1-knob EQ and EQ type, gate, compressor
(with the de-esser) and sends to the AUX mixes (routes /api/tf5/strip/<usb>, registered by
console/module.py).

Input, EQ, gate and compressor are the desk's own, read and written over the TF Editor protocol
by console/strip_desk.py (only on a person's edit; the write guard decides what may change).
Without the editor protocol (tf5.eq off) they say they aren't available; nothing is kept by the
application for them. Sends: levels and ONs are read over RCP; pre / pan aren't read yet, so the
application keeps them in its own store, *simulated*: in demo mode a send level or ON is set on
the simulated desk itself (it comes back over RCP like a desk change); with a real desk sends
can't be changed from here.

SPECS holds every parameter as the page shows it (label, kind, step) and where it lives on the
desk; range, default, unit and scaling come from the TF5's own schema (editor/tree.py
``spec``), and raw <-> value is converted here only (``to_value`` / ``to_raw``).
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, replace
from typing import Any

from .editor import tree
from .editor.guard import families_for
from .patch import source_label
from .rcp import NEG_INF, TF_MIXES

NO_WRITE = "can't be changed on the desk from here yet (docs/desk-coverage.md)"
INF = "inf"  # the ratio ∞:1
SOURCES = ("INPUT", "USB", "SLOT")  # InputChannel/Patch/Select 0, 1, 2 (docs/desk-coverage-howto-complete.md 3.3)
DESK_SECTIONS = ("input", "eq", "gate", "comp")  # on the desk (strip_desk.py); "sends" is the rest


@dataclass(frozen=True)
class Spec:
    key: str
    label: str
    kind: str  # "number", "switch", "choice", "text" (shown only)
    unit: str = ""
    lo: float = 0
    hi: float = 0
    step: float = 1
    default: Any = 0
    log: bool = False  # a slider moves along a log scale (times, frequencies)
    choices: tuple[Any, ...] = ()
    inf: bool = False  # the slider's left end is −∞ (value None)
    # Where it lives on the desk: the function, what ``path`` is relative to ("channel": the
    # input channel in Mixing, "port": the channel's input port in Processing, "root": the
    # function's root), and raw = value x scaling (choices: their raw values, in order).
    func: str = ""
    at: str = ""
    path: str = ""
    scaling: float = 1
    raw_choices: tuple[Any, ...] = ()


_SAMPLE = {"channel": "InputChannel:0/", "port": "BuiltIn/InputPort:0/", "root": ""}
_FUNC = {"channel": "Mixing", "port": "Processing"}


def allowed_strings(func: str, path: str) -> tuple[str, ...]:
    """The values the write guard allows for a text parameter (guard.FAMILIES), sorted."""
    out: set[str] = set()
    for _, rule in families_for(func, path):
        for suffix, values in rule.strings.items():
            if values and (path == suffix or path.endswith("/" + suffix)):
                out |= values
    return tuple(sorted(out))


def _desk(key: str, label: str, kind: str, path: str, at: str = "channel", func: str = "", **kw: Any) -> Spec:
    """A Spec for a desk parameter, its range / default / unit / scaling from the V4.55 schema."""
    func = func or _FUNC[at]
    full = _SAMPLE[at] + path
    s = tree.spec(func, full)
    base = Spec(key, label, kind, func=func, at=at, path=path)
    if s.type == "str":
        if kind == "choice" and "choices" not in kw:
            values = allowed_strings(func, full)
            kw.update(choices=values, raw_choices=values)
        return replace(base, default=None, **kw)
    assert s.min is not None and s.max is not None and s.default is not None
    if kind == "switch":
        return replace(base, default=bool(s.default), **kw)
    if kind == "choice" and all(isinstance(c, str) for c in kw["choices"]):  # an enumeration: raw = index
        return replace(base, default=kw["choices"][s.default], raw_choices=tuple(range(len(kw["choices"]))), **kw)
    sc = s.scaling or 1
    lo, inf = s.min / sc, False
    if s.min == NEG_INF:  # the schema's -32768 is −∞; the slider's range starts at ``floor``
        lo, inf = kw.pop("floor"), True
    out = replace(base, unit=s.unit or "", lo=lo, hi=s.max / sc, default=s.default / sc, scaling=sc, inf=inf)
    if kind == "choice":  # numbers from a list (the ratio); INF is the schema's maximum
        choices = kw["choices"]
        raws = tuple(s.max if c == INF else round(c * sc) for c in choices)
        default = next((c for c, r in zip(choices, raws, strict=True) if r == s.default), out.default)
        return replace(out, default=default, raw_choices=raws, **kw)
    return replace(out, **kw)


RATIOS = (1, 1.1, 1.3, 1.5, 1.7, 2, 2.5, 3, 3.5, 4, 5, 6, 8, 10, 20, INF)  # TF StageMix's ratio steps
_COMP = "Comp/Bank/Compressor/"
_DEESS = "Comp/Bank/DeEsser/"
SPECS: dict[str, list[Spec]] = {
    "input": [
        _desk("source", "Source", "choice", "Patch/Select", choices=SOURCES),
        _desk("gain", "Gain", "number", "HeadAmp/Gain", at="port"),
        _desk("phantom", "+48V", "switch", "HeadAmp/48V", at="port"),
        _desk("phase", "Phase", "switch", "Input/Phase"),
        _desk("digital_gain", "Digital gain", "number", "Input/Gain", step=0.1),
        _desk("phantom_master", "+48V master", "switch", "PhantomMaster/On", at="root", func="Setup"),
    ],
    "eq": [
        _desk("type", "EQ type", "text", "PEQ/Bank/Type"),
        _desk("one_knob", "1-knob EQ", "switch", "PEQ/Bank/OneKnob/On"),
        _desk("mode", "Mode", "choice", "PEQ/Bank/OneKnob/Mode"),
        _desk("depth", "Level", "number", "PEQ/Bank/OneKnob/Depth"),
    ],
    "gate": [
        _desk("on", "Gate", "switch", "Gate/On"),
        _desk("type", "Type", "text", "Gate/Bank/Type"),
        _desk("threshold", "Threshold", "number", "Gate/Bank/Gate/Threshold"),
        _desk("range", "Range", "number", "Gate/Bank/Gate/Range", floor=-72),
        _desk("attack", "Attack", "number", "Gate/Bank/Gate/Attack"),
        _desk("hold", "Hold", "number", "Gate/Bank/Gate/Hold", step=0.01, log=True),
        _desk("decay", "Decay", "number", "Gate/Bank/Gate/Decay", step=0.01, log=True),
    ],
    "comp": [
        _desk("on", "Comp", "switch", "Comp/On"),
        _desk("type", "Type", "text", "Comp/Bank/Type"),
        _desk("one_knob", "1-knob", "switch", "Comp/Bank/OneKnob/On"),
        _desk("level", "Amount", "number", "Comp/Bank/OneKnob/Depth"),
        _desk("threshold", "Threshold", "number", _COMP + "Threshold"),
        _desk("ratio", "Ratio", "choice", _COMP + "Ratio", choices=RATIOS),
        _desk("attack", "Attack", "number", _COMP + "Attack"),
        _desk("release", "Release", "number", _COMP + "Release", step=0.01, log=True),
        _desk("gain", "Out gain", "number", _COMP + "Gain", step=0.5),
        _desk("knee", "Knee", "choice", _COMP + "Knee"),
        _desk("ds_threshold", "De-esser threshold", "number", _DEESS + "Threshold"),
        _desk("ds_freq", "De-esser frequency", "number", _DEESS + "Frequency", log=True),
        _desk("ds_q", "De-esser Q", "number", _DEESS + "Q", step=0.01, log=True),
        _desk("ds_type", "De-esser type", "text", _DEESS + "Type"),
    ],
    "send": [
        Spec("level", "Level", "number", "dB", -60, 10, 0.5, None),  # None = -inf (below -60)
        Spec("on", "On", "switch", default=True),
        Spec("pre", "Pre", "switch", default=False),
        Spec("pan", "Pan", "number", "", -63, 63, 1, 0),
    ],
}


def limit(spec: Spec, v: Any) -> Any:
    """``v`` held to the spec (ValueError when it can't be)."""
    if spec.kind == "text":
        raise ValueError(f"{spec.key}: shown only (other values aren't verified on the desk)")
    if spec.kind == "switch":
        if not isinstance(v, bool):
            raise ValueError(f"{spec.key}: true or false")
        return v
    if spec.kind == "choice":
        if v not in spec.choices:
            raise ValueError(f"{spec.key}: one of {list(spec.choices)}")
        return v
    if v is None and (spec.default is None or spec.inf):
        return None
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise ValueError(f"{spec.key}: a number")
    v = round(round(float(v) / spec.step) * spec.step, 4)
    return min(spec.hi, max(spec.lo, v))


def to_value(spec: Spec, raw: int | str) -> Any:
    """A desk parameter's raw value as the page shows it."""
    if spec.kind == "switch":
        return bool(raw)
    if spec.kind == "choice" and raw in spec.raw_choices:
        return spec.choices[spec.raw_choices.index(raw)]
    if isinstance(raw, str):
        return raw
    if spec.inf and raw <= NEG_INF:
        return None
    v = raw / spec.scaling
    return int(v) if spec.scaling == 1 else round(v, 4)


def to_raw(spec: Spec, value: Any) -> int | str:
    """A value held to the spec (``limit``) as the desk takes it."""
    if spec.kind == "switch":
        return int(value)
    if spec.kind == "choice":
        return spec.raw_choices[spec.choices.index(value)]
    if value is None:
        return NEG_INF
    return round(value * spec.scaling)


class Strip:
    """Per channel: the desk's input / EQ / dynamics (``desk``, strip_desk.DeskStrip), and the
    sends (RCP, with pre / pan in the application's simulated store)."""

    def __init__(self, patch: list, rcp: Any, simulator: Any = None, desk: Any = None) -> None:
        self.patch, self.rcp, self.simulator, self.desk = patch, rcp, simulator, desk
        self._lock = threading.Lock()
        self._sends: dict[tuple[str, int, int], dict[str, Any]] = {}  # (kind, x, mix) -> pre / pan

    @property
    def simulated(self) -> bool:
        return self.simulator is not None

    def version(self) -> Any:
        """Changes whenever what get() shows from the desk's editor protocol may have."""
        return self.desk.version() if self.desk is not None else None

    def _src(self, usb: int) -> tuple[str, int] | None:
        return self.patch[usb] if 0 <= usb < len(self.patch) else None

    def get(self, usb: int) -> dict[str, Any]:
        src = self._src(usb)
        out: dict[str, Any] = {
            "index": usb,
            "source": source_label(src) if src is not None else None,
            "simulated": self.simulated,
            "specs": {k: [asdict(s) for s in v] for k, v in SPECS.items()},
        }
        for section in DESK_SECTIONS:
            if src is None or src[0] != "InCh":
                out[section] = {"available": False, "reason": f"{section} isn't on this channel"}
            elif self.desk is None:
                out[section] = {"available": False, "reason": "not read: the desk's editor protocol is off (tf5.eq)"}
            else:
                out[section] = self.desk.section(section, src[1])
        with self._lock:
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
        """Apply ``changes`` (by parameter key) to a section: on the desk (strip_desk.py, only for a
        person's edit), or for a send. ``{"applied": {key: value}}``, desk sections also
        ``"confirmed": {key: echoed by the desk}``. ValueError / PermissionError when it can't be done."""
        src = self._src(usb)
        if section == "sends":
            return {"applied": self._put_send(usb, src, mix, changes)}
        if section not in DESK_SECTIONS or src is None or src[0] != "InCh":
            raise ValueError(f"no {section} on this channel")
        if self.desk is None:
            raise PermissionError(f"{section}: the desk's editor protocol is off (tf5.eq)")
        return self.desk.put(src[1], section, changes)

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
        clean = {k: limit(specs[k], v) for k, v in changes.items()}
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
