"""An input channel's sends read from and written to the desk over the TF Editor protocol
(docs/desk-coverage-howto-complete.md 3.2): to the 20 AUX (level, ON, pre / post, pan), FX 1/2
(level, ON, pre) and MONO (level, ON). It gives the SEND tab's data in the shape of
console/strip.py's ``sends`` section and takes its edits, replacing strip.py's RCP read and
app-simulated pre / pan once the editor protocol has the desk's data (console/module.py's strip
routes ask ``DeskSends.live``).

Rows: ``mix`` 0-19 are the AUX (RCP's numbering; buses.aux_send maps them to ``ToMix`` /
``ToStMix``), 20-21 FX 1/2, 22 MONO. Values as strip.py's send SPECS: level in dB (None = -inf,
at or below its lowest), on / pre booleans, pan -63..63. A send to a stereo AUX pair is one send
on the desk: a change goes to both halves [INFERRED: the desk keeps them equal]. Pre / post's tap
point (``Mix|StMix/SendPoint/Pre``) and ``Signal/PanLink`` are shown, not written (no family).
Written only on a person's edit, within the ``mix`` write family (tf5.mix_write).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from .buses import N_AUX, N_FX, Buses, aux_bus, aux_halves, aux_label, aux_send
from .patch import Source
from .rcp import NEG_INF
from .strip import SPECS

FX_ROW = N_AUX  # rows 20, 21: FX 1 / 2
MONO_ROW = N_AUX + N_FX  # row 22
LEVEL = next(s for s in SPECS["send"] if s.key == "level")
PAN = next(s for s in SPECS["send"] if s.key == "pan")


def _send(row: int) -> tuple[str, str, list[str]]:
    """Row -> (kind, label, the send collections a change goes to)."""
    if 0 <= row < N_AUX:
        return "aux", aux_label(row), [aux_send(a) for a in aux_halves(row)]
    if FX_ROW <= row < MONO_ROW:
        return "fx", f"FX {row - FX_ROW + 1}", [f"ToFX:{row - FX_ROW}"]
    if row == MONO_ROW:
        return "mono", "MONO", ["ToMono"]
    raise ValueError(f"mix: 0-{MONO_ROW} (AUX 1-{N_AUX}, FX, MONO)")


def level_raw(db: Any) -> int:
    if db is None:
        return NEG_INF
    if isinstance(db, bool) or not isinstance(db, int | float):
        raise ValueError("level: dB or null (-inf)")
    if db <= LEVEL.lo:
        return NEG_INF
    return round(round(min(LEVEL.hi, float(db)) / LEVEL.step) * LEVEL.step * 100)


class DeskSends:
    def __init__(self, patch: Sequence[Source | None], buses: Buses, rcp_mixes: Callable[[], list[str | None]]) -> None:
        self.patch, self.buses, self.ec, self.rcp_mixes = patch, buses, buses.ec, rcp_mixes

    @property
    def live(self) -> bool:
        return self.ec is not None and self.ec.synced

    def _channel(self, usb: int) -> str | None:
        src = self.patch[usb] if 0 <= usb < len(self.patch) else None
        return f"InputChannel:{src[1]}" if src is not None and src[0] == "InCh" else None

    def _name(self, row: int, rcp: list[str | None]) -> str:
        kind, label, _ = _send(row)
        if kind != "aux":
            return label
        first = aux_halves(row)[0]  # both halves of a pair carry the first's name (the SEND tab pairs them)
        assert self.ec is not None
        name = self.ec.get(f"{aux_bus(first)}/Label/Name") or (rcp[first] if first < len(rcp) else None)
        return str(name or label)

    def over(self, strip: dict[str, Any]) -> dict[str, Any]:
        """strip.py's data for a USB input with its ``sends`` read from the desk (when live)."""
        ch = self._channel(int(strip.get("index", -1)))
        if self.live and ch is not None:
            strip["sends"] = self.section(ch)
        return strip

    def section(self, ch: str) -> dict[str, Any]:
        ec = self.ec
        assert ec is not None
        rcp, rows = self.rcp_mixes(), []
        for row in range(MONO_ROW + 1):
            kind, label, sends = _send(row)
            p = f"{ch}/{sends[0] if kind != 'aux' else aux_send(row)}"
            level, on = ec.get(f"{p}/Level"), ec.get(f"{p}/On")
            r: dict[str, Any] = {"mix": row, "kind": kind, "label": label, "name": self._name(row, rcp)}
            r["level"] = None if not isinstance(level, int) or level <= NEG_INF else level / 100
            r["on"] = on == 1
            r["pre"] = ec.get(f"{p}/Pre") == 1 if kind != "mono" else None
            r["pan"] = ec.get(f"{p}/Pan") if kind == "aux" else None
            if kind == "aux":
                bus = aux_bus(row)
                r["send_point"] = ec.get(f"{bus}/SendPoint/Pre")  # where pre taps: 100 pre-fader, 50 pre-EQ?
                r["pan_link"] = ec.get(f"{bus}/Signal/PanLink")
            rows.append(r)
        writable = "mix" in ec.enabled_families
        return {
            "available": True,
            "writable": writable,
            "pre_pan": True,
            "source": "editor",
            "reason": None if writable else "writing sends is off (tf5.mix_write)",
            "mixes": rows,
        }

    def put(self, usb: int, mix: Any, changes: dict[str, Any]) -> dict[str, Any]:
        """SET a send (strip.py's ``changes``: level / on / pre / pan) on the desk."""
        ch = self._channel(usb)
        if ch is None:
            raise ValueError("no sends on this channel")
        if isinstance(mix, bool) or not isinstance(mix, int):
            raise ValueError(f"mix: 0-{MONO_ROW}")
        kind, label, sends = _send(mix)
        keys = {"aux": {"level", "on", "pre", "pan"}, "fx": {"level", "on", "pre"}, "mono": {"level", "on"}}[kind]
        bad = set(changes) - keys
        if bad or not changes:
            raise ValueError(f"{label}: send parameters are {sorted(keys)} (not {sorted(bad)})")
        raw: dict[str, int] = {}
        if "level" in changes:
            raw["Level"] = level_raw(changes["level"])
        for key in ("on", "pre"):
            if key in changes:
                if not isinstance(changes[key], bool):
                    raise ValueError(f"{key}: true or false")
                raw[key.capitalize()] = int(changes[key])
        if "pan" in changes:
            v = changes["pan"]
            if isinstance(v, bool) or not isinstance(v, int | float):
                raise ValueError("pan: -63..63")
            raw["Pan"] = int(max(PAN.lo, min(PAN.hi, round(v))))
        self.buses.write("Mixing", [(f"{ch}/{s}/{k}", v) for s in sends for k, v in raw.items()])
        clean = dict(changes)
        if "level" in raw:
            clean["level"] = None if raw["Level"] <= NEG_INF else raw["Level"] / 100
        if "pan" in raw:
            clean["pan"] = raw["Pan"]
        return clean
