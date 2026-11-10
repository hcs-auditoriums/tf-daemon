"""The desk's buses, DCAs, mute groups and cue over the TF Editor protocol: TF StageMix's home
screen beyond the input faders (docs/desk-coverage-howto-complete.md 2 and 3.1), for the Mix
page (static/mix.html).

Read from the console's data (TFEditorClient.get: Mixing, Setup, Volatile, FX), so a change at
the desk shows at the next poll; written with TFEditorClient.write on a person's confirmed edit,
only what an enabled write family allows (guard.FAMILIES ``mix``: tf5.mix_write).

The AUX numbering lives here, once (``aux_bus`` / ``aux_send``): AUX 1-8 are ``Mix:0..7``, AUX
9-20 the stereo pairs ``StMix:0..11`` (AUX 9/10 = ``StMix:0, 1``); RCP numbers all 20 as one
``Mix`` list (y 0-19), which is the ``aux`` index used here. A stereo strip (an AUX pair, the
stereo bus) is one strip on the page and every change goes to both halves.

Mute groups: channels have four group switches (``MuteGroup:0..3/Assign``) and FX two, the desk
six masters. Which master a group switch belongs to is [INFERRED], not verified on a desk:
channel group g = master g, FX group g = master 5 + g (MUTE_MAP).

Routes (registered by console/module.py; GEQ and bus PEQ: console/busproc.py):
  GET  /mix                           the Mix page
  GET  /api/tf5/mix[?since=<version>]  every master strip (AUX, ST, MONO, MATRIX, DCA: name, level,
                                      ON, balance, cue), each source's DCA / mute group assignment
                                      and cue, FX mute groups, the mute masters, the cue category;
                                      {"version", "unchanged": true} when nothing changed since
  PUT  /api/tf5/mix/strip/<key>       {"level": raw, "on": 0|1, "balance": -63..63, "cue": 0|1 (any of
                                      them), "confirm": true}: SET a master strip (keys: aux1..aux20,
                                      st, mono, mtx1..mtx4, dca1..dca8; cue also in1.., stin1.., fxrtn1..)
  PUT  /api/tf5/mix/assign            {"source": "in3", "dca": 0..7 | "mute": 0..3, "on": 0|1, "confirm":
                                      true}: SET a DCA / mute group assignment (FX: "fx1", "ins1", mute 0..1)
  PUT  /api/tf5/mix/mute/<g>          {"on": 0|1, "confirm": true}: SET mute master g (0..5)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from flask import Blueprint, Flask, jsonify, request

from .editor import tree

if TYPE_CHECKING:
    from .editor.client import Changes, TFEditorClient

log = logging.getLogger(__name__)


def _size(func: str, *names: str) -> int:
    node = tree.tree(func)
    for name in names:
        node = next(c for c in node["children"] if c["name"] == name)
    return int(node["size"])


N_MIX, N_STMIX = _size("Mixing", "Mix"), _size("Mixing", "StMix")  # 8 mono AUX, 12 stereo halves
N_AUX = N_MIX + N_STMIX  # 20, RCP's Mix y 0-19
N_FX = _size("Mixing", "InputChannel", "ToFX")  # 2
N_DCA = _size("Mixing", "DCA")
N_MUTE = _size("Setup", "MuteMaster")  # 6
N_CH_GROUPS = _size("Mixing", "InputChannel", "MuteGroup")  # 4 per channel
N_FX_GROUPS = _size("FX", "FX", "MuteGroup")  # 2 per FX
# Group switch -> mute master [INFERRED: channels have 4, FX 2, the desk 6 masters; verify by
# toggling an assignment on the desk and watching the push]. Desk names: INPUT, FX, MUTE 3..6.
MUTE_MAP = {"channel": list(range(N_CH_GROUPS)), "fx": [N_MUTE - N_FX_GROUPS + g for g in range(N_FX_GROUPS)]}


def aux_bus(aux: int) -> str:
    """AUX ``aux`` (0-based, RCP's Mix y) -> its Mixing strip: "Mix:3", "StMix:4"."""
    if not 0 <= aux < N_AUX:
        raise ValueError(f"AUX {aux + 1}: there are {N_AUX}")
    return f"Mix:{aux}" if aux < N_MIX else f"StMix:{aux - N_MIX}"


def aux_send(aux: int) -> str:
    """The send to AUX ``aux`` from a source strip: "ToMix:3", "ToStMix:4"."""
    aux_bus(aux)
    return f"ToMix:{aux}" if aux < N_MIX else f"ToStMix:{aux - N_MIX}"


def aux_halves(aux: int) -> list[int]:
    """The AUX a change to ``aux`` goes to: itself, or both halves of its stereo pair."""
    aux_bus(aux)
    if aux < N_MIX:
        return [aux]
    first = N_MIX + (aux - N_MIX) // 2 * 2
    return [first, first + 1]


def aux_label(aux: int) -> str:
    h = aux_halves(aux)
    return f"AUX {h[0] + 1}" if len(h) == 1 else f"AUX {h[0] + 1}/{h[1] + 1}"


@dataclass(frozen=True)
class Bus:
    """A strip of the desk as the page shows it: one or two halves of a Mixing collection."""

    key: str  # "aux1", "aux9" (AUX 9/10), "st", "mono", "mtx1", "dca1", "in1", "stin1", "fxrtn1"
    label: str  # as the desk labels it
    group: str  # "aux" | "main" | "matrix" | "dca" | "input"
    col: str  # its Mixing collection
    halves: tuple[int, ...] = ()  # indices in it; () for Mono (no index)

    @property
    def paths(self) -> list[str]:
        return [f"{self.col}:{i}" for i in self.halves] if self.halves else [self.col]

    @property
    def balance(self) -> bool:
        return self.col in ("Mix", "StMix", "Stereo", "Matrix")

    @property
    def geq(self) -> bool:  # only AUX 1-8 and the stereo bus have one (3.4)
        return self.col in ("Mix", "Stereo")

    @property
    def peq(self) -> bool:
        return self.group in ("aux", "main", "matrix")


def _masters() -> list[Bus]:
    out = [Bus(f"aux{a + 1}", aux_label(a), "aux", "Mix", (a,)) for a in range(N_MIX)]
    for a in range(N_MIX, N_AUX, 2):
        out.append(Bus(f"aux{a + 1}", aux_label(a), "aux", "StMix", (a - N_MIX, a - N_MIX + 1)))
    out += [Bus("st", "ST", "main", "Stereo", (0, 1)), Bus("mono", "MONO", "main", "Mono")]
    out += [Bus(f"mtx{i + 1}", f"MATRIX {i + 1}", "matrix", "Matrix", (i,)) for i in range(_size("Mixing", "Matrix"))]
    return out + [Bus(f"dca{i + 1}", f"DCA {i + 1}", "dca", "DCA", (i,)) for i in range(N_DCA)]


def _sources() -> list[Bus]:
    lr = lambda i: f"{i // 2 + 1}{'LR'[i % 2]}"  # noqa: E731
    out = [
        Bus(f"in{x + 1}", f"CH {x + 1}", "input", "InputChannel", (x,)) for x in range(_size("Mixing", "InputChannel"))
    ]
    out += [
        Bus(f"stin{i + 1}", f"ST IN {lr(i)}", "input", "StInChannel", (i,))
        for i in range(_size("Mixing", "StInChannel"))
    ]
    return out + [
        Bus(f"fxrtn{i + 1}", f"FX RTN {lr(i)}", "input", "FxRtnChannel", (i,))
        for i in range(_size("Mixing", "FxRtnChannel"))
    ]


MASTERS, SOURCES = _masters(), _sources()
FX_UNITS = [(f"fx{i + 1}", f"FX {i + 1}", f"FX:{i}") for i in range(_size("FX", "FX"))] + [
    (f"ins{i + 1}", f"INS FX {i + 1}", f"InsFX:{i}") for i in range(_size("FX", "InsFX"))
]
BY_KEY: dict[str, Bus] = {b.key: b for b in MASTERS + SOURCES}
for _a in range(N_MIX, N_AUX):  # aux10 is the AUX 9/10 pair too
    BY_KEY.setdefault(f"aux{_a + 1}", BY_KEY[f"aux{aux_halves(_a)[0] + 1}"])


def bus(key: str) -> Bus:
    b = BY_KEY.get(key)
    if b is None:
        raise ValueError(f"no strip {key!r}")
    return b


def _bit(v: Any) -> int:
    if v not in (0, 1) or isinstance(v, bool):
        raise ValueError(f"{v!r}: 0 or 1")
    return int(v)


def _int(v: Any, what: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"{what}: a whole number (raw)")
    return v


def confirmed() -> tuple[dict[str, Any], Any]:
    """A route's JSON body, and the 400 reply to give when it lacks ``"confirm": true`` (else None)."""
    body = request.get_json(silent=True) or {}
    if body.get("confirm") is not True:
        return body, (jsonify({"ok": False, "error": 'changing the desk needs "confirm": true'}), 400)
    return body, None


class Desk(Protocol):
    """What this needs of console/module.py's TF5Module: its editor client (None: tf5.eq off)
    and its one generic write, ``write_params`` (guard.FAMILIES decide what it may set)."""

    eq_client: TFEditorClient | None

    def write_params(self, func: str, changes: Changes) -> list[dict[str, Any]]: ...


class Buses:
    """The masters, DCAs, mute groups and cue of the desk."""

    def __init__(self, desk: Desk, simulated: bool = False) -> None:
        self.desk, self.ec, self.simulated = desk, desk.eq_client, simulated

    def version(self) -> int:
        return sum(self.ec.versions.values()) if self.ec is not None else 0

    def get(self, path: str, func: str = "Mixing") -> int | str | None:
        return self.ec.get(path, func) if self.ec is not None else None

    def can(self, family: str) -> bool:
        return self.ec is not None and family in self.ec.enabled_families

    def write(self, func: str, changes: Changes) -> list[dict[str, Any]]:
        """SET ``changes`` in ``func``: only from the confirmed routes below (and busproc.py's,
        sends.py's put from module.py's), through TF5Module.write_params (TFEditorClient.write)."""
        return self.desk.write_params(func, changes) if changes else []

    # ------------------------------------------------------------------ read

    def _strip(self, b: Bus) -> dict[str, Any]:
        p, g = b.paths[0], self.get
        return {
            "key": b.key,
            "label": b.label,
            "group": b.group,
            "stereo": len(b.halves) == 2,
            "name": g(f"{p}/Label/Name"),
            "color": g(f"{p}/Label/Color"),
            "level": g(f"{p}/Fader/Level"),
            "on": g(f"{p}/Fader/On"),
            "balance": g(f"{p}/Output/Balance") if b.balance else None,
            "cue": g(f"Cue/{p}/On", "Volatile"),
            "geq": b.geq,
            "peq": b.peq,
        }

    def _source(self, b: Bus) -> dict[str, Any]:
        p, g = b.paths[0], self.get
        return {
            "key": b.key,
            "label": b.label,
            "name": g(f"{p}/Label/Name"),
            "dca": [g(f"{p}/DCAGroup:{d}/Assign") for d in range(N_DCA)],
            "mute": [g(f"{p}/MuteGroup:{m}/Assign") for m in range(N_CH_GROUPS)],
            "cue": g(f"Cue/{p}/On", "Volatile"),
        }

    def snapshot(self) -> dict[str, Any]:
        ec = self.ec
        return {
            "version": self.version(),
            "available": ec is not None,
            "connected": bool(ec and ec.connected),
            "synced": bool(ec and ec.synced),
            "simulated": self.simulated,
            "writable": {"mix": self.can("mix"), "processing": self.can("processing")},
            "masters": [self._strip(b) for b in MASTERS],
            "sources": [self._source(b) for b in SOURCES],
            "fx": [
                {
                    "key": k,
                    "label": label,
                    "plugin": self.get(f"{p}/Param/PluginName", "FX"),
                    "mute": [self.get(f"{p}/MuteGroup:{m}/Assign", "FX") for m in range(N_FX_GROUPS)],
                }
                for k, label, p in FX_UNITS
            ],
            "mute": [
                {
                    "index": g,
                    "name": self.get(f"MuteMaster:{g}/Name", "Setup"),
                    "on": self.get(f"MuteMaster:{g}/On", "Setup"),
                }
                for g in range(N_MUTE)
            ],
            "mute_map": {**MUTE_MAP, "verified": False},
            "cue_category": self.get("Cue/ActiveCategory", "Volatile"),
        }

    # ------------------------------------------------------------------ write

    def set_strip(self, key: str, values: dict[str, Any]) -> list[dict[str, Any]]:
        """SET a strip's fader level / ON / balance (Mixing) and cue (Volatile), both halves of a
        stereo strip. Sources (in1.. / stin1.. / fxrtn1..) take only cue here."""
        b = bus(key)
        allowed = {"cue"} if b.group == "input" else {"level", "on", "cue"} | ({"balance"} if b.balance else set())
        bad = set(values) - allowed
        if bad or not values:
            raise ValueError(f"{b.label}: set any of {sorted(allowed)} (not {sorted(bad)})")
        mixing: list[tuple[str, int | str]] = []
        if values.get("on") == 0:  # off before the level, on only after it (as the input fader)
            mixing += [(f"{p}/Fader/On", 0) for p in b.paths]
        if "level" in values:
            mixing += [(f"{p}/Fader/Level", _int(values["level"], "level")) for p in b.paths]
        if values.get("on") not in (None, 0):
            mixing += [(f"{p}/Fader/On", _bit(values["on"])) for p in b.paths]
        if "balance" in values:
            mixing += [(f"{p}/Output/Balance", _int(values["balance"], "balance")) for p in b.paths]
        cue = [(f"Cue/{p}/On", _bit(values["cue"])) for p in b.paths] if "cue" in values else []
        return self.write("Mixing", mixing) + self.write("Volatile", cue)

    def assign(self, source: str, what: str, index: Any, on: Any) -> list[dict[str, Any]]:
        """SET one DCA / mute group assignment of a source strip, or an FX's mute group."""
        value, fx = _bit(on), {k: p for k, _, p in FX_UNITS}
        if source in fx:
            if what != "mute" or isinstance(index, bool) or index not in range(N_FX_GROUPS):
                raise ValueError(f"{source}: only mute groups 0..{N_FX_GROUPS - 1}")
            return self.write("FX", [(f"{fx[source]}/MuteGroup:{index}/Assign", value)])
        b = bus(source)
        n = {"dca": N_DCA, "mute": N_CH_GROUPS}.get(what)
        if b.group != "input" or n is None or index not in range(n) or isinstance(index, bool):
            raise ValueError(f"{source}: assign a source to dca 0..{N_DCA - 1} or mute 0..{N_CH_GROUPS - 1}")
        group = "DCAGroup" if what == "dca" else "MuteGroup"
        return self.write("Mixing", [(f"{b.paths[0]}/{group}:{index}/Assign", value)])

    def set_mute(self, g: int, on: Any) -> list[dict[str, Any]]:
        if not 0 <= g < N_MUTE:
            raise ValueError(f"mute master {g}: 0..{N_MUTE - 1}")
        return self.write("Setup", [(f"MuteMaster:{g}/On", _bit(on))])

    # ------------------------------------------------------------------ routes

    def register(self, app: Flask, bp: Blueprint, desk_call: Callable[[Callable[[], dict[str, Any]]], Any]) -> None:
        @app.get("/mix")
        def mix_page():
            return app.send_static_file("mix.html")

        @bp.get("/mix")
        def mix_get():
            since = request.args.get("since", type=int)
            if since is not None and since == self.version():
                return jsonify({"version": since, "unchanged": True})
            return jsonify(self.snapshot())

        @bp.put("/mix/strip/<key>")
        def mix_strip(key: str):
            body, err = confirmed()
            values = {k: v for k, v in body.items() if k != "confirm"}
            return err or desk_call(lambda: {"results": self.set_strip(key, values)})

        @bp.put("/mix/assign")
        def mix_assign():
            body, err = confirmed()
            what = "dca" if "dca" in body else "mute"
            return err or desk_call(
                lambda: {"results": self.assign(str(body.get("source")), what, body.get(what), body.get("on"))}
            )

        @bp.put("/mix/mute/<int:g>")
        def mix_mute(g: int):
            body, err = confirmed()
            return err or desk_call(lambda: {"results": self.set_mute(g, body.get("on"))})
