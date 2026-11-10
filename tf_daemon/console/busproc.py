"""The buses' processing for the Mix page: the 31-band GEQ (AUX 1-8 and the stereo bus) and the
PEQ of every output bus (AUX, ST, MONO, MATRIX), over the TF Editor protocol
(docs/desk-coverage-howto-complete.md 3.4). Written on a person's confirmed edit, only within
the ``processing`` write family (tf5.processing_write).

GEQ: band b (0-30) is 20 Hz..20 kHz in 1/3 octaves (dsp/ring.py GEQ_BANDS, where the ring
detector names its nearest band), gain raw dB x 100 (+-15 dB). The TF lets at most 12 bands be
off 0 dB at once; the desk UI enforces that and whether the console refuses a 13th from a client
is [PENDING], so this module refuses it before sending. The stereo bus has a GEQ per side: with
its L/R link on, a change goes to both, otherwise to the side asked for.

PEQ: read as console/editor/dump.py channel_eq (the same shape as an input channel's), with the
curve from dsp/eq.py. A stereo bus is one strip on the desk: an edit goes to both halves
[INFERRED: the desk's own stereo strips keep them equal]; ``halves_differ`` says when they don't.

Routes (registered with console/buses.py):
  GET  /api/tf5/mix/geq/<key>[?side=0|1]  {"key", "label", "on", "lr_link", "bands": [{"index",
                                          "freq", "gain" (dB)}], "adjusted", "max_adjusted", "writable"}
  PUT  /api/tf5/mix/geq/<key>             {"on": 0|1, "lr_link": 0|1, "gains": {"<band>": raw}, "side":
                                          0|1, "confirm": true} (any of on / lr_link / gains): SET the GEQ
  GET  /api/tf5/mix/eq/<key>[?side=0|1]   {"key", "label", "eq", "freqs", "curve", "halves_differ", "writable"}
  PUT  /api/tf5/mix/eq/<key>              {"changes": [[path under PEQ, raw], ...], "confirm": true}: SET
                                          the bus PEQ (e.g. ["Bank/Band:0/Gain", -300], ["On", 1])
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from flask import Blueprint, jsonify, request

from ..dsp.eq import CURVE_FREQS, eq_curve
from ..dsp.ring import GEQ_BANDS
from .buses import Bus, Buses, bus, confirmed

GEQ_MAX_ADJUSTED = 12  # TF: at most 12 bands off 0 dB at once
N_GEQ = len(GEQ_BANDS)  # 31


def _geq_bus(key: str) -> Bus:
    b = bus(key)
    if not b.geq:
        raise ValueError(f"{b.label} has no GEQ (only AUX 1-8 and ST)")
    return b


def _peq_bus(key: str) -> Bus:
    b = bus(key)
    if not b.peq:
        raise ValueError(f"{b.label}: no bus PEQ here")
    return b


def _side(b: Bus, side: Any) -> str:
    i = 0 if side is None else side
    if isinstance(i, bool) or i not in range(len(b.paths)):
        raise ValueError(f"{b.label}: side 0..{len(b.paths) - 1}")
    return b.paths[i]


class BusProcessing:
    def __init__(self, buses: Buses) -> None:
        self.buses = buses

    # ------------------------------------------------------------------ GEQ

    def geq(self, key: str, side: int | None = None) -> dict[str, Any]:
        b = _geq_bus(key)
        p, g = _side(b, side), self.buses.get
        gains = [g(f"{p}/GEQ/Band:{i}/Gain") for i in range(N_GEQ)]
        return {
            "key": b.key,
            "label": b.label,
            "name": g(f"{p}/Label/Name"),
            "sides": len(b.paths),
            "side": b.paths.index(p),
            "on": g(f"{p}/GEQ/On"),
            "lr_link": g(f"{p}/GEQ/LRLink"),
            "bands": [
                {"index": i, "freq": f, "gain": None if v is None else int(v) / 100}
                for i, (f, v) in enumerate(zip(GEQ_BANDS, gains, strict=True))
            ],
            "adjusted": sum(1 for v in gains if v),
            "max_adjusted": GEQ_MAX_ADJUSTED,
            "writable": self.buses.can("processing"),
        }

    def set_geq(self, key: str, body: dict[str, Any]) -> list[dict[str, Any]]:
        """SET a GEQ's on, L/R link and band gains (raw); refuses more than 12 adjusted bands."""
        b, g = _geq_bus(key), self.buses.get
        on, link, gains = body.get("on"), body.get("lr_link"), body.get("gains") or {}
        if on is None and link is None and not gains:
            raise ValueError('set any of "on", "lr_link", "gains"')
        if not isinstance(gains, dict):
            raise ValueError('"gains": {"<band 0-30>": raw dB x 100}')
        try:
            bands = {int(k): v for k, v in gains.items()}
        except ValueError:
            raise ValueError("gains: band numbers 0-30") from None
        for i, v in bands.items():
            if i not in range(N_GEQ) or isinstance(v, bool) or not isinstance(v, int):
                raise ValueError(f"gains: band {i}: 0-{N_GEQ - 1} with a whole raw number")
        first = _side(b, body.get("side"))
        linked = (link if link is not None else g(f"{b.paths[0]}/GEQ/LRLink")) == 1
        targets = b.paths if linked or len(b.paths) == 1 else [first]
        for p in targets:
            now = {i: g(f"{p}/GEQ/Band:{i}/Gain") or 0 for i in range(N_GEQ)}
            n = sum(1 for v in {**now, **bands}.values() if v)
            if n > GEQ_MAX_ADJUSTED:
                raise ValueError(f"the TF allows {GEQ_MAX_ADJUSTED} adjusted GEQ bands at once (this would make {n})")
        changes: list[tuple[str, int | str]] = [(f"{p}/GEQ/LRLink", link) for p in b.paths] if link is not None else []
        for p in targets:
            if on is not None:
                changes.append((f"{p}/GEQ/On", on))
            changes += [(f"{p}/GEQ/Band:{i}/Gain", v) for i, v in sorted(bands.items())]
        return self.buses.write("Mixing", changes)

    # ------------------------------------------------------------------ PEQ

    def eq(self, key: str, side: int | None = None) -> dict[str, Any]:
        b, ec = _peq_bus(key), self.buses.ec
        p = _side(b, side)
        i = b.paths.index(p)
        x = b.halves[i] if b.halves else 0
        settings = ec.eq(b.col, x) if ec is not None else None
        others = [ec.eq(b.col, h) for h in b.halves] if ec is not None and len(b.halves) > 1 else []
        return {
            "key": b.key,
            "label": b.label,
            "name": self.buses.get(f"{p}/Label/Name"),
            "sides": len(b.paths),
            "side": i,
            "eq": settings,
            "halves_differ": any(o != others[0] for o in others),
            "freqs": [round(float(f), 2) for f in CURVE_FREQS] if settings else [],
            "curve": [round(float(v), 2) for v in eq_curve(settings)] if settings else [],
            "writable": self.buses.can("processing"),
        }

    def set_eq(self, key: str, changes: Any) -> list[dict[str, Any]]:
        b = _peq_bus(key)
        if not isinstance(changes, list) or not changes:
            raise ValueError('"changes": [[path under PEQ, raw], ...]')
        out: list[tuple[str, int | str]] = []
        for c in changes:
            if not (isinstance(c, list) and len(c) == 2 and isinstance(c[0], str)) or isinstance(c[1], bool):
                raise ValueError('"changes": [[path under PEQ, raw], ...]')
            if ".." in c[0] or c[0].startswith("/"):
                raise ValueError(f"{c[0]}: a path under PEQ")
            out += [(f"{p}/PEQ/{c[0]}", c[1]) for p in b.paths]
        return self.buses.write("Mixing", out)

    # ------------------------------------------------------------------ routes

    def register(self, bp: Blueprint, desk_call: Callable[[Callable[[], dict[str, Any]]], Any]) -> None:
        def read(fn: Callable[[], dict[str, Any]]) -> Any:
            try:
                return jsonify(fn())
            except ValueError as e:
                return jsonify({"error": str(e)}), 404

        @bp.get("/mix/geq/<key>")
        def mix_geq_get(key: str):
            return read(lambda: self.geq(key, request.args.get("side", type=int)))

        @bp.put("/mix/geq/<key>")
        def mix_geq_set(key: str):
            body, err = confirmed()
            return err or desk_call(lambda: {"results": self.set_geq(key, body)})

        @bp.get("/mix/eq/<key>")
        def mix_eq_get(key: str):
            return read(lambda: self.eq(key, request.args.get("side", type=int)))

        @bp.put("/mix/eq/<key>")
        def mix_eq_set(key: str):
            body, err = confirmed()
            return err or desk_call(lambda: {"results": self.set_eq(key, body.get("changes"))})
